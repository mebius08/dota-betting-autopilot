# Dota Betting Autopilot

Rule-based MVP for Dota 2 betting research. It can run a scoped tournament
session, collect fake matches/odds/streamer speech opinions, score bet
candidates, select a small number of candidates, and record paper bets.

No real money is used. The project does not click bookmaker sites, bypass
captchas, parse private bookmaker accounts, or execute real bets. `auto` mode is
intentionally a safe stub:

```text
Auto execution is intentionally not implemented.
Use an official permitted API adapter only.
```

## Quick Start

```bash
cd dota-betting-autopilot
pip install -e ".[dev]"
pytest
python -m app.main
```

If you prefer `requirements.txt`:

```bash
cd dota-betting-autopilot
pip install -r requirements.txt
pytest
python -m app.main
```

## Pipeline

1. Load `config.example.yaml`.
2. Start a session for a tournament keyword.
3. Fetch fake Dota matches and filter them by tournament scope.
4. Fetch fake odds snapshots and fake streamer utterances.
5. Build streamer speech/opinion signals with simple keyword rules.
6. Score markets with rule-based quality, phase, odds, streamer, and riskophile
   components.
7. Select up to `max_bets_per_match` candidates above threshold.
8. In `paper` mode, record virtual bets in memory.
9. Persist the session and paper bets to SQLite.
10. Build a summary report from stored paper bets.

## Modes

- `paper`: records virtual bets automatically.
- `signal`: scores and selects candidates without placing paper bets.
- `confirm`: reserved for a future confirmation UI/CLI.
- `auto`: deliberately not implemented for safety.

## CLI Usage

Show available commands:

```bash
python -m app.cli --help
```

Run one pass with a manual transcript file:

```bash
python -m app.cli run-once \
  --tournament DreamLeague \
  --transcript data/streamer_transcript.txt
```

Run a bounded loop:

```bash
python -m app.cli loop \
  --tournament DreamLeague \
  --transcript data/streamer_transcript.txt \
  --iterations 5 \
  --interval-seconds 30
```

Typical workflow:

1. Start `loop` during the selected tournament.
2. Write or paste streamer phrases into the transcript file, one phrase per line.
3. The bot reads the transcript, scores candidates, and records paper bets.
4. Paper trading data is stored in `data/autopilot.db`.
5. Use `report` to inspect stored paper trading history.
6. Delete `data/autopilot.db` for a clean research start.

## UX CLI Helpers

Manual transcript helpers:

```bash
python -m app.cli show-transcript --transcript data/streamer_transcript.txt
python -m app.cli add-utterance \
  --transcript data/streamer_transcript.txt \
  --speaker streamer \
  --text "over kills looks playable"
python -m app.cli clear-transcript --transcript data/streamer_transcript.txt
```

Offline SQLite session inspection:

```bash
python -m app.cli list-sessions --db data/autopilot.db
python -m app.cli show-session --db data/autopilot.db --session-id SESSION_ID
```

Transcript commands work with the manual/fake transcript workflow. Session
commands only read local SQLite paper-trading storage. They do not place real
bets, automate bookmaker sites, call Twitch/STT APIs, or use browser automation.

## Reports

Show a paper trading summary from the default SQLite database:

```bash
python -m app.cli report
python -m app.cli report --db data/autopilot.db
```

Filter by session and limit recent bets:

```bash
python -m app.cli report \
  --db data/autopilot.db \
  --session-id SESSION_ID \
  --last-bets 20
```

Include recent streamer utterances:

```bash
python -m app.cli report \
  --db data/autopilot.db \
  --show-utterances \
  --last-utterances 20
```

The report prints sessions, total bets, open and settled bets, profit units,
ROI, average bets per match, and recent bet history. If the database does not
exist yet, run `python -m app.main` or `python -m app.cli run-once` first.

## Settlement

Paper bets start as open bets with `status=placed`, `result=unknown`, and
`profit_units=0.0`. Manual settlement records the final paper result so reports
and ML training can use the history.

List open bets:

```bash
python -m app.cli open-bets
```

Settle a paper bet:

```bash
python -m app.cli settle-bet --bet-id BET_ID --result win
python -m app.cli settle-bet --bet-id BET_ID --result loss
```

`win` and `loss` settled bets are used by ML training. `push` and `void` are
stored for reporting, but they are not training targets. Profit is calculated
from `stake_pct`:

- `win`: `stake_pct * (odds - 1)`
- `loss`: `-stake_pct`
- `push` / `void`: `0.0`

## Data export/import

Export stored paper research data for offline analysis:

```bash
python -m app.cli export-bets --db data/autopilot.db --out exports/bets.csv
python -m app.cli export-candidates \
  --db data/autopilot.db \
  --out exports/candidates.csv
python -m app.cli export-utterances \
  --db data/autopilot.db \
  --out exports/utterances.csv
```

Exports use UTF-8 CSV, create the output directory when needed, always write a
header, and keep columns in the same stable order as the persisted SQLite/domain
fields. Exported CSV files are intended for local offline inspection and are
usually not committed.

Settlement imports work only with existing paper bets:

```csv
bet_id,outcome,profit_units
<bet-id>,win,1.25
<bet-id>,loss,-1.0
```

```bash
python -m app.cli import-settlements --db data/autopilot.db --csv settlements.csv
```

Invalid rows are skipped with warnings, while valid rows are still applied. The
CSV `profit_units` value is validated as numeric input; stored paper profit is
calculated by the existing settlement logic from the bet odds, stake, and
outcome.

Inspect offline data readiness before ML training or evaluation:

```bash
python -m app.cli inspect-dataset --db data/autopilot.db
```

The inspection report prints entity counts, open and settled bet counts,
win/loss/push/void outcomes, streamer utterance counts, usable ML records, and a
simple readiness status. This remains paper/research data only. It is not real
betting, bookmaker automation, or financial advice.

## Real match data adapter

The optional PandaScore adapter is a read-only source for Dota 2 match metadata.
It fetches matches and maps them into the existing `Match` domain entity. It
does not fetch bookmaker odds, place bets, run scoring, or execute paper bets.
Fake/manual collectors remain the default fallback for the local workflow.

Set a local PandaScore token before using the real provider:

```powershell
$env:PANDASCORE_TOKEN="your-token-here"
```

Fetch match metadata explicitly:

```bash
python -m app.cli fetch-matches --provider pandascore
python -m app.cli fetch-matches --provider pandascore --status upcoming --limit 10
python -m app.cli fetch-matches --provider pandascore --status live --limit 10
python -m app.cli fetch-matches --provider pandascore --scope ewc-2026
```

Network access happens only when the real provider command is used. Unit tests
mock the network boundary and stay offline. API availability, rate limits, and
provider plan behavior are controlled by PandaScore. Do not commit credentials.

## Historical professional Dota match dataset

The future historical ML model v2 is expected to train on completed
professional Dota match results, not on settled paper bets as its main data
source. This stage adds the storage and inspection foundation only. It stores
provider-listed past Dota matches, preserves provider tournament/league/series
metadata, records explicit match-winner labels when they can be mapped safely,
and keeps unresolved outcomes out of training queries.

This layer does not claim that every internet match returned by a provider is
Tier 1 or fully training-eligible. Provider metadata is preserved so tournament
quality and eligibility rules can be refined later. The current `train-ml`
command remains the old paper-bet v1 pipeline; historical ML v2, recency
weighting, roster lineage, model retraining, and champion/challenger workflows
are not implemented here.

Set a local PandaScore token before running an explicit real sync:

```powershell
$env:PANDASCORE_TOKEN="your-token-here"
```

Sync a bounded historical window:

```powershell
python -m app.cli sync-history --provider pandascore --db data/autopilot.db --since 2025-07-08 --until 2026-07-07 --page-size 100 --timeout 30
```

The sync uses the PandaScore past Dota match source and an explicit date
window. `--since` and `--until` are mandatory. If `--max-pages` is supplied,
the sync reads at most that many provider pages. If `--max-pages` is omitted,
pagination continues until the provider returns an empty or short terminal page
inside the explicit date window, with repeated-page detection to avoid endless
loops. Repeating the same sync is idempotent through
`source + source_match_id`, and later richer provider metadata can update an
existing row. The command is read-only against the provider and does not place
bets, create live signals, train models, or call bookmaker write APIs.

Inspect and export the local historical dataset:

```bash
python -m app.cli history-status --db data/autopilot.db
python -m app.cli export-history --db data/autopilot.db --out exports/history.csv
```

History export writes UTF-8 CSV with a stable header, creates the parent
directory, and never includes API tokens. `history-status` is offline and
reports total matches, usable winner records, point-in-time-ready records,
date ranges, stage distribution, unique teams, and unique tournaments.

Point-in-time safety is strict: a match is available to future features only
after its result is known. If a prediction timestamp is January 3 at 12:00 and
a match completed January 3 at 15:00, that match is not available for the 12:00
prediction. Match start time is not result availability time.

Team matching in this pre-roster layer is limited to PandaScore team IDs or a
controlled normalized-name fallback inside the same organization identity. It
does not create permanent aliases such as Tundra -> 1W or HEROIC -> LGD. A
roster-lineage layer connects relevant history through player/roster continuity
instead of organization-name aliases.

## Player and roster history foundation

The roster-history layer stores player identities separately from team
organizations. Player identity is keyed by provider `source + source_player_id`;
team organization identity is keyed by `source + source_team_id`. Display names
and nicknames can change without creating a new identity, and organization names
are not treated as complete competitive roster identity.

Roster snapshots are temporal, provider-observed records. They preserve the
provider context where the roster was seen, optional tournament identity,
`observed_at`, nullable provider validity metadata, player memberships, optional
coach membership when the provider payload actually contains stable coach data,
and provider provenance. Missing player IDs are not converted into name-only
global identities, missing team IDs are not converted into name-only
organizations, and coaches without stable provider IDs are ignored
conservatively instead of becoming global name-based identities.

The player-composition fingerprint is deterministic, order-independent, and
based on stable player IDs only. Organization ID is not part of that player-only
fingerprint, and coach data does not silently alter it. This means the same five
players may appear under different organizations without merging those
organizations or creating permanent transfer aliases. The storage foundation
does not persist lineage truth; the derived lineage layer below computes
continuity from snapshots at query time.

Sync bounded roster history from PandaScore tournament contexts already present
in the historical match table. The provider call uses PandaScore's tournament
roster endpoint, `GET /tournaments/{tournament_id_or_slug}/rosters`:

```powershell
$env:PANDASCORE_TOKEN="your-token-here"
python -m app.cli sync-rosters --provider pandascore --db data/autopilot.db --max-tournaments 25
```

`sync-rosters` is read-only against PandaScore, bounded by
`--max-tournaments`, and derives the most recent tournament IDs from persisted
historical matches. It stores provider-observed or expected tournament rosters.
The sync does not crawl the entire provider universe, place bets, train models,
or call bookmaker APIs.

Inspect the local roster dataset offline:

```powershell
python -m app.cli roster-status --db data/autopilot.db
```

`roster-status` makes no network calls. It reports player and organization
counts, roster snapshot counts, player and coach memberships, temporal-validity
coverage, observed timestamp range, and unique player-roster fingerprints.
`observed_at` is the point-in-time availability boundary: future-observed roster
information is not backfilled into earlier prediction timestamps. Provider
validity is stored only when the provider supplies it; the project does not
invent roster validity from tournament dates. Derived lineage logic can compare
historical roster continuity from these snapshots without adding permanent
aliases such as Tundra -> 1W or HEROIC -> LGD.

## Derived roster lineage / competitive continuity

Competitive lineage is derived from roster snapshots at query time. It does not
persist permanent transfer aliases, merge `TeamOrganization` rows, rewrite
provider IDs, or create a global competitive-team identity. Organization
identity is explanatory metadata; it is not lineage identity.

Continuity evidence uses stable provider player IDs, not player display names.
Exact player-set equality and strong player overlap are the primary evidence.
Stable coach continuity is represented separately by stable provider coach ID
and can support a qualifying three-player overlap, but coach names are ignored
and coach continuity alone is insufficient. The default conservative policy is:

- `EXACT`: same stable player set with at least five core players.
- `STRONG`: at least four shared players and at least 0.8 overlap against the
  smaller roster.
- `COACH_SUPPORTED`: at least three shared players, at least 0.6 overlap against
  the smaller roster, and the same stable coach provider ID.
- `WEAK`: at least three shared players without enough evidence to auto-link.
- `NONE`: insufficient stable player continuity.

These thresholds are project heuristics for the current lineage foundation, not
statistically proven optimal values. The policy is centralized so later
evaluation can change it without rewriting the resolver.

Availability time and competitive chronology are separate. `observed_at < as_of`
controls whether a snapshot may participate in a point-in-time lineage graph,
and a snapshot observed exactly at `as_of` is excluded. Competitive chronology
only orders already-available snapshots. Chronology precedence is:

1. explicit `valid_from`, when present;
2. tournament historical-match context from point-in-time eligible completed
   matches for the same provider tournament, using strict `ended_at < as_of`,
   then the earliest eligible `historical_matches.started_at`, or the latest
   eligible `ended_at` only if no start timestamp is available;
3. `observed_at` fallback.

Tournament match dates may help order already-available historical roster
observations, but they never make a snapshot available before its `observed_at`.
Lineage is directional: accepted edges point from previous snapshot to current
snapshot, and predecessor history follows those edges backwards. Future
competitive descendants are never returned as predecessor history.

Inspect the derived lineage summary offline:

```powershell
python -m app.cli lineage-status --db data/autopilot.db --as-of 2026-07-07T12:00:00Z
```

`lineage-status` makes no network calls and prints point-in-time available
snapshot count, chronology source counts, accepted exact/strong/coach-supported
links, ambiguity count, root snapshots, derived components, organization-crossing
links, and largest predecessor chain size. It does not print fake confidence
percentages and it does not sync or persist lineages.

## Point-in-time historical features

Historical feature generation is a deterministic offline input layer for a
future Historical ML Model v2. It is not model training, calibration, a betting
signal, bookmaker automation, or a live candidate engine.

Feature rows require an explicit `HistoricalPredictionContext`. For historical
training rows, the prediction timestamp is the target match `started_at`, never
`ended_at`, ingestion time, or current wall-clock time. Only completed historical
matches with `ended_at < prediction_timestamp` may contribute. A match completed
exactly at the prediction timestamp is excluded, matches still in progress at
the timestamp are excluded, and the target match is explicitly removed by
`source + source_match_id` even if malformed fixture timing would otherwise make
it look eligible.

Raw team form uses stable provider team IDs, not display names. Unknown or
unusable winner records are excluded from the denominator rather than counted as
losses. Empty history uses neutral cold-start defaults:
`raw_win_rate=0.5`, `recency_weighted_win_rate=0.5`, and
`opponent_adjusted_strength=0.0`, while explicit sample-count features remain in
the row so the future model can distinguish no history from known .500 history.

Roster lineage is used conservatively where point-in-time roster snapshots make
it safe. The bridge follows accepted predecessor snapshots only, uses chronology
windows for those roster organizations, and does not auto-use ambiguous
branches. Organization identity alone is not competitive history: old unrelated
1W matches do not automatically attach to a transferred Tundra roster, and
ancient unrelated LGD matches do not automatically attach to a transferred
HEROIC roster. Because historical match rows currently store organization/team
IDs rather than exact roster snapshot IDs, this bridge is conservative
organization-window attribution; it does not claim perfect match-to-roster
membership.

Recency weights are computed dynamically for each prediction timestamp with:

```text
exp(-age_days / decay_days)
```

where `age_days = prediction_timestamp - historical_match.ended_at`.
The default `decay_days=90.0` is an initial baseline configuration, not proven
optimal truth. No static eternal match weight is persisted, and the policy can be
changed later to evaluate alternatives such as 30, 60, 120, or 180 days.

Opponent-adjusted strength is a basic deterministic v1 strength-of-schedule
adjustment. For one prediction timestamp it builds one point-in-time historical
universe, computes recency-weighted base form for stable team IDs, runs fixed
batch iterations, and applies low-sample shrinkage toward neutral. Team A and
Team B are read from the same point-in-time strength state, so future opponent
results cannot affect an older row. Difference features consistently use
`Team A value - Team B value`; swapping the target orientation swaps A/B groups
and flips the difference signs.

Inspect feature readiness offline:

```powershell
python -m app.cli feature-status --db data/autopilot.db --as-of 2026-07-07T12:00:00Z --decay-days 90
```

`feature-status` makes no network calls. It reports the cutoff timestamp, decay
policy, available historical matches, usable match-result records, stable teams
in the derived strength state, average raw and weighted history mass,
opponent-adjusted strength range, and neutral cold-start policy.

## Tournament competitive stage model

The current product target is EWC 2026 Dota 2. EWC 2026 currently uses a
single-elimination playoff path, but that is not treated as the only supported
Dota tournament format. Historical professional Dota tournaments may use
double-elimination brackets, so the domain keeps upper-bracket and lower-bracket
contexts available for future historical features.

The model-oriented competitive stages are:

- `group`
- `crossover`
- `upper_bracket`
- `lower_bracket`
- `single_elimination`
- `grand_final`
- `placement`
- `unknown`

Round detail is preserved separately from the high-level competitive stage.
Quarterfinal and semifinal are round metadata; when there is no upper/lower
bracket context, they currently map to the broader `single_elimination` stage.
Upper-bracket matches mean a loss may move a team to a lower bracket.
Lower-bracket matches normally mean a loss eliminates the team.

Current EWC 2026 stage mapping:

- `Group Stage` -> `group`
- `Survival` / crossover matches -> `crossover`
- `Quarterfinal` / `Semifinal` -> `single_elimination`
- `Grand Final` -> `grand_final`
- `Third place` -> `placement`

`Survival - Grand Final` is a survival-phase final, not the tournament grand
final. Stage metadata is a future ML predictor and does not add a manual betting
coefficient or change the current scorer. A future historical model should learn
team-specific behavior in these contexts.

Inspect persisted EWC 2026 scope locally:

```bash
python -m app.cli ewc-status --db data/autopilot.db
```

`ewc-status` is read-only. It makes no network calls, places no bets, creates no
signals, and does not train or run the historical ML v2 layer. If no persisted
EWC 2026 matches exist, it still prints the canonical tournament id and a
friendly no-data message.

Team organization tags are not treated as permanent competitive roster identity.
For example, future roster-lineage work may relate a Tundra roster to current
1W, or a HEROIC roster to current LGD Gaming, but those are not team aliases in
the current system. Competitive relevance will later follow roster/player
continuity instead of organization-name substitution.

## Real odds data adapter

The optional OddsPapi adapter is a read-only source for Dota 2 match-winner odds.
It uses OddsPapi REST API v4 with Dota 2 `sportId=16`, maps supported
match-winner prices into the existing internal `map_winner` odds convention, and
does not run scoring, select candidates, create paper bets, settle bets, or place
real bets. Fake odds remain the default local workflow.

Set a local OddsPapi API key before using the real odds provider:

```powershell
$env:ODDSPAPI_API_KEY="your-api-key-here"
```

Fetch odds explicitly:

```bash
python -m app.cli fetch-odds --provider oddspapi
python -m app.cli fetch-odds --provider oddspapi --limit 10
python -m app.cli fetch-odds --provider oddspapi --bookmakers pinnacle,bet365
```

PandaScore match IDs and OddsPapi fixture IDs are different provider IDs. The
adapter includes deterministic team-name and start-time reconciliation helpers,
but ambiguous matches are skipped rather than attaching odds to the wrong match.
Network access happens only when the explicit odds command is used. Do not commit
credentials.

## Market Probability and Estimated Edge

The edge layer is a read-only research calculation over stored candidates and
odds. It does not create signals, place bets, calculate stake size, call
bookmaker write APIs, or provide financial advice.

Raw implied probability is calculated directly from decimal odds:

```text
raw implied probability = 1 / decimal_odds
```

For a complete two-way `map_winner` market from one bookmaker, bookmaker margin
is removed by normalizing both raw implied probabilities:

```text
fair market probability = selection raw implied probability / market overround
```

This fair market probability is a market-derived baseline after simple two-way
margin normalization. Incomplete markets are not guessed; one side is never
invented from the other side.

Model probability is only available when the ML predictor can load a model that
exposes a real positive-class `predict_proba` output. The current probability is
reported as raw `ml_predict_proba`; it is not claimed to be calibrated. Rule
score is not probability. Hybrid score is not probability. The code does not
normalize arbitrary scores by dividing by 100 or applying a sigmoid.

Estimated edge is a probability-point difference:

```text
estimated edge = model probability - fair market probability
```

Example:

```text
Model probability: 56%
Fair market probability: 49%
Estimated edge: +7 percentage points
```

Expected value per 1 unit is:

```text
expected value = model probability * decimal_odds - 1
```

Inspect persisted data:

```bash
python -m app.cli analyze-edge --db data/autopilot.db
python -m app.cli analyze-edge --db data/autopilot.db --model-path data/models/bet_model.joblib
```

If the model file is missing or cannot provide a valid class probability, the
command reports model probability, estimated edge, and expected value as
unavailable instead of failing. If stored data does not contain complete
same-bookmaker two-way market snapshots for a candidate, the command reports the
market as incomplete. OddsPapi fetching remains read-only and does not imply
that fetched odds have already been persisted to SQLite.

## Historical ML v2

Historical ML v2 predicts professional Dota `P(Team A wins)` from
point-in-time historical feature rows. It is separate from the legacy
settled-paper-bet ML layer and it is not a betting recommendation, market edge
calculation, Nix layer, bookmaker integration, or automatic betting system.

Training rows are built by the existing historical feature engine. For a target
match, the prediction timestamp is the match `started_at`. Only historical
matches completed strictly before that timestamp (`ended_at < started_at`) may
contribute. Roster observations also remain point-in-time safe with
`observed_at < prediction_timestamp`; future roster snapshots are represented
as missing rather than backfilled.

The default Historical ML v2 EWC 2026 baseline target scope is
`ewc_2026_baseline`. It starts at `2025-07-08T00:00:00Z` inclusive and allows
main-event target rows from these competition families:

* The International
* Esports World Cup / EWC
* DreamLeague
* BLAST
* ESL
* PGL
* FISSURE Playground
* BetBoom Dacha

Qualifiers are excluded. Competition family classification is based on
normalized provider tournament, league, and series metadata; season, year, and
edition variations do not need exact full-string equality. `FISSURE Playground`
is allowed, but `FISSURE Universe` and generic FISSURE tournaments are not
automatically allowed. BetBoom Dacha requires both BetBoom and Dacha identity;
generic BetBoom events are not automatically allowed.

Historical SQLite storage remains broad. The raw `historical_matches` table may
contain other professional Dota matches, and those records are not deleted or
rewritten merely because the default model target scope is curated. Target scope
and feature history are distinct concepts: scoped target rows may still draw
point-in-time-safe history from the existing feature engine, but no match can
contribute before its result is available. The strict feature-history rule stays
`ended_at < prediction_timestamp`.

The numeric schema is explicit and deterministic. Metadata such as source IDs,
team names, player names, tournament names, winner fields, and labels are not
model inputs. Competitive stage is represented as one-hot features:
`stage_group`, `stage_crossover`, `stage_upper_bracket`,
`stage_lower_bracket`, `stage_single_elimination`, `stage_grand_final`,
`stage_placement`, and `stage_unknown`; exactly one is active for a row.

Roster features are conservative. Missing rosters use explicit zero/neutral
values. Accepted continuity reuses derived roster lineage and exposes exact,
strong, and coach-supported flags. Ambiguous predecessor branches are not
auto-selected. `roster_matches_together` counts only safely attributable
lineage-window history, so old unrelated organization history does not inflate
transferred roster form.

Player aggregate features use stable provider player IDs, not names. Historical
player participation is attributed only when current point-in-time roster
history safely links the player to a roster snapshot window. Sparse coverage is
expected; no safely attributable player history gives zero sample counts and
neutral raw win-rate values.

The first baseline model is an sklearn `Pipeline`:

```text
StandardScaler -> LogisticRegression
```

Rows are split chronologically instead of randomly. The default baseline policy
is 70% train, 15% validation, and 15% test, with equal prediction timestamp
groups kept together where possible. Preprocessing and the logistic model are
fit only on the train partition; validation and test are never used to fit.
Metrics reported for train, validation, and test are Brier score, log loss,
accuracy, row count, positive label rate, and average predicted probability.

The default artifact path is separate from the legacy model:

```text
data/models/historical_match_win.joblib
```

The artifact stores the fitted pipeline, model type, feature schema version,
ordered feature names, training timestamp, recency decay policy, temporal split
policy, minimum-row policy, competition scope metadata, row counts, and
recorded metrics. Loading is strict: schema version, ordered feature names, and
competition scope metadata must match the current code.

Patch-aware Historical ML features remain a future data-source/enrichment task
because the currently confirmed PandaScore historical match payload represented
by this integration does not expose a trusted patch/version field. The current
pipeline does not infer Dota patches from match dates and does not hardcode a
patch calendar.

PowerShell workflow:

```powershell
python -m app.cli sync-history --provider pandascore --db data/autopilot.db --since 2025-07-08 --until 2026-07-07 --page-size 100 --timeout 30
python -m app.cli sync-rosters --provider pandascore --db data/autopilot.db
python -m app.cli historical-ml-status --db data/autopilot.db
python -m app.cli train-historical-ml --db data/autopilot.db --decay-days 90
python -m app.cli evaluate-historical-ml --db data/autopilot.db
```

`train-historical-ml` does not sync or download data. If the database has no
historical matches, or fewer than the configured minimum usable rows, it fails
cleanly without writing a model artifact.

## ML Layer

The bot still starts with rule-based scoring. The optional ML layer is a v1
overlay that learns from settled paper bets and blends its score with the rule
score. If no model exists, the bot keeps using the rule-based fallback.

Train a model from stored paper trading history:

```bash
python -m app.cli train-ml --db data/autopilot.db
```

Run one pass with optional ML scoring:

```bash
python -m app.cli run-once \
  --tournament DreamLeague \
  --transcript data/streamer_transcript.txt \
  --use-ml
```

ML training uses only settled `win` and `loss` paper bets. `unknown`, open,
push, and void bets are not training targets. Until there is enough settled
history, `train-ml` exits cleanly with `Not enough settled bets to train model`.

The current model is a simple `LogisticRegression` pipeline over candidate,
rule-score, odds, and streamer-speech features. It does not place real bets,
call bookmaker APIs, use Twitch APIs, run speech-to-text, or add browser
automation.

## ML Status

Check whether paper trading history has enough settled win/loss examples for
training:

```bash
python -m app.cli ml-status
python -m app.cli ml-status --min-rows 30
```

`ml-status` reports training rows, win/loss balance, ignored bets, and whether
the data is ready for training. Unknown/open bets are not used by ML training.
`push` and `void` bets are stored for reporting, but they are also excluded from
the training dataset.

## Evaluation / Backtest

Evaluate whether the optional ML scorer looks useful compared with the stored
rule-based score on settled paper bets:

```bash
python -m app.cli evaluate-ml --db data/autopilot.db
```

`evaluate-ml` builds an offline train/test split from settled `win` and `loss`
paper bets, trains an in-memory ML model on the train split, and compares it
with the saved rule-based candidate score on the test split. The report prints
usable settled records, train/test sizes, win/loss distribution, rule metrics,
ML metrics, profit/ROI-style paper metrics, and a simple conclusion.

`push`, `void`, open, and `unknown` results are counted in the report but are
not used as binary labels. If there are too few settled bets, the output is
`not_enough_data` or inconclusive instead of a traceback. This is research-only
paper evaluation, not financial advice and not a real-money betting signal.

## MVP Data

The default demo uses fake collectors. The media layer is based on streamer
speech/opinion text, not Twitch chat. Implemented collectors:

- `FakeStreamerSpeechCollector`: built-in fake streamer phrases.
- `TranscriptFileStreamerSpeechCollector`: reads one utterance per non-empty
  line from a local transcript text file.

Real speech-to-text is intentionally not implemented in this MVP. A future
adapter can use a local Whisper/faster-whisper style transcription pipeline or
another permitted STT backend. `python -m app.main` works without real Twitch,
real audio, browser automation, or network calls.

## SQLite Storage

Paper trading data is persisted to `data/autopilot.db`. The database file and
its parent directory are created automatically when you run:

```bash
python -m app.main
```

For a clean research run, stop the app and delete `data/autopilot.db`; the next
demo run will recreate it from `app/storage/schema.sql`.

This is still paper/research tracking only. The project does not place real
bets or implement real auto execution.
