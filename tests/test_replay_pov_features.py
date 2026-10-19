from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

from app.replay_pov_features import (
    ENEMY_FILENAME, TEAM_FILENAME, build_pov_feature_rows, extract_pov_features,
)


def _coordinates(player, x, y):
    player.update(x=x, y=y, cell_x=int(x // 128), cell_y=int(y // 128),
                  offset_x=x % 128, offset_y=y % 128)


def _inputs(*times):
    spatial = {
        "schema_version": 1, "artifact_kind": "replay_hero_positions",
        "match_id": 123, "replay_sha256": "a" * 64,
        "snapshot_interval_seconds": 10, "clock_normalization_status": "PROVEN",
        "coordinate_system": {"name": "SOURCE2_NATIVE_CELL_GRID"}, "snapshots": [],
    }
    visibility = {k: deepcopy(v) for k, v in spatial.items()
                  if k != "coordinate_system"}
    visibility["artifact_kind"] = "replay_hero_visibility"
    for time in times:
        frame = {
            "game_time_seconds": time, "source_game_time_seconds": time,
            "replay_tick": 1000 + round(time * 30),
            "target_game_time_seconds": int(time // 10) * 10, "players": [],
        }
        vframe = {**frame, "players": []}
        for slot in range(10):
            team = "RADIANT" if slot < 5 else "DIRE"
            player = {
                "player_slot": slot, "team": team, "hero_id": slot + 1,
                "selected_hero_handle": 100 + slot,
                "hero_entity_handle": 100 + slot,
                "hero_entity_class": f"CDOTA_Unit_Hero_Test{slot}",
                "life_state": 0, "position_status": "OBSERVED",
            }
            _coordinates(player, 0 if slot < 5 else 100 * (slot - 4),
                         3 * slot if slot < 5 else 0)
            frame["players"].append(player)
            vframe["players"].append({
                **{k: v for k, v in player.items() if k in (
                    "player_slot", "team", "hero_id", "selected_hero_handle",
                    "hero_entity_handle", "hero_entity_class",
                )},
                "visible_to_radiant": "VISIBLE" if team == "RADIANT" else "HIDDEN",
                "visible_to_dire": "VISIBLE" if team == "DIRE" else "HIDDEN",
            })
        spatial["snapshots"].append(frame)
        visibility["snapshots"].append(vframe)
    return spatial, visibility


def _flag(visibility, frame, slot, state, viewer="radiant"):
    visibility["snapshots"][frame]["players"][slot]["visible_to_" + viewer] = state


def _pov(rows, team="RADIANT"):
    return [r for r in rows if r["pov_team"] == team]


def test_only_current_visible_enemy_positions_enter_geometry():
    spatial, visibility = _inputs(0)
    _flag(visibility, 0, 5, "VISIBLE")
    _coordinates(spatial["snapshots"][0]["players"][6], 1, 0)
    teams, enemies = build_pov_feature_rows(spatial, visibility)
    radiant, dire = teams
    assert radiant["visible_enemy_count"] == 1
    assert radiant["visible_enemy_with_position_count"] == 1
    assert radiant["hidden_enemy_count"] == 4
    assert radiant["nearest_visible_enemy_distance"] == 100
    assert radiant["own_team_spread"] == 6
    assert dire["nearest_visible_enemy_distance"] is None
    hidden = next(r for r in enemies if r["pov_team"] == "RADIANT"
                  and r["enemy_player_slot"] == 6)
    assert hidden["current_x"] is None and hidden["current_y"] is None
    assert hidden["last_seen_status"] == "NEVER_SEEN"


def test_last_seen_uses_actual_time_and_remains_historical_for_hidden_unknown():
    spatial, visibility = _inputs(100.025, 110.05, 140.075)
    for index, state in enumerate(("VISIBLE", "HIDDEN", "UNKNOWN")):
        _flag(visibility, index, 5, state)
        _coordinates(spatial["snapshots"][index]["players"][5],
                     100 + 900 * index, 0)
    teams, enemies = build_pov_feature_rows(spatial, visibility)
    records = [r for r in _pov(enemies) if r["enemy_player_slot"] == 5]
    assert records[0]["last_seen_status"] == "OBSERVED_NOW"
    assert records[1]["current_position_status"] == "HIDDEN"
    assert records[2]["current_position_status"] == "UNKNOWN"
    assert records[2]["last_seen_game_time_seconds"] == 100.025
    assert records[2]["last_seen_age_seconds"] == pytest.approx(40.05)
    assert records[2]["last_seen_x"] == 100
    assert records[2]["current_x"] is None
    assert _pov(teams)[1]["nearest_visible_enemy_distance"] is None
    assert _pov(teams)[2]["unknown_enemy_count"] == 1
    assert _pov(teams)[2]["hidden_enemy_count"] == 4
    assert all(r["last_seen_status"] == "NEVER_SEEN" for r in _pov(enemies, "DIRE"))


def test_hidden_global_coordinate_perturbation_cannot_change_observer_pov():
    spatial, visibility = _inputs(0, 10)
    _flag(visibility, 0, 5, "VISIBLE")
    before = build_pov_feature_rows(spatial, visibility)
    changed = deepcopy(spatial)
    _coordinates(changed["snapshots"][1]["players"][5], 9000, 8000)
    after = build_pov_feature_rows(changed, visibility)
    assert _pov(before[0]) == _pov(after[0])
    assert _pov(before[1]) == _pov(after[1])
    assert _pov(before[0], "DIRE") != _pov(after[0], "DIRE")


def test_other_teams_visibility_and_global_status_do_not_enter_observer_pov():
    spatial, visibility = _inputs(0)
    _flag(visibility, 0, 5, "VISIBLE")
    before = build_pov_feature_rows(spatial, visibility)
    changed = deepcopy(visibility)
    for row in changed["snapshots"][0]["players"]:
        row["visible_to_dire"] = "UNKNOWN"
        row["visibility_status"] = "PARTIAL_UNKNOWN"
    after = build_pov_feature_rows(spatial, changed)
    assert _pov(before[0]) == _pov(after[0])
    assert _pov(before[1]) == _pov(after[1])


def test_dead_or_missing_positions_do_not_refresh_last_seen():
    spatial, visibility = _inputs(0, 10, 20)
    for frame in range(3):
        _flag(visibility, frame, 5, "VISIBLE")
    spatial["snapshots"][1]["players"][5]["life_state"] = 2
    _coordinates(spatial["snapshots"][2]["players"][5], 200, 0)
    teams, enemies = build_pov_feature_rows(spatial, visibility)
    records = [r for r in _pov(enemies) if r["enemy_player_slot"] == 5]
    assert _pov(teams)[1]["visible_enemy_count"] == 1
    assert _pov(teams)[1]["visible_enemy_with_position_count"] == 0
    assert records[1]["current_position_status"] == "VISIBLE_POSITION_UNAVAILABLE"
    assert records[1]["last_seen_age_seconds"] == 10
    assert records[1]["last_seen_x"] == 100 and records[1]["current_x"] is None
    assert records[2]["last_seen_x"] == 200
    assert records[2]["last_seen_age_seconds"] == 0


def test_never_seen_visible_without_coordinate_and_insufficient_own_coverage():
    spatial, visibility = _inputs(0)
    _flag(visibility, 0, 5, "VISIBLE")
    spatial["snapshots"][0]["players"][5]["x"] = None
    for player in spatial["snapshots"][0]["players"][1:5]:
        player["life_state"] = 2
    teams, enemies = build_pov_feature_rows(spatial, visibility)
    assert teams[0]["own_valid_position_count"] == 1
    assert teams[0]["own_team_spread"] is None
    assert teams[0]["nearest_visible_enemy_distance"] is None
    assert enemies[0]["last_seen_status"] == "NEVER_SEEN"
    assert enemies[0]["last_seen_x"] is None


def test_appending_future_visibility_and_positions_preserves_both_prefixes():
    spatial, visibility = _inputs(0, 10)
    prefix = build_pov_feature_rows(spatial, visibility)
    extended_spatial, extended_visibility = _inputs(0, 10, 20, 40)
    for frame in (2, 3):
        _flag(extended_visibility, frame, 5, "VISIBLE")
        _coordinates(extended_spatial["snapshots"][frame]["players"][5],
                     1000 + frame, 1000)
    full = build_pov_feature_rows(extended_spatial, extended_visibility)
    assert full[0][:len(prefix[0])] == prefix[0]
    assert full[1][:len(prefix[1])] == prefix[1]


@pytest.mark.parametrize("mismatch", [
    "match_id", "replay_sha256", "frame_count", "time", "tick", "hero_id",
    "team", "selected_hero_handle", "duplicate_slot",
])
def test_exact_join_rejects_mismatches(mismatch):
    spatial, visibility = _inputs(0)
    if mismatch == "match_id":
        visibility["match_id"] = 999
    elif mismatch == "replay_sha256":
        visibility["replay_sha256"] = "b" * 64
    elif mismatch == "frame_count":
        visibility["snapshots"] = []
    elif mismatch == "time":
        visibility["snapshots"][0]["game_time_seconds"] = 0.000001
        visibility["snapshots"][0]["source_game_time_seconds"] = 0.000001
    elif mismatch == "tick":
        visibility["snapshots"][0]["replay_tick"] += 1
    elif mismatch == "duplicate_slot":
        visibility["snapshots"][0]["players"][1]["player_slot"] = 0
    else:
        player = visibility["snapshots"][0]["players"][0]
        player[mismatch] = "DIRE" if mismatch == "team" else 999
    with pytest.raises(ValueError):
        build_pov_feature_rows(spatial, visibility)


def test_player_array_order_does_not_determine_hero_join():
    spatial, visibility = _inputs(0)
    expected = build_pov_feature_rows(spatial, visibility)
    visibility["snapshots"][0]["players"].reverse()
    assert build_pov_feature_rows(spatial, visibility) == expected


def test_csv_nulls_and_input_artifacts_are_preserved(tmp_path: Path):
    spatial, visibility = _inputs(0)
    sp, vp = tmp_path / "spatial.json", tmp_path / "visibility.json"
    sp.write_text(json.dumps(spatial))
    vp.write_text(json.dumps(visibility))
    before = sp.read_bytes(), vp.read_bytes()
    assert extract_pov_features(sp, vp, tmp_path / "output") == (2, 10)
    assert (sp.read_bytes(), vp.read_bytes()) == before
    with (tmp_path / "output" / ENEMY_FILENAME).open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["current_x"] == rows[0]["last_seen_x"] == ""
    assert rows[0]["last_seen_status"] == "NEVER_SEEN"
    with (tmp_path / "output" / TEAM_FILENAME).open(newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["nearest_visible_enemy_distance"] == ""
    assert "visible_to_dire" not in row and "nearest_enemy_distance" not in row
