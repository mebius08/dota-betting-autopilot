# Local Clarity replay probe

This tool is intentionally isolated from the Python application. It reads a local
`.dem` with Clarity 4.0.1 and writes a gitignored JSON file under `output/`. The
launcher copies only the required Clarity artifacts already present in the
local Gradle cache into the gitignored `local-libs/` directory. The build has no
remote repositories.

Clock normalization is accepted only after the parser witnesses either a direct
game-clock zero crossing or the replay's
`CDOTAGamerulesProxy.m_pGameRules.m_flGameStartTime` property become positive.
For the latter format, elapsed replay ticks are adjusted with the entity's pause
tick properties and frozen when `m_flGameEndTime` becomes positive. Replay tick
time is retained separately. No fixed offset and no combat-log gold
reconstruction are used.

From the repository root, using only the already-local Gradle distribution and
dependency cache:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\replay_probe\invoke.ps1 -Task probeReplay -Replay local-data\replays\8897588873.dem -Output tools\replay_probe\output\8897588873.json
```
