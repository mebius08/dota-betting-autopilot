from datetime import datetime, timedelta

from app.history import (
    HistoricalFeaturePolicy,
    HistoricalPredictionContext,
    build_historical_match_features,
    build_point_in_time_strength_state,
)
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_equal_raw_record_against_stronger_schedule_gets_more_strength() -> None:
    prediction_timestamp = _dt("2026-02-01T00:00:00Z")
    matches = [
        *_opponent_quality_matches("strong", strong=True),
        *_opponent_quality_matches("weak", strong=False),
        *_record_matches("team-a", "strong", wins=3, losses=2, start_day=20),
        *_record_matches("team-b", "weak", wins=3, losses=2, start_day=30),
    ]

    state = build_point_in_time_strength_state(prediction_timestamp, matches)

    assert state.opponent_adjusted_strengths["pandascore:team-a"] > (
        state.opponent_adjusted_strengths["pandascore:team-b"]
    )


def test_beating_strong_opponent_helps_more_than_beating_weak() -> None:
    prediction_timestamp = _dt("2026-02-01T00:00:00Z")
    matches = [
        *_opponent_quality_matches("strong", strong=True),
        *_opponent_quality_matches("weak", strong=False),
        _match(
            "beat-strong",
            day=20,
            team_a="team-strong-win",
            team_b="strong-1",
            winner_side="team_a",
        ),
        _match(
            "beat-weak",
            day=21,
            team_a="team-weak-win",
            team_b="weak-1",
            winner_side="team_a",
        ),
    ]

    state = build_point_in_time_strength_state(prediction_timestamp, matches)

    assert state.opponent_adjusted_strengths["pandascore:team-strong-win"] > (
        state.opponent_adjusted_strengths["pandascore:team-weak-win"]
    )


def test_losing_to_strong_opponent_is_less_damaging_than_losing_to_weak() -> None:
    prediction_timestamp = _dt("2026-02-01T00:00:00Z")
    matches = [
        *_opponent_quality_matches("strong", strong=True),
        *_opponent_quality_matches("weak", strong=False),
        _match(
            "lose-strong",
            day=20,
            team_a="team-strong-loss",
            team_b="strong-1",
            winner_side="team_b",
        ),
        _match(
            "lose-weak",
            day=21,
            team_a="team-weak-loss",
            team_b="weak-1",
            winner_side="team_b",
        ),
    ]

    state = build_point_in_time_strength_state(prediction_timestamp, matches)

    assert state.opponent_adjusted_strengths["pandascore:team-strong-loss"] > (
        state.opponent_adjusted_strengths["pandascore:team-weak-loss"]
    )


def test_cold_start_strength_is_neutral(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target",
            prediction_timestamp=_dt("2026-02-01T00:00:00Z"),
            team_a_source_id="cold-a",
            team_b_source_id="cold-b",
        ),
    )

    assert row.team_a_opponent_adjusted_strength == 0.0
    assert row.team_b_opponent_adjusted_strength == 0.0


def test_low_sample_strength_is_shrunk_toward_neutral() -> None:
    prediction_timestamp = _dt("2026-02-01T00:00:00Z")
    state = build_point_in_time_strength_state(
        prediction_timestamp,
        [
            _match(
                "one-win",
                day=1,
                team_a="one-win-team",
                team_b="opponent",
                winner_side="team_a",
            )
        ],
        policy=HistoricalFeaturePolicy(low_sample_shrinkage_matches=5.0),
    )

    strength = state.opponent_adjusted_strengths["pandascore:one-win-team"]
    assert 0.0 < strength < 0.5


def test_point_in_time_strength_excludes_future_and_exact_cutoff() -> None:
    prediction_timestamp = _dt("2026-02-01T00:00:00Z")
    state = build_point_in_time_strength_state(
        prediction_timestamp,
        [
            _match(
                "exact",
                day=31,
                team_a="team-a",
                team_b="opponent",
                winner_side="team_a",
                ended_at=prediction_timestamp,
            ),
            _match(
                "future",
                day=32,
                team_a="team-a",
                team_b="opponent",
                winner_side="team_a",
            ),
        ],
    )

    assert "pandascore:team-a" not in state.team_summaries


def test_strength_is_deterministic_independent_of_input_order() -> None:
    prediction_timestamp = _dt("2026-02-01T00:00:00Z")
    matches = [
        *_opponent_quality_matches("strong", strong=True),
        *_record_matches("team-a", "strong", wins=3, losses=2, start_day=20),
    ]

    forward = build_point_in_time_strength_state(prediction_timestamp, matches)
    reversed_state = build_point_in_time_strength_state(
        prediction_timestamp,
        list(reversed(matches)),
    )

    assert forward.opponent_adjusted_strengths == (
        reversed_state.opponent_adjusted_strengths
    )


def _opponent_quality_matches(prefix: str, *, strong: bool):
    matches = []
    for opponent_index in range(1, 6):
        opponent = f"{prefix}-{opponent_index}"
        for result_index in range(1, 4):
            filler = f"{prefix}-filler-{opponent_index}-{result_index}"
            if strong:
                matches.append(
                    _match(
                        f"{opponent}-quality-win-{result_index}",
                        day=result_index,
                        team_a=opponent,
                        team_b=filler,
                        winner_side="team_a",
                    )
                )
            else:
                matches.append(
                    _match(
                        f"{opponent}-quality-loss-{result_index}",
                        day=result_index,
                        team_a=opponent,
                        team_b=filler,
                        winner_side="team_b",
                    )
                )
    return matches


def _record_matches(
    team: str,
    opponent_prefix: str,
    *,
    wins: int,
    losses: int,
    start_day: int,
):
    matches = []
    index = 0
    for _ in range(wins):
        index += 1
        matches.append(
            _match(
                f"{team}-win-{index}",
                day=start_day + index,
                team_a=team,
                team_b=f"{opponent_prefix}-{index}",
                winner_side="team_a",
            )
        )
    for _ in range(losses):
        index += 1
        matches.append(
            _match(
                f"{team}-loss-{index}",
                day=start_day + index,
                team_a=team,
                team_b=f"{opponent_prefix}-{index}",
                winner_side="team_b",
            )
        )
    return matches


def _match(
    match_id: str,
    *,
    day: int,
    team_a: str,
    team_b: str,
    winner_side: str,
    ended_at: datetime | None = None,
):
    start = _dt("2026-01-01T10:00:00Z") + timedelta(days=day - 1)
    return make_historical_match(
        match_id,
        started_at=start,
        ended_at=ended_at or start.replace(hour=12),
        team_a_source_id=team_a,
        team_b_source_id=team_b,
        winner_side=winner_side,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
