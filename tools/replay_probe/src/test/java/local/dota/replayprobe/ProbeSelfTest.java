package local.dota.replayprobe;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class ProbeSelfTest {
    private ProbeSelfTest() {
    }

    public static void main(String[] args) {
        testClockRequiresWitnessedCrossing();
        testClockCapturesCrossing();
        testGameStartPropertyAndPauseTicks();
        testJsonEscapingAndNonFiniteNumbers();
        testDuplicateDetection();
        testCompactSnapshotOrderingAndAggregates();
        testCompactSnapshotRequiresTenPlayers();
        testCellPositionConversion();
        testInvalidPositionComponents();
        testSelectedHeroPosition();
        testPositionRequiresCurrentPrimaryHero();
        testDeadOrUnknownLifeStateHasNoPosition();
        testMissingEntityOrAxisNeverReusesPosition();
        System.out.println("probeTest: 13 focused tests passed");
    }

    private static void testClockRequiresWitnessedCrossing() {
        ReplayProbe.ClockTracker tracker = new ReplayProbe.ClockTracker();
        tracker.observe(100, 3.0, 12.0f, 4);
        require(!tracker.isProven(), "a replay first observed after zero must stay unresolved");
    }

    private static void testClockCapturesCrossing() {
        ReplayProbe.ClockTracker tracker = new ReplayProbe.ClockTracker();
        tracker.observe(90, 2.5, -0.03125f, 3);
        tracker.observe(91, 2.53125, 0.0f, 4);
        require(tracker.isProven(), "negative-to-zero crossing must prove normalization");
        require(tracker.zeroTick() == 91, "wrong zero tick");
        require(Float.valueOf(-0.03125f).equals(tracker.previousGameTime()), "wrong preceding value");
    }

    private static void testGameStartPropertyAndPauseTicks() {
        ReplayProbe.ClockTracker tracker = new ReplayProbe.ClockTracker();
        tracker.observe(99, 3.3, 33.333333f, null, 4, false, 10, null, 0.0f, 0.0f);
        tracker.observe(100, 3.333333, 33.333333f, null, 5, false, 10, null, 500.0f, 0.0f);
        tracker.observe(160, 5.333333, 33.333333f, null, 5, false, 20, null, 500.0f, 0.0f);
        require(tracker.isProven(), "game-start timestamp transition must prove zero");
        require(tracker.mode() == ReplayProbe.ClockMode.GAME_START_TIME_AND_PAUSE_TICKS,
                "wrong game-start clock mode");
        require(Math.abs(tracker.normalizedGameTime() - (50 * 33.333333f / 1000.0f)) < 0.0001f,
                "completed pause ticks were not removed");
        float beforePause = tracker.normalizedGameTime();
        tracker.observe(170, 5.666667, 33.333333f, null, 5, true, 20, 160, 500.0f, 0.0f);
        tracker.observe(200, 6.666667, 33.333333f, null, 5, true, 20, 160, 500.0f, 0.0f);
        require(tracker.normalizedGameTime() == beforePause, "active pause advanced the shared snapshot clock");
        tracker.observe(220, 7.333333, 33.333333f, null, 5, false, 80, null, 500.0f, 0.0f);
        require(tracker.normalizedGameTime() == beforePause, "unpausing caused a game-time jump");
    }

    private static void testJsonEscapingAndNonFiniteNumbers() {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("text", "a\n\"b");
        value.put("nan", Double.NaN);
        String json = JsonWriter.toJson(value);
        require(json.contains("\"text\": \"a\\n\\\"b\""), "string was not escaped");
        require(json.contains("\"nan\": null"), "non-finite number must be JSON null");
    }

    private static void testDuplicateDetection() {
        Map<String, Object> row1 = Map.of("player_slot", 0);
        Map<String, Object> row2 = Map.of("player_slot", 0);
        Map<String, Object> snapshot = Map.of("game_time_seconds", 60, "players", List.of(row1, row2));
        require(ReplayProbe.findDuplicateKeys(List.of(snapshot)).size() == 1, "duplicate key was not detected");
    }

    private static void testCompactSnapshotOrderingAndAggregates() {
        List<Map<String, Object>> players = new java.util.ArrayList<>();
        for (int slot = 9; slot >= 0; slot--) {
            String team = slot < 5 ? "RADIANT" : "DIRE";
            players.add(ReplayProbe.mapOf(
                    "player_slot", slot,
                    "team", team,
                    "hero_id", 100 + slot,
                    "hero_name", "hero_" + slot,
                    "level", slot + 1,
                    "kills", slot,
                    "deaths", slot + 1,
                    "assists", slot + 2,
                    "last_hits", slot + 3,
                    "denies", slot + 4,
                    "net_worth", slot + 5,
                    "current_gold", slot + 6,
                    "total_xp", slot + 7,
                    "items", List.of(ReplayProbe.mapOf(
                            "inventory_slot", 1,
                            "item_name", "item_" + slot,
                            "entity_handle", 900 + slot,
                            "source_property_path", "forbidden"
                    ))
            ));
        }
        Map<String, Object> snapshot = ReplayProbe.mapOf(
                "game_time_seconds", 60,
                "source_game_time_seconds", 60.01,
                "replay_tick", 2000,
                "raw_replay_time_seconds", 66.0,
                "game_state", 5,
                "players", players
        );

        Map<String, Object> compact = ReplayProbe.compactSnapshot(snapshot);
        require(new java.util.ArrayList<>(compact.keySet()).equals(List.of(
                "game_time_seconds", "source_game_time_seconds", "replay_tick", "game_state", "teams", "players"
        )), "compact snapshot field order changed");

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> compactPlayers = (List<Map<String, Object>>) compact.get("players");
        require(compactPlayers.size() == 10, "compact snapshot lost players");
        require(compactPlayers.get(0).get("player_slot").equals(0), "compact players were not slot ordered");
        require(compactPlayers.get(9).get("player_slot").equals(9), "compact players were not slot ordered");

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> items = (List<Map<String, Object>>) compactPlayers.get(0).get("items");
        require(new java.util.ArrayList<>(items.get(0).keySet()).equals(List.of("inventory_slot", "item_name")),
                "compact item retained diagnostic fields");

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> teams = (List<Map<String, Object>>) compact.get("teams");
        require(teams.size() == 2, "compact snapshot must have two team rows");
        require("RADIANT".equals(teams.get(0).get("team")), "Radiant team row must be first");
        require("DIRE".equals(teams.get(1).get("team")), "Dire team row must be second");
        require(teams.get(0).get("kills").equals(10), "Radiant aggregate was not summed from five rows");
        require(teams.get(1).get("kills").equals(35), "Dire aggregate was not summed from five rows");
    }

    private static void testCompactSnapshotRequiresTenPlayers() {
        Map<String, Object> snapshot = ReplayProbe.mapOf(
                "game_time_seconds", 0,
                "source_game_time_seconds", 0.0,
                "replay_tick", 1,
                "game_state", 4,
                "players", List.of()
        );
        boolean rejected = false;
        try {
            ReplayProbe.compactSnapshot(snapshot);
        } catch (IllegalStateException expected) {
            rejected = true;
        }
        require(rejected, "compact snapshot accepted a non-ten-player source snapshot");
    }

    private static void testCellPositionConversion() {
        require(HeroPositions.coordinate(132, 97.75).equals(16993.75), "cell scale or offset was lost");
        require(HeroPositions.coordinate(90, 246.375).equals(11766.375),
                "a valid offset greater than one cell must not be rejected or wrapped");
        require(HeroPositions.coordinate(131, 225.75).equals(HeroPositions.coordinate(132, 97.75)),
                "equivalent cell/offset representations must agree");
    }

    private static void testInvalidPositionComponents() {
        for (Object[] pair : new Object[][] {
                {null, 1}, {1, null}, {-1, 1}, {65536, 1}, {1.5, 1},
                {Double.NaN, 1}, {1, Double.POSITIVE_INFINITY}, {1, -1}, {1, 256}, {"132", 1}
        }) {
            require(HeroPositions.coordinate(pair[0], pair[1]) == null, "invalid coordinate became an observation");
        }
    }

    private static Map<String, Object> positionProperties() {
        return new LinkedHashMap<>(Map.of(
                "m_iTeamNum", 2, "m_lifeState", 0, "m_bIsClone", false,
                HeroPositions.CELL_X, 132, HeroPositions.CELL_Y, 90,
                HeroPositions.OFFSET_X, 97.75, HeroPositions.OFFSET_Y, 246.375
        ));
    }

    private static HeroPositions.Source positionSource(Map<String, Object> properties) {
        return new HeroPositions.Source(123, "CDOTA_Unit_Hero_Juggernaut", true, true, properties::get);
    }

    private static Map<String, Object> positionRow(HeroPositions.Source source) {
        return HeroPositions.observeSource(0, 2, 8, 123, source);
    }

    private static void requireNoPosition(Map<String, Object> row, String status) {
        require(status.equals(row.get("position_status")), "wrong missing-position reason: " + row);
        require(row.get("x") == null && row.get("y") == null, "unreliable position must be explicit null");
    }

    private static void testSelectedHeroPosition() {
        Map<String, Object> row = positionRow(positionSource(positionProperties()));
        require("OBSERVED".equals(row.get("position_status")), "selected hero position was not observed");
        require(row.get("x").equals(16993.75) && row.get("y").equals(11766.375), "wrong world position");
        require(row.get("player_slot").equals(0) && row.get("hero_id").equals(8), "player identity changed");
        require(row.get("selected_hero_handle").equals(row.get("hero_entity_handle")), "selected handle was lost");
        Map<String, Object> properties = positionProperties();
        Float decodedOffset = 105.53124f;
        properties.put(HeroPositions.OFFSET_Y, decodedOffset);
        Map<String, Object> precise = positionRow(positionSource(properties));
        require(precise.get("offset_y").equals(decodedOffset.doubleValue()),
                "serialized component must preserve the same decoded precision as the world coordinate");
    }

    private static void testPositionRequiresCurrentPrimaryHero() {
        Map<String, Object> properties = positionProperties();
        requireNoPosition(positionRow(new HeroPositions.Source(124, "CDOTA_Unit_Hero_Juggernaut",
                true, true, properties::get)), "IDENTITY_UNVERIFIED");
        requireNoPosition(positionRow(new HeroPositions.Source(123, "CDOTA_Unit_Courier",
                true, true, properties::get)), "IDENTITY_UNVERIFIED");
        requireNoPosition(positionRow(new HeroPositions.Source(123, "CDOTA_Unit_Hero_Juggernaut",
                true, false, properties::get)), "ENTITY_INACTIVE");
        requireNoPosition(positionRow(new HeroPositions.Source(123, "CDOTA_Unit_Hero_Juggernaut",
                false, true, properties::get)), "ENTITY_INACTIVE");
        properties.put("m_iTeamNum", 3);
        requireNoPosition(positionRow(positionSource(properties)), "IDENTITY_UNVERIFIED");
        properties.put("m_iTeamNum", 2);
        properties.put("m_bIsClone", true);
        requireNoPosition(positionRow(positionSource(properties)), "IDENTITY_UNVERIFIED");
    }

    private static void testDeadOrUnknownLifeStateHasNoPosition() {
        Map<String, Object> properties = positionProperties();
        for (int state : List.of(1, 2)) {
            properties.put("m_lifeState", state);
            requireNoPosition(positionRow(positionSource(properties)), "HERO_NOT_ALIVE");
        }
        properties.remove("m_lifeState");
        requireNoPosition(positionRow(positionSource(properties)), "LIFE_STATE_UNAVAILABLE");
    }

    private static void testMissingEntityOrAxisNeverReusesPosition() {
        Map<String, Object> properties = positionProperties();
        Map<String, Object> observed = positionRow(positionSource(properties));
        requireNoPosition(positionRow(null), "ENTITY_UNAVAILABLE");
        properties.remove(HeroPositions.OFFSET_Y);
        requireNoPosition(positionRow(positionSource(properties)), "COORDINATES_UNAVAILABLE");
        require(observed.get("x").equals(16993.75), "an older observation was mutated");
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }
}
