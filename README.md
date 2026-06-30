# Dota Betting Autopilot

Rule-based MVP for Dota 2 betting research. It can run a scoped tournament
session, collect fake matches/odds/chat, score bet candidates, select a small
number of candidates, and record paper bets.

No real money is used. The project does not click bookmaker sites, bypass
captchas, parse private bookmaker accounts, or execute real bets. `auto` mode is
intentionally a safe stub:

```text
Auto execution is intentionally not implemented. Use an official permitted API adapter only.
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
4. Fetch fake odds snapshots and fake Twitch chat messages.
5. Build streamer signals with simple keyword rules.
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

## MVP Data

All collectors are fake implementations. Real odds or Twitch adapters can be
added later behind the same interfaces, preferably via official APIs.

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
