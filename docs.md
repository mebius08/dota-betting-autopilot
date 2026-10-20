# Dota 2 Live Analytics & Prediction Platform API

## GET /api/matches

Returns the historical match records stored in the selected SQLite database.
The endpoint does not fetch external data or generate match records.

**Parameters:** none. There are no path or query parameters and no request body.

**Response:** HTTP `200`, `Content-Type: application/json`, a JSON array of
`HistoricalMatch` records. Records follow the existing repository's ordering:
`started_at`, then `source`, then `source_match_id`. A database with no historical
matches returns `[]`.

Illustrative response structure; actual values come from the database:

```json
[
  {
    "id": "history-1",
    "source": "pandascore",
    "source_match_id": "history-1",
    "started_at": "2026-01-01T10:00:00Z",
    "ended_at": "2026-01-01T12:00:00Z",
    "team_a_name": "Team Spirit",
    "team_b_name": "PARIVISION",
    "team_a_source_id": "10",
    "team_b_source_id": "20",
    "winner_name": "Team Spirit",
    "winner_source_id": "10",
    "winner_side": "team_a",
    "tournament_name": "DreamLeague",
    "tournament_source_id": "100",
    "league_name": "DreamLeague",
    "league_source_id": "200",
    "series_name": "Season 25",
    "series_source_id": "300",
    "raw_stage_label": "Group Stage",
    "competitive_stage": "group",
    "normalized_round": "group",
    "best_of": 3,
    "status": "finished",
    "ingested_at": "2026-01-02T00:00:00Z"
  }
]
```

Identifiers are strings. Dates use ISO 8601 with the timezone stored in the
database; nullable fields remain JSON `null`. Stage and round enums serialize
to their string values. The endpoint exposes stored statuses without inferring
whether a historical record describes an individual map or a series.

## Database selection and errors

Start the server from the repository root:

```bash
python -m uvicorn app.api:create_app --factory --host 127.0.0.1 --port 8000
```

The default backend is SQLite with the repository's existing `data/autopilot.db`.
To select another file, set `DOTA_ANALYTICS_DB_PATH` before starting the server.
Relative configured paths are resolved from the current working directory. Python callers
can use `create_app(db_path=...)`; an explicit path takes precedence over the
environment variable.

For SQLite, application creation initializes the existing schema through
`SQLiteRepository`. A missing database file is created with empty tables; no match
data is inserted. Database initialization errors prevent startup. Database errors
while reading matches produce HTTP `500`; they are not converted to an empty
successful response. The browser interface at `GET /` loads records from this
endpoint.

Set `DOTA_ANALYTICS_DB_BACKEND=postgresql` to use the PostgreSQL adapter with the
same JSON response contract. This requires `POSTGRES_HOST`, `POSTGRES_PORT`,
`POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD`; `db_path` is then invalid.
Only the historical match table is initialized. Initialization and read errors
are propagated, without a fallback to SQLite. See the README for Compose startup.
