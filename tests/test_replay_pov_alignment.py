from __future__ import annotations

from copy import deepcopy

import pytest

from app.replay_pov_alignment import (
    FEATURE_COLUMNS, align_pov_files, build_alignment_rows,
)


def _inputs(grid=60, actual=60.0002, tick=2800):
    compact = {
        "match_id": 123, "replay_sha256": "a" * 64,
        "clock_normalization_status": "PROVEN",
        "radiant_team_id": 10, "dire_team_id": 20,
        "snapshots": [{"game_time_seconds": grid,
                       "source_game_time_seconds": actual, "replay_tick": tick}],
    }
    decisions = [
        {"match_id": "123", "replay_sha256": "a" * 64,
         "game_time_seconds": str(grid), "team": team, "team_id": str(team_id)}
        for team, team_id in (("RADIANT", 10), ("DIRE", 20))
    ]
    return decisions, compact


def _pov(time, tick, team="RADIANT", visible=1):
    return {
        "match_id": "123", "replay_sha256": "a" * 64, "pov_team": team,
        "game_time_seconds": str(time), "replay_tick": str(tick),
        "own_alive_hero_count": "5", "own_valid_position_count": "5",
        "own_unknown_life_state_count": "0", "own_team_spread": "100.5",
        "visible_enemy_count": str(visible),
        "visible_enemy_with_position_count": str(visible),
        "hidden_enemy_count": str(5 - visible), "unknown_enemy_count": "0",
        "nearest_visible_enemy_distance": "200.5" if visible else "",
        "own_team_spread_status": "OK",
        "nearest_visible_enemy_status": "OK" if visible else "NO_VISIBLE_ENEMY_POSITION",
    }


def test_actual_decision_time_comes_from_exact_compact_source_not_grid():
    decisions, compact = _inputs(actual=60.02)
    rows = build_alignment_rows(decisions, compact, [_pov(60.01, 2800)])
    assert rows[0]["decision_grid_time_seconds"] == 60
    assert rows[0]["decision_game_time_seconds"] == 60.02
    assert rows[0]["pov_game_time_seconds"] == 60.01
    assert rows[0]["pov_age_seconds"] == pytest.approx(0.01)
    assert rows[0]["decision_id"] == "123:60:RADIANT"


def test_nearest_future_frame_is_replaced_by_previous_actual_observation():
    decisions, compact = _inputs()
    past, future = _pov(50.0334, 2501), _pov(60.0334, 2801, visible=5)
    row = build_alignment_rows(decisions, compact, [past, future])[0]
    assert row["alignment_status"] == "MATCHED"
    assert row["pov_game_time_seconds"] == 50.0334
    assert row["pov_replay_tick"] == 2501
    assert row["pov_age_seconds"] == pytest.approx(9.9668)
    assert row["visible_enemy_count"] == 1


@pytest.mark.parametrize("time,tick", [(60.0002, 2801), (59.9, 2801), (60.0003, 2799)])
def test_both_actual_time_and_replay_tick_must_not_exceed_decision(time, tick):
    decisions, compact = _inputs()
    row = build_alignment_rows(
        decisions, compact, [_pov(50, 2500), _pov(time, tick)],
    )[0]
    assert row["pov_game_time_seconds"] == 50


def test_exact_source_frame_is_allowed_and_teams_never_cross():
    decisions, compact = _inputs()
    pov = [_pov(60.0002, 2800), _pov(60.0002, 2800, "DIRE", visible=4)]
    rows = build_alignment_rows(decisions, compact, pov)
    assert [r["visible_enemy_count"] for r in rows] == [1, 4]
    assert [r["team_id"] for r in rows] == [10, 20]
    assert all(r["pov_age_seconds"] == 0 for r in rows)
    assert len({r["decision_id"] for r in rows}) == len(decisions)


@pytest.mark.parametrize("actual,status", [(120, "MATCHED"), (120.000001, "STALE_POV")])
def test_fifteen_second_limit_is_inclusive_without_tolerance(actual, status):
    decisions, compact = _inputs(grid=120, actual=actual)
    row = build_alignment_rows(decisions, compact, [_pov(105, 2700)])[0]
    assert row["alignment_status"] == status
    assert row["latest_past_pov_game_time_seconds"] == 105
    assert row["latest_past_pov_age_seconds"] == pytest.approx(actual - 105)
    if status == "STALE_POV":
        assert row["pov_game_time_seconds"] is None
        assert row["pov_replay_tick"] is None and row["pov_age_seconds"] is None
        assert all(row[name] is None for name in FEATURE_COLUMNS)


def test_before_first_observation_and_missing_team_have_no_past_pov():
    decisions, compact = _inputs(grid=0, actual=0, tick=100)
    rows = build_alignment_rows(decisions, compact, [_pov(0.001, 101)])
    assert all(r["alignment_status"] == "NO_PAST_POV" for r in rows)
    assert all(r["latest_past_pov_game_time_seconds"] is None for r in rows)
    assert all(r[name] is None for r in rows for name in FEATURE_COLUMNS)


def test_missing_geometry_is_preserved_without_global_or_last_seen_fallback():
    decisions, compact = _inputs()
    pov = _pov(60.0002, 2800, visible=0)
    pov.update(own_team_spread="", nearest_enemy_distance=1,
               visible_to_dire="VISIBLE", last_seen_age_seconds=0)
    decisions[0]["future_team_kills"] = 999
    row = build_alignment_rows(decisions, compact, [pov])[0]
    assert row["own_team_spread"] is None
    assert row["nearest_visible_enemy_distance"] is None
    assert row["visible_enemy_count"] == 0
    assert not ({"nearest_enemy_distance", "visible_to_dire",
                 "last_seen_age_seconds", "future_team_kills"} & row.keys())


def test_appending_future_pov_frames_cannot_change_any_aligned_row():
    decisions, compact = _inputs()
    pov = [_pov(50.0334, 2501), _pov(50.0334, 2501, "DIRE", visible=3)]
    before = build_alignment_rows(decisions, compact, pov)
    future = [_pov(60.0334, 2801, team, visible=5) for team in ("RADIANT", "DIRE")]
    future += [_pov(100, 4000)]
    assert build_alignment_rows(decisions, compact, pov + future) == before


@pytest.mark.parametrize("where", ["decision", "pov"])
@pytest.mark.parametrize("field,value", [("match_id", "999"), ("replay_sha256", "b" * 64)])
def test_foreign_match_or_replay_is_rejected(where, field, value):
    decisions, compact = _inputs()
    pov = [_pov(50, 2500)]
    (decisions if where == "decision" else pov)[0][field] = value
    with pytest.raises(ValueError, match="mismatch"):
        build_alignment_rows(decisions, compact, pov)


@pytest.mark.parametrize("duplicate", ["decision", "pov"])
def test_duplicates_are_rejected_instead_of_multiplying_decisions(duplicate):
    decisions, compact = _inputs()
    pov = [_pov(50, 2500)]
    target = decisions if duplicate == "decision" else pov
    target.append(deepcopy(target[0]))
    with pytest.raises(ValueError, match="Duplicate"):
        build_alignment_rows(decisions, compact, pov)


def test_missing_source_and_wrong_team_id_fail_explicitly():
    decisions, compact = _inputs()
    decisions[0]["game_time_seconds"] = "59"
    with pytest.raises(ValueError, match="exact compact"):
        build_alignment_rows(decisions, compact, [])
    decisions[0].update(game_time_seconds="60", team_id="20")
    with pytest.raises(ValueError, match="team_id"):
        build_alignment_rows(decisions, compact, [])


@pytest.mark.parametrize("field", ["game_time_seconds", "own_team_spread"])
def test_nonfinite_pov_values_are_rejected(field):
    decisions, compact = _inputs()
    pov = _pov(50, 2500)
    pov[field] = "NaN"
    with pytest.raises(ValueError, match="finite"):
        build_alignment_rows(decisions, compact, [pov])


def test_file_alignment_cannot_overwrite_its_input(tmp_path):
    source = tmp_path / "decisions.csv"
    source.write_bytes(b"preserve me\n")
    with pytest.raises(ValueError, match="overwrite"):
        align_pov_files(source, tmp_path / "compact.json", tmp_path / "pov.csv", source)
    assert source.read_bytes() == b"preserve me\n"
