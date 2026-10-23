"""Inspect one historical Dota 2 snapshot from existing local replay artifacts.

Run from the repository root with python -m tools.inspect_historical_snapshot.
This adapter loads observations only; selection belongs to HistoricalSnapshot.
"""

import argparse
from collections.abc import Sequence
import csv
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

from app.analytics.historical_snapshot import (
    ENEMY_OBSERVATION_FIELDS, POV_FIELDS, TEAMS, MatchIdentity,
    build_historical_snapshot,
)
from app.replay_pov_features import TEAM_COLUMNS


ROOT = Path(__file__).resolve().parents[1]
MATCH_ID = "8960991322"
SOURCE = "replay_clarity"
COMPACT_PATH = Path("tools/replay_probe/output/8960991322.compact.json")
SPATIAL_PATH = Path("tools/replay_probe/output/visibility-check/8960991322.compact.spatial.json")
POV_PATH = Path("tools/replay_probe/output/pov-lifecycle-check/team_pov_features.csv")
ENEMY_PATH = Path("tools/replay_probe/output/pov-lifecycle-check/enemy_observations.csv")
# Fixed compact/spatial/POV/enemy paths: never discover or substitute sources.
MATCH_SOURCES = {
    MATCH_ID: (COMPACT_PATH, SPATIAL_PATH, POV_PATH, ENEMY_PATH),
    "8960882635": (
        Path("tools/replay_probe/output/independent-8960882635/8960882635.compact.json"),
        Path("tools/replay_probe/output/independent-8960882635/8960882635.compact.spatial.json"),
        Path("tools/replay_probe/output/independent-8960882635/pov/team_pov_features.csv"),
        Path("tools/replay_probe/output/independent-8960882635/pov/enemy_observations.csv"),
    ),
    "8958734008": (
        Path("tools/replay_probe/output/different-teams-8958734008/8958734008.compact.json"),
        Path("tools/replay_probe/output/different-teams-8958734008/8958734008.compact.spatial.json"),
        Path("tools/replay_probe/output/different-teams-8958734008/pov/team_pov_features.csv"),
        Path("tools/replay_probe/output/different-teams-8958734008/pov/enemy_observations.csv"),
    ),
    "8957013760": (
        Path("tools/replay_probe/output/fourth-match-8957013760/8957013760.compact.json"),
        Path("tools/replay_probe/output/fourth-match-8957013760/8957013760.compact.spatial.json"),
        Path("tools/replay_probe/output/fourth-match-8957013760/pov/team_pov_features.csv"),
        Path("tools/replay_probe/output/fourth-match-8957013760/pov/enemy_observations.csv"),
    ),
}


def _json_source(path: Path, match_id: str) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream, parse_float=Decimal)
    if not isinstance(data, dict) or str(data.get("match_id")) != match_id:
        raise ValueError(f"{path.name}: match identity does not match {match_id}.")
    if data.get("clock_normalization_status") != "PROVEN":
        raise ValueError(f"{path.name}: actual game clock is not PROVEN.")
    if not isinstance(data.get("replay_sha256"), str) or not isinstance(data.get("snapshots"), list):
        raise ValueError(f"{path.name}: missing replay SHA or snapshot list.")
    for frame in data["snapshots"]:
        if not isinstance(frame, dict):
            raise ValueError(f"{path.name}: snapshot must be an object.")
        if (str(frame.get("match_id", match_id)) != match_id
                or frame.get("replay_sha256", data["replay_sha256"]) != data["replay_sha256"]):
            raise ValueError(f"{path.name}: conflicting frame identity/SHA.")
    return data


def _csv_source(path: Path, columns: tuple[str, ...], match_id: str, sha: str) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(columns):
            raise ValueError(f"{path.name}: expected the canonical observation-only CSV schema.")
        rows = list(reader)
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"{path.name}: malformed CSV row.")
        if row["match_id"] != match_id or row["replay_sha256"] != sha:
            raise ValueError(f"{path.name}: conflicting match identity/SHA.")
    return rows


def _economy_rows(frames: list[dict], match_id: str) -> list[dict]:
    rows = []
    for frame in frames:
        teams = frame["teams"]
        if (not isinstance(teams, list) or len(teams) != 2
                or any(not isinstance(team, dict) for team in teams)
                or {team.get("team") for team in teams} != set(TEAMS)):
            raise ValueError("Compact economy requires exactly one Radiant and one Dire record.")
        values = {f"{team['team'].lower()}_{field}": team[field]
                  for team in teams for field in ("net_worth", "current_gold", "total_xp")}
        rows.append(dict(
            source=SOURCE, source_match_id=match_id,
            actual_game_time_seconds=frame["source_game_time_seconds"],
            replay_tick=frame["replay_tick"], **values,
        ))
    return rows


def _pov_rows(rows: list[dict], match_id: str) -> list[dict]:
    result = []
    for row in rows:
        values = {}
        for field in POV_FIELDS:
            raw = row[field]
            values[field] = (None if raw == "" else raw if field.endswith("_status")
                             else int(raw) if field.endswith("_count") else Decimal(raw))
        result.append(dict(
            source=SOURCE, source_match_id=match_id,
            actual_game_time_seconds=Decimal(row["game_time_seconds"]),
            replay_tick=int(row["replay_tick"]), pov_team=row["pov_team"], **values,
        ))
    return result


def inspect_snapshot(match_id: str, at_seconds: Decimal, at_tick: int) -> dict:
    """Load a confirmed match's explicit sources and build five channels once."""
    if match_id not in MATCH_SOURCES:
        raise ValueError(f"Unsupported match ID {match_id}; supported: {', '.join(MATCH_SOURCES)}.")
    paths = MATCH_SOURCES[match_id]
    for path in paths:
        if not (ROOT / path).is_file():
            raise ValueError(f"Match {match_id}: required source file is missing: {ROOT / path}")
    compact_path, spatial_path, pov_path, enemy_path = paths
    compact = _json_source(ROOT / compact_path, match_id)
    spatial = _json_source(ROOT / spatial_path, match_id)
    sha = compact["replay_sha256"]
    if spatial["replay_sha256"] != sha or spatial.get("artifact_kind") != "replay_hero_positions":
        raise ValueError("Spatial source has a different replay SHA or artifact kind.")
    pov = _csv_source(ROOT / pov_path, TEAM_COLUMNS, match_id, sha)
    enemies = _csv_source(ROOT / enemy_path, ENEMY_OBSERVATION_FIELDS, match_id, sha)
    lineage = dict(source=SOURCE, source_match_id=match_id, match_id=match_id, replay_sha256=sha)
    snapshot = build_historical_snapshot(
        MatchIdentity(SOURCE, match_id), at_seconds,
        _economy_rows(compact["snapshots"], match_id), _pov_rows(pov, match_id),
        hero_records=[lineage | frame for frame in compact["snapshots"]],
        spatial_records=[lineage | frame for frame in spatial["snapshots"]],
        enemy_records=enemies, requested_replay_tick=at_tick, replay_sha256=sha,
    )
    return asdict(snapshot) | {"requested_replay_tick": at_tick, "replay_sha256": sha}


def _time_argument(value: str) -> Decimal:
    try:
        time = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("Expected finite game seconds.") from error
    if not time.is_finite():
        raise argparse.ArgumentTypeError("Expected finite game seconds.")
    return time


def _json_decimal(value: object) -> str:
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", required=True, choices=tuple(MATCH_SOURCES))
    parser.add_argument("--at-seconds", required=True, type=_time_argument)
    parser.add_argument("--at-tick", required=True, type=int)
    args = parser.parse_args(argv)
    if args.at_tick < 0:
        parser.error("--at-tick must be a nonnegative replay tick.")
    try:
        payload = inspect_snapshot(args.match_id, args.at_seconds, args.at_tick)
        output = json.dumps(payload, default=_json_decimal, ensure_ascii=False, allow_nan=False, indent=2)
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as error:
        print(f"Historical snapshot error: match {args.match_id}: {error}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
