# Dota 2 Live Analytics & Prediction Platform API

## GET /api/matches

Returns the historical match records stored in the configured SQLite or
PostgreSQL database.
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

## GET /api/matches/{source}/{source_match_id}

Returns one historical match identified by the exact pair of source and
source-local match ID. The internal catalog `id` is not used for this lookup.

**Path parameters:** `source` and `source_match_id` are strings. The same
`source_match_id` may exist under different sources; both values must match.
Each identifier occupies one URL path segment. There are no query parameters
or request body.

Example request for one of the imported local replay records:

```bash
curl http://127.0.0.1:8000/api/matches/replay_clarity/8960991322
```

**Response:** HTTP `200`, `Content-Type: application/json`, a single
`HistoricalMatch` object with exactly the same 24 fields and serialization as
one element of `GET /api/matches`, without the enclosing array. Nullable fields
remain `null`; dates and enum values follow the contract above.

An unknown pair returns HTTP `404`:

```json
{"detail": "Historical match not found."}
```

Database connection, query and decoding errors produce HTTP `500`, not `404`
or an empty successful response. Both backends use parameterized queries.

## GET /api/matches/{source}/{source_match_id}/economy

Returns the stored economic history of one historical match from PostgreSQL.
The data is **HISTORICAL_ORACLE** context from replay callbacks; it is not
guaranteed to be available in LIVE. This endpoint only reads existing data and
does not import snapshots or create the economy table.

**Path parameters:** `source` and `source_match_id` are strings identifying the
exact catalog pair, as in the match detail endpoint. There are no query
parameters or request body.

Example request:

```bash
curl http://127.0.0.1:8000/api/matches/replay_clarity/8960991322/economy
```

**Response:** HTTP `200`, `Content-Type: application/json`, a JSON array ordered
by `actual_game_time_seconds`, then `replay_tick`, both ascending. Each object
has exactly these eight fields. This excerpt shows one snapshot from the
canonical compact file for `8960991322`:

```json
[
  {
    "actual_game_time_seconds": 60.000004,
    "replay_tick": 34034,
    "radiant_net_worth": 3894,
    "dire_net_worth": 3839,
    "radiant_current_gold": 669,
    "dire_current_gold": 1099,
    "radiant_total_xp": 533,
    "dire_total_xp": 599
  }
]
```

`actual_game_time_seconds` is the normalized game time in seconds at the actual
callback, read without rounding from the stored floating-point value. It is not
the nominal minute grid time. `replay_tick` is the separate integer replay tick;
the API does not convert it to time. Net worth and current gold are team totals
in gold units; total XP is accumulated team experience in XP units. All six
economic totals are stored integers. Compact callbacks may be synthetic, so this
contract does not imply a real network observation at every minute.

For PostgreSQL, an unknown catalog pair returns HTTP `404` with
`{"detail": "Historical match not found."}`. An existing match without snapshots
returns HTTP `200` and `[]`. Connection, query and decoding errors, including a
missing economy table, produce HTTP `500`; they are not empty successful results.

For the SQLite backend, this capability is unsupported, regardless of whether
the match exists. Requests return HTTP `501`:

```json
{"detail": "Historical match economy is not supported by this database backend."}
```

## GET /api/matches/{source}/{source_match_id}/pov

Returns the stored **HISTORICAL_TEAM_POV** history from PostgreSQL. These are
historical team observations, not GLOBAL data; availability of an equivalent
LIVE stream has not been demonstrated. The endpoint only reads the existing
`historical_match_pov_snapshots` table and does not initialize or import it.

**Path parameters:** `source` and `source_match_id` are strings identifying the
exact catalog pair. Each occupies one URL path segment. There are no query
parameters or request body. Both Radiant and Dire histories are returned.

Example request:

```bash
curl http://127.0.0.1:8000/api/matches/replay_clarity/8960991322/pov
```

**Response:** HTTP `200`, `Content-Type: application/json`, a JSON array ordered
ascending by `actual_game_time_seconds`, `replay_tick`, then `pov_team` (DIRE
before RADIANT at the same frame). Each object has exactly the following ten
fields. This excerpt shows the two stored POV rows around 38:30 from the
canonical local POV CSV for `8960991322`:

```json
[
  {
    "actual_game_time_seconds": 2310.0334,
    "replay_tick": 101535,
    "pov_team": "DIRE",
    "visible_enemy_count": 3,
    "visible_enemy_with_position_count": 0,
    "hidden_enemy_count": 2,
    "own_team_spread": 1908.7880430130106,
    "nearest_visible_enemy_distance": null,
    "own_team_spread_status": "OK",
    "nearest_visible_enemy_status": "NO_VISIBLE_ENEMY_POSITION"
  },
  {
    "actual_game_time_seconds": 2310.0334,
    "replay_tick": 101535,
    "pov_team": "RADIANT",
    "visible_enemy_count": 3,
    "visible_enemy_with_position_count": 3,
    "hidden_enemy_count": 2,
    "own_team_spread": 5879.364732537277,
    "nearest_visible_enemy_distance": 5397.91892634111,
    "own_team_spread_status": "OK",
    "nearest_visible_enemy_status": "OK"
  }
]
```

`actual_game_time_seconds` is the actual normalized game time in seconds, read
without rounding to the nominal ten-second grid. `replay_tick` remains a separate
integer source tick. `pov_team` is `RADIANT` or `DIRE`. Distances and spread retain
their stored Source 2 coordinate units, without unit conversion.

`visible_enemy_count` counts raw VISIBLE flags, which can persist after death;
it is not a count of observed living opponents. The separate
`visible_enemy_with_position_count` counts VISIBLE opponents with verified living
positions. `hidden_enemy_count` counts HIDDEN flags; UNKNOWN is not classified as
HIDDEN. Spread and nearest distance may be `null`; nulls are never replaced by
zero. Their original statuses are returned unchanged:
`own_team_spread_status` is `OK` or `INSUFFICIENT_OWN_POSITIONS`;
`nearest_visible_enemy_status` is `OK`, `NO_OWN_POSITION` or
`NO_VISIBLE_ENEMY_POSITION`. Missing distance does not prove that no opponents
are nearby. Hidden coordinates, GLOBAL values and future targets are not exposed.

For PostgreSQL, an unknown catalog pair returns HTTP `404` with
`{"detail": "Historical match not found."}`. An existing match without POV rows
returns HTTP `200` and `[]`. Database connection/query and response validation
errors produce HTTP `500`, including a missing POV table; no SQLite fallback or
empty successful result masks them.

SQLite does not support this capability, regardless of whether the match exists.
Requests return HTTP `501`:

```json
{"detail": "Historical match POV is not supported by this database backend."}
```

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
