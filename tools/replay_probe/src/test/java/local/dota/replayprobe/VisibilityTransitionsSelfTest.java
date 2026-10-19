package local.dota.replayprobe;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

final class VisibilityTransitionsSelfTest {
    private static final Map<String, Integer> UPDATES = Map.of(
            HeroVisibility.RADIANT_SOURCE, 1, HeroVisibility.DIRE_SOURCE, 1);

    static void run() {
        testOnlyBitChangesAndDuplicateFrames();
        testWindowBaselineAndBounds();
        testSeparateEnemyPerspectives();
        testUnknownAndIdentityDiscontinuities();
        testSyntheticAndPausedFramesWithDifferentTickOrigins();
        testMissingBaselineAndRewind();
        testSourceWitnessAndNoFutureMutation();
    }

    private static List<Map<String, Object>> rows() {
        List<Map<String, Object>> result = new ArrayList<>();
        for (int slot = 0; slot < 10; slot++) {
            result.add(ReplayProbe.mapOf(
                    "player_slot", slot, "team", slot < 5 ? "RADIANT" : "DIRE",
                    "hero_id", slot + 1, "hero_entity_handle", 100 + slot,
                    "hero_entity_index", slot + 10, "hero_entity_class", "CDOTA_Unit_Hero_Test",
                    "life_state", 0, "visible_to_radiant", slot < 5 ? "VISIBLE" : "HIDDEN",
                    "visible_to_dire", slot < 5 ? "HIDDEN" : "VISIBLE",
                    "radiant_evidence", evidence(HeroVisibility.RADIANT_SOURCE, 902),
                    "dire_evidence", evidence(HeroVisibility.DIRE_SOURCE, 903)
            ));
        }
        return result;
    }

    private static Map<String, Object> evidence(String source, int handle) {
        return ReplayProbe.mapOf("source_entity_class", source, "source_entity_handle", handle,
                "property_path", "m_bNPCVisibleState.0000", "raw_word_unsigned", "0");
    }

    private static void observe(VisibilityTransitions tracker, float time, int tick, List<Map<String, Object>> rows) {
        tracker.observe(time, tick, false, false, rows, 2, UPDATES);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> events(VisibilityTransitions tracker, String kind) {
        return (List<Map<String, Object>>) tracker.artifact().get(kind);
    }

    private static void testOnlyBitChangesAndDuplicateFrames() {
        VisibilityTransitions tracker = new VisibilityTransitions(1, 2);
        List<Map<String, Object>> rows = rows();
        observe(tracker, 0.9f, 9, rows);
        observe(tracker, 1, 10, rows); // Entity/word update without a hero bit change.
        require(events(tracker, "transitions").isEmpty(), "entity update became a visibility event");
        rows.get(5).put("visible_to_radiant", "VISIBLE");
        observe(tracker, 1.1f, 11, rows);
        observe(tracker, 1.1f, 11, rows);
        observe(tracker, 1.2f, 12, rows);
        require(events(tracker, "transitions").size() == 1, "unchanged/duplicate frame emitted a duplicate");
    }

    private static void testWindowBaselineAndBounds() {
        VisibilityTransitions tracker = new VisibilityTransitions(1, 2);
        List<Map<String, Object>> rows = rows();
        observe(tracker, 0.8f, 8, rows);
        observe(tracker, 0.9f, 9, rows);
        rows.get(5).put("visible_to_radiant", "VISIBLE");
        observe(tracker, 1, 10, rows);
        rows.get(5).put("visible_to_radiant", "HIDDEN");
        observe(tracker, 2, 20, rows);
        rows.get(5).put("visible_to_radiant", "VISIBLE");
        observe(tracker, 2.1f, 21, rows);
        require(events(tracker, "transitions").size() == 2, "window endpoints were not respected");
        Map<String, Object> first = events(tracker, "transitions").get(0);
        require(first.get("previous_observed_replay_tick").equals(9), "latest pre-window baseline was lost");
        require(first.get("game_time_seconds").equals(1.0f), "first in-window transition was dropped");
    }

    private static void testSeparateEnemyPerspectives() {
        VisibilityTransitions tracker = new VisibilityTransitions(1, 2);
        List<Map<String, Object>> rows = rows();
        observe(tracker, 0.9f, 9, rows);
        rows.get(5).put("visible_to_dire", "HIDDEN"); // Own-team flag: never an enemy observation.
        rows.get(5).put("visible_to_radiant", "VISIBLE");
        rows.get(0).put("visible_to_dire", "VISIBLE");
        observe(tracker, 1, 10, rows);
        List<Map<String, Object>> events = events(tracker, "transitions");
        require(events.size() == 2, "own team visibility entered enemy events");
        require(events.stream().anyMatch(e -> e.get("observing_team").equals("RADIANT") && e.get("player_slot").equals(5)), "Radiant mapped to wrong enemy");
        require(events.stream().anyMatch(e -> e.get("observing_team").equals("DIRE") && e.get("player_slot").equals(0)), "Dire mapped to wrong enemy");
    }

    private static void testUnknownAndIdentityDiscontinuities() {
        VisibilityTransitions tracker = new VisibilityTransitions(1, 2);
        List<Map<String, Object>> rows = rows();
        observe(tracker, 0.9f, 9, rows);
        observe(tracker, 1, 10, rows);
        rows.get(5).put("visible_to_radiant", "UNKNOWN");
        observe(tracker, 1.1f, 11, rows);
        rows.get(5).put("visible_to_radiant", "VISIBLE");
        observe(tracker, 1.2f, 12, rows);
        rows.get(5).put("hero_entity_handle", 999);
        observe(tracker, 1.3f, 13, rows);
        require(events(tracker, "transitions").isEmpty(), "unknown gap/entity replacement invented a bit transition");
        require(events(tracker, "discontinuities").size() == 3, "availability/identity changes were hidden");
    }

    private static void testSyntheticAndPausedFramesWithDifferentTickOrigins() {
        ReplayProbe.ClockTracker clock = new ReplayProbe.ClockTracker();
        clock.observeNetworkTick(99, 399);
        clock.observe(99, 3.3, 1000f / 30, null, 4, false, 0, null, 0f, 0f);
        clock.observe(100, 100 / 30.0, 1000f / 30, null, 5, false, 0, null, 500f, 0f);
        clock.observe(129, 129 / 30.0, 1000f / 30, null, 5, false, 0, null, 500f, 0f);
        float beforePause = clock.normalizedGameTime();
        clock.observe(130, 130 / 30.0, 1000f / 30, null, 5, true, 0, 430, 500f, 0f);
        float pausedTime = clock.normalizedGameTime();
        VisibilityTransitions tracker = new VisibilityTransitions(pausedTime, 2);
        List<Map<String, Object>> rows = rows();
        observe(tracker, beforePause, 129, rows);
        tracker.observe(pausedTime, 130, false, true, rows, 2, UPDATES);
        rows.get(5).put("visible_to_radiant", "VISIBLE");
        tracker.observe(2, 160, true, false, rows, 2, UPDATES);
        require(events(tracker, "transitions").isEmpty(), "synthetic callback emitted a transition");
        clock.observe(133, 133 / 30.0, 1000f / 30, null, 5, true, 0, 430, 500f, 0f);
        tracker.observe(clock.normalizedGameTime(), 133, false, true, rows, 2, UPDATES);
        Map<String, Object> event = events(tracker, "transitions").get(0);
        require(event.get("game_time_seconds").equals(pausedTime) && event.get("replay_tick").equals(133), "pause/tick domains changed event time");
        require(event.get("game_paused").equals(true) && clock.networkToReplayTickOffset() == -300, "paused frame context was lost");
    }

    private static void testMissingBaselineAndRewind() {
        boolean rejected = false;
        try { observe(new VisibilityTransitions(1, 2), 1, 10, rows()); }
        catch (IllegalStateException expected) { rejected = true; }
        require(rejected, "missing pre-window state was silently assumed HIDDEN");
        VisibilityTransitions tracker = new VisibilityTransitions(1, 2);
        observe(tracker, 0.9f, 9, rows());
        observe(tracker, 1, 10, rows());
        rejected = false;
        try { observe(tracker, 0.95f, 11, rows()); }
        catch (IllegalStateException expected) { rejected = true; }
        require(rejected, "rewound game clock was accepted");
    }

    private static void testSourceWitnessAndNoFutureMutation() {
        VisibilityTransitions tracker = new VisibilityTransitions(1, 2);
        List<Map<String, Object>> rows = rows();
        observe(tracker, 0.9f, 9, rows);
        rows.get(5).put("visible_to_radiant", "VISIBLE");
        boolean rejected = false;
        try { tracker.observe(1, 10, false, false, rows, 1, Map.of()); }
        catch (IllegalStateException expected) { rejected = true; }
        require(rejected, "bit transition had no real source update witness");
        tracker = new VisibilityTransitions(1, 2);
        observe(tracker, 0.9f, 9, rows());
        observe(tracker, 1, 10, rows);
        Map<String, Object> first = events(tracker, "transitions").get(0);
        String before = JsonWriter.toJson(first);
        rows.get(5).put("visible_to_radiant", "HIDDEN");
        observe(tracker, 1.1f, 11, rows);
        require(before.equals(JsonWriter.toJson(first)), "future update mutated an old event");
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
