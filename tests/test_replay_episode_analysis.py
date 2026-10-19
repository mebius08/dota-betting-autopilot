from __future__ import annotations

from copy import deepcopy

import pytest

from app.replay_episode_analysis import (
    GLOBAL_FIELDS, death_windows, feature_snapshots, review_windows, select_windows,
)


def _frame(grid, radiant=0, dire=0):
    return {
        "game_time_seconds": grid, "source_game_time_seconds": grid + 0.0002 if grid else 0,
        "replay_tick": 1000 + grid * 30,
        "players": [{"player_slot": slot, "hero_id": slot + 1,
                     "team": "RADIANT" if slot < 5 else "DIRE", "kills": 999,
                     "deaths": radiant if slot == 0 else dire if slot == 5 else 0}
                    for slot in range(10)],
    }


def _compact(*frames):
    return {"match_id": 123, "replay_sha256": "a" * 64,
            "clock_normalization_status": "PROVEN", "radiant_team_id": 10,
            "dire_team_id": 20, "snapshots": list(frames)}


def _aligned(grid=60, source=50.0334, tick=2501, team="RADIANT"):
    actual = grid + 0.0002 if grid else 0
    return {
        "match_id": "123", "replay_sha256": "a" * 64, "team": team,
        "team_id": "10" if team == "RADIANT" else "20",
        "decision_id": f"123:{grid}:{team}", "decision_grid_time_seconds": str(grid),
        "decision_game_time_seconds": str(actual), "decision_replay_tick": str(1000 + grid * 30),
        "pov_game_time_seconds": str(source), "pov_replay_tick": str(tick),
        "pov_age_seconds": str(actual - source), "alignment_status": "MATCHED",
        "own_alive_hero_count": "5", "own_valid_position_count": "5",
        "own_unknown_life_state_count": "0", "own_team_spread": "100",
        "visible_enemy_count": "0", "visible_enemy_with_position_count": "0",
        "hidden_enemy_count": "5", "unknown_enemy_count": "0",
        "nearest_visible_enemy_distance": "", "own_team_spread_status": "OK",
        "nearest_visible_enemy_status": "NO_VISIBLE_ENEMY_POSITION",
    }


def _global(time=50.0334, tick=2501, team="RADIANT", nearest=123):
    return {
        "match_id": "123", "replay_sha256": "a" * 64, "team": team,
        "game_time_seconds": str(time), "replay_tick": str(tick),
        "team_spread": "100", "nearest_enemy_distance": str(nearest),
        "valid_position_count": "5", "team_spread_change_30s": "-20",
        "lookback_game_time_seconds": str(time - 30), "lookback_elapsed_seconds": "30",
        "team_spread_status": "OK", "nearest_enemy_status": "OK", "spread_change_status": "OK",
    }


def test_proxy_uses_player_death_deltas_not_kills_and_has_no_window_after_last_frame():
    compact = _compact(_frame(0), _frame(60, 1, 1), _frame(120, 1, 1), _frame(180, 1, 2))
    windows = death_windows(compact)
    assert [w["eval_next_minute_total_deaths"] for w in windows] == [2, 0, 1]
    assert [w["decision_grid_time_seconds"] for w in windows] == [0, 60, 120]
    assert windows[0]["eval_window_elapsed_seconds"] == 60.0002
    assert windows[0]["eval_next_minute_radiant_deaths"] == 1
    assert death_windows(_compact(_frame(0))) == []


@pytest.mark.parametrize("field,value", [("game_time_seconds", 120), ("source_game_time_seconds", 0), ("replay_tick", 1000)])
def test_window_boundaries_require_adjacent_minutes_and_increasing_actual_clocks(field, value):
    end = _frame(60)
    end[field] = value
    with pytest.raises(ValueError, match="adjacent minute"):
        death_windows(_compact(_frame(0), end))


def test_player_counter_reset_cannot_be_hidden_by_another_players_death():
    start, end = _frame(0, 1), _frame(60)
    end["players"][1]["deaths"] = 1
    with pytest.raises(ValueError, match="counter decreased"):
        death_windows(_compact(start, end))


def test_selection_uses_deaths_only_spaces_clusters_and_keeps_unique_prior_controls():
    windows = [{"decision_grid_time_seconds": t, "decision_game_time_seconds": t,
                "eval_next_minute_total_deaths": deaths}
               for t, deaths in [(0, 0), (60, 2), (120, 5), (180, 0), (420, 4),
                                 (480, 0), (720, 3), (780, 0), (1020, 3), (1080, 0), (1320, 2)]]
    selected = select_windows(windows)
    episodes = [w for w in selected if w["window_kind"] == "KILL_CLUSTER_PROXY"]
    controls = [w for w in selected if w["window_kind"] == "ZERO_DEATH_CONTROL"]
    assert [w["decision_grid_time_seconds"] for w in episodes] == [120, 420, 720, 1020, 1320]
    assert [w["decision_grid_time_seconds"] for w in controls] == [0, 180, 480, 780, 1080]
    assert all(w["decision_game_time_seconds"] < w["paired_episode_grid_seconds"] for w in controls)


@pytest.mark.parametrize("time,tick", [(60.0334, 2801), (60.0002, 2801), (59.99, 2801)])
def test_global_lookup_rejects_future_time_or_tick_and_never_fills_pov_from_global(time, tick):
    features = feature_snapshots(_compact(_frame(60)), [_aligned()], [_global(), _global(time, tick, nearest=1)])
    row = features[60, "RADIANT"]
    assert row["global_game_time_seconds"] == 50.0334
    assert row["global_age_seconds"] == pytest.approx(9.9668)
    assert row["global_nearest_enemy_distance"] == 123
    assert row["pov_nearest_visible_enemy_distance"] is None


@pytest.mark.parametrize("field,value", [("pov_game_time_seconds", "60.0003"), ("pov_replay_tick", "2801"), ("decision_game_time_seconds", "60.0334")])
def test_inconsistent_or_future_aligned_pov_is_rejected(field, value):
    pov = _aligned()
    pov[field] = value
    with pytest.raises(ValueError):
        feature_snapshots(_compact(_frame(60)), [pov], [])


def test_missing_or_stale_global_has_no_feature_fallback():
    for source, expected in [([], "NO_PAST_GLOBAL"), ([_global(40, 2200)], "STALE_GLOBAL")]:
        row = feature_snapshots(_compact(_frame(60)), [_aligned()], source)[60, "RADIANT"]
        assert row["global_alignment_status"] == expected
        assert all(row["global_" + name] is None for name in GLOBAL_FIELDS)
        assert row["pov_own_team_spread"] == 100


def test_future_global_frames_decisions_and_deaths_do_not_change_earlier_features():
    compact = _compact(_frame(0), _frame(60))
    before = feature_snapshots(compact, [_aligned()], [_global()])
    extended = deepcopy(compact)
    extended["snapshots"].append(_frame(120, 8, 9))
    after = feature_snapshots(
        extended, [_aligned(), _aligned(120, 120.0002, 4600)],
        [_global(), _global(60.0334, 2801, nearest=1), _global(120.0002, 4600)],
    )
    assert before[60, "RADIANT"] == after[60, "RADIANT"]
    assert not any(k.startswith("eval_") for k in before[60, "RADIANT"])


def test_global_spread_change_may_not_refer_to_future_lookback():
    source = _global()
    source["lookback_game_time_seconds"] = "51"
    with pytest.raises(ValueError, match="lookback"):
        feature_snapshots(_compact(_frame(60)), [_aligned()], [source])


def test_outcomes_are_evaluation_only_and_unavailable_horizons_remain_missing():
    compact = _compact(_frame(0), _frame(60, 1, 1), _frame(120, 1, 1))
    aligned = [_aligned(0, 0, 1000, team) for team in ("RADIANT", "DIRE")]
    features = feature_snapshots(compact, aligned, [])
    windows = death_windows(compact)[:1]
    outcome = {"match_id": "123", "game_time_seconds": "0", "team": "RADIANT",
               "horizon_seconds": "120", "future_game_time_seconds": "120",
               "future_team_deaths": "1", "future_opponent_deaths": "1",
               "net_worth_diff_change": "100", "kill_diff_change": "0"}
    before = review_windows(compact, windows, features, [outcome])
    outcome["net_worth_diff_change"] = "99999"
    after = review_windows(compact, windows, features, [outcome])
    assert before[0]["eval_total_deaths_120"] == 2
    assert before[0]["eval_future_game_time_120"] == 120.0002
    assert before[0]["eval_outcome_status_180"] == "NO_FUTURE_SNAPSHOT"
    assert before[0]["eval_total_deaths_180"] is None
    assert before[1]["eval_outcome_status_120"] == "OUTCOME_MISSING"
    assert before[1]["eval_total_deaths_120"] is None
    assert {k:v for k,v in before[0].items() if not k.startswith("eval_")} == {k:v for k,v in after[0].items() if not k.startswith("eval_")}
    outcome["future_game_time_seconds"] = "121"
    with pytest.raises(ValueError, match="horizon"):
        review_windows(compact, windows, features, [outcome])
