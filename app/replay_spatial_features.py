"""Small offline spatial-feature prototype; independent of trajectory datasets.

Distances use Source 2 world units. The 30-second change is an observed spread
difference, not a label or an interpolated position. Its actual elapsed time is
exported: the nearest past frame to t-30 must be within five seconds of that
target. Missing CSV values are empty cells with explicit reason columns.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections.abc import Mapping, Sequence
import csv
from itertools import combinations
import json
import math
from pathlib import Path


TEAMS = ("RADIANT", "DIRE")
LOOKBACK_SECONDS = 30.0
LOOKBACK_TOLERANCE_SECONDS = 5.0
COLUMNS = (
    "match_id", "replay_sha256", "game_time_seconds", "replay_tick", "team",
    "alive_hero_count", "valid_position_count", "unknown_life_state_count",
    "team_spread", "nearest_enemy_distance", "lookback_game_time_seconds",
    "lookback_elapsed_seconds", "team_spread_change_30s", "team_spread_status",
    "nearest_enemy_status", "spread_change_status",
)
FeatureRow = dict[str, int | float | str | None]
Point = tuple[float, float]


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _position(player: Mapping[str, object]) -> Point | None:
    if type(player.get("life_state")) is not int or player["life_state"] != 0:
        return None
    if player.get("position_status") != "OBSERVED":
        return None
    handle = player.get("selected_hero_handle")
    hero_id = player.get("hero_id")
    if (
        type(handle) is not int or handle < 0
        or handle != player.get("hero_entity_handle")
        or type(hero_id) is not int or hero_id <= 0
        or not str(player.get("hero_entity_class", "")).startswith(
            "CDOTA_Unit_Hero_"
        )
    ):
        return None
    result = []
    for axis in ("x", "y"):
        world = player.get(axis)
        cell = player.get(f"cell_{axis}")
        offset = player.get(f"offset_{axis}")
        if not all(_finite(v) for v in (world, cell, offset)):
            return None
        world, cell, offset = float(world), float(cell), float(offset)
        # Check the sidecar's provenance even for zero-valued coordinates.
        if (
            not 0 <= cell <= 65535 or cell != math.floor(cell)
            or not 0 <= offset < 256
            or not math.isclose(world, cell * 128 + offset, abs_tol=1e-6,
                                rel_tol=0)
        ):
            return None
        result.append(world)
    return result[0], result[1]


def _spread(points: list[Point]) -> float | None:
    distances = [math.dist(a, b) for a, b in combinations(points, 2)]
    return math.fsum(distances) / len(distances) if distances else None


def _lookback_index(past_times: list[float], now: float) -> int | None:
    target = now - LOOKBACK_SECONDS
    index = bisect_left(past_times, target)
    candidates = [i for i in (index - 1, index) if 0 <= i < len(past_times)]
    if not candidates:
        return None
    chosen = min(candidates, key=lambda i: (abs(past_times[i] - target),
                                          past_times[i]))
    if abs(past_times[chosen] - target) > LOOKBACK_TOLERANCE_SECONDS:
        return None
    return chosen


def build_spatial_feature_rows(sidecar: Mapping[str, object]) -> list[FeatureRow]:
    """Use only current positions and previously processed frame aggregates."""
    coordinate_system = sidecar.get("coordinate_system")
    match_id, replay_sha256 = sidecar.get("match_id"), sidecar.get("replay_sha256")
    if (
        sidecar.get("artifact_kind") != "replay_hero_positions"
        or sidecar.get("schema_version") != 1
        or sidecar.get("snapshot_interval_seconds") != 10
        or sidecar.get("clock_normalization_status") != "PROVEN"
        or not isinstance(coordinate_system, Mapping)
        or coordinate_system.get("name") != "SOURCE2_NATIVE_CELL_GRID"
        or type(match_id) is not int or match_id <= 0
        or not isinstance(replay_sha256, str) or len(replay_sha256) != 64
        or any(c not in "0123456789abcdefABCDEF" for c in replay_sha256)
    ):
        raise ValueError("Expected a PROVEN 10-second Source 2 spatial sidecar")
    snapshots = sidecar.get("snapshots")
    if not isinstance(snapshots, list):
        raise ValueError("snapshots must be a list")

    rows: list[FeatureRow] = []
    past_times: list[float] = []
    past_spreads: list[dict[str, float | None]] = []
    previous_tick = -1
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            raise ValueError("Each snapshot must be an object")
        time, tick = snapshot.get("game_time_seconds"), snapshot.get("replay_tick")
        if (
            not _finite(time) or time < 0
            or snapshot.get("source_game_time_seconds") != time
            or (past_times and time <= past_times[-1])
            or type(tick) is not int or tick <= previous_tick
            or snapshot.get("game_paused") is True
            or snapshot.get("synthetic") is True
        ):
            raise ValueError("Expected monotonic actual unpaused frame times/ticks")
        time = float(time)
        previous_tick = tick
        players = snapshot.get("players")
        if not isinstance(players, list):
            raise ValueError("players must be a list")
        grouped = {team: [] for team in TEAMS}
        positions: dict[str, list[Point]] = {team: [] for team in TEAMS}
        seen_slots, seen_handles = set(), set()
        for player in players:
            if not isinstance(player, Mapping):
                raise ValueError("Each player must be an object")
            team, slot = player.get("team"), player.get("player_slot")
            if team not in TEAMS or type(slot) is not int or not 0 <= slot < 64:
                raise ValueError("Invalid player team or slot")
            if slot in seen_slots or len(grouped[team]) >= 5:
                raise ValueError("Duplicate player slot or more than five team rows")
            seen_slots.add(slot)
            grouped[team].append(player)
            position = _position(player)
            if position is not None:
                handle = player["hero_entity_handle"]
                if handle in seen_handles:
                    raise ValueError("One hero entity was assigned to multiple players")
                seen_handles.add(handle)
                positions[team].append(position)

        spreads = {team: _spread(positions[team]) for team in TEAMS}
        nearest = min(
            (math.dist(a, b) for a in positions["RADIANT"]
             for b in positions["DIRE"]), default=None,
        )
        reference = _lookback_index(past_times, time)
        for team in TEAMS:
            opponent = "DIRE" if team == "RADIANT" else "RADIANT"
            spread = spreads[team]
            previous_spread = (
                past_spreads[reference][team] if reference is not None else None
            )
            if reference is None:
                change_status = "NO_30S_LOOKBACK"
            elif spread is None:
                change_status = "INSUFFICIENT_CURRENT_POSITIONS"
            elif previous_spread is None:
                change_status = "INSUFFICIENT_LOOKBACK_POSITIONS"
            else:
                change_status = "OK"
            rows.append({
                "match_id": match_id,
                "replay_sha256": replay_sha256,
                "game_time_seconds": time,
                "replay_tick": tick,
                "team": team,
                "alive_hero_count": sum(type(p.get("life_state")) is int
                                        and p["life_state"] == 0
                                        for p in grouped[team]),
                "valid_position_count": len(positions[team]),
                "unknown_life_state_count": 5 - len(grouped[team]) + sum(
                    type(p.get("life_state")) is not int
                    or p["life_state"] not in (0, 1, 2) for p in grouped[team]
                ),
                "team_spread": spread,
                "nearest_enemy_distance": nearest,
                "lookback_game_time_seconds": (
                    past_times[reference] if reference is not None else None
                ),
                "lookback_elapsed_seconds": (
                    time - past_times[reference] if reference is not None else None
                ),
                "team_spread_change_30s": (
                    spread - previous_spread if change_status == "OK" else None
                ),
                "team_spread_status": (
                    "OK" if spread is not None else "INSUFFICIENT_TEAM_POSITIONS"
                ),
                "nearest_enemy_status": (
                    "MISSING_TEAM_POSITIONS" if not positions[team]
                    else "MISSING_ENEMY_POSITIONS" if not positions[opponent]
                    else "OK"
                ),
                "spread_change_status": change_status,
            })
        # Append only after both teams' rows are complete: no future frame is visible.
        past_times.append(time)
        past_spreads.append(spreads)
    return rows


def extract_spatial_features(input_path: Path, output_path: Path) -> int:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("The output must not overwrite the input sidecar")
    sidecar = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(sidecar, Mapping):
        raise ValueError("The sidecar root must be an object")
    rows = build_spatial_feature_rows(sidecar)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        count = extract_spatial_features(args.input, args.output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"WROTE rows={count} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
