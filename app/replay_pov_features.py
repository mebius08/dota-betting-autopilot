"""Offline team POV features from exactly synchronized replay sidecars.

Only own positions and VISIBLE enemy positions enter current geometry. Last
seen means the last sampled VISIBLE position, not the exact disappearance time.
Historical coordinates have separate columns and never fill current positions.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import csv
import json
import math
from pathlib import Path

from app.replay_spatial_features import (
    FeatureRow, Point, TEAMS, _finite, _position, _spread,
)


STATES = ("VISIBLE", "HIDDEN", "UNKNOWN")
TEAM_FILENAME = "team_pov_features.csv"
ENEMY_FILENAME = "enemy_observations.csv"
TEAM_COLUMNS = (
    "match_id", "replay_sha256", "game_time_seconds", "replay_tick", "pov_team",
    "own_alive_hero_count", "own_valid_position_count",
    "own_unknown_life_state_count", "own_team_spread", "visible_enemy_count",
    "visible_enemy_with_position_count", "hidden_enemy_count",
    "unknown_enemy_count", "nearest_visible_enemy_distance",
    "own_team_spread_status", "nearest_visible_enemy_status",
)
ENEMY_COLUMNS = (
    "match_id", "replay_sha256", "game_time_seconds", "replay_tick", "pov_team",
    "enemy_player_slot", "enemy_hero_id", "enemy_team", "visibility_state",
    "current_position_status", "current_x", "current_y", "last_seen_status",
    "last_seen_game_time_seconds", "last_seen_age_seconds",
    "last_seen_x", "last_seen_y",
)
IDENTITY_FIELDS = (
    "hero_id", "team", "selected_hero_handle", "hero_entity_handle",
    "hero_entity_class",
)


def _header(sidecar: Mapping, kind: str) -> None:
    if (
        sidecar.get("artifact_kind") != kind
        or sidecar.get("schema_version") != 1
        or sidecar.get("snapshot_interval_seconds") != 10
        or sidecar.get("clock_normalization_status") != "PROVEN"
        or type(sidecar.get("match_id")) is not int or sidecar["match_id"] <= 0
    ):
        raise ValueError(f"Expected a PROVEN 10-second {kind} sidecar")
    sha = sidecar.get("replay_sha256")
    if (not isinstance(sha, str) or len(sha) != 64
            or any(c not in "0123456789abcdefABCDEF" for c in sha)):
        raise ValueError("Invalid replay_sha256")


def _players(frame: Mapping) -> dict[int, Mapping]:
    players = frame.get("players")
    if not isinstance(players, list) or len(players) != 10:
        raise ValueError("Each joined frame must have ten primary hero rows")
    indexed = {}
    for player in players:
        if not isinstance(player, Mapping):
            raise ValueError("Player row must be an object")
        slot, hero, team = (player.get(k) for k in ("player_slot", "hero_id", "team"))
        if (type(slot) is not int or not 0 <= slot < 64 or slot in indexed
                or type(hero) is not int or hero <= 0 or team not in TEAMS):
            raise ValueError("Invalid or duplicate player identity")
        indexed[slot] = player
    if Counter(p["team"] for p in players) != {"RADIANT": 5, "DIRE": 5}:
        raise ValueError("Each frame must contain five heroes per team")
    handles = [p.get("hero_entity_handle") for p in players
               if p.get("hero_entity_handle") is not None]
    if any(type(h) is not int for h in handles) or len(handles) != len(set(handles)):
        raise ValueError("Invalid or duplicated primary hero handle")
    return indexed


def _joined_frames(spatial: Mapping, visibility: Mapping):
    _header(spatial, "replay_hero_positions")
    _header(visibility, "replay_hero_visibility")
    for key in (
        "match_id", "replay_sha256", "clock_normalization_method",
        "zero_replay_tick", "pause_ticks", "pause_seconds", "pause_witnessed",
        "game_end_time_seconds",
    ):
        if spatial.get(key) != visibility.get(key):
            raise ValueError(f"Sidecar header mismatch: {key}")
    coordinate_system = spatial.get("coordinate_system")
    if (not isinstance(coordinate_system, Mapping)
            or coordinate_system.get("name") != "SOURCE2_NATIVE_CELL_GRID"):
        raise ValueError("Expected Source 2 native cell-grid positions")
    sf, vf = spatial.get("snapshots"), visibility.get("snapshots")
    if not isinstance(sf, list) or not isinstance(vf, list) or len(sf) != len(vf):
        raise ValueError("Sidecar frame counts do not match")
    previous_time, previous_tick, roster = -1.0, -1, None
    for position_frame, visibility_frame in zip(sf, vf, strict=True):
        for frame in (position_frame, visibility_frame):
            if not isinstance(frame, Mapping):
                raise ValueError("Frame must be an object")
            time, tick = frame.get("game_time_seconds"), frame.get("replay_tick")
            if (not _finite(time) or time < 0 or type(tick) is not int or tick < 0
                    or frame.get("source_game_time_seconds") != time
                    or frame.get("game_paused") is True
                    or frame.get("synthetic") is True):
                raise ValueError("Invalid actual unpaused frame timestamp/tick")
        for key in ("replay_tick", "game_time_seconds", "source_game_time_seconds",
                    "target_game_time_seconds", "grid_offset_seconds"):
            if position_frame.get(key) != visibility_frame.get(key):
                raise ValueError(f"Exact frame join mismatch: {key}")
        time, tick = (position_frame[k] for k in ("game_time_seconds", "replay_tick"))
        if time <= previous_time or tick <= previous_tick:
            raise ValueError("Frame game time and replay tick must increase")
        positions, flags = _players(position_frame), _players(visibility_frame)
        if positions.keys() != flags.keys():
            raise ValueError("Player slots do not match between sidecars")
        for slot in positions:
            for key in IDENTITY_FIELDS:
                if (key not in positions[slot] or key not in flags[slot]
                        or positions[slot][key] != flags[slot][key]):
                    raise ValueError(f"Hero identity mismatch: slot={slot}, {key}")
            for key in ("visible_to_radiant", "visible_to_dire"):
                if flags[slot].get(key) not in STATES:
                    raise ValueError(f"Invalid visibility state: {key}")
        current_roster = {slot: (p["team"], p["hero_id"])
                          for slot, p in positions.items()}
        if roster is not None and current_roster != roster:
            raise ValueError("Hero/team roster changed between frames")
        roster = current_roster
        previous_time, previous_tick = float(time), tick
        yield float(time), tick, positions, flags


def build_pov_feature_rows(
    spatial: Mapping, visibility: Mapping,
) -> tuple[list[FeatureRow], list[FeatureRow]]:
    team_rows: list[FeatureRow] = []
    enemy_rows: list[FeatureRow] = []
    last_seen: dict[tuple[str, int], tuple[float, Point]] = {}
    for time, tick, positions, flags in _joined_frames(spatial, visibility):
        for pov in TEAMS:
            lineage = {
                "match_id": spatial["match_id"],
                "replay_sha256": spatial["replay_sha256"],
                "game_time_seconds": time, "replay_tick": tick, "pov_team": pov,
            }
            own = [p for p in positions.values() if p["team"] == pov]
            own_points = [point for p in own if (point := _position(p)) is not None]
            visible_points: list[Point] = []
            counts = Counter()
            flag_key = "visible_to_radiant" if pov == "RADIANT" else "visible_to_dire"
            for slot in sorted(positions):
                enemy = positions[slot]
                if enemy["team"] == pov:
                    continue
                state = flags[slot][flag_key]
                counts[state] += 1
                # Do not even inspect hidden/unknown enemy coordinates or life state.
                point = _position(enemy) if state == "VISIBLE" else None
                if point is not None:
                    visible_points.append(point)
                    last_seen[pov, slot] = (time, point)
                known = last_seen.get((pov, slot))
                seen_at, historical_point = known if known else (None, None)
                enemy_rows.append({
                    **lineage, "enemy_player_slot": slot,
                    "enemy_hero_id": enemy["hero_id"], "enemy_team": enemy["team"],
                    "visibility_state": state,
                    "current_position_status": (
                        "OBSERVED" if point is not None else
                        "VISIBLE_POSITION_UNAVAILABLE" if state == "VISIBLE"
                        else state
                    ),
                    "current_x": point[0] if point is not None else None,
                    "current_y": point[1] if point is not None else None,
                    "last_seen_status": (
                        "OBSERVED_NOW" if point is not None else
                        "LAST_SEEN" if known else "NEVER_SEEN"
                    ),
                    "last_seen_game_time_seconds": seen_at,
                    "last_seen_age_seconds": time - seen_at if known else None,
                    "last_seen_x": historical_point[0] if known else None,
                    "last_seen_y": historical_point[1] if known else None,
                })
            spread = _spread(own_points)
            nearest = min((math.dist(a, b) for a in own_points
                           for b in visible_points), default=None)
            team_rows.append({
                **lineage,
                "own_alive_hero_count": sum(type(p.get("life_state")) is int
                                            and p["life_state"] == 0 for p in own),
                "own_valid_position_count": len(own_points),
                "own_unknown_life_state_count": sum(
                    type(p.get("life_state")) is not int
                    or p["life_state"] not in (0, 1, 2) for p in own
                ),
                "own_team_spread": spread,
                "visible_enemy_count": counts["VISIBLE"],
                "visible_enemy_with_position_count": len(visible_points),
                "hidden_enemy_count": counts["HIDDEN"],
                "unknown_enemy_count": counts["UNKNOWN"],
                "nearest_visible_enemy_distance": nearest,
                "own_team_spread_status": (
                    "OK" if spread is not None else "INSUFFICIENT_OWN_POSITIONS"
                ),
                "nearest_visible_enemy_status": (
                    "NO_OWN_POSITION" if not own_points else
                    "NO_VISIBLE_ENEMY_POSITION" if not visible_points else "OK"
                ),
            })
    return team_rows, enemy_rows


def extract_pov_features(
    spatial_path: Path, visibility_path: Path, output_dir: Path,
) -> tuple[int, int]:
    spatial = json.loads(spatial_path.read_text(encoding="utf-8"))
    visibility = json.loads(visibility_path.read_text(encoding="utf-8"))
    if not isinstance(spatial, Mapping) or not isinstance(visibility, Mapping):
        raise ValueError("Sidecar roots must be objects")
    teams, enemies = build_pov_feature_rows(spatial, visibility)
    outputs = ((TEAM_FILENAME, TEAM_COLUMNS, teams),
               (ENEMY_FILENAME, ENEMY_COLUMNS, enemies))
    sources = {spatial_path.resolve(), visibility_path.resolve()}
    if any((output_dir / name).resolve() in sources for name, _, _ in outputs):
        raise ValueError("POV outputs must not overwrite input sidecars")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, columns, rows in outputs:
        with (output_dir / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    return len(teams), len(enemies)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spatial", type=Path, required=True)
    parser.add_argument("--visibility", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        teams, enemies = extract_pov_features(
            args.spatial, args.visibility, args.output_dir,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"WROTE team_rows={teams} enemy_rows={enemies} output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
