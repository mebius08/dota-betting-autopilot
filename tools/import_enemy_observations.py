"""Import existing historical Team POV enemy CSVs, never GLOBAL positions.

Default: PostgreSQL read-only preflight; --apply adds one table and commits one
match per transaction. --limit bounds incomplete matches attempted. Manifests
identify all inputs. Only frame metadata/rosters are read from existing tables;
current and last-seen coordinates come exclusively from each observer's CSV.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
import csv
import json
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from app.analytics.historical_snapshot import _enemy_decimal, _enemy_integer, _enemy_positions
from app.replay_pov_features import ENEMY_COLUMNS, TEAMS
from app.storage.postgres import postgres_connection_options_from_env
from tools import import_hero_snapshots as heroes


ROOT, OUTPUT, MANIFESTS = heroes.ROOT, heroes.OUTPUT, heroes.MANIFESTS
SCHEMA = ROOT / "app/storage/postgres_enemy_observations_schema.sql"
TABLE = "historical_match_enemy_observations"
SOURCE = "replay_clarity"
KEY = ("source", "source_match_id", "replay_tick", "pov_team", "enemy_player_slot")
POSITION_FIELDS = ENEMY_COLUMNS[8:]
COLUMNS = (*KEY, "replay_sha256", "actual_game_time_seconds", "provenance",
           "enemy_hero_id", "enemy_team", *POSITION_FIELDS)


@dataclass
class EnemyData:
    match_id: int
    replay_sha256: str
    rows: list[tuple]
    # (tick, observer) -> (exact actual time, visible flags, visible positions, hidden flags)
    frames: dict[tuple, tuple]
    roster: set[tuple]


def discover(manifests):
    confirmed, excluded, protected = heroes.discover(manifests)
    artifacts = {}
    for manifest in manifests:
        entries = json.loads(heroes.batch.source_path(manifest).read_text())["matches"]
        for entry in entries.values() if isinstance(entries, dict) else entries:
            if entry.get("status", entry.get("parser_status")) not in ("PARSE_OK", "PASSED"):
                continue
            found = [a for a in entry["artifacts"] if a["path"].endswith("/enemy_observations.csv")]
            if len(found) != 1:
                raise ValueError(f"Expected one enemy observation CSV: {entry['match_id']}")
            if artifacts.setdefault(entry["match_id"], found[0]) != found[0]:
                raise ValueError(f"Conflicting enemy CSV manifest references: {entry['match_id']}")
    return [(mid, sha, artifacts[mid]) for mid, sha, _ in confirmed], excluded, protected


def prepare(record):
    mid, digest, artifact = record
    path = heroes.batch.source_path(artifact["path"])
    if heroes.sha256(path) != artifact["sha256"]:
        raise ValueError(f"Enemy CSV artifact SHA mismatch: {mid}")
    groups = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(ENEMY_COLUMNS):
            raise ValueError("Expected exact enemy CSV schema; extra fields are forbidden")
        for raw in reader:
            if None in raw or any(v is None for v in raw.values()):
                raise ValueError("Malformed enemy CSV row")
            if _enemy_integer(raw["match_id"], minimum=1) != mid or raw["replay_sha256"] != digest:
                raise ValueError(f"Enemy match/replay SHA mismatch: {mid}")
            at = _enemy_decimal(raw["game_time_seconds"])
            tick = _enemy_integer(raw["replay_tick"])
            slot = _enemy_integer(raw["enemy_player_slot"])
            hero = _enemy_integer(raw["enemy_hero_id"], minimum=1)
            team, enemy = raw["pov_team"], raw["enemy_team"]
            if (at < 0 or tick > 2**63 - 1 or slot > 9 or hero > 2**31 - 1
                    or team not in TEAMS or enemy not in TEAMS or team == enemy):
                raise ValueError("Invalid time/tick/observer/enemy identity")
            positions = _enemy_positions(raw, at)  # Existing Decimal/status/causality validator.
            group = groups.setdefault((tick, team), {})
            if slot in group or any(r["enemy_hero_id"] == hero for r in group.values()):
                raise ValueError("Duplicate enemy identity within observer frame")
            group[slot] = dict(actual_game_time_seconds=at, enemy_player_slot=slot,
                               enemy_hero_id=hero, enemy_team=enemy, **positions)
    if not groups:
        raise ValueError("Enemy CSV contains no observations")
    rows, frames, history, roster = [], {}, {}, None
    previous_time = None
    for tick in sorted({tick for tick, _ in groups}):
        paired = [groups.get((tick, team), {}) for team in TEAMS]
        if any(len(group) != 5 for group in paired):
            raise ValueError(f"Incomplete paired enemy frame: {mid}, tick={tick}")
        times = {r["actual_game_time_seconds"] for group in paired for r in group.values()}
        current = {(slot, r["enemy_team"], r["enemy_hero_id"])
                   for group in paired for slot, r in group.items()}
        if (len(times) != 1 or len({r[0] for r in current}) != 10
                or len({r[2] for r in current}) != 10
                or Counter(r[1] for r in current) != {"RADIANT": 5, "DIRE": 5}
                or (roster is not None and roster != current)):
            raise ValueError("Inconsistent paired time or primary hero roster")
        at = times.pop()
        if previous_time is not None and at <= previous_time:
            raise ValueError("Actual time must increase with source replay ticks")
        previous_time, roster = at, current
        for team, group in zip(TEAMS, paired, strict=True):
            frames[tick, team] = (
                at, sum(r["visibility_state"] == "VISIBLE" for r in group.values()),
                sum(r["current_x"] is not None for r in group.values()),
                sum(r["visibility_state"] == "HIDDEN" for r in group.values()),
            )
            for slot, r in sorted(group.items()):
                identity = (team, slot)
                if r["current_x"] is not None:
                    history[identity] = (at, r["current_x"], r["current_y"])
                seen = None if r["last_seen_status"] == "NEVER_SEEN" else (
                    r["last_seen_game_time_seconds"], r["last_seen_x"], r["last_seen_y"])
                if seen != history.get(identity):
                    raise ValueError(f"Last-seen is not this observer's prior visible sample: {identity}")
                rows.append((SOURCE, str(mid), tick, team, slot, digest, at, "HISTORICAL_TEAM_POV",
                             r["enemy_hero_id"], r["enemy_team"], *(r[k] for k in POSITION_FIELDS)))
    return EnemyData(mid, digest, rows, frames, roster)


def check_parent(connection, data):
    params = (SOURCE, str(data.match_id))
    with connection.cursor() as cursor:
        cursor.execute("SELECT actual_game_time_seconds,replay_tick,pov_team,replay_sha256,"
                       "visible_enemy_count,visible_enemy_with_position_count,hidden_enemy_count "
                       "FROM historical_match_pov_snapshots WHERE source=%s AND source_match_id=%s", params)
        expected = {(float(at), tick, team, data.replay_sha256, visible, positioned, hidden)
                    for (tick, team), (at, visible, positioned, hidden) in data.frames.items()}
        # Existing team aggregate time is DOUBLE; only this compatibility comparison
        # uses float. All new values remain exact NUMERIC/Decimal.
        if set(cursor.fetchall()) != expected:
            raise ValueError(f"Stored Team POV frame/SHA/count conflict or missing match: {data.match_id}")
        # Check the existing exact clock metadata, NEVER load GLOBAL coordinates.
        cursor.execute("SELECT actual_game_time_seconds,replay_tick,replay_sha256 "
                       "FROM historical_match_spatial_frames WHERE source=%s AND source_match_id=%s", params)
        if set(cursor.fetchall()) != {(v[0], tick, data.replay_sha256) for (tick, _), v in data.frames.items()}:
            raise ValueError(f"Exact source frame time/tick/SHA conflict: {data.match_id}")
        cursor.execute("SELECT DISTINCT player_slot,team,hero_id FROM historical_match_hero_snapshots "
                       "WHERE source=%s AND source_match_id=%s", params)
        if set(cursor.fetchall()) != data.roster:
            raise ValueError(f"Stored primary hero roster conflict: {data.match_id}")


def check_existing(connection, data, present=True):
    actual = set()
    if present:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT {} FROM {} WHERE source=%s AND source_match_id=%s").format(
                sql.SQL(", ").join(map(sql.Identifier, COLUMNS)), sql.Identifier(TABLE)),
                (SOURCE, str(data.match_id)))
            actual = set(cursor.fetchall())
    expected = set(data.rows)
    if not actual <= expected:
        raise ValueError(f"Existing enemy observations conflict with source: {data.match_id}")
    return len(expected - actual)


def apply_match(connection, data):
    with connection.transaction():
        check_parent(connection, data)
        check_existing(connection, data)
        with connection.cursor() as cursor:
            inserted = heroes.insert_pages(cursor, TABLE, COLUMNS, KEY, data.rows)
        if check_existing(connection, data):
            raise ValueError("Incomplete enemy observation transaction")
    return inserted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report", type=Path, default=OUTPUT / "postgres-enemy-import/report.json")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if not args.report.resolve().is_relative_to(OUTPUT.resolve()):
        parser.error("--report must be inside replay_probe/output")
    if args.report.resolve() in {Path(p).resolve() for p in MANIFESTS}:
        parser.error("--report cannot overwrite a manifest")
    report = dict(mode="APPLY" if args.apply else "PREFLIGHT", provenance="HISTORICAL_TEAM_POV",
                  errors=[], matches=[], source_observations=0, source_frames=0)
    try:
        records, report["excluded"], protected = discover(MANIFESTS)
        if args.report.resolve() in protected:
            parser.error("--report cannot overwrite a source artifact")
        prepared, visibility, current, last_seen = [], Counter(), Counter(), Counter()
        for record in records:
            try:
                data = prepare(record)
                prepared.append(data)
                report["source_observations"] += len(data.rows)
                report["source_frames"] += len(data.frames) // 2
                for row in data.rows:
                    r = dict(zip(COLUMNS, row, strict=True))
                    visibility[r["visibility_state"]] += 1
                    current[r["current_position_status"]] += 1
                    last_seen[r["last_seen_status"]] += 1
            except (ValueError, OSError, KeyError, TypeError, ArithmeticError, csv.Error) as exc:
                report["errors"].append(dict(match_id=record[0], phase="SOURCE", reason=str(exc)))
        report.update(visibility_counts=dict(visibility), current_position_status_counts=dict(current),
                      last_seen_status_counts=dict(last_seen))
        planned = []
        with psycopg.connect(**postgres_connection_options_from_env(), autocommit=True,
                             connect_timeout=5, options="-c default_transaction_read_only=on") as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                with connection.cursor() as cursor:
                    present = cursor.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()[0] is not None
                for data in prepared:
                    try:
                        check_parent(connection, data)
                        planned.append((data, check_existing(connection, data, present)))
                    except ValueError as exc:
                        report["errors"].append(dict(match_id=data.match_id, phase="DATABASE", reason=str(exc)))
        report["compatible_matches"] = len(planned)
        report["planned_missing"] = sum(missing for _, missing in planned)
        if args.apply and planned:
            with psycopg.connect(**postgres_connection_options_from_env(), autocommit=True,
                                 connect_timeout=5) as connection:
                with connection.transaction():
                    connection.execute(SCHEMA.read_text())
                attempted = 0
                for data, missing in planned:
                    if not missing:
                        report["matches"].append(dict(match_id=data.match_id, status="SKIPPED"))
                        continue
                    if args.limit is not None and attempted >= args.limit:
                        continue
                    attempted += 1
                    try:
                        inserted = apply_match(connection, data)
                        report["matches"].append(dict(match_id=data.match_id, status="IMPORTED",
                                                      inserted=inserted, replay_sha256=data.replay_sha256))
                    except (ValueError, psycopg.Error) as exc:
                        report["errors"].append(dict(match_id=data.match_id, phase="APPLY", reason=str(exc)))
                    heroes.save(args.report, report)
        else:
            report["matches"] = [dict(match_id=d.match_id, status="COMPATIBLE", missing=m)
                                  for d, m in planned]
        heroes.save(args.report, report)
        print(json.dumps({k: v for k, v in report.items() if k not in ("matches", "excluded")}, indent=2))
        print("Results: " + json.dumps(dict(Counter(e["status"] for e in report["matches"]))))
        return 1 if report["errors"] else 0
    except (ValueError, OSError, KeyError, TypeError, psycopg.Error) as exc:
        print(f"Enemy observation import stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
