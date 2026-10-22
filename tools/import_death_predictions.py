"""Validate/import HISTORICAL_OUT_OF_FOLD predictions; target is retrospective only."""

import argparse
from collections import Counter
from collections.abc import Sequence
import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from app.replay_pov_alignment import TEAMS, _integer
from app.storage.postgres import postgres_connection_options_from_env
from tools.import_compact_matches import ROOT, SOURCE


INPUT_PATH = ROOT / "tools/replay_probe/output/pov-deaths-oof-four-matches.csv"
SCHEMA_PATH = ROOT / "app/storage/postgres_death_predictions_schema.sql"
ORIGIN = "HISTORICAL_OUT_OF_FOLD"
INPUT_COLUMNS = (
    "match_id", "replay_tick", "game_time_seconds", "pov_team", "horizon_seconds",
    "constant_probability", "logistic_probability", "target",
)
COLUMNS = ("source", "source_match_id", *INPUT_COLUMNS[1:], "prediction_origin")
TABLE = sql.Identifier("historical_match_death_predictions")


def nonnegative_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Expected a finite nonnegative decimal.") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("Expected a finite nonnegative decimal.")
    return number


def build_predictions(path: Path) -> list[dict]:
    frames = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(INPUT_COLUMNS):
            raise ValueError("Expected the existing eight-column OOF CSV schema.")
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError("Malformed OOF row.")
            mid, tick = _integer(raw["match_id"]), _integer(raw["replay_tick"])
            team, horizon = raw["pov_team"], _integer(raw["horizon_seconds"])
            if mid == 0 or tick > 2**63 - 1 or team not in TEAMS or horizon != 30:
                raise ValueError("Invalid match, replay tick, POV team or horizon.")
            if raw["target"] not in ("0", "1"):
                raise ValueError("Target must be exactly 0 or 1.")
            time = nonnegative_decimal(raw["game_time_seconds"])
            probabilities = {}
            for name in ("constant_probability", "logistic_probability"):
                value = nonnegative_decimal(raw[name])
                if value > 1:
                    raise ValueError(f"{name} must be within [0, 1].")
                # The exporter writes float64 probabilities; this round-trips
                # those values into PostgreSQL DOUBLE PRECISION.
                probabilities[name] = float(value)
            frame = frames.setdefault((mid, tick), {"time": time, "teams": {}})
            if frame["time"] != time or team in frame["teams"]:
                raise ValueError("Duplicate match/tick/team/horizon or disagreeing actual time.")
            frame["teams"][team] = dict(
                source=SOURCE, source_match_id=str(mid), replay_tick=tick,
                game_time_seconds=time, pov_team=team, horizon_seconds=horizon,
                **probabilities, target=int(raw["target"]), prediction_origin=ORIGIN,
            )
    if not frames:
        raise ValueError("OOF source must not be empty.")
    rows, previous = [], {}
    for (mid, tick), frame in sorted(frames.items(), key=lambda item: (item[0][0], item[1]["time"])):
        if set(frame["teams"]) != set(TEAMS):
            raise ValueError("Each frame must contain both Radiant and Dire predictions.")
        old_time, old_tick = previous.get(mid, (Decimal(-1), -1))
        if frame["time"] <= old_time or tick <= old_tick:
            raise ValueError("Actual times and replay ticks must strictly increase.")
        previous[mid] = frame["time"], tick
        rows.extend(frame["teams"][team] for team in TEAMS)
    return rows


def apply_rows(rows: Sequence[dict]) -> int:
    if not rows:
        return 0
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in COLUMNS)
    insert = sql.SQL(
        "INSERT INTO {} ({}) VALUES ({}) "
        "ON CONFLICT (source, source_match_id, replay_tick, pov_team, horizon_seconds) DO NOTHING"
    ).format(TABLE, columns, sql.SQL(", ").join(sql.Placeholder() for _ in COLUMNS))
    identities = sorted({row["source_match_id"] for row in rows})
    select = sql.SQL(
        "SELECT {} FROM {} WHERE source = %s AND source_match_id IN ({})"
    ).format(columns, TABLE, sql.SQL(", ").join(sql.Placeholder() for _ in identities))
    parameters = [tuple(row[name] for name in COLUMNS) for row in rows]
    with psycopg.connect(
        **postgres_connection_options_from_env(), connect_timeout=5
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            cursor.executemany(insert, parameters)
            inserted = cursor.rowcount
            cursor.execute(select, (SOURCE, *identities))
            if set(cursor.fetchall()) != set(parameters):
                raise ValueError("Existing death predictions conflict with the source CSV.")
    return inserted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-path", type=Path, default=INPUT_PATH)
    parser.add_argument("--apply", action="store_true", help="Create/import in one transaction.")
    args = parser.parse_args(argv)
    try:
        rows = build_predictions(args.csv_path)
        if args.apply:
            inserted = apply_rows(rows)
            print(f"APPLY: source={len(rows)}, inserted={inserted}, skipped={len(rows) - inserted}.")
        else:
            print(f"DRY RUN: {len(rows)} historical OOF predictions ready; PostgreSQL was not contacted.")
        for mid, count in sorted(Counter(row["source_match_id"] for row in rows).items()):
            selected = [row for row in rows if row["source_match_id"] == mid]
            print(f"{mid}: rows={count}, frames={count // 2}, "
                  f"actual_time={selected[0]['game_time_seconds']}..{selected[-1]['game_time_seconds']}; "
                  "duplicate=0; paired=YES; horizon=30; values=VALID.")
        print(f"Origin: {ORIGIN}. target is a future outcome for retrospective evaluation, never an input feature.")
        print("Actual time is copied as NUMERIC/Decimal; no POV joins, rounding or retraining.")
        print("Dry-run does not check PostgreSQL catalog parents; --apply enforces the foreign key.")
        return 0
    except (OSError, ValueError, csv.Error, psycopg.Error) as exc:
        print(f"Death prediction import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
