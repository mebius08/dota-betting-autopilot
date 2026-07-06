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
```

Network access happens only when the real provider command is used. Unit tests
mock the network boundary and stay offline. API availability, rate limits, and
provider plan behavior are controlled by PandaScore. Do not commit credentials.

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
