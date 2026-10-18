package local.dota.replayprobe;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

final class HeroVisibilitySelfTest {
    private HeroVisibilitySelfTest() {}

    static void run() {
        testTeamMappingAndBitIndex();
        testUnsignedWordsAndBoundaries();
        testUnknownIsNotHidden();
        testIdentityAndLifecycle();
        testDeathAndNoGlobalCoordinates();
        testNoCachedOrFutureState();
    }

    private static HeroVisibility.Hero hero(int index, int handle, boolean active, Map<String, Object> properties) {
        return new HeroVisibility.Hero(new HeroPositions.Source(handle, "CDOTA_Unit_Hero_Juggernaut",
                true, active, properties::get), index, 492);
    }

    private static Map<String, Object> heroProperties(int life) {
        return new HashMap<>(Map.of("m_iTeamNum", 2, "m_bIsClone", false, "m_lifeState", life));
    }

    private static HeroPositions.Source teamSource(int team, Object word) {
        return new HeroPositions.Source(900 + team, team == 2 ? HeroVisibility.RADIANT_SOURCE : HeroVisibility.DIRE_SOURCE,
                true, true, name -> name.equals("m_iTeamNum") ? team : word);
    }

    private static Map<String, Object> row(int index, Object radiant, Object dire) {
        return HeroVisibility.observeSource(0, 2, 8, 8062285,
                hero(index, 8062285, true, heroProperties(0)), teamSource(2, radiant), teamSource(3, dire));
    }

    private static void states(Map<String, Object> row, String radiant, String dire) {
        require(radiant.equals(row.get("visible_to_radiant")) && dire.equals(row.get("visible_to_dire")),
                "wrong perspective states: " + row);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> evidence(Map<String, Object> row, String team) {
        return (Map<String, Object>) row.get(team + "_evidence");
    }

    private static void testTeamMappingAndBitIndex() {
        Map<String, Object> row = row(1357, 1L << 13, 0L);
        states(row, "VISIBLE", "HIDDEN");
        require("m_bNPCVisibleState.0021".equals(evidence(row, "radiant").get("property_path")), "wrong word path");
        require(evidence(row, "radiant").get("bit_index").equals(13), "bit must address NPC index, not team number");
        require("8192".equals(evidence(row, "radiant").get("raw_word_unsigned")), "raw evidence was lost");
        Map<String, Object> properties = heroProperties(0);
        properties.put("m_iTeamNum", 3);
        Map<String, Object> direHero = HeroVisibility.observeSource(5, 3, 54, 123,
                hero(410, 123, true, properties), teamSource(2, 0L), teamSource(3, 1L << 26));
        states(direHero, "HIDDEN", "VISIBLE");
    }

    private static void testUnsignedWordsAndBoundaries() {
        states(row(63, Long.MIN_VALUE, 0L), "VISIBLE", "HIDDEN");
        states(row(64, 1L, 0L), "VISIBLE", "HIDDEN");
        states(row(16383, Long.MIN_VALUE, -1L), "VISIBLE", "VISIBLE");
        require("9223372036854775808".equals(evidence(row(63, Long.MIN_VALUE, 0L), "radiant").get("raw_word_unsigned")),
                "uint64 high bit was interpreted as an invalid negative word");
        states(row(-1, -1L, -1L), "UNKNOWN", "UNKNOWN");
        states(row(16384, -1L, -1L), "UNKNOWN", "UNKNOWN");
    }

    private static void testUnknownIsNotHidden() {
        for (Object missing : new Object[] {null, 0, false, 0.0}) {
            Map<String, Object> row = row(1357, missing, 0L);
            states(row, "UNKNOWN", "HIDDEN");
            require("PARTIAL_UNKNOWN".equals(row.get("visibility_status")), "one unavailable view invalidated both views");
        }
        HeroVisibility.Hero hero = hero(1357, 8062285, true, heroProperties(0));
        states(HeroVisibility.observeSource(0, 2, 8, 8062285, hero, teamSource(3, -1L), teamSource(2, -1L)),
                "UNKNOWN", "UNKNOWN");
        HeroPositions.Source inactive = new HeroPositions.Source(902, HeroVisibility.RADIANT_SOURCE, true, false,
                name -> name.equals("m_iTeamNum") ? 2 : -1L);
        states(HeroVisibility.observeSource(0, 2, 8, 8062285, hero, inactive, null), "UNKNOWN", "UNKNOWN");
    }

    private static void testIdentityAndLifecycle() {
        HeroPositions.Source rad = teamSource(2, -1L), dire = teamSource(3, -1L);
        states(HeroVisibility.observeSource(0, 2, 8, 8062285, null, rad, dire), "UNKNOWN", "UNKNOWN");
        for (HeroVisibility.Hero invalid : List.of(
                hero(1357, 123, true, heroProperties(0)), hero(1357, 8062285, false, heroProperties(0)))) {
            states(HeroVisibility.observeSource(0, 2, 8, 8062285, invalid, rad, dire), "UNKNOWN", "UNKNOWN");
        }
        Map<String, Object> properties = heroProperties(0);
        properties.put("m_bIsClone", true);
        states(HeroVisibility.observeSource(0, 2, 8, 8062285, hero(1357, 8062285, true, properties), rad, dire),
                "UNKNOWN", "UNKNOWN");
        states(row(1357, 1L << 13, 1L << 13), "VISIBLE", "VISIBLE");
        // A reappearing current primary hero is read by its new index/handle, without old visibility.
        states(HeroVisibility.observeSource(0, 2, 8, 123, hero(1408, 123, true, heroProperties(0)),
                teamSource(2, 0L), teamSource(3, 0L)), "HIDDEN", "HIDDEN");
    }

    private static void testDeathAndNoGlobalCoordinates() {
        for (int life : List.of(0, 1, 2)) {
            Map<String, Object> row = HeroVisibility.observeSource(0, 2, 8, 8062285,
                    hero(1357, 8062285, true, heroProperties(life)), teamSource(2, 1L << 13), teamSource(3, 1L << 13));
            states(row, "VISIBLE", "VISIBLE");
            require(row.get("life_state").equals(life), "death/respawn context was lost");
            String json = JsonWriter.toJson(row);
            require(!json.contains("\"x\"") && !json.contains("\"y\"") && !json.contains("\"items\""),
                    "global position or inventory leaked into a visibility observation");
        }
    }

    private static void testNoCachedOrFutureState() {
        Map<String, Object> fields = new HashMap<>(Map.of("m_iTeamNum", 2, "m_bNPCVisibleState.0021", 1L << 13));
        HeroPositions.Source rad = new HeroPositions.Source(902, HeroVisibility.RADIANT_SOURCE, true, true, fields::get);
        HeroVisibility.Hero hero = hero(1357, 8062285, true, heroProperties(0));
        Map<String, Object> before = HeroVisibility.observeSource(0, 2, 8, 8062285, hero, rad, teamSource(3, 0L));
        fields.put("m_bNPCVisibleState.0021", 0L);
        Map<String, Object> later = HeroVisibility.observeSource(0, 2, 8, 8062285, hero, rad, teamSource(3, 0L));
        states(before, "VISIBLE", "HIDDEN");
        states(later, "HIDDEN", "HIDDEN");
        fields.remove("m_bNPCVisibleState.0021");
        states(HeroVisibility.observeSource(0, 2, 8, 8062285, hero, rad, teamSource(3, 0L)), "UNKNOWN", "HIDDEN");
        require("8192".equals(evidence(before, "radiant").get("raw_word_unsigned")), "a future update changed old evidence");
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
