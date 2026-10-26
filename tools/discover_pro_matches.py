"""Discover OpenDota professional metadata over a fixed, inclusive 366-day window.

Smoke: --max-pages 3. Persistence: --apply. Omit --max-pages for the full walk.
Repeat with the same checkpoint to resume; --restart rescans without deleting DB
records. Dry-run and apply checkpoints cannot be mixed. There is no league
allowlist, DEM request, model training, or mutation of historical match tables.
"""

import argparse
from collections.abc import Sequence
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.storage.postgres import postgres_connection_options_from_env


ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://api.opendota.com/api/proMatches"
WINDOW_DAYS = 366
TABLE = "opendota_pro_matches"
CHECKPOINT = ROOT / "tools/replay_probe/output/pro-match-discovery-366d/checkpoint.json"
SCHEMA = ROOT / "app/storage/postgres_pro_matches_schema.sql"
FIELDS = {
    "match_id": "match_id", "start_time": "start_time", "duration_seconds": "duration",
    "league_id": "leagueid", "league_name": "league_name",
    "radiant_team_id": "radiant_team_id", "radiant_team_name": "radiant_name",
    "dire_team_id": "dire_team_id", "dire_team_name": "dire_name", "radiant_win": "radiant_win",
    "radiant_score": "radiant_score", "dire_score": "dire_score", "series_id": "series_id",
    "series_type": "series_type", "version": "version",
}
COLUMNS = (*FIELDS, "raw_metadata")
UPSERT = (
    f"INSERT INTO {TABLE} ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))}) "
    "ON CONFLICT (match_id) DO UPDATE SET "
    + ", ".join(f"{name} = EXCLUDED.{name}" for name in COLUMNS if name != "match_id")
    + f", metadata_updated_at = CURRENT_TIMESTAMP WHERE {TABLE}.raw_metadata IS DISTINCT FROM EXCLUDED.raw_metadata "
    "RETURNING (xmax = 0) AS inserted"
)
COUNTERS = ("api_rows", "duplicates", "window_matches", "unknown_date_matches", "older_matches", "newer_matches",
            "inserted", "updated", "skipped")


def _integer(value, *, nullable=False, maximum=2**63 - 1):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError("Expected a nonnegative integer metadata field, or explicit NULL.")
    return value


def parse_match(raw):
    if not isinstance(raw, dict):
        raise ValueError("Professional match metadata must be an object.")
    result = {}
    for column, source in FIELDS.items():
        value = raw.get(source)
        if column.endswith("_name"):
            if value is not None and (not isinstance(value, str) or "\x00" in value):
                raise ValueError(f"Invalid metadata text: {source}")
        elif column == "radiant_win":
            if value is not None and not isinstance(value, bool):
                raise ValueError("radiant_win must be boolean or NULL.")
        else:
            maximum = 2**31 - 1 if column in {"duration_seconds", "radiant_score", "dire_score", "series_type", "version"} else 2**63 - 1
            value = _integer(value, nullable=column != "match_id", maximum=maximum)
        result[column] = value
    if not result["match_id"]:
        raise ValueError("A real positive match_id is required.")
    # Source zero means an unknown date, not a fabricated date in 1970.
    result["start_time"] = result["start_time"] or None
    result["raw_metadata"] = json.loads(json.dumps(raw, allow_nan=False))
    return result


def parse_page(payload, cursor, cutoff, as_of):
    if not isinstance(payload, list):
        raise ValueError("OpenDota proMatches must return an array.")
    matches, duplicates = {}, 0
    for raw in payload:
        row = parse_match(raw)
        mid = row["match_id"]
        if cursor is not None and mid > cursor:
            raise ValueError("OpenDota returned IDs above the requested cursor; pagination is not safe.")
        if mid == cursor:
            duplicates += 1
            continue
        if mid in matches:
            if json.dumps(matches[mid]["raw_metadata"], sort_keys=True) != json.dumps(row["raw_metadata"], sort_keys=True):
                raise ValueError("Conflicting duplicate match_id within one API page.")
            duplicates += 1
        matches[mid] = row
    if payload and not matches:
        raise ValueError("Cursor pagination made no progress.")
    next_cursor = min(matches) if matches else cursor
    if cursor is not None and matches and next_cursor >= cursor:
        raise ValueError("Cursor must strictly decrease.")
    known = [r["start_time"] for r in matches.values() if r["start_time"] is not None]
    selected = [r for r in matches.values() if r["start_time"] is None or cutoff <= r["start_time"] <= as_of]
    in_window = [r for r in selected if r["start_time"] is not None]
    older = sum(t < cutoff for t in known)
    # A single old match never terminates a mixed page. Unknown dates cannot prove a boundary.
    reason = "API_EXHAUSTED" if not payload else "OLDER_PAGE" if older == len(matches) else None
    summary = dict(api_rows=len(payload), duplicates=duplicates, window_matches=len(in_window),
                   unknown_date_matches=len(matches) - len(known), older_matches=older,
                   newer_matches=sum(t > as_of for t in known),
                   min_start_time=min(known, default=None), max_start_time=max(known, default=None))
    return selected, in_window, next_cursor, summary, reason


def fetch_page(client, cursor, *, delay=1.2, attempts=3, sleep=time.sleep, now=None):
    wait = delay
    for attempt in range(attempts):
        sleep(wait)
        response = None
        try:
            response = client.get(API_URL, params={} if cursor is None else {"less_than_match_id": cursor})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            status = response.status_code if response is not None else None
            if status is not None and not 200 <= status < 300 and status not in {408, 425, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"OpenDota HTTP {status}; checkpoint remains resumable.") from error
            if attempt + 1 == attempts:
                raise RuntimeError(f"OpenDota request failed after {attempts} attempts: {error}") from error
            wait = max(delay, min(2 ** (attempt + 1), 30))
            retry_after = response.headers.get("Retry-After") if response is not None else None
            if retry_after:
                try:
                    required = float(retry_after)
                except ValueError:
                    try:
                        required = (parsedate_to_datetime(retry_after) - (now or datetime.now(timezone.utc))).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        required = 0
                if required > 60:
                    raise RuntimeError(f"Retry-After requires {required}s; stopping rather than retrying early. Resume later.") from error
                wait = max(wait, required)


def save_checkpoint(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_checkpoint(path, *, apply, restart=False, as_of=None):
    if path.exists() and not restart:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state["version"] != 1 or state["days"] != WINDOW_DAYS or state["api_url"] != API_URL
                or state["apply"] != apply or state["cutoff"] != state["as_of"] - WINDOW_DAYS * 86400
                or (as_of is not None and state["as_of"] != as_of)):
            raise ValueError("Checkpoint scope/window/mode mismatch; use a separate checkpoint or explicit --restart.")
        if state["next_cursor"] is not None and not _integer(state["next_cursor"]):
            raise ValueError("Invalid checkpoint cursor.")
        _integer(state["pages"])
        for name in COUNTERS:
            _integer(state["counts"][name])
        if not isinstance(state["complete"], bool):
            raise ValueError("Invalid checkpoint completion state.")
        return state
    timestamp = int(datetime.now(timezone.utc).timestamp()) if as_of is None else as_of
    return dict(version=1, days=WINDOW_DAYS, api_url=API_URL, apply=apply, as_of=timestamp,
                cutoff=timestamp - WINDOW_DAYS * 86400, next_cursor=None, pages=0, complete=False,
                completion_reason=None, counts=dict.fromkeys(COUNTERS, 0), league_ids=[], team_ids=[],
                min_start_time=None, max_start_time=None)


def upsert_page(connection, rows):
    counts = dict(inserted=0, updated=0, skipped=0)
    # Caller uses autocommit=True; each page commits BEFORE its cursor is saved.
    with connection.transaction():
        for row in rows:
            values = tuple(Jsonb(row[name]) if name == "raw_metadata" else row[name] for name in COLUMNS)
            result = connection.execute(UPSERT, values).fetchone()
            counts["skipped" if result is None else "inserted" if result["inserted"] else "updated"] += 1
    return counts


def database_counts(connection):
    names = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename").fetchall()
    return {row["tablename"]: connection.execute(sql.SQL("SELECT count(*) AS n FROM public.{}").format(
        sql.Identifier(row["tablename"]))).fetchone()["n"] for row in names}


def catalog_stats(connection, cutoff, as_of):
    result = connection.execute(f"""SELECT count(*) AS catalog_rows,
        count(*) FILTER (WHERE start_time BETWEEN %s AND %s) AS window_rows,
        count(*) FILTER (WHERE start_time IS NULL) AS unknown_date_rows,
        min(start_time) FILTER (WHERE start_time BETWEEN %s AND %s) AS min_start_time,
        max(start_time) FILTER (WHERE start_time BETWEEN %s AND %s) AS max_start_time,
        count(DISTINCT league_id) FILTER (WHERE start_time BETWEEN %s AND %s AND league_id > 0) AS leagues
        FROM {TABLE}""", (cutoff, as_of) * 4).fetchone()
    result["teams"] = connection.execute(f"""SELECT count(DISTINCT team_id) AS n FROM (
        SELECT radiant_team_id AS team_id FROM {TABLE} WHERE start_time BETWEEN %s AND %s
        UNION SELECT dire_team_id FROM {TABLE} WHERE start_time BETWEEN %s AND %s
        ) teams WHERE team_id > 0""", (cutoff, as_of) * 2).fetchone()["n"]
    result["source_statuses"] = connection.execute(
        f"SELECT dem_status, game_state_status, count(*) AS rows FROM {TABLE} GROUP BY dem_status, game_state_status"
    ).fetchall()
    return result


def discover(client, checkpoint, *, apply=False, connection=None, max_pages=None, restart=False,
             as_of=None, delay=1.2, attempts=3, sleep=time.sleep, database_counts_before=None):
    if apply and connection is None:
        raise ValueError("--apply requires a real PostgreSQL connection; no storage fallback.")
    state = load_checkpoint(checkpoint, apply=apply, restart=restart, as_of=as_of)
    if database_counts_before is not None:
        state.setdefault("initial_database_counts", database_counts_before)
    save_checkpoint(checkpoint, state)
    initial_counts = dict(state["counts"])
    run_pages = 0
    try:
        while not state["complete"] and (max_pages is None or run_pages < max_pages):
            payload = fetch_page(client, state["next_cursor"], delay=delay, attempts=attempts, sleep=sleep)
            rows, window, cursor, info, reason = parse_page(payload, state["next_cursor"], state["cutoff"], state["as_of"])
            written = upsert_page(connection, rows) if apply else dict(inserted=0, updated=0, skipped=0)
            pending = deepcopy(state)
            for name in COUNTERS:
                pending["counts"][name] += info.get(name, written.get(name, 0))
            pending.update(next_cursor=cursor, pages=state["pages"] + 1, complete=reason is not None,
                           completion_reason=reason, last_page=info, last_error=None)
            pending["league_ids"] = sorted(set(state["league_ids"]) | {r["league_id"] for r in window if r["league_id"] and r["league_id"] > 0})
            pending["team_ids"] = sorted(set(state["team_ids"]) | {r[field] for r in window for field in ("radiant_team_id", "dire_team_id") if r[field] and r[field] > 0})
            times = [r["start_time"] for r in window]
            if state["min_start_time"] is not None:
                times.extend([state["min_start_time"], state["max_start_time"]])
            pending.update(min_start_time=min(times, default=None), max_start_time=max(times, default=None))
            save_checkpoint(checkpoint, pending)
            state, run_pages = pending, run_pages + 1
            print(f"PAGE {state['pages']}: cursor={cursor}, window={len(window)}, writes={written}", file=sys.stderr, flush=True)
    except (httpx.HTTPError, RuntimeError, ValueError, psycopg.Error) as error:
        state["last_error"] = str(error)
        save_checkpoint(checkpoint, state)
        raise
    return state | {"run_pages": run_pages,
                    "run_counts": {name: state["counts"][name] - initial_counts[name] for name in COUNTERS}}


def _as_of(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of requires an explicit timezone.")
    return int(parsed.timestamp())


def summary(state):
    def date(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None
    return dict(mode="APPLY" if state["apply"] else "DRY_RUN", complete=state["complete"],
                window_boundary_reached=state["completion_reason"] == "OLDER_PAGE",
                completion_reason=state["completion_reason"], pages=state["pages"], next_cursor=state["next_cursor"],
                window=dict(from_utc=date(state["cutoff"]), through_utc=date(state["as_of"])),
                observed_dates=dict(from_utc=date(state["min_start_time"]), through_utc=date(state["max_start_time"])),
                counts_cumulative=state["counts"], counts_this_run=state["run_counts"], run_pages=state["run_pages"],
                leagues=len(state["league_ids"]), teams=len(state["team_ids"]),
                note="Metadata only. No DEM or complete game-state availability is inferred; no tournament allowlist filter.")


def main(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT)
    parser.add_argument("--max-pages", type=int, help="Bound this invocation only; omission allows the entire window.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restart", action="store_true", help="Rescan from newest; never clears the database.")
    parser.add_argument("--as-of", type=_as_of, help="Freeze the inclusive window at an ISO timestamp, otherwise checkpoint/UTC now.")
    parser.add_argument("--request-delay", type=float, default=1.2)
    args = parser.parse_args(argv)
    if (args.max_pages is not None and args.max_pages < 1) or args.request_delay < 1:
        parser.error("--max-pages must be positive; --request-delay must be at least 1 second.")
    try:
        with httpx.Client(timeout=15, headers={"User-Agent": "DotaAnalytics/pro-match-discovery", "Accept": "application/json"}) as client:
            if args.apply:
                with psycopg.connect(**postgres_connection_options_from_env(), connect_timeout=5,
                                      autocommit=True, row_factory=dict_row) as connection:
                    before = database_counts(connection)
                    print("DATABASE_COUNTS_BEFORE " + json.dumps(before, sort_keys=True), file=sys.stderr, flush=True)
                    connection.execute(SCHEMA.read_text(encoding="utf-8"))
                    state = discover(client, args.checkpoint, apply=True, connection=connection, max_pages=args.max_pages,
                                     restart=args.restart, as_of=args.as_of, delay=args.request_delay, database_counts_before=before)
                    report = summary(state) | dict(database_counts_before=before, database_counts_after=database_counts(connection),
                                                   catalog=catalog_stats(connection, state["cutoff"], state["as_of"]))
                    if any(report["database_counts_after"].get(name) != count for name, count in before.items() if name != TABLE):
                        raise RuntimeError("Existing table counts changed during discovery; investigate without deleting data.")
            else:
                report = summary(discover(client, args.checkpoint, max_pages=args.max_pages, restart=args.restart,
                                          as_of=args.as_of, delay=args.request_delay))
        print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, psycopg.Error, httpx.HTTPError) as error:
        print(f"Professional discovery failed: {error}; checkpoint={args.checkpoint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
