# Local Clarity replay probe

This tool is intentionally isolated from the Python application. It reads a local
`.dem` with Clarity 4.0.1 and writes a gitignored JSON file under `output/`. The
Windows launcher copies the required Clarity artifacts from the local Gradle
cache into the gitignored `local-libs/` directory. The macOS/Linux launcher has
an explicit preparation step. The Gradle build has no remote repositories.

Clock normalization is accepted only after the parser witnesses either a direct
game-clock zero crossing or the replay's
`CDOTAGamerulesProxy.m_pGameRules.m_flGameStartTime` property become positive.
For the latter format, elapsed replay ticks are adjusted with the entity's pause
tick properties and frozen when `m_flGameEndTime` becomes positive. Replay tick
time is retained separately. No fixed offset and no combat-log gold
reconstruction are used.

## macOS / Linux

From the repository root:

```bash
./tools/replay_probe/invoke.sh --prepare
./tools/replay_probe/invoke.sh --version
./tools/replay_probe/invoke.sh check
```

Preparation needs Bash, curl, tar, unzip, and shasum or sha256sum. It downloads
Temurin JDK 17.0.20.1+1 from the official Adoptium release, Gradle 8.14.4 from
the official Gradle distribution service, and exactly these Maven Central JARs:

- clarity 4.0.1
- clarity-protobuf 6.1
- fastutil-core 8.5.12
- snappy-java 1.1.10.4
- slf4j-api 2.0.7

Every download is checked against a SHA-256 pinned in `invoke.sh`. The JDK hashes
come from the official release metadata, and the Gradle hash from its published
checksum. The JAR hashes were verified against Maven Central's published
SHA-256, or SHA-1 where that older artifact only publishes SHA-1.

Tools, archives, and Gradle caches stay in the ignored
`local-data/tool-cache/replay-probe/`; JARs stay in `local-libs/`. No administrator
access or global Java replacement is needed. An existing JDK 17 can instead be
selected with `JAVA_HOME`. If your shell exports a different Java version,
prefix the commands with `env -u JAVA_HOME` to use the local JDK without changing
that shell setting. Preparation supports macOS/Linux on arm64 and x86_64;
only the current host is downloaded. Ordinary launches never download anything:
they verify the JARs, select JDK 17 locally, disable toolchain auto-download, and
run Gradle with `--offline`. After preparation they work without network access.

`check` runs the existing Java self-tests; it does not need or parse a replay.
For a real replay already available locally:

```bash
./tools/replay_probe/invoke.sh probeReplay local-data/replays/MATCH_ID.dem tools/replay_probe/output/MATCH_ID.json
./tools/replay_probe/invoke.sh probeCompactReplay local-data/replays/MATCH_ID.dem tools/replay_probe/output/MATCH_ID.compact.json
```

For the bounded visibility investigation on the existing match, a separate mode
writes only a diagnostic JSON (no compact/spatial/visibility sidecars):

```bash
./tools/replay_probe/invoke.sh probeVisibilityTransitions local-data/replays/8960991322.dem tools/replay_probe/output/visibility-transitions-check/8960991322.visibility-transitions.json
```

The window is actual game time 2250–2340 seconds, seeded by the last real frame
before 2250. Enemy flags are compared at every completed non-synthetic network
frame, with source-word update witnesses; unchanged entity updates are not
transitions. Same-tick updates are coalesced to the final frame state. UNKNOWN
gaps and entity replacements are separate discontinuities. Real paused frames
retain frozen game time and increasing replay ticks. Recorded time is replay
network observation time, not the time a player noticed the enemy. Cadence and
the pre-window baseline are included in the diagnostic artifact.

Relative replay/output paths are always relative to the repository root, even
when the launcher is called from another directory. `probeTest` is also accepted
to run only the focused Java self-tests. These checks establish build readiness;
they do not establish successful extraction from a real `.dem`.

## Windows

From the repository root, using only the already-local Gradle distribution and
dependency cache:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\replay_probe\invoke.ps1 -Task probeReplay -Replay local-data\replays\8897588873.dem -Output tools\replay_probe\output\8897588873.json
```

The compact trajectory is a separate projection over the same parser state and
clock normalization. It keeps match identity, team draft metadata, normalized
minute snapshots, two team sums, and ten compact player rows while omitting
diagnostic paths, property inventories, handles, provenance, and validation
mismatch detail:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\replay_probe\invoke.ps1 -Task probeCompactReplay -Replay local-data\replays\8897588873.dem -Output tools\replay_probe\output\8897588873.compact.json
```
