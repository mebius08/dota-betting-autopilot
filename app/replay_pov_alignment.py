"""Align replay decisions to their team's last causal POV, at most 15s old.

Decision CSV times are nominal grid keys. The compact source supplies their
actual game times and replay ticks. Both bound the backward-only POV lookup;
no rounding or clock tolerance can admit a future frame. Outcomes and enemy
last-seen rows are not inputs. Only the existing team POV columns are copied.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections.abc import Mapping, Sequence
import csv
import math
from pathlib import Path

from app.replay_pov_features import TEAM_COLUMNS
from app.replay_trajectory import load_replay_trajectory


MAX_POV_AGE_SECONDS = 15.0
TEAMS = ("RADIANT", "DIRE")
FEATURE_COLUMNS = TEAM_COLUMNS[5:]
COLUMNS = (
    "decision_id", "match_id", "replay_sha256", "team", "team_id",
    "decision_grid_time_seconds", "decision_game_time_seconds",
    "decision_replay_tick", "pov_game_time_seconds", "pov_replay_tick",
    "pov_age_seconds", "latest_past_pov_game_time_seconds",
    "latest_past_pov_age_seconds", "alignment_status", *FEATURE_COLUMNS,
)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Expected a nonnegative integer: {value!r}")
    result = int(value)
    if result < 0:
        raise ValueError("Expected a nonnegative integer")
    return result


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Expected a finite nonnegative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("Expected a finite nonnegative number")
    return result


def _lineage(row: Mapping) -> tuple[int, str]:
    match_id = _integer(row["match_id"])
    sha = row["replay_sha256"]
    if (match_id == 0 or not isinstance(sha, str) or len(sha) != 64
            or any(c not in "0123456789abcdefABCDEF" for c in sha)):
        raise ValueError("Invalid match/replay identity")
    return match_id, sha.lower()


def _features(row: Mapping) -> dict:
    result = {}
    for name in FEATURE_COLUMNS:
        value = row[name]
        if name.endswith("_status"):
            if not isinstance(value, str) or not value:
                raise ValueError(f"Missing POV status: {name}")
        elif value is None or value == "":
            value = None
        elif name.endswith("_count"):
            value = _integer(value)
            if value > 5:
                raise ValueError(f"Invalid hero count: {name}")
        else:
            value = _number(value)
        result[name] = value
    return result


def build_alignment_rows(
    decisions: Sequence[Mapping], compact: Mapping, pov_rows: Sequence[Mapping],
) -> list[dict]:
    if compact.get("clock_normalization_status") != "PROVEN":
        raise ValueError("Compact clock must be PROVEN")
    lineage = _lineage(compact)
    frames = {}
    previous_time, previous_tick = -1.0, -1
    for frame in compact["snapshots"]:
        grid = _integer(frame["game_time_seconds"])
        time = _number(frame["source_game_time_seconds"])
        tick = _integer(frame["replay_tick"])
        if grid in frames or time < previous_time or tick <= previous_tick:
            raise ValueError("Duplicate or nonmonotonic compact timestamps")
        frames[grid] = (time, tick)
        previous_time, previous_tick = time, tick

    by_team = {team: [] for team in TEAMS}
    for row in pov_rows:
        if _lineage(row) != lineage or row["pov_team"] not in TEAMS:
            raise ValueError("POV match/replay/team mismatch")
        by_team[row["pov_team"]].append((
            _number(row["game_time_seconds"]), _integer(row["replay_tick"]),
            _features(row),
        ))
    indexes = {}
    for team, rows in by_team.items():
        rows.sort(key=lambda row: row[0])
        times, ticks = [r[0] for r in rows], [r[1] for r in rows]
        if (any(b <= a for a, b in zip(times, times[1:]))
                or any(b <= a for a, b in zip(ticks, ticks[1:]))):
            raise ValueError("Duplicate or nonmonotonic POV timestamps/ticks")
        indexes[team] = (times, ticks)

    result, seen = [], set()
    for decision in decisions:
        team = decision["team"]
        if _lineage(decision) != lineage or team not in TEAMS:
            raise ValueError("Decision match/replay/team mismatch")
        grid = _integer(decision["game_time_seconds"])
        key = (lineage[0], grid, team)
        if key in seen:
            raise ValueError(f"Duplicate decision: {key}")
        seen.add(key)
        if grid not in frames:
            raise ValueError("Decision has no exact compact source snapshot")
        expected_id = compact[team.lower() + "_team_id"]
        team_id = decision["team_id"]
        team_id = None if team_id is None or team_id == "" else _integer(team_id)
        if team_id != expected_id:
            raise ValueError("Decision team_id does not match compact team")
        time, tick = frames[grid]
        times, ticks = indexes[team]
        index = min(bisect_right(times, time), bisect_right(ticks, tick)) - 1
        past = by_team[team][index] if index >= 0 else None
        age = time - past[0] if past else None
        matched = past is not None and age <= MAX_POV_AGE_SECONDS
        result.append({
            "decision_id": f"{lineage[0]}:{grid}:{team}",
            "match_id": lineage[0], "replay_sha256": lineage[1],
            "team": team, "team_id": team_id,
            "decision_grid_time_seconds": grid,
            "decision_game_time_seconds": time, "decision_replay_tick": tick,
            "pov_game_time_seconds": past[0] if matched else None,
            "pov_replay_tick": past[1] if matched else None,
            "pov_age_seconds": age if matched else None,
            # Stale candidates remain diagnostic only; their features are blank.
            "latest_past_pov_game_time_seconds": past[0] if past else None,
            "latest_past_pov_age_seconds": age,
            "alignment_status": (
                "MATCHED" if matched else "STALE_POV" if past else "NO_PAST_POV"
            ),
            **(past[2] if matched else dict.fromkeys(FEATURE_COLUMNS)),
        })
    return result


def align_pov_files(
    decisions_path: Path, compact_path: Path, pov_path: Path, output_path: Path,
) -> list[dict]:
    if output_path.resolve() in {
        p.resolve() for p in (decisions_path, compact_path, pov_path)
    }:
        raise ValueError("Alignment output must not overwrite an input")
    with decisions_path.open(encoding="utf-8", newline="") as stream:
        decisions = list(csv.DictReader(stream))
    with pov_path.open(encoding="utf-8", newline="") as stream:
        pov = list(csv.DictReader(stream))
    rows = build_alignment_rows(
        decisions, load_replay_trajectory(compact_path), pov,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--compact", type=Path, required=True)
    parser.add_argument("--pov", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        rows = align_pov_files(args.decisions, args.compact, args.pov, args.output)
    except (KeyError, ValueError, OSError) as exc:
        parser.error(str(exc))
    matched = sum(row["alignment_status"] == "MATCHED" for row in rows)
    print(f"WROTE decisions={len(rows)} matched={matched} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
