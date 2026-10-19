package local.dota.replayprobe;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/** Changes of enemy visibility at completed real network frames, not a time grid. */
final class VisibilityTransitions {
    private record Key(String viewer, int slot) {}
    private record State(int heroId, Object handle, Object index, Object sourceHandle,
                         String value, Map<String, Object> row) {}

    private final float start;
    private final float end;
    private final List<Map<String, Object>> transitions = new ArrayList<>();
    private final List<Map<String, Object>> discontinuities = new ArrayList<>();
    private final Map<Integer, Integer> frameTickGaps = new LinkedHashMap<>();
    private final Map<String, Integer> wordUpdates = new LinkedHashMap<>();
    private Map<Key, State> previous = new LinkedHashMap<>();
    private Map<String, Object> previousFrame;
    private Map<String, Object> initialState;
    private Map<String, Object> lastState;
    private Integer lastTick;
    private Float lastTime;
    private Integer windowPreviousTick;
    private int frames;
    private int pausedFrames;
    private int entityUpdates;
    private int framesWithWordUpdates;
    private float minFrameTimeGap = Float.POSITIVE_INFINITY;
    private float maxFrameTimeGap;

    VisibilityTransitions(float start, float end) {
        if (!Float.isFinite(start) || !Float.isFinite(end) || start < 0 || end <= start) {
            throw new IllegalArgumentException("Invalid visibility window");
        }
        this.start = start;
        this.end = end;
    }

    boolean needsFrame(float time) { return time >= 0 && time <= end; }

    void observe(float time, int tick, boolean synthetic, boolean paused,
                 List<Map<String, Object>> rows, int entityUpdateCount, Map<String, Integer> sourceUpdates) {
        if (synthetic || time > end) return;
        if (!Float.isFinite(time) || time < 0 || tick < 0) {
            throw new IllegalArgumentException("Invalid visibility frame clock");
        }
        if (lastTick != null) {
            if (tick < lastTick || time < lastTime || (tick == lastTick && time != lastTime)) {
                throw new IllegalStateException("Visibility clock rewound");
            }
            if (tick == lastTick) return;
        }
        Map<Key, State> current = states(rows);
        Map<String, Object> frame = frame(time, tick, paused, current);
        if (time >= start) {
            if (initialState == null) {
                if (previousFrame == null || lastTime >= start || previous.size() != 10 || current.size() != 10) {
                    throw new IllegalStateException("Window needs a real pre-window baseline and ten heroes");
                }
                initialState = previousFrame;
            }
            if (current.size() != 10 || !current.keySet().equals(previous.keySet())) {
                throw new IllegalStateException("Missing/changed player slots inside visibility window");
            }
            frames++;
            if (paused) pausedFrames++;
            entityUpdates += entityUpdateCount;
            if (!sourceUpdates.isEmpty()) framesWithWordUpdates++;
            sourceUpdates.forEach((source, count) -> wordUpdates.merge(source, count, Integer::sum));
            if (windowPreviousTick != null) {
                frameTickGaps.merge(tick - windowPreviousTick, 1, Integer::sum);
                minFrameTimeGap = Math.min(minFrameTimeGap, time - lastTime);
                maxFrameTimeGap = Math.max(maxFrameTimeGap, time - lastTime);
            }
            windowPreviousTick = tick;
            for (Map.Entry<Key, State> entry : current.entrySet()) {
                Key key = entry.getKey();
                State old = previous.get(key), now = entry.getValue();
                boolean sameIdentity = old.heroId == now.heroId && Objects.equals(old.handle, now.handle)
                        && Objects.equals(old.index, now.index) && Objects.equals(old.sourceHandle, now.sourceHandle);
                if (sameIdentity && old.value.equals(now.value)) continue;
                Map<String, Object> event = new LinkedHashMap<>(now.row);
                event.remove("state");
                event.putAll(ReplayProbe.mapOf(
                        "previous_state", old.value, "new_state", now.value,
                        "replay_tick", tick, "game_time_seconds", time, "game_paused", paused,
                        "previous_observed_replay_tick", lastTick,
                        "previous_observed_game_time_seconds", lastTime,
                        "previous_raw_word_unsigned", old.row.get("raw_word_unsigned")
                ));
                boolean witnessed = sourceUpdates.containsKey((String) now.row.get("source_entity_class"));
                event.put("visibility_word_updated_this_tick", witnessed);
                if (sameIdentity && known(old.value) && known(now.value)) {
                    if (!witnessed) {
                        throw new IllegalStateException("Flag changed without a witnessed source-word update");
                    }
                    transitions.add(event);
                } else {
                    event.put("reason", sameIdentity ? "UNKNOWN_STATE_GAP" : "IDENTITY_RESET");
                    discontinuities.add(event);
                }
            }
            lastState = frame;
        }
        previous = current;
        previousFrame = frame;
        lastTick = tick;
        lastTime = time;
    }

    private static boolean known(String value) {
        return value.equals("VISIBLE") || value.equals("HIDDEN");
    }

    @SuppressWarnings("unchecked")
    private static Map<Key, State> states(List<Map<String, Object>> rows) {
        Map<Key, State> result = new LinkedHashMap<>();
        for (Map<String, Object> hero : rows) {
            String team = (String) hero.get("team");
            String viewer = team.equals("RADIANT") ? "DIRE" : "RADIANT";
            String prefix = viewer.toLowerCase(java.util.Locale.ROOT);
            Map<String, Object> evidence = (Map<String, Object>) hero.get(prefix + "_evidence");
            String value = (String) hero.get("visible_to_" + prefix);
            if (!known(value) && !value.equals("UNKNOWN")) throw new IllegalArgumentException("Invalid visibility state");
            int slot = ((Number) hero.get("player_slot")).intValue();
            Map<String, Object> row = ReplayProbe.mapOf(
                    "observing_team", viewer, "enemy_team", team,
                    "player_slot", slot, "hero_id", hero.get("hero_id"),
                    "hero_entity_class", hero.get("hero_entity_class"),
                    "hero_entity_handle", hero.get("hero_entity_handle"),
                    "hero_entity_index", hero.get("hero_entity_index"),
                    "life_state_global_context", hero.get("life_state"), "state", value,
                    "source_entity_class", evidence.get("source_entity_class"),
                    "source_entity_handle", evidence.get("source_entity_handle"),
                    "property_path", evidence.get("property_path"),
                    "raw_word_unsigned", evidence.get("raw_word_unsigned")
            );
            int heroId = hero.get("hero_id") instanceof Number n ? n.intValue() : 0;
            Key key = new Key(viewer, slot);
            if (result.put(key, new State(heroId, hero.get("hero_entity_handle"), hero.get("hero_entity_index"),
                    evidence.get("source_entity_handle"), value, row)) != null) {
                throw new IllegalStateException("Duplicate visibility player slot");
            }
        }
        return result;
    }

    private static Map<String, Object> frame(float time, int tick, boolean paused, Map<Key, State> states) {
        return ReplayProbe.mapOf("game_time_seconds", time, "replay_tick", tick,
                "game_paused", paused, "enemy_states", states.values().stream().map(State::row).toList());
    }

    Map<String, Object> artifact() {
        if (initialState == null || lastState == null) throw new IllegalStateException("Visibility window not observed");
        return ReplayProbe.mapOf(
                "window_start_game_time_seconds", start, "window_end_game_time_seconds", end,
                "initial_state", initialState, "last_state", lastState,
                "real_frames_in_window", frames, "paused_frames_in_window", pausedFrames,
                "source_entity_update_callbacks", entityUpdates,
                "frames_with_visibility_word_updates", framesWithWordUpdates,
                "visibility_word_updates_by_source", wordUpdates,
                "real_frame_tick_gap_histogram", frameTickGaps,
                "real_frame_game_time_gap_min_seconds", Float.isFinite(minFrameTimeGap) ? minFrameTimeGap : null,
                "real_frame_game_time_gap_max_seconds", maxFrameTimeGap,
                "transitions", transitions, "discontinuities", discontinuities
        );
    }
}
