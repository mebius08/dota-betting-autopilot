from datetime import datetime
from pathlib import Path

import pytest

from app.history import (
    STAGE_FEATURE_COLUMNS,
    HistoricalPredictionContext,
    build_historical_match_features,
)
from app.storage import SQLiteRepository
from app.tournaments import CompetitiveStage
from tests.history_test_helpers import make_historical_match
from tests.roster_test_helpers import (
    make_coach,
    make_organization,
    make_player,
    make_roster_snapshot,
)


@pytest.mark.parametrize("stage", list(CompetitiveStage))
def test_stage_one_hot_features_are_explicit(
    tmp_path: Path,
    stage: CompetitiveStage,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id=f"target-{stage.value}",
            prediction_timestamp=_dt("2026-01-10T00:00:00Z"),
            team_a_source_id="team-a",
            team_b_source_id="team-b",
            competitive_stage=stage,
        ),
    )
    numeric = row.numeric_features()

    assert set(STAGE_FEATURE_COLUMNS).issubset(numeric)
    assert sum(int(numeric[column]) for column in STAGE_FEATURE_COLUMNS) == 1
    assert numeric[f"stage_{stage.value}"] == 1


def test_missing_and_future_roster_features_are_neutral(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    future_roster = make_roster_snapshot(
        "future",
        organization=make_organization("team-a"),
        observed_at=_dt("2026-01-11T00:00:00Z"),
        valid_from=_dt("2026-01-11T00:00:00Z"),
    )
    repository.upsert_roster_snapshot(future_roster)

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

    assert row.team_a_roster_snapshot_present == 0
    assert row.team_a_roster_player_count == 0
    assert row.team_a_roster_matches_together == 0
    assert row.team_a_avg_player_history_matches == 0.0
    assert row.team_a_avg_player_raw_win_rate == 0.5


@pytest.mark.parametrize(
    ("players", "coach", "expected_flag"),
    [
        (
            ["p1", "p2", "p3", "p4", "p5"],
            None,
            "team_a_roster_exact_continuity",
        ),
        (
            ["p1", "p2", "p3", "p4", "new"],
            None,
            "team_a_roster_strong_continuity",
        ),
        (
            ["p1", "p2", "p3", "new-1", "new-2"],
            make_coach("coach-1"),
            "team_a_roster_coach_supported_continuity",
        ),
    ],
)
def test_accepted_roster_continuity_flags(
    tmp_path: Path,
    players: list[str],
    coach: object,
    expected_flag: str,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    previous = make_roster_snapshot(
        "previous",
        organization=make_organization("old-team"),
        players=[make_player(f"p{index}") for index in range(1, 6)],
        coach=coach,
        observed_at=_dt("2026-01-01T00:00:00Z"),
        valid_from=_dt("2026-01-01T00:00:00Z"),
    )
    current = make_roster_snapshot(
        "current",
        organization=make_organization("team-a"),
        players=[make_player(player) for player in players],
        coach=coach,
        observed_at=_dt("2026-01-02T00:00:00Z"),
        valid_from=_dt("2026-01-02T00:00:00Z"),
    )
    for snapshot in (previous, current):
        repository.upsert_roster_snapshot(snapshot)

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

    assert row.team_a_roster_predecessor_chain_length == 1
    assert row.team_a_roster_continuity_overlap_count >= 3
    assert getattr(row, expected_flag) == 1
    assert (
        row.team_a_roster_exact_continuity
        + row.team_a_roster_strong_continuity
        + row.team_a_roster_coach_supported_continuity
        == 1
    )


def test_ambiguous_predecessor_is_not_auto_selected(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    shared_players = [make_player(f"p{index}") for index in range(1, 5)]
    current = make_roster_snapshot(
        "current",
        organization=make_organization("team-a"),
        players=[*shared_players, make_player("current-only")],
        observed_at=_dt("2026-01-03T00:00:00Z"),
        valid_from=_dt("2026-01-03T00:00:00Z"),
    )
    previous_a = make_roster_snapshot(
        "previous-a",
        organization=make_organization("previous-a"),
        players=[*shared_players, make_player("a-only")],
        observed_at=_dt("2026-01-01T00:00:00Z"),
        valid_from=_dt("2026-01-01T00:00:00Z"),
    )
    previous_b = make_roster_snapshot(
        "previous-b",
        organization=make_organization("previous-b"),
        players=[*shared_players, make_player("b-only")],
        observed_at=_dt("2026-01-01T00:00:00Z"),
        valid_from=_dt("2026-01-01T00:00:00Z"),
    )
    for snapshot in (previous_a, previous_b, current):
        repository.upsert_roster_snapshot(snapshot)

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

    assert row.team_a_roster_predecessor_chain_length == 0
    assert row.team_a_roster_exact_continuity == 0
    assert row.team_a_roster_strong_continuity == 0
    assert row.team_a_roster_coach_supported_continuity == 0


def test_player_history_uses_stable_ids_not_names(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    old_roster = make_roster_snapshot(
        "old",
        organization=make_organization("old-team"),
        players=[make_player(f"old-{index}", "Same Name") for index in range(5)],
        observed_at=_dt("2026-01-01T00:00:00Z"),
        valid_from=_dt("2026-01-01T00:00:00Z"),
    )
    current_roster = make_roster_snapshot(
        "current",
        organization=make_organization("team-a"),
        players=[make_player(f"new-{index}", "Same Name") for index in range(5)],
        observed_at=_dt("2026-01-02T00:00:00Z"),
        valid_from=_dt("2026-01-02T00:00:00Z"),
    )
    for snapshot in (old_roster, current_roster):
        repository.upsert_roster_snapshot(snapshot)
    repository.save_historical_match(
        make_historical_match(
            "old-win",
            started_at=_dt("2026-01-01T10:00:00Z"),
            ended_at=_dt("2026-01-01T12:00:00Z"),
            team_a_source_id="old-team",
            team_b_source_id="opponent",
            winner_side="team_a",
        )
    )

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

    assert row.team_a_avg_player_history_matches == 0.0
    assert row.team_a_avg_player_raw_win_rate == 0.5


def test_transferred_roster_counts_safe_player_history(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
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
    for snapshot in (previous, current):
        repository.upsert_roster_snapshot(snapshot)
    repository.save_historical_match(
        make_historical_match(
            "safe-win",
            started_at=_dt("2026-01-01T10:00:00Z"),
            ended_at=_dt("2026-01-01T12:00:00Z"),
            team_a_source_id="previous-team",
            team_b_source_id="opponent",
            winner_side="team_a",
        )
    )

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

    assert row.team_a_roster_matches_together == 1
    assert row.team_a_avg_player_history_matches == 1.0
    assert row.team_a_min_player_history_matches == 1
    assert row.team_a_max_player_history_matches == 1
    assert row.team_a_avg_player_raw_win_rate == 1.0


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
