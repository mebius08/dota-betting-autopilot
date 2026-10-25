"""Build historical GLOBAL/oracle economy transitions from four saved compacts.

Only FEATURES_AT_T may enter a predictor. FUTURE_TARGETS are retrospective
labels; METADATA, especially actual_horizon_seconds, is not a feature set.
Horizons count observations, not fixed durations. No LIVE availability is implied.
"""

import argparse
from collections.abc import Mapping, Sequence
import csv
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import sys
import tempfile

from app.replay_trajectory import (
    _reject_non_finite_constant, _unique_json_object, validate_replay_trajectory,
)


ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    "8960991322": Path("tools/replay_probe/output/8960991322.compact.json"),
    "8960882635": Path("tools/replay_probe/output/independent-8960882635/8960882635.compact.json"),
    "8958734008": Path("tools/replay_probe/output/different-teams-8958734008/8958734008.compact.json"),
    "8957013760": Path("tools/replay_probe/output/fourth-match-8957013760/8957013760.compact.json"),
}
HORIZONS = (1, 5)
METADATA = (
    "source", "source_match_id", "replay_sha256", "provenance",
    "source_game_time_seconds", "replay_tick", "future_source_game_time_seconds",
    "future_replay_tick", "horizon_steps", "actual_horizon_seconds",
)
FEATURES_AT_T = (
    "radiant_net_worth", "dire_net_worth", "net_worth_lead_radiant_minus_dire",
    "radiant_total_xp", "dire_total_xp", "xp_lead_radiant_minus_dire",
    "past_delta_net_worth_lead", "previous_observation_interval_seconds",
)
FUTURE_TARGETS = (
    "future_delta_radiant_net_worth", "future_delta_dire_net_worth", "future_delta_net_worth_lead",
    "future_delta_radiant_total_xp", "future_delta_dire_total_xp", "future_delta_xp_lead",
)
COLUMNS = (*METADATA, *FEATURES_AT_T, *FUTURE_TARGETS)
ECONOMY_FIELDS = FEATURES_AT_T[:6]


def _interval(end: Decimal, start: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(end.adjusted(), start.adjusted()) - min(
            end.as_tuple().exponent, start.as_tuple().exponent,
        ) + 2
        return end - start


def _economy_snapshots(path: Path, match_id: str) -> tuple[str, list[dict]]:
    text = path.read_text(encoding="utf-8")
    # Reuse full schema/roster/aggregate validation. Its numeric API uses floats;
    # read exact clock tokens separately from the SAME text, never those floats.
    trajectory = validate_replay_trajectory(json.loads(
        text, object_pairs_hook=_unique_json_object, parse_constant=_reject_non_finite_constant,
    ))
    if str(trajectory["match_id"]) != match_id:
        raise ValueError(f"{path}: expected match {match_id}, found {trajectory['match_id']}.")
    exact = json.loads(text, parse_float=Decimal)
    result, previous_time, previous_tick = [], None, None
    for validated, raw in zip(trajectory["snapshots"], exact["snapshots"], strict=True):
        time = Decimal(raw["source_game_time_seconds"])
        tick = validated["replay_tick"]
        if (not time.is_finite() or time < 0
                or (previous_time is not None and time <= previous_time)
                or (previous_tick is not None and tick <= previous_tick)):
            raise ValueError(f"{path}: actual time and replay tick must strictly increase without duplicates.")
        teams = {team["team"]: team for team in validated["teams"]}
        radiant, dire = teams["RADIANT"], teams["DIRE"]
        result.append(dict(
            source_game_time_seconds=time, replay_tick=tick,
            radiant_net_worth=radiant["net_worth"], dire_net_worth=dire["net_worth"],
            net_worth_lead_radiant_minus_dire=radiant["net_worth"] - dire["net_worth"],
            radiant_total_xp=radiant["total_xp"], dire_total_xp=dire["total_xp"],
            xp_lead_radiant_minus_dire=radiant["total_xp"] - dire["total_xp"],
        ))
        previous_time, previous_tick = time, tick
    return trajectory["replay_sha256"], result


def _coverage(rows: list[dict], excluded: int) -> dict:
    intervals = [row["actual_horizon_seconds"] for row in rows]
    return dict(
        rows=len(rows), excluded_tail_frames=excluded,
        min_actual_horizon_seconds=min(intervals, default=None),
        max_actual_horizon_seconds=max(intervals, default=None),
        null_past_delta_net_worth_lead=sum(row["past_delta_net_worth_lead"] is None for row in rows),
        null_previous_observation_interval_seconds=sum(row["previous_observation_interval_seconds"] is None for row in rows),
    )


def build_transitions(paths: Mapping[str, Path]) -> tuple[list[dict], dict]:
    """Return deterministic rows and coverage; no writes and no cross-match pairs."""
    if not paths:
        raise ValueError("At least one explicit compact source is required.")
    rows, matches, sha_matches = [], [], {}
    for match_id in sorted(paths, key=int):
        sha, snapshots = _economy_snapshots(paths[match_id], match_id)
        if sha in sha_matches:
            raise ValueError(f"Replay SHA is reused across matches {sha_matches[sha]} and {match_id}.")
        sha_matches[sha] = match_id
        match_rows = []
        for index, current in enumerate(snapshots):
            previous = snapshots[index - 1] if index else None
            features = {field: current[field] for field in ECONOMY_FIELDS}
            features.update(
                past_delta_net_worth_lead=(current["net_worth_lead_radiant_minus_dire"]
                                          - previous["net_worth_lead_radiant_minus_dire"]) if previous else None,
                previous_observation_interval_seconds=_interval(
                    current["source_game_time_seconds"], previous["source_game_time_seconds"],
                ) if previous else None,
            )
            for horizon in HORIZONS:
                if index + horizon >= len(snapshots):
                    continue
                future = snapshots[index + horizon]
                targets = {target: future[field] - current[field]
                           for target, field in zip(FUTURE_TARGETS, ECONOMY_FIELDS, strict=True)}
                match_rows.append(dict(
                    source="replay_clarity", source_match_id=match_id, replay_sha256=sha,
                    provenance="HISTORICAL_ORACLE", source_game_time_seconds=current["source_game_time_seconds"],
                    replay_tick=current["replay_tick"], future_source_game_time_seconds=future["source_game_time_seconds"],
                    future_replay_tick=future["replay_tick"], horizon_steps=horizon,
                    actual_horizon_seconds=_interval(future["source_game_time_seconds"], current["source_game_time_seconds"]),
                    **features, **targets,
                ))
        rows.extend(match_rows)
        matches.append(dict(
            source_match_id=match_id, replay_sha256=sha, snapshots=len(snapshots),
            horizons={str(h): _coverage([row for row in match_rows if row["horizon_steps"] == h],
                                        min(h, len(snapshots))) for h in HORIZONS},
        ))
    report = dict(
        provenance="HISTORICAL_ORACLE", match_count=len(matches), snapshots=sum(m["snapshots"] for m in matches),
        rows=len(rows), column_groups={"METADATA": METADATA, "FEATURES_AT_T": FEATURES_AT_T, "FUTURE_TARGETS": FUTURE_TARGETS},
        horizons={str(h): _coverage([row for row in rows if row["horizon_steps"] == h],
                                    sum(m["horizons"][str(h)]["excluded_tail_frames"] for m in matches)) for h in HORIZONS},
        matches=matches,
    )
    return rows, report


def write_csv(path: Path, rows: list[dict]) -> None:
    """Replace only the explicit destination atomically; clean up failed writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _json_decimal(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported report value: {type(value).__name__}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Explicit CSV destination; omission performs a read-only dry-run.")
    args = parser.parse_args(argv)
    paths = {match_id: ROOT / path for match_id, path in INPUTS.items()}
    try:
        if args.out is not None and args.out.resolve() in {path.resolve() for path in paths.values()}:
            raise ValueError("Output must not overwrite an input compact.")
        rows, report = build_transitions(paths)
        if args.out is not None:
            write_csv(args.out, rows)
        report.update(mode="EXPORTED" if args.out is not None else "DRY_RUN", out=str(args.out) if args.out is not None else None)
        print(json.dumps(report, default=_json_decimal, ensure_ascii=False, allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError) as error:
        print(f"Economy transitions error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
