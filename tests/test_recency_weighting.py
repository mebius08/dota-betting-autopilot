from datetime import datetime
from pathlib import Path

import pytest

from app.history import (
    HistoricalPredictionContext,
    RecencyWeightingPolicy,
    build_historical_match_features,
    calculate_recency_weight,
)
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_newer_completed_match_has_greater_deterministic_weight() -> None:
    policy = RecencyWeightingPolicy(decay_days=90.0)
    prediction_timestamp = _dt("2026-01-10T00:00:00Z")
    older = _dt("2026-01-01T00:00:00Z")
    newer = _dt("2026-01-09T00:00:00Z")

    older_weight = calculate_recency_weight(older, prediction_timestamp, policy)
    newer_weight = calculate_recency_weight(newer, prediction_timestamp, policy)

    assert newer_weight > older_weight
    assert calculate_recency_weight(older, prediction_timestamp, policy) == (
        older_weight
    )


def test_same_match_gets_different_weight_at_different_predictions() -> None:
    policy = RecencyWeightingPolicy(decay_days=90.0)
    ended_at = _dt("2026-01-01T00:00:00Z")

    near_weight = calculate_recency_weight(
        ended_at,
        _dt("2026-01-10T00:00:00Z"),
        policy,
    )
    later_weight = calculate_recency_weight(
        ended_at,
        _dt("2026-02-10T00:00:00Z"),
        policy,
    )

    assert near_weight != later_weight
    assert near_weight > later_weight


def test_invalid_decay_and_future_weight_are_rejected() -> None:
    with pytest.raises(ValueError, match="decay_days"):
        RecencyWeightingPolicy(decay_days=0)

    with pytest.raises(ValueError, match="end before prediction"):
        calculate_recency_weight(
            _dt("2026-01-10T00:00:00Z"),
            _dt("2026-01-10T00:00:00Z"),
        )

    with pytest.raises(ValueError, match="end before prediction"):
        calculate_recency_weight(
            _dt("2026-01-11T00:00:00Z"),
            _dt("2026-01-10T00:00:00Z"),
        )


def test_weighted_wins_and_mass_use_match_ended_at(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    older_win = make_historical_match(
        "older-win",
        started_at=_dt("2026-01-01T10:00:00Z"),
        ended_at=_dt("2026-01-01T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent-1",
        winner_side="team_a",
    )
    newer_loss = make_historical_match(
        "newer-loss",
        started_at=_dt("2026-01-08T10:00:00Z"),
        ended_at=_dt("2026-01-08T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent-2",
        winner_side="team_b",
    )
    repository.save_historical_match(older_win)
    repository.save_historical_match(newer_loss)

    prediction_timestamp = _dt("2026-01-10T00:00:00Z")
    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target",
            prediction_timestamp=prediction_timestamp,
            team_a_source_id="team-a",
            team_b_source_id="team-b",
        ),
    )

    older_weight = calculate_recency_weight(
        older_win.ended_at,
        prediction_timestamp,
    )
    newer_weight = calculate_recency_weight(
        newer_loss.ended_at,
        prediction_timestamp,
    )

    assert row.team_a_recency_weighted_matches == pytest.approx(
        older_weight + newer_weight
    )
    assert row.team_a_recency_weighted_wins == pytest.approx(older_weight)
    assert row.team_a_recency_weighted_win_rate == pytest.approx(
        older_weight / (older_weight + newer_weight)
    )


def test_no_history_gives_neutral_weighted_win_rate(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target",
            prediction_timestamp=_dt("2026-01-10T00:00:00Z"),
            team_a_source_id="team-a",
            team_b_source_id="team-b",
        ),
    )

    assert row.team_a_history_matches == 0
    assert row.team_a_raw_win_rate == 0.5
    assert row.team_a_recency_weighted_matches == 0.0
    assert row.team_a_recency_weighted_win_rate == 0.5


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
