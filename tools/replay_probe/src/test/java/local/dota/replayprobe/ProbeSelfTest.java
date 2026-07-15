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
        System.out.println("probeTest: 5 focused tests passed");
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

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }
}
