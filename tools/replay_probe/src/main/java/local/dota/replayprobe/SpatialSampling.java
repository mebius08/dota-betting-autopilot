package local.dota.replayprobe;

/** A sampling schedule over the existing normalized clock, not a second clock. */
final class SpatialSampling {
    static final int INTERVAL_SECONDS = 10;

    record Sample(int targetGameTimeSeconds, float frameGameTimeSeconds, int replayTick) {}

    private int nextTarget;
    private int skippedTargets;
    private int syntheticTicksIgnored;
    private int pausedFramesIgnored;
    private Integer previousTick;
    private Float previousFrameTime;

    Sample observe(float frameGameTime, int replayTick) {
        return observe(frameGameTime, replayTick, false, false);
    }

    Sample observe(float frameGameTime, int replayTick, boolean synthetic, boolean paused) {
        // Clarity may fill ticks with retained entity state. Such ticks are not new observations.
        if (synthetic) {
            syntheticTicksIgnored++;
            return null;
        }
        // The entity pause-start counter may use a different tick origin from the demo.
        // Ignore active-pause frames before checking the normalized clock's monotonicity.
        if (paused) {
            pausedFramesIgnored++;
            return null;
        }
        if (!Float.isFinite(frameGameTime) || frameGameTime < 0 || replayTick < 0) {
            throw new IllegalArgumentException("Spatial samples require finite nonnegative frame time and tick");
        }
        if (previousTick != null) {
            if (replayTick < previousTick || frameGameTime < previousFrameTime
                    || (replayTick == previousTick && frameGameTime != previousFrameTime)) {
                throw new IllegalStateException("Spatial frame game time and replay tick must be monotonic: previous="
                        + previousFrameTime + "@" + previousTick + ", current=" + frameGameTime + "@" + replayTick);
            }
            if (replayTick == previousTick) {
                return null;
            }
        }
        previousTick = replayTick;
        previousFrameTime = frameGameTime;
        if (frameGameTime < nextTarget) {
            return null;
        }

        int target = Math.multiplyExact((int) Math.floor(frameGameTime / (double) INTERVAL_SECONDS),
                INTERVAL_SECONDS);
        // Emit only this current frame. Never copy it into older targets crossed during a gap.
        skippedTargets += (target - nextTarget) / INTERVAL_SECONDS;
        nextTarget = Math.addExact(target, INTERVAL_SECONDS);
        return new Sample(target, frameGameTime, replayTick);
    }

    int skippedTargets() {
        return skippedTargets;
    }

    int syntheticTicksIgnored() {
        return syntheticTicksIgnored;
    }

    int pausedFramesIgnored() {
        return pausedFramesIgnored;
    }
}
