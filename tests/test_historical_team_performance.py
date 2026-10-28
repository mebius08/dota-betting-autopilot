from copy import deepcopy
import math

import pytest

from app.analytics.historical_team_performance import (
    HALF_LIFE_SECONDS, SOURCE, build_historical_team_performance,
)


NOW = 1800000000
TARGET = dict(match_id=1000, start_time=NOW, radiant_team_id=10, dire_team_id=20)


def match(mid, *, end=NOW-HALF_LIFE_SECONDS, duration=3600, radiant=10, dire=20, win=True):
    return dict(match_id=mid, source=SOURCE, start_time=end-duration,
                duration_seconds=duration, radiant_team_id=radiant, dire_team_id=dire,
                radiant_win=win)


def evaluate(rows, target=None):
    return build_historical_team_performance(TARGET if target is None else target, rows)


def test_wins_losses_side_orientation_and_45_day_decay_from_completion():
    result = evaluate([match(1), match(2, end=NOW-2*HALF_LIFE_SECONDS, win=False)])
    radiant, dire = result["teams"]["RADIANT"], result["teams"]["DIRE"]
    assert radiant["prior_match_count"] == 2 and radiant["wins"] == radiant["losses"] == 1
    assert radiant["weighted_matches"] == 0.75 and radiant["weighted_wins"] == 0.5
    assert radiant["weighted_win_fraction"] == pytest.approx(2/3)
    assert dire["weighted_wins"] == 0.25 and dire["weighted_win_fraction"] == pytest.approx(1/3)
    assert result["half_life_days"] == 45
    assert result["completion_time_basis"] == "start_time + duration_seconds"
    assert result["decay_reference"] == "CATALOG_COMPLETION_TIME"


def test_team_identity_follows_side_changes_and_unrelated_teams_are_not_counted():
    result = evaluate([match(1, radiant=30, dire=10, win=False), match(2, radiant=40, dire=50)])
    assert result["teams"]["RADIANT"]["wins"] == 1
    assert result["teams"]["RADIANT"]["weighted_win_fraction"] == 1
    assert result["teams"]["DIRE"]["prior_match_count"] == 0
    assert result["coverage"]["eligible_completed_matches"] == 2
    assert result["coverage"]["eligible_relevant_matches"] == 1
    assert result["coverage"]["team_id_is_fixed_roster"] is False


def test_future_simultaneous_overlapping_and_exact_end_boundary_are_excluded():
    rows = [match(1, end=NOW+7200), match(2, end=NOW+3600),
            match(3, end=NOW+1), match(4, end=NOW), match(5, end=NOW-1)]
    result = evaluate(rows)
    assert result["teams"]["RADIANT"]["prior_match_count"] == 1
    assert result["coverage"]["excluded"]["not_started_before_target"] == 2
    assert result["coverage"]["excluded"]["not_completed_before_target"] == 2


def test_target_result_and_post_game_statistics_never_enter_own_features():
    target = dict(TARGET, radiant_win=object(), duration_seconds=object(), final_rating=999)
    result = evaluate([dict(match(TARGET["match_id"]), radiant_win=object()), match(1)], target)
    assert result["teams"] == evaluate([match(1)])["teams"]
    assert result["coverage"]["excluded"]["target_match"] == 1
    assert "final_rating" not in result and "radiant_win" not in result


@pytest.mark.parametrize("field, value, reason", [
    ("match_id", None, "unknown_match_id"), ("match_id", 0, "unknown_match_id"),
    ("source", None, "unknown_source"), ("start_time", None, "unknown_start_time"),
    ("start_time", 0, "unknown_start_time"), ("duration_seconds", None, "unknown_duration"),
    ("duration_seconds", 0, "unknown_duration"), ("radiant_team_id", None, "unknown_team_id"),
    ("dire_team_id", 0, "unknown_team_id"), ("radiant_win", None, "unknown_result"),
])
def test_unknown_history_values_are_reported_without_fabrication(field, value, reason):
    row = match(1)
    row[field] = value
    result = evaluate([row])
    assert result["coverage"]["excluded"][reason] == 1
    team = result["teams"]["RADIANT"]
    assert team["prior_match_count"] == team["wins"] == team["losses"] == 0
    assert team["weighted_win_fraction"] is None


def test_empty_history_is_not_a_zero_winrate_or_complete_career():
    result = evaluate([])
    team = result["teams"]["RADIANT"]
    assert team["status"] == "NO_OBSERVED_HISTORY" and team["prior_match_count"] == 0
    assert team["weighted_matches"] == team["weighted_wins"] == 0
    assert team["weighted_win_fraction"] is None
    assert result["coverage"]["complete_career_history"] is False
    assert result["source"] == SOURCE
    assert result["provenance"] == "HISTORICAL_OPENDOTA_TEAM_PERFORMANCE"


def test_unknown_target_time_does_not_consume_history_or_invent_order():
    def forbidden():
        raise AssertionError("History must not be evaluated without target time")
        yield
    result = evaluate(forbidden(), dict(TARGET, start_time=None))
    assert result["coverage"]["history_evaluated"] is False
    assert result["coverage"]["eligible_completed_matches"] is None
    assert all(team["prior_match_count"] is team["wins"] is team["losses"] is None
               for team in result["teams"].values())
    assert all(team["status"] == "UNKNOWN_TARGET_START_TIME" for team in result["teams"].values())


def test_unknown_target_team_does_not_borrow_opponent_identity():
    result = evaluate([match(1)], dict(TARGET, radiant_team_id=None))
    assert result["teams"]["RADIANT"]["status"] == "UNKNOWN_TEAM_ID"
    assert result["teams"]["RADIANT"]["weighted_matches"] is None
    assert result["teams"]["DIRE"]["losses"] == 1


def test_identical_duplicates_are_counted_once_and_conflicts_fail():
    result = evaluate([match(1), match(1)])
    assert result["teams"]["RADIANT"]["prior_match_count"] == 1
    assert result["coverage"]["duplicate_rows"] == 1
    with pytest.raises(ValueError, match="Conflicting"):
        evaluate([match(1), match(1, win=False)])
    with pytest.raises(ValueError, match="Conflicting"):
        evaluate([match(1), dict(match(1), radiant_win=1)])


@pytest.mark.parametrize("field, value", [
    ("start_time", True), ("duration_seconds", -1), ("duration_seconds", 1.5),
    ("radiant_win", 1), ("radiant_win", "false"), ("source", "unconfirmed"),
    ("dire_team_id", 10),
])
def test_malformed_or_ambiguous_records_fail_explicitly(field, value):
    row = match(1)
    row[field] = value
    with pytest.raises(ValueError):
        evaluate([row])


def test_order_independent_finite_statistics_and_read_only_inputs():
    rows = [match(2, win=False), match(1, end=NOW-3*HALF_LIFE_SECONDS)]
    target, before = deepcopy(TARGET), deepcopy(rows)
    result = evaluate(rows, target)
    assert result == evaluate(list(reversed(rows)), target)
    assert rows == before and target == TARGET
    for team in result["teams"].values():
        assert math.isfinite(team["weighted_wins"]) and math.isfinite(team["weighted_matches"])
        assert 0 <= team["weighted_win_fraction"] <= 1
