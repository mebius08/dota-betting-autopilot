from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.history import (
    EWC_2026_BASELINE_SCOPE,
    HistoricalCompetitionScopePolicy,
    HistoricalFeaturePolicy,
    HistoricalPredictionContext,
    build_historical_feature_dataset,
    build_historical_match_features,
    calculate_recency_weight,
)
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match
from tests.roster_test_helpers import (
    make_organization,
    make_player,
    make_roster_snapshot,
)


TARGET_TIME = "2026-01-10T12:00:00Z"


@pytest.mark.parametrize(
    ("tournament_name", "expected_matches", "expected_wins"),
    [
        ("Newcastle Cup", 0, 0),
        ("DreamLeague Season 28", 1, 1),
        ("DreamLeague Closed Qualifier", 0, 0),
        ("FISSURE Universe", 0, 0),
        ("FISSURE Playground 2", 1, 1),
    ],
)
def test_competition_scope_controls_team_form_history(
    tmp_path: Path,
    tournament_name: str,
    expected_matches: int,
    expected_wins: int,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        _match(
            "prior-win",
            tournament_name=tournament_name,
            team_a="team-a",
            team_b="opponent",
            winner_side="team_a",
        )
    )

    row = _scoped_feature_row(repository)

    assert row.team_a_history_matches == expected_matches
    assert row.team_a_history_wins == expected_wins
    if expected_matches == 0:
        assert row.team_a_raw_win_rate == 0.5
        assert row.team_a_recency_weighted_matches == 0.0


def test_match_before_scope_start_does_not_contribute(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        _match(
            "pre-scope-win",
            started_at=_dt("2025-07-07T10:00:00Z"),
            ended_at=_dt("2025-07-07T12:00:00Z"),
            tournament_name="DreamLeague Season 28",
            team_a="team-a",
            team_b="opponent",
            winner_side="team_a",
        )
    )

    row = _scoped_feature_row(repository)

    assert row.team_a_history_matches == 0
    assert row.team_a_raw_win_rate == 0.5


def test_strict_completion_boundary_still_applies_after_scope_filter(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    target_timestamp = _dt(TARGET_TIME)
    repository.save_historical_match(
        _match(
            "ended-before",
            started_at=_dt("2026-01-01T10:00:00Z"),
            ended_at=_dt("2026-01-01T12:00:00Z"),
            tournament_name="DreamLeague Season 28",
            team_a="team-a",
            team_b="opponent-1",
            winner_side="team_a",
        )
    )
    repository.save_historical_match(
        _match(
            "ended-at-target",
            started_at=_dt("2026-01-10T10:00:00Z"),
            ended_at=target_timestamp,
            tournament_name="DreamLeague Season 28",
            team_a="team-a",
            team_b="opponent-2",
            winner_side="team_a",
        )
    )
    repository.save_historical_match(
        _match(
            "ended-after-target",
            started_at=_dt("2026-01-10T10:00:00Z"),
            ended_at=_dt("2026-01-10T13:00:00Z"),
            tournament_name="DreamLeague Season 28",
            team_a="team-a",
            team_b="opponent-3",
            winner_side="team_a",
        )
    )

    row = _scoped_feature_row(repository)

    assert row.team_a_history_matches == 1
    assert row.team_a_history_wins == 1


def test_opponent_adjusted_state_excludes_disallowed_matches_indirectly(
    tmp_path: Path,
) -> None:
    absent = _opponent_scope_row(tmp_path / "absent.db", link_tournament=None)
    disallowed = _opponent_scope_row(
        tmp_path / "disallowed.db",
        link_tournament="Newcastle Cup",
    )
    allowed = _opponent_scope_row(
        tmp_path / "allowed.db",
        link_tournament="DreamLeague Season 28",
    )

    assert disallowed.team_a_opponent_adjusted_strength == pytest.approx(
        absent.team_a_opponent_adjusted_strength
    )
    assert abs(
        allowed.team_a_opponent_adjusted_strength
        - absent.team_a_opponent_adjusted_strength
    ) > 1e-6


def test_recency_weighting_ignores_recent_disallowed_result(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    older_allowed_loss = _match(
        "older-allowed-loss",
        started_at=_dt("2025-08-01T10:00:00Z"),
        ended_at=_dt("2025-08-01T12:00:00Z"),
        tournament_name="DreamLeague Season 28",
        team_a="team-a",
        team_b="opponent-1",
        winner_side="team_b",
    )
    repository.save_historical_match(older_allowed_loss)
    repository.save_historical_match(
        _match(
            "recent-disallowed-win",
            started_at=_dt("2026-01-09T10:00:00Z"),
            ended_at=_dt("2026-01-09T12:00:00Z"),
            tournament_name="Newcastle Cup",
            team_a="team-a",
            team_b="opponent-2",
            winner_side="team_a",
        )
    )

    row = _scoped_feature_row(repository)

    assert row.team_a_history_matches == 1
    assert row.team_a_history_wins == 0
    assert row.team_a_recency_weighted_wins == 0.0
    assert row.team_a_recency_weighted_matches == pytest.approx(
        calculate_recency_weight(
            older_allowed_loss.ended_at,
            _dt(TARGET_TIME),
        )
    )
    assert row.team_a_recency_weighted_win_rate == 0.0


def test_roster_matches_together_and_player_history_use_scoped_matches(
    tmp_path: Path,
) -> None:
    disallowed_repository = SQLiteRepository(tmp_path / "disallowed.db")
    _add_linked_rosters(disallowed_repository)
    disallowed_repository.save_historical_match(
        _match(
            "disallowed-roster-win",
            tournament_name="Newcastle Cup",
            team_a="previous-team",
            team_b="opponent",
            winner_side="team_a",
        )
    )

    disallowed = _scoped_feature_row(disallowed_repository)

    assert disallowed.team_a_roster_matches_together == 0
    assert disallowed.team_a_avg_player_history_matches == 0.0
    assert disallowed.team_a_avg_player_raw_win_rate == 0.5

    allowed_repository = SQLiteRepository(tmp_path / "allowed.db")
    _add_linked_rosters(allowed_repository)
    allowed_repository.save_historical_match(
        _match(
            "allowed-roster-win",
            tournament_name="DreamLeague Season 28",
            team_a="previous-team",
            team_b="opponent",
            winner_side="team_a",
        )
    )

    allowed = _scoped_feature_row(allowed_repository)

    assert allowed.team_a_roster_matches_together == 1
    assert allowed.team_a_avg_player_history_matches == 1.0
    assert allowed.team_a_avg_player_raw_win_rate == 1.0


def test_raw_repository_stays_broad_but_feature_history_is_curated(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for match in (
        _match("allowed", tournament_name="DreamLeague Season 28"),
        _match("disallowed", tournament_name="FISSURE Universe"),
        _match("qualifier", tournament_name="DreamLeague Closed Qualifier"),
    ):
        repository.save_historical_match(match)

    row = _scoped_feature_row(repository)

    assert repository.count_historical_matches() == 3
    assert row.team_a_history_matches == 1
    assert row.team_a_history_wins == 1


def test_historical_ml_dataset_uses_same_scope_for_targets_and_features(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        _match(
            "disallowed-prior-win",
            tournament_name="Newcastle Cup",
            team_a="team-a",
            team_b="opponent",
            winner_side="team_a",
        )
    )
    repository.save_historical_match(
        _match(
            "allowed-target",
            started_at=_dt(TARGET_TIME),
            ended_at=_dt("2026-01-10T14:00:00Z"),
            tournament_name="DreamLeague Season 28",
            team_a="team-a",
            team_b="team-b",
            winner_side="team_a",
        )
    )

    rows = build_historical_feature_dataset(
        repository,
        competition_scope_policy=EWC_2026_BASELINE_SCOPE,
    )

    assert [row.feature_row.source_match_id for row in rows] == ["allowed-target"]
    assert rows[0].feature_row.team_a_history_matches == 0


def test_dataset_rejects_divergent_target_and_feature_history_scopes(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    divergent_scope = HistoricalCompetitionScopePolicy(
        scope_id=EWC_2026_BASELINE_SCOPE.scope_id,
        target_start_at=EWC_2026_BASELINE_SCOPE.target_start_at,
        allowed_families=EWC_2026_BASELINE_SCOPE.allowed_families,
        exclude_qualifiers=False,
    )

    with pytest.raises(ValueError, match="must match"):
        build_historical_feature_dataset(
            repository,
            competition_scope_policy=EWC_2026_BASELINE_SCOPE,
            target_scope_policy=divergent_scope,
        )


def _opponent_scope_row(path: Path, *, link_tournament: str | None):
    repository = SQLiteRepository(path)
    repository.save_historical_match(
        _match(
            "team-a-beats-opponent",
            started_at=_dt("2026-01-05T10:00:00Z"),
            ended_at=_dt("2026-01-05T12:00:00Z"),
            tournament_name="DreamLeague Season 28",
            team_a="team-a",
            team_b="linked-opponent",
            winner_side="team_a",
        )
    )
    if link_tournament is not None:
        repository.save_historical_match(
            _match(
                "opponent-link",
                started_at=_dt("2026-01-04T10:00:00Z"),
                ended_at=_dt("2026-01-04T12:00:00Z"),
                tournament_name=link_tournament,
                team_a="linked-opponent",
                team_b="filler",
                winner_side="team_a",
            )
        )
    return _scoped_feature_row(
        repository,
        policy=HistoricalFeaturePolicy(
            low_sample_shrinkage_matches=0.0,
            opponent_iterations=6,
        ),
    )


def _add_linked_rosters(repository: SQLiteRepository) -> None:
    players = [make_player(f"p{index}") for index in range(5)]
    previous = make_roster_snapshot(
        "previous",
        organization=make_organization("previous-team"),
        players=players,
        observed_at=_dt("2026-01-01T00:00:00Z"),
        valid_from=_dt("2026-01-01T00:00:00Z"),
    )
    current = make_roster_snapshot(
        "current",
        organization=make_organization("team-a"),
        players=list(reversed(players)),
        observed_at=_dt("2026-01-02T00:00:00Z"),
        valid_from=_dt("2026-01-02T00:00:00Z"),
    )
    repository.upsert_roster_snapshot(previous)
    repository.upsert_roster_snapshot(current)


def _scoped_feature_row(
    repository: SQLiteRepository,
    *,
    policy: HistoricalFeaturePolicy | None = None,
):
    return build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target",
            prediction_timestamp=_dt(TARGET_TIME),
            team_a_source_id="team-a",
            team_b_source_id="team-b",
        ),
        policy=policy,
        competition_scope_policy=EWC_2026_BASELINE_SCOPE,
    )


def _match(
    match_id: str,
    *,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    tournament_name: str,
    team_a: str = "team-a",
    team_b: str = "opponent",
    winner_side: str = "team_a",
):
    started = started_at or _dt("2026-01-01T10:00:00Z")
    return make_historical_match(
        match_id,
        started_at=started,
        ended_at=ended_at or started + timedelta(hours=2),
        team_a_source_id=team_a,
        team_b_source_id=team_b,
        winner_side=winner_side,
        tournament_name=tournament_name,
        league_name=None,
        series_name=None,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
