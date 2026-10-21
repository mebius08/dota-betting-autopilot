"""Validate or import historical replay economy; LIVE availability is unverified."""

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from app.replay_trajectory import load_replay_trajectory
from app.storage.postgres import postgres_connection_options_from_env
from tools.import_compact_matches import COMPACTS, ROOT, SOURCE


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "app/storage/postgres_economy_schema.sql"
)
METRICS = ("net_worth", "current_gold", "total_xp")
COLUMNS = (
    "source",
    "source_match_id",
    "actual_game_time_seconds",
    "replay_tick",
    "replay_sha256",
    *(f"{side}_{metric}" for side in ("radiant", "dire") for metric in METRICS),
)
TABLE = sql.Identifier("historical_match_economy_snapshots")


def build_snapshots(root: Path) -> list[dict]:
    rows = []
    keys, times = set(), set()
    for expected_id, relative_path in COMPACTS.items():
        trajectory = load_replay_trajectory(root / relative_path)
        if trajectory["match_id"] != expected_id:
            raise ValueError(f"Canonical compact match ID mismatch: {expected_id}.")
        previous_time = -1.0
        for snapshot in trajectory["snapshots"]:
            actual_time = snapshot["source_game_time_seconds"]
            if actual_time < 0 or actual_time <= previous_time:
                raise ValueError(
                    f"Actual callback times must strictly increase: {expected_id}."
                )
            previous_time = actual_time
            row = dict(
                source=SOURCE,
                source_match_id=str(expected_id),
                actual_game_time_seconds=actual_time,
                replay_tick=snapshot["replay_tick"],
                replay_sha256=trajectory["replay_sha256"],
            )
            teams = {team["team"]: team for team in snapshot["teams"]}
            for side in ("radiant", "dire"):
                for metric in METRICS:
                    row[f"{side}_{metric}"] = teams[side.upper()][metric]
            for name in ("replay_tick", *COLUMNS[5:]):
                if row[name] > 2**63 - 1:
                    raise ValueError(f"{name} exceeds PostgreSQL BIGINT.")
            key = (row["source"], row["source_match_id"], row["replay_tick"])
            time_key = (row["source"], row["source_match_id"], actual_time)
            if key in keys or time_key in times:
                raise ValueError("Duplicate economic snapshot tick or actual time.")
            keys.add(key)
            times.add(time_key)
            rows.append(row)
    return rows


def apply_snapshots(rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    column_sql = sql.SQL(", ").join(sql.Identifier(name) for name in COLUMNS)
    insert = sql.SQL(
        "INSERT INTO {} ({}) VALUES ({}) "
        "ON CONFLICT (source, source_match_id, replay_tick) DO NOTHING"
    ).format(TABLE, column_sql, sql.SQL(", ").join(sql.Placeholder() for _ in COLUMNS))
    identities = sorted({row["source_match_id"] for row in rows})
    select = sql.SQL(
        "SELECT {} FROM {} WHERE source = %s AND source_match_id IN ({})"
    ).format(
        column_sql, TABLE, sql.SQL(", ").join(sql.Placeholder() for _ in identities)
    )
    parameters = [tuple(row[name] for name in COLUMNS) for row in rows]
    with psycopg.connect(
        **postgres_connection_options_from_env(), connect_timeout=5
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(schema)
            cursor.executemany(insert, parameters)
            inserted = cursor.rowcount
            cursor.execute(select, (SOURCE, *identities))
            if set(cursor.fetchall()) != set(parameters):
                raise ValueError(
                    "Existing economy snapshots conflict with the canonical compact data."
                )
    return inserted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the economy table and import in one transaction.",
    )
    args = parser.parse_args(argv)
    try:
        rows = build_snapshots(ROOT)
        if args.apply:
            inserted = apply_snapshots(rows)
            print(
                f"APPLY: source={len(rows)}, inserted={inserted}, skipped={len(rows) - inserted}."
            )
        else:
            print(
                f"DRY RUN: {len(rows)} economic snapshots ready; PostgreSQL was not contacted."
            )
        for mid in COMPACTS:
            snapshots = [row for row in rows if row["source_match_id"] == str(mid)]
            print(
                f"{mid}: snapshots={len(snapshots)}, "
                f"actual_time={snapshots[0]['actual_game_time_seconds']!r}..{snapshots[-1]['actual_game_time_seconds']!r}, "
                f"replay_ticks={snapshots[0]['replay_tick']}..{snapshots[-1]['replay_tick']}; duplicates=0."
            )
        print(
            "Time source: source_game_time_seconds, without nominal-minute rounding or tick conversion."
        )
        print("Fields: " + ", ".join(COLUMNS))
        print(
            "Scope: HISTORICAL_ORACLE; callbacks may be synthetic; LIVE coverage is unverified."
        )
        return 0
    except (OSError, ValueError, psycopg.Error) as exc:
        print(f"Economy import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
