package local.dota.replayprobe;

import java.util.List;
import java.util.Map;

final class HeroDeathsSelfTest {
    static void run() {
        testPrimaryIdentityAndCloneAmbiguity();
        testOnlyRealHeroVictimsAndDeduplication();
        testExactFrameJoinRegardlessOfListenerOrder();
        testIllusionAttackerIsNotAssignedToItsHeroOwner();
        testNativeTimestampIsSeparateFromFrameTime();
        testMissingOrWrongTimeDomainDoesNotInventTime();
        testPauseMustNotBeSubtractedTwice();
    }

    private static HeroDeaths.Primary primary(int team, boolean verified, boolean clone) {
        return new HeroDeaths.Primary(0, team, 8, "npc_dota_hero_juggernaut", 123,
                "CDOTA_Unit_Hero_Juggernaut", verified, clone, 1);
    }

    private static HeroDeaths.Death death(boolean hero, boolean illusion, String attacker, boolean attackerIllusion) {
        return new HeroDeaths.Death("npc_dota_hero_juggernaut", 2, hero, illusion, true,
                3380.7668f, 3380.7668f, attacker, 3, true, attackerIllusion,
                "npc_dota_hero_dark_seer", null, "npc_dota_hero_juggernaut", false);
    }

    private static Map<String, Object> frame(float time, int tick, int pauseTicks) {
        return ReplayProbe.mapOf("frame_replay_tick", tick, "frame_game_time_seconds", time,
                "frame_clock_proven", true, "frame_synthetic", false,
                "frame_game_end_frozen", false, "frame_game_start_time_property", 1083.5f,
                "frame_millis_per_tick", 1000f / 30, "frame_pause_ticks", pauseTicks);
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> events(HeroDeaths tracker) {
        return (List<Map<String, Object>>) tracker.artifact(List.of()).get("hero_deaths");
    }

    private static void testPrimaryIdentityAndCloneAmbiguity() {
        require(HeroDeaths.identify("npc_dota_hero_juggernaut", 2, List.of(primary(2, true, false))) != null, "verified primary lost");
        for (HeroDeaths.Primary invalid : List.of(primary(3, true, false), primary(2, false, false), primary(2, true, true))) {
            require(HeroDeaths.identify("npc_dota_hero_juggernaut", 2, List.of(invalid)) == null, "unverified/clone/team mismatch accepted");
        }
        require(HeroDeaths.identify("npc_dota_hero_juggernaut", null, List.of(primary(2, true, false), primary(3, true, false))) == null, "ambiguous name assigned to one player");
    }

    private static void testOnlyRealHeroVictimsAndDeduplication() {
        HeroDeaths tracker = new HeroDeaths();
        tracker.frame(100, frame(2297.2334f, 100, 0));
        List<HeroDeaths.Primary> roster = List.of(primary(2, true, false));
        tracker.accept(100, death(false, false, null, false), roster);
        tracker.accept(100, death(true, true, null, false), roster);
        tracker.accept(100, death(true, false, null, false), roster);
        tracker.accept(100, death(true, false, null, false), roster);
        Map<String, Object> artifact = tracker.artifact(List.of());
        require(events(tracker).size() == 1 && artifact.get("duplicate_count").equals(1), "duplicate/nonhero/illusion death exported");
        require(artifact.get("excluded_nonhero_death_count").equals(1) && artifact.get("excluded_illusion_death_count").equals(1), "exclusions not accounted for");
    }

    private static void testExactFrameJoinRegardlessOfListenerOrder() {
        HeroDeaths early = new HeroDeaths(), late = new HeroDeaths();
        var roster = List.of(primary(2, true, false));
        var death = death(true, false, null, false);
        early.accept(101151, death, roster);
        early.frame(101151, frame(2297.2334f, 101151, 0));
        late.frame(101151, frame(2297.2334f, 101151, 0));
        late.accept(101151, death, roster);
        require(events(early).equals(events(late)), "callback order changed event/frame clock join");
    }

    private static void testIllusionAttackerIsNotAssignedToItsHeroOwner() {
        HeroDeaths tracker = new HeroDeaths();
        tracker.frame(100, frame(2297.2334f, 100, 0));
        var sourceHero = new HeroDeaths.Primary(9, 3, 55, "npc_dota_hero_dark_seer", 999,
                "CDOTA_Unit_Hero_DarkSeer", true, false, 0);
        tracker.accept(100, death(true, false, "npc_dota_hero_juggernaut", true), List.of(primary(2, true, false), sourceHero));
        Map<String, Object> event = events(tracker).get(0);
        require(event.get("killer_player_slot") == null && event.get("attacker_is_illusion").equals(true), "illusion attacker assigned to victim or guessed controller");
        require(event.get("damage_source_unit_name").equals("npc_dota_hero_dark_seer"), "explicit damage source discarded");
    }

    private static void testNativeTimestampIsSeparateFromFrameTime() {
        Map<String, Object> event = ReplayProbe.mapOf("combat_log_timestamp", 3380.7668f, "combat_log_timestamp_raw", 3380.7668f);
        HeroDeaths.attachClock(event, frame(2297.2334f, 100, 0));
        float normalized = ((Number) event.get("event_game_time_seconds")).floatValue();
        require(normalized == 3380.7668f - 1083.5f, "native event timestamp was replaced by delivery time");
        require(normalized > 2297.2334f, "one-tick event/frame distinction was clamped away");
    }

    private static void testMissingOrWrongTimeDomainDoesNotInventTime() {
        for (Float timestamp : new Float[] {null, Float.NaN, 2297.2f}) {
            Map<String, Object> event = ReplayProbe.mapOf("combat_log_timestamp", timestamp);
            HeroDeaths.attachClock(event, frame(2297.2334f, 100, 0));
            require(event.get("event_game_time_seconds") == null, "unverified native timestamp was normalized by guessing");
        }
        Map<String, Object> event = ReplayProbe.mapOf("combat_log_timestamp", 3380.7668f);
        Map<String, Object> synthetic = frame(2297.2334f, 100, 0);
        synthetic.put("frame_synthetic", true);
        HeroDeaths.attachClock(event, synthetic);
        require(event.get("event_game_time_seconds") == null, "synthetic delivery frame proved a timestamp");
    }

    private static void testPauseMustNotBeSubtractedTwice() {
        Map<String, Object> event = ReplayProbe.mapOf("combat_log_timestamp", 3380.7668f);
        HeroDeaths.attachClock(event, frame(2297.2334f, 100, 185));
        require(event.get("event_game_time_seconds").equals(3380.7668f - 1083.5f), "paused ticks subtracted twice from engine clock");
        Map<String, Object> rawIncludesPause = ReplayProbe.mapOf("combat_log_timestamp", 3380.7668f + 185f / 30);
        HeroDeaths.attachClock(rawIncludesPause, frame(2297.2334f, 100, 185));
        require(rawIncludesPause.get("event_game_time_seconds") == null, "pause-inclusive native clock silently accepted as elapsed game time");
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
