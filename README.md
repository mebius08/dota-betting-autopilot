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

## Reports

Show a paper trading summary from the default SQLite database:

```bash
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
