"""Align historical Team POV features with retrospective oracle economy targets.

Only FEATURES_AT_T is a predictor allowlist. METADATA and FUTURE_TARGETS must
stay outside predictors. Paired teams and overlapping horizons are correlated;
future evaluation must split at least by match_id, never randomly by row.
Historical Team POV does not establish availability in LIVE.
"""

import argparse
from collections import defaultdict
from collections.abc import Sequence
import csv
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import sys
import tempfile

from app.replay_pov_alignment import TEAMS, _integer, _lineage
from app.replay_pov_corpus import COLUMNS as POV_COLUMNS, _decimal
from tools.build_economy_transitions import COLUMNS as ECONOMY_COLUMNS, HORIZONS, _interval


ROOT = Path(__file__).resolve().parents[1]
ECONOMY_PATH = Path("tools/replay_probe/output/economy-transitions-four-matches.csv")
POV_PATH = Path("tools/replay_probe/output/pov-death-corpus-four-matches/pov-death-corpus.csv")
SCOPE = "HISTORICAL_TEAM_POV_FEATURES_WITH_ORACLE_TARGET"
METADATA = (
    "match_id", "replay_sha256", "pov_team", "economy_game_time_seconds", "economy_replay_tick",
    "pov_game_time_seconds", "pov_replay_tick", "pov_age_seconds", "horizon_steps",
    "future_source_game_time_seconds", "future_replay_tick", "actual_horizon_seconds", "scope",
)
# Deliberately explicit: additions to upstream schemas must not expand predictors.
FEATURES_AT_T = (
    "own_alive_hero_count", "own_valid_position_count", "own_unknown_life_state_count",
    "own_team_spread", "visible_enemy_count", "visible_enemy_with_position_count",
    "hidden_enemy_count", "unknown_enemy_count", "nearest_visible_enemy_distance",
    "own_team_spread_status", "nearest_visible_enemy_status",
)
FUTURE_TARGETS = ("future_delta_own_net_worth_lead", "future_delta_own_xp_lead")
COLUMNS = (*METADATA, *FEATURES_AT_T, *FUTURE_TARGETS)


def _read_csv(path: Path, columns: tuple) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(columns):
            raise ValueError(f"Incompatible CSV schema: {path}")
        rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError(f"Malformed CSV row: {path}")
    return rows


def _identity(match_id: str, sha: str, lineages: dict, replay_matches: dict) -> tuple[str, str]:
    mid, sha = _lineage(dict(match_id=match_id, replay_sha256=sha))
    mid = str(mid)
    if lineages.setdefault(mid, sha) != sha or replay_matches.setdefault(sha, mid) != mid:
        raise ValueError(f"Match/replay SHA identity conflict for match {mid}.")
    return mid, sha


def _features(row: dict) -> dict:
    result = {}
    for name in FEATURES_AT_T:
        value = row[name]
        if name.endswith("_status"):
            if not value.strip():
                raise ValueError(f"Missing POV availability status: {name}")
        elif value != "":
            if name.endswith("_count"):
                if _integer(value) > 5:
                    raise ValueError(f"POV count outside 0..5: {name}")
            else:
                _decimal(value)
        # Preserve exact source cells, including empty NULLs and status strings.
        result[name] = value
    return result


def _pov_frames(rows: list[dict], lineages: dict, replay_matches: dict) -> dict:
    frames = defaultdict(dict)
    for row in rows:
        mid, _ = _identity(row["match_id"], row["replay_sha256"], lineages, replay_matches)
        time, tick, team = _decimal(row["game_time_seconds"]), _integer(row["replay_tick"]), row["pov_team"]
        if team not in TEAMS:
            raise ValueError(f"Invalid POV team: {team}")
        frame = frames[mid].setdefault(tick, dict(time=time, tick=tick, teams={}))
        if frame["time"] != time or team in frame["teams"]:
            raise ValueError(f"Duplicate or inconsistent POV frame: {mid}/{tick}/{team}")
        frame["teams"][team] = _features(row)
    result = {}
    for mid, by_tick in frames.items():
        ordered = [by_tick[tick] for tick in sorted(by_tick)]
        if any(set(frame["teams"]) != set(TEAMS) for frame in ordered):
            raise ValueError(f"Incomplete Radiant/Dire POV pair for match {mid}.")
        if any(a["time"] >= b["time"] for a, b in zip(ordered, ordered[1:])):
            raise ValueError(f"POV actual time must increase with replay tick: {mid}")
        result[mid] = ordered
    return result


def _anchors(rows: list[dict], lineages: dict, replay_matches: dict) -> list[dict]:
    anchors, seen, clocks = [], set(), {}
    for row in rows:
        mid, sha = _identity(row["source_match_id"], row["replay_sha256"], lineages, replay_matches)
        if row["source"] != "replay_clarity" or row["provenance"] != "HISTORICAL_ORACLE":
            raise ValueError(f"Unexpected economy source/provenance: {mid}")
        time, tick = _decimal(row["source_game_time_seconds"]), _integer(row["replay_tick"])
        future_time, future_tick = _decimal(row["future_source_game_time_seconds"]), _integer(row["future_replay_tick"])
        horizon, interval = _integer(row["horizon_steps"]), _decimal(row["actual_horizon_seconds"])
        if horizon not in HORIZONS or future_time <= time or future_tick <= tick or interval != _interval(future_time, time):
            raise ValueError(f"Invalid economy transition time/tick/horizon: {mid}/{tick}")
        key = (mid, tick, horizon)
        if key in seen or clocks.setdefault((mid, tick), time) != time:
            raise ValueError(f"Duplicate or inconsistent economy anchor: {mid}/{tick}/{horizon}")
        seen.add(key)
        anchors.append(dict(
            match_id=mid, replay_sha256=sha, economy_game_time_seconds=time, economy_replay_tick=tick,
            horizon_steps=horizon, future_source_game_time_seconds=future_time,
            future_replay_tick=future_tick, actual_horizon_seconds=interval,
            future_delta_own_net_worth_lead=int(row["future_delta_net_worth_lead"]),
            future_delta_own_xp_lead=int(row["future_delta_xp_lead"]),
        ))
    return sorted(anchors, key=lambda row: (int(row["match_id"]), row["economy_replay_tick"], row["horizon_steps"]))


def _coverage(rows: list[dict], anchor_count: int) -> dict:
    ages = sorted(row["pov_age_seconds"] for row in rows)
    median = None
    if ages:
        low, high = ages[(len(ages) - 1) // 2], ages[len(ages) // 2]
        with localcontext() as context:
            context.prec = max(low.adjusted(), high.adjusted()) - min(
                low.as_tuple().exponent, high.as_tuple().exponent,
            ) + 3
            median = (low + high) / 2
    skipped = anchor_count * 2 - len(rows)
    return dict(
        anchors=anchor_count, rows=len(rows), skipped_anchor_rows=skipped // 2, skipped_team_rows=skipped,
        skip_reasons={"NO_PRECEDING_POV": skipped} if skipped else {},
        min_pov_age_seconds=min(ages, default=None), median_pov_age_seconds=median,
        max_pov_age_seconds=max(ages, default=None),
    )


def build_transitions(economy_path: Path, pov_path: Path) -> tuple[list[dict], dict]:
    """Read explicit CSVs; select paired as-of frames without using death labels."""
    lineages, replay_matches = {}, {}
    anchors = _anchors(_read_csv(economy_path, ECONOMY_COLUMNS), lineages, replay_matches)
    frames = _pov_frames(_read_csv(pov_path, POV_COLUMNS), lineages, replay_matches)
    rows = []
    for anchor in anchors:
        frame = next((frame for frame in reversed(frames.get(anchor["match_id"], []))
                      if frame["time"] <= anchor["economy_game_time_seconds"]
                      and frame["tick"] <= anchor["economy_replay_tick"]), None)
        if frame is None:
            continue  # Both team rows are counted explicitly as skipped in coverage.
        for team, sign in (("RADIANT", 1), ("DIRE", -1)):
            rows.append(dict(
                **{name: anchor[name] for name in METADATA if name in anchor},
                pov_team=team, pov_game_time_seconds=frame["time"], pov_replay_tick=frame["tick"],
                pov_age_seconds=_interval(anchor["economy_game_time_seconds"], frame["time"]), scope=SCOPE,
                **frame["teams"][team],
                **{name: sign * anchor[name] for name in FUTURE_TARGETS},
            ))
    matches = sorted({anchor["match_id"] for anchor in anchors}, key=int)
    report = dict(
        scope=SCOPE, match_count=len(matches), **_coverage(rows, len(anchors)),
        column_groups=dict(METADATA=METADATA, FEATURES_AT_T=FEATURES_AT_T, FUTURE_TARGETS=FUTURE_TARGETS),
        evaluation_note="Split at least by match_id. Paired teams and overlapping horizons are not independent; no random row split.",
        matches=[dict(match_id=mid, horizons={str(h): _coverage(
            [row for row in rows if row["match_id"] == mid and row["horizon_steps"] == h],
            sum(a["match_id"] == mid and a["horizon_steps"] == h for a in anchors),
        ) for h in HORIZONS}) for mid in matches],
    )
    return rows, report


def write_csv(path: Path, rows: list[dict]) -> None:
    """Publish a complete CSV atomically; remove temporary output on any failure."""
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
    raise TypeError(f"Unsupported report value: {type(value).__name__}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Explicit CSV destination; omission is a read-only dry-run.")
    args = parser.parse_args(argv)
    economy_path, pov_path = ROOT / ECONOMY_PATH, ROOT / POV_PATH
    try:
        if args.out is not None and args.out.resolve() in {economy_path.resolve(), pov_path.resolve()}:
            raise ValueError("Output must not overwrite either input CSV.")
        rows, report = build_transitions(economy_path, pov_path)
        if args.out is not None:
            write_csv(args.out, rows)
        report.update(mode="EXPORTED" if args.out is not None else "DRY_RUN", out=str(args.out) if args.out else None)
        print(json.dumps(report, default=_json_decimal, ensure_ascii=False, allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError) as error:
        print(f"POV economy transitions error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
