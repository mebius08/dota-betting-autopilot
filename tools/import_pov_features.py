"""Validate/import existing historical team POV features; LIVE access is unverified."""

import argparse
from collections import Counter
from collections.abc import Sequence
from contextlib import nullcontext
import csv
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from app.replay_pov_alignment import TEAMS, _features, _integer, _lineage, _number
from app.replay_pov_features import TEAM_COLUMNS
from app.storage.postgres import postgres_connection_options_from_env
from tools.import_compact_matches import ROOT, SOURCE


INPUT_PATH = ROOT / (
    "tools/replay_probe/output/global-pov-oracle-four-matches/pov-features.csv"
)
SCHEMA_PATH = ROOT / "app/storage/postgres_pov_schema.sql"
EXPECTED_ROWS = {8960991322: 774, 8960882635: 532, 8958734008: 524, 8957013760: 412}
FEATURES = (
    "visible_enemy_count", "visible_enemy_with_position_count", "hidden_enemy_count",
    "nearest_visible_enemy_distance", "own_team_spread",
    "own_team_spread_status", "nearest_visible_enemy_status",
)
COLUMNS = (
    "source", "source_match_id", "replay_sha256", "actual_game_time_seconds",
    "replay_tick", "pov_team", *FEATURES,
)
TABLE = sql.Identifier("historical_match_pov_snapshots")


def build_pov_rows(path: Path, *, expected_rows=None) -> list[dict]:
    expected_rows = EXPECTED_ROWS if expected_rows is None else expected_rows
    frames, lineages, replay_matches, counts = {}, {}, {}, Counter()
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(TEAM_COLUMNS):
            raise ValueError("Expected exact team POV schema; extra columns are forbidden.")
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError("Malformed POV CSV row.")
            mid, sha = _lineage(raw)
            if mid not in expected_rows:
                raise ValueError(f"Unexpected match ID: {mid}.")
            if lineages.setdefault(mid, sha) != sha or replay_matches.setdefault(sha, mid) != mid:
                raise ValueError("Match/replay SHA conflict.")
            time, tick, team = (
                _number(raw["game_time_seconds"]),
                _integer(raw["replay_tick"]),
                raw["pov_team"],
            )
            if tick > 2**63 - 1 or team not in TEAMS:
                raise ValueError("Invalid replay tick or POV team.")
            features = _features(raw)
            if any(features[name] is None for name in TEAM_COLUMNS if name.endswith("_count")):
                raise ValueError("POV hero counts must be present.")
            visible = features["visible_enemy_count"]
            positioned = features["visible_enemy_with_position_count"]
            own = features["own_valid_position_count"]
            if (
                visible + features["hidden_enemy_count"] + features["unknown_enemy_count"] != 5
                or positioned > visible
                or own > features["own_alive_hero_count"]
                or features["own_alive_hero_count"] + features["own_unknown_life_state_count"] > 5
            ):
                raise ValueError("Inconsistent POV counts or coverage.")
            spread_status = "OK" if own >= 2 else "INSUFFICIENT_OWN_POSITIONS"
            nearest_status = (
                "NO_OWN_POSITION" if own == 0 else
                "NO_VISIBLE_ENEMY_POSITION" if positioned == 0 else "OK"
            )
            for metric, status, expected in (
                ("own_team_spread", "own_team_spread_status", spread_status),
                ("nearest_visible_enemy_distance", "nearest_visible_enemy_status", nearest_status),
            ):
                if features[status] != expected or (features[metric] is not None) != (expected == "OK"):
                    raise ValueError(f"POV value/status mismatch: {metric}.")
            frame = frames.setdefault((mid, tick), {"time": time, "teams": {}})
            if frame["time"] != time or team in frame["teams"]:
                raise ValueError("Duplicate match/tick/team or inconsistent actual frame time.")
            frame["teams"][team] = dict(
                source=SOURCE, source_match_id=str(mid), replay_sha256=sha,
                actual_game_time_seconds=time, replay_tick=tick, pov_team=team,
                **{name: features[name] for name in FEATURES},
            )
            counts[mid] += 1
    if counts != Counter(expected_rows):
        raise ValueError(f"Incomplete POV source: {dict(counts)}.")
    rows, previous = [], {}
    for (mid, tick), frame in sorted(frames.items(), key=lambda item: (item[0][0], item[1]["time"])):
        time = frame["time"]
        old_time, old_tick = previous.get(mid, (-1, -1))
        if set(frame["teams"]) != set(TEAMS):
            raise ValueError("Every frame must contain exactly one Radiant and one Dire row.")
        if time <= old_time or tick <= old_tick:
            raise ValueError("Actual frame times and replay ticks must strictly increase.")
        previous[mid] = (time, tick)
        rows.extend(frame["teams"][team] for team in TEAMS)
    return rows


def apply_rows(rows: Sequence[dict], *, connection=None) -> int:
    """A supplied connection owns the transaction and requires existing tables."""
    if not rows:
        return 0
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in COLUMNS)
    insert = sql.SQL(
        "INSERT INTO {} ({}) VALUES ({}) "
        "ON CONFLICT (source, source_match_id, replay_tick, pov_team) DO NOTHING"
    ).format(TABLE, columns, sql.SQL(", ").join(sql.Placeholder() for _ in COLUMNS))
    identities = sorted({row["source_match_id"] for row in rows})
    select = sql.SQL(
        "SELECT {} FROM {} WHERE source = %s AND source_match_id IN ({})"
    ).format(columns, TABLE, sql.SQL(", ").join(sql.Placeholder() for _ in identities))
    parameters = [tuple(row[name] for name in COLUMNS) for row in rows]
    external = connection is not None
    context = nullcontext(connection) if external else psycopg.connect(
        **postgres_connection_options_from_env(), connect_timeout=5
    )
    with context as connection:
        with connection.cursor() as cursor:
            if not external:
                cursor.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            cursor.executemany(insert, parameters)
            inserted = cursor.rowcount
            cursor.execute(select, (SOURCE, *identities))
            if set(cursor.fetchall()) != set(parameters):
                raise ValueError("Existing POV snapshots conflict with the source CSV.")
    return inserted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-path", type=Path, default=INPUT_PATH)
    parser.add_argument("--apply", action="store_true", help="Create/import in one transaction.")
    args = parser.parse_args(argv)
    try:
        rows = build_pov_rows(args.csv_path)
        if args.apply:
            inserted = apply_rows(rows)
            print(f"APPLY: source={len(rows)}, inserted={inserted}, skipped={len(rows) - inserted}.")
        else:
            print(f"DRY RUN: {len(rows)} historical POV rows ready; PostgreSQL was not contacted.")
        for mid in sorted(EXPECTED_ROWS):
            selected = [row for row in rows if row["source_match_id"] == str(mid)]
            availability = {
                name: sum(row[name] is not None for row in selected)
                for name in ("own_team_spread", "nearest_visible_enemy_distance")
            }
            print(
                f"{mid}: rows={len(selected)}, frames={len(selected) // 2}, "
                f"actual_time={selected[0]['actual_game_time_seconds']!r}..{selected[-1]['actual_game_time_seconds']!r}; "
                f"available={availability}; duplicate=0; paired=YES."
            )
        print("Time source: actual game_time_seconds from POV CSV; no rounding or frame joins.")
        print("Scope: HISTORICAL_TEAM_POV; LIVE availability is unverified.")
        print("visible_enemy_count is raw VISIBLE flags; usable positions have a separate count.")
        print("No GLOBAL coordinates, future targets, last-seen extrapolation or imputation.")
        return 0
    except (OSError, ValueError, csv.Error, psycopg.Error) as exc:
        print(f"POV import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
