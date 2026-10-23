"""Inspect one historical Dota 2 snapshot from existing local replay artifacts.

Run from the repository root with python -m tools.inspect_historical_snapshot.
This adapter loads observations only; selection belongs to HistoricalSnapshot.
"""

import argparse
from collections.abc import Sequence
import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

from app.analytics.historical_snapshot import (
    ENEMY_OBSERVATION_FIELDS, POV_FIELDS, TEAMS, MatchIdentity,
    _elapsed_seconds, _seconds, build_historical_snapshot,
)
from app.analytics.snapshot_views import VIEWS, project_snapshot
from app.replay_pov_features import TEAM_COLUMNS


ROOT = Path(__file__).resolve().parents[1]
MATCH_ID = "8960991322"
SOURCE = "replay_clarity"
DEFAULT_VIEW = "HISTORICAL_GLOBAL"
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


def _observation_metadata(observation: dict | None) -> dict | None:
    return None if observation is None else {key: observation[key] for key in (
        "origin", "status", "actual_game_time_seconds", "replay_tick", "age_seconds",
    )}


def _change_group(current: dict, baseline: dict | None, baseline_time: Decimal,
                  baseline_reason: str | None) -> tuple[str, dict]:
    """Calculate only from already-projected observations; never raw sources."""
    team = VIEWS.get(current.get("view", DEFAULT_VIEW))
    channel = "pov" if team else "economy"
    fields = (("visible_enemy_count", "own_team_spread", "nearest_visible_enemy_distance") if team else (
        "radiant_net_worth", "dire_net_worth", "radiant_total_xp", "dire_total_xp",
    ))
    now = current["observations"].get(channel)
    before = baseline["observations"].get(channel) if baseline is not None else None
    status = baseline_reason
    elapsed = None
    if status is None:
        if now is None or now["status"] != "AVAILABLE":
            status = "NO_CURRENT_OBSERVATION"
        elif before is None or before["status"] != "AVAILABLE":
            status = "NO_BASELINE_OBSERVATION"
        else:
            elapsed = _elapsed_seconds(now["actual_game_time_seconds"], before["actual_game_time_seconds"])
            if elapsed < 0:
                raise ValueError("Baseline observation is later than the current observation.")
            if (now["actual_game_time_seconds"], now["replay_tick"]) == (
                before["actual_game_time_seconds"], before["replay_tick"],
            ):
                status = "NO_NEW_OBSERVATION"

    def values(observation):
        if observation is None or observation["status"] != "AVAILABLE":
            return {}
        return observation["values"][team] if team else observation["values"]

    current_values, baseline_values = values(now), values(before)
    deltas, field_statuses = {}, {}
    for field in fields:
        a, b = current_values.get(field), baseline_values.get(field)
        field_status = status
        if field_status is None and (a is None or b is None):
            field_status = ("MISSING_BOTH_VALUES" if a is None and b is None else
                            "MISSING_CURRENT_VALUE" if a is None else "MISSING_BASELINE_VALUE")
        deltas[field] = _elapsed_seconds(_seconds(a), _seconds(b)) if field_status is None else None
        field_statuses[field] = field_status or "AVAILABLE"
    if team is None:
        a, b = deltas["radiant_net_worth"], deltas["dire_net_worth"]
        # Derived gap change = delta(Radiant NW) - delta(Dire NW).
        field = "radiant_minus_dire_net_worth"
        deltas[field] = _elapsed_seconds(a, b) if a is not None and b is not None else None
        field_statuses[field] = "AVAILABLE" if deltas[field] is not None else status or "MISSING_NET_WORTH_COMPONENT"
    if status is None:
        available = sum(value == "AVAILABLE" for value in field_statuses.values())
        status = "AVAILABLE" if available == len(field_statuses) else "PARTIAL" if available else "MISSING_VALUES"
    group = {
        "requested_current_time_seconds": current["requested_game_time_seconds"],
        "requested_baseline_time_seconds": baseline_time,
        "current_observation": _observation_metadata(now),
        "baseline_observation": _observation_metadata(before),
        "elapsed_game_time_seconds": elapsed, "status": status,
        "deltas": deltas, "field_statuses": field_statuses,
    }
    if team:
        group["pov_team"] = team
        for label, features in (("current", current_values), ("baseline", baseline_values)):
            group[f"{label}_feature_statuses"] = {field: features.get(field) for field in (
                "own_team_spread_status", "nearest_visible_enemy_status",
            )}
    return channel, group


def _lookback_changes(current: dict, lookback: Decimal, match: MatchIdentity,
                      economy: list[dict], pov: list[dict], sources: dict) -> dict:
    baseline_time = _elapsed_seconds(current["requested_game_time_seconds"], lookback)
    baseline, boundary, reason = None, None, None
    if baseline_time < 0:
        reason = "BASELINE_BEFORE_GAME_START"
    else:
        # Probe ONLY the paired POV timeline through the existing selector.
        # The current tick is a ceiling, not an inferred tick at baseline_time.
        probe = build_historical_snapshot(
            match, baseline_time, [], pov, requested_replay_tick=current["requested_replay_tick"],
        ).observations["pov"]
        if probe.status != "AVAILABLE":
            reason = "NO_PRECEDING_BASELINE_FRAME"
        else:
            boundary = {"actual_game_time_seconds": probe.actual_game_time_seconds,
                        "replay_tick": probe.replay_tick, "age_seconds": probe.age_seconds}
            snapshot = build_historical_snapshot(
                match, baseline_time, economy, pov, requested_replay_tick=probe.replay_tick, **sources,
            )
            baseline = project_snapshot(snapshot, current.get("view", DEFAULT_VIEW),
                                        requested_replay_tick=probe.replay_tick)
    channel, group = _change_group(current, baseline, baseline_time, reason)
    return {
        "lookback_seconds": lookback,
        "requested_current_time_seconds": current["requested_game_time_seconds"],
        "requested_baseline_time_seconds": baseline_time,
        "requested_current_replay_tick": current["requested_replay_tick"],
        "baseline_tick_boundary": boundary, "status": group["status"], "groups": {channel: group},
    }


def inspect_snapshot(match_id: str, at_seconds: Decimal, at_tick: int, *,
                     view: str = DEFAULT_VIEW, lookback_seconds: Decimal | None = None) -> dict:
    """Project current observations, optionally comparing with a confirmed past state."""
    if lookback_seconds is not None:
        lookback_seconds = _seconds(lookback_seconds)
        if lookback_seconds <= 0:
            raise ValueError("Lookback seconds must be positive.")
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
    match = MatchIdentity(SOURCE, match_id)
    economy, pov = _economy_rows(compact["snapshots"], match_id), _pov_rows(pov, match_id)
    sources = dict(
        hero_records=[lineage | frame for frame in compact["snapshots"]],
        spatial_records=[lineage | frame for frame in spatial["snapshots"]],
        enemy_records=enemies, replay_sha256=sha,
    )
    snapshot = build_historical_snapshot(
        match, at_seconds, economy, pov, requested_replay_tick=at_tick, **sources,
    )
    payload = project_snapshot(snapshot, view, requested_replay_tick=at_tick)
    if lookback_seconds is not None:
        payload["changes"] = _lookback_changes(payload, lookback_seconds, match, economy, pov, sources)
    if view == DEFAULT_VIEW:
        payload.pop("view")  # Preserve the existing GLOBAL top-level JSON contract.
    return payload | {"replay_sha256": sha}


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


def _lookback_argument(value: str) -> Decimal:
    time = _time_argument(value)
    if time <= 0:
        raise argparse.ArgumentTypeError("Lookback seconds must be positive.")
    return time


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", required=True, choices=tuple(MATCH_SOURCES))
    parser.add_argument("--at-seconds", required=True, type=_time_argument)
    parser.add_argument("--at-tick", required=True, type=int)
    parser.add_argument("--view", choices=tuple(VIEWS), default=DEFAULT_VIEW)
    parser.add_argument("--lookback-seconds", type=_lookback_argument)
    args = parser.parse_args(argv)
    if args.at_tick < 0:
        parser.error("--at-tick must be a nonnegative replay tick.")
    try:
        payload = inspect_snapshot(args.match_id, args.at_seconds, args.at_tick,
                                   view=args.view, lookback_seconds=args.lookback_seconds)
        output = json.dumps(payload, default=_json_decimal, ensure_ascii=False, allow_nan=False, indent=2)
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as error:
        print(f"Historical snapshot error: match {args.match_id}: {error}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
