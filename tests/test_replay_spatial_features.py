from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from app.replay_spatial_features import (
    build_spatial_feature_rows,
    extract_spatial_features,
)


def _player(slot, team, x, y, life=0):
    return {
        "player_slot": slot, "team": team, "hero_id": slot + 1,
        "hero_entity_class": f"CDOTA_Unit_Hero_Test{slot}",
        "selected_hero_handle": 100 + slot, "hero_entity_handle": 100 + slot,
        "life_state": life, "position_status": "OBSERVED",
        "x": x, "y": y,
        "cell_x": int(x // 128) if x is not None else None,
        "cell_y": int(y // 128) if y is not None else None,
        "offset_x": x % 128 if x is not None else None,
        "offset_y": y % 128 if y is not None else None,
    }


def _snapshot(time, distance=100):
    return {
        "game_time_seconds": time, "source_game_time_seconds": time,
        "target_game_time_seconds": int(time // 10) * 10,
        "replay_tick": 1000 + round(time * 30),
        "players": [
            _player(0, "RADIANT", 0, 0),
            _player(1, "RADIANT", distance, 0),
            _player(5, "DIRE", 0, 100),
            _player(6, "DIRE", 100, 100),
        ],
    }


def _sidecar(*snapshots):
    return {
        "schema_version": 1, "artifact_kind": "replay_hero_positions",
        "match_id": 123, "replay_sha256": "a" * 64,
        "snapshot_interval_seconds": 10, "clock_normalization_status": "PROVEN",
        "coordinate_system": {"name": "SOURCE2_NATIVE_CELL_GRID"},
        "snapshots": list(snapshots),
    }


def test_known_distances_and_team_orientation():
    snapshot = _snapshot(0)
    snapshot["players"] = [
        _player(0, "RADIANT", 0, 0), _player(1, "RADIANT", 3, 4),
        _player(2, "RADIANT", 6, 8), _player(5, "DIRE", 0, 8),
        _player(6, "DIRE", 0, 10),
    ]
    radiant, dire = build_spatial_feature_rows(_sidecar(snapshot))
    assert radiant["team"] == "RADIANT" and dire["team"] == "DIRE"
    assert radiant["team_spread"] == pytest.approx(20 / 3)
    assert dire["team_spread"] == 2
    assert radiant["nearest_enemy_distance"] == dire["nearest_enemy_distance"] == 5
    assert radiant["alive_hero_count"] == radiant["valid_position_count"] == 3


def test_dead_and_null_positions_are_excluded_without_fill():
    first = _snapshot(0)
    later = _snapshot(30)
    later["players"][1]["life_state"] = 2  # Stale coordinates still present.
    later["players"].append(_player(2, "RADIANT", None, None))
    later["players"].append(_player(3, "RADIANT", None, None, life=None))
    row = build_spatial_feature_rows(_sidecar(first, later))[2]
    assert row["alive_hero_count"] == 2
    assert row["valid_position_count"] == 1
    assert row["unknown_life_state_count"] == 2
    assert row["team_spread"] is None
    assert row["team_spread_change_30s"] is None
    assert row["spread_change_status"] == "INSUFFICIENT_CURRENT_POSITIONS"


def test_actual_time_controls_lookback_and_future_cannot_change_prefix():
    source = _sidecar(_snapshot(0, 100), _snapshot(10.02, 90),
                      _snapshot(29.99, 70), _snapshot(40.04, 50))
    prefix = build_spatial_feature_rows(source)
    row = prefix[-2]
    assert row["game_time_seconds"] == 40.04
    assert row["lookback_game_time_seconds"] == 10.02
    assert row["lookback_elapsed_seconds"] == pytest.approx(30.02)
    assert row["team_spread_change_30s"] == -40
    extended = deepcopy(source)
    extended["snapshots"].append(_snapshot(50, 9999))
    assert build_spatial_feature_rows(extended)[:len(prefix)] == prefix


def test_lookback_outside_tolerance_is_not_replaced_with_other_history():
    rows = build_spatial_feature_rows(
        _sidecar(_snapshot(0), _snapshot(20), _snapshot(40))
    )
    assert rows[-2]["team_spread"] == 100
    assert rows[-2]["team_spread_change_30s"] is None
    assert rows[-2]["lookback_elapsed_seconds"] is None
    assert rows[-2]["spread_change_status"] == "NO_30S_LOOKBACK"


def test_insufficient_team_coverage_and_missing_enemy():
    snapshot = _snapshot(0)
    snapshot["players"] = [snapshot["players"][0]]
    radiant, dire = build_spatial_feature_rows(_sidecar(snapshot))
    assert radiant["team_spread"] is None
    assert radiant["team_spread_status"] == "INSUFFICIENT_TEAM_POSITIONS"
    assert radiant["nearest_enemy_distance"] is None
    assert radiant["nearest_enemy_status"] == "MISSING_ENEMY_POSITIONS"
    assert dire["nearest_enemy_status"] == "MISSING_TEAM_POSITIONS"
    assert dire["alive_hero_count"] == dire["valid_position_count"] == 0
    assert dire["unknown_life_state_count"] == 5


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1.0, 0.0])
def test_unverified_or_invalid_coordinate_is_not_a_position(value):
    snapshot = _snapshot(0)
    # Setting x alone must not manufacture a coordinate, including a zero default.
    snapshot["players"][1]["x"] = value
    row = build_spatial_feature_rows(_sidecar(snapshot))[0]
    assert row["valid_position_count"] == 1
    assert row["team_spread"] is None


def test_verified_zero_coordinates_are_valid():
    row = build_spatial_feature_rows(_sidecar(_snapshot(0, 5)))[0]
    assert row["valid_position_count"] == 2
    assert row["team_spread"] == 5


@pytest.mark.parametrize("key,value", [("game_paused", True), ("synthetic", True),
                                       ("replay_tick", 999)])
def test_paused_synthetic_and_rewound_frames_are_rejected(key, value):
    first, later = _snapshot(0), _snapshot(10)
    later[key] = value
    with pytest.raises(ValueError, match="monotonic actual unpaused"):
        build_spatial_feature_rows(_sidecar(first, later))


def test_csv_preserves_explicit_missing_values_and_input(tmp_path: Path):
    source, output = tmp_path / "source.json", tmp_path / "features.csv"
    content = json.dumps(_sidecar(_snapshot(0)))
    source.write_text(content)
    assert extract_spatial_features(source, output) == 2
    assert source.read_text() == content
    with output.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["team_spread_change_30s"] == ""
    assert rows[0]["spread_change_status"] == "NO_30S_LOOKBACK"
    assert rows[0]["game_time_seconds"] == "0.0"
    with pytest.raises(ValueError, match="overwrite"):
        extract_spatial_features(source, source)
