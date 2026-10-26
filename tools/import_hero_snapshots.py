"""Store confirmed compact hero/inventory observations, never Team POV.

Default: read-only preflight, including manifest hashes and DB compatibility.
--apply: additive schema and one transaction per match. --limit bounds incomplete
matches attempted. Multi-row INSERTs preserve conflicts instead of overwriting.
Missing slot rows mean unexported entries, not proven empty inventory slots.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from app.replay_trajectory import PLAYER_FIELDS, load_replay_trajectory
from app.storage.postgres import postgres_connection_options_from_env
from tools import import_replay_batch as batch
from tools.ingest_target_replays import GOLD_LIMITATION, save, sha256


ROOT, OUTPUT = batch.ROOT, batch.OUTPUT
MANIFESTS = batch.MANIFESTS
SCHEMA = ROOT / "app/storage/postgres_heroes_schema.sql"
HERO_TABLE = "historical_match_hero_snapshots"
ITEM_TABLE = "historical_match_hero_items"
KEY = ("source", "source_match_id", "replay_tick", "player_slot")
STATS = tuple(PLAYER_FIELDS[4:-1])
HERO_COLUMNS = (*KEY, "actual_game_time_seconds", "replay_sha256", "provenance",
                "team", "team_source_id", "hero_id", "hero_name", "game_state",
                "clock_normalization_status", "clock_normalization_method", *STATS,
                "current_gold_status", "inventory_status")
ITEM_COLUMNS = (*KEY, "inventory_slot", "item_name")
TABLES = ((HERO_TABLE, HERO_COLUMNS, KEY),
          (ITEM_TABLE, ITEM_COLUMNS, (*KEY, "inventory_slot")))
PAGE_SIZE = 1000


@dataclass
class HeroData:
    match_id: int
    replay_sha256: str
    heroes: list[tuple]
    items: list[tuple]


def discover(manifests):
    """Existing=True denotes an already parsed replay, not an imported hero table."""
    found, replays, excluded, protected = {}, {}, [], set()
    for manifest in manifests:
        path = batch.source_path(manifest)
        protected.add(path)
        document = json.loads(path.read_text())
        rows = document["matches"]
        if isinstance(rows, dict):
            if any(str(e["match_id"]) != k for k, e in rows.items()):
                raise ValueError("Manifest key/match identity mismatch")
            rows = list(rows.values())
        for entry in rows:
            for item in entry.get("artifacts", []):
                protected.add((ROOT / item["path"]).resolve())
            if entry.get("dem_path"):
                protected.add((ROOT / entry["dem_path"]).resolve())
            status = entry.get("status", entry.get("parser_status"))
            if status not in ("PARSE_OK", "PASSED"):
                excluded.append(dict(match_id=entry["match_id"], status=status))
                continue
            artifacts = [a for a in entry["artifacts"] if a["path"].endswith(".compact.json")]
            if len(artifacts) != 1:
                raise ValueError("Manifest must identify exactly one canonical compact")
            mid, digest = entry["match_id"], entry["dem_sha256"]
            if type(mid) is not int or mid <= 0:
                raise ValueError("Invalid manifest match identity")
            record = (mid, digest, artifacts[0])
            if mid in found and found[mid] != record:
                raise ValueError(f"Conflicting compact/SHA references for {mid}")
            if replays.setdefault(digest, mid) != mid:
                raise ValueError("Different matches share a replay SHA")
            found[mid] = record
    return [found[k] for k in sorted(found)], excluded, protected


def prepare(record):
    mid, digest, artifact = record
    path = batch.source_path(artifact["path"])
    if sha256(path) != artifact["sha256"]:
        raise ValueError(f"Compact artifact SHA mismatch: {mid}")
    # Reuse established schema/aggregate/clock validation. Extract from a second,
    # Decimal JSON view to avoid the loader's float conversion of actual time.
    validated = load_replay_trajectory(path)
    if validated["match_id"] != mid or validated["replay_sha256"] != digest:
        raise ValueError(f"Compact match/replay SHA mismatch: {mid}")
    raw = json.loads(path.read_text(), parse_float=Decimal)
    heroes, items, hero_keys, item_keys = [], [], set(), set()
    previous_time, previous_tick, roster = Decimal(-1), -1, None
    for frame in raw["snapshots"]:
        at, tick = Decimal(frame["source_game_time_seconds"]), frame["replay_tick"]
        if not at.is_finite() or at < 0 or at <= previous_time or tick <= previous_tick:
            raise ValueError("Non-increasing actual time/tick")
        previous_time, previous_tick = at, tick
        current = {p["player_slot"]: (p["hero_id"], p["team"]) for p in frame["players"]}
        if (len(current) != 10 or len({v[0] for v in current.values()}) != 10
                or Counter(v[1] for v in current.values()) != {"RADIANT": 5, "DIRE": 5}
                or (roster is not None and current != roster)):
            raise ValueError("Invalid or changing ten-hero roster/teams")
        roster = current
        for player in sorted(frame["players"], key=lambda p: p["player_slot"]):
            key = ("replay_clarity", str(mid), tick, player["player_slot"])
            if key in hero_keys:
                raise ValueError("Duplicate hero snapshot")
            hero_keys.add(key)
            values = [player[k] for k in STATS]
            if any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in values):
                raise ValueError("Unsupported/missing hero statistic; no zero substitution")
            team_id = raw[f"{player['team'].lower()}_team_id"]
            heroes.append((*key, at, digest, "HISTORICAL_ORACLE", player["team"],
                           str(team_id) if team_id is not None else None,
                           player["hero_id"], player["hero_name"], frame["game_state"],
                           raw["clock_normalization_status"], raw["clock_normalization_method"],
                           *values, "COMPONENT_COMPLETENESS_UNVERIFIED", "EXPORTED_ENTRIES_ONLY"))
            for item in sorted(player["items"], key=lambda i: i["inventory_slot"]):
                slot = item["inventory_slot"]
                if not 0 <= slot < 24 or (*key, slot) in item_keys:
                    raise ValueError("Invalid/duplicate inventory slot")
                item_keys.add((*key, slot))
                items.append((*key, slot, item["item_name"]))
    return HeroData(mid, digest, heroes, items)


def check_parent(connection, data):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM historical_matches WHERE source=%s AND source_match_id=%s",
                       ("replay_clarity", str(data.match_id)))
        if cursor.fetchone() is None:
            raise ValueError(f"Match is absent from PostgreSQL: {data.match_id}")
        cursor.execute(
            "SELECT actual_game_time_seconds,replay_tick,replay_sha256 "
            "FROM historical_match_economy_snapshots WHERE source=%s AND source_match_id=%s",
            ("replay_clarity", str(data.match_id)),
        )
        known = {(tick, sha, at) for at, tick, sha in cursor.fetchall()}
        expected = {(row[2], data.replay_sha256, float(row[4])) for row in data.heroes}
        # Existing economy is DOUBLE PRECISION; new hero time remains exact NUMERIC.
        if known != expected:
            raise ValueError(f"Stored economy/replay identity or clock conflict: {data.match_id}")


def available_tables(connection):
    with connection.cursor() as cursor:
        return {table: cursor.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is not None
                for table, _, _ in TABLES}


def check_existing(connection, data, present):
    missing = {}
    with connection.cursor() as cursor:
        for (table, columns, _), rows in zip(TABLES, (data.heroes, data.items), strict=True):
            actual = set()
            if present[table]:
                cursor.execute(sql.SQL("SELECT {} FROM {} WHERE source=%s AND source_match_id=%s").format(
                    sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table)),
                    ("replay_clarity", str(data.match_id)))
                actual = set(cursor.fetchall())
            expected = set(rows)
            if not actual <= expected:
                raise ValueError(f"Existing hero/inventory data conflict: {table}, {data.match_id}")
            missing[table] = len(expected - actual)
    return missing


def insert_pages(cursor, table, columns, key, rows):
    inserted = 0
    template = sql.SQL("({})").format(sql.SQL(", ").join(sql.Placeholder() for _ in columns))
    for offset in range(0, len(rows), PAGE_SIZE):
        page = rows[offset:offset + PAGE_SIZE]
        query = sql.SQL("INSERT INTO {} ({}) VALUES {} ON CONFLICT ({}) DO NOTHING").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(template for _ in page), sql.SQL(", ").join(map(sql.Identifier, key)))
        cursor.execute(query, tuple(v for row in page for v in row))
        inserted += cursor.rowcount
    return inserted


def apply_match(connection, data):
    with connection.transaction():
        check_parent(connection, data)
        check_existing(connection, data, {t: True for t, _, _ in TABLES})
        with connection.cursor() as cursor:
            inserted = {table: insert_pages(cursor, table, cols, key, rows)
                        for (table, cols, key), rows in zip(TABLES, (data.heroes, data.items), strict=True)}
        if any(check_existing(connection, data, {t: True for t, _, _ in TABLES}).values()):
            raise ValueError("Incomplete hero/inventory transaction")
    return inserted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report", type=Path, default=OUTPUT / "postgres-hero-import/report.json")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if not args.report.resolve().is_relative_to(OUTPUT.resolve()):
        parser.error("--report must be inside replay_probe/output")
    if args.report.resolve() in {Path(p).resolve() for p in MANIFESTS}:
        parser.error("--report cannot overwrite an input manifest")
    report = dict(mode="APPLY" if args.apply else "PREFLIGHT", errors=[], matches=[],
                  provenance="HISTORICAL_ORACLE", current_gold_limitation=GOLD_LIMITATION,
                  inventory_semantics="EXPORTED_ENTRIES_ONLY; CLARITY_CLASS_NOT_NUMERIC_ITEM_ID")
    protected = set()
    try:
        records, report["excluded"], protected = discover(MANIFESTS)
        if args.report.resolve() in protected:
            parser.error("--report cannot overwrite a source artifact")
        prepared = []
        for record in records:
            try:
                prepared.append(prepare(record))
            except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
                report["errors"].append(dict(match_id=record[0], phase="SOURCE", reason=str(exc)))
        planned = []
        with psycopg.connect(**postgres_connection_options_from_env(), autocommit=True,
                             connect_timeout=5, options="-c default_transaction_read_only=on") as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                present = available_tables(connection)
                for data in prepared:
                    try:
                        check_parent(connection, data)
                        planned.append((data, check_existing(connection, data, present)))
                    except ValueError as exc:
                        report["errors"].append(dict(match_id=data.match_id, phase="DATABASE", reason=str(exc)))
        report["compatible_matches"] = len(planned)
        report["source_counts"] = dict(hero_snapshots=sum(len(d.heroes) for d, _ in planned),
                                       items=sum(len(d.items) for d, _ in planned))
        report["planned_missing"] = {t: sum(m[t] for _, m in planned) for t, _, _ in TABLES}
        if args.apply and planned:
            with psycopg.connect(**postgres_connection_options_from_env(), autocommit=True,
                                 connect_timeout=5) as connection:
                with connection.transaction():
                    connection.execute(SCHEMA.read_text())
                attempted = 0
                for data, missing in planned:
                    if not any(missing.values()):
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
                    save(args.report, report)
        else:
            report["matches"] = [dict(match_id=d.match_id, status="COMPATIBLE", missing=m)
                                  for d, m in planned]
        save(args.report, report)
        print(json.dumps({k: v for k, v in report.items() if k not in ("matches", "excluded")}, indent=2))
        print("Results: " + json.dumps(dict(Counter(e["status"] for e in report["matches"]))))
        return 1 if report["errors"] else 0
    except (ValueError, OSError, KeyError, TypeError, psycopg.Error) as exc:
        print(f"Hero import stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
