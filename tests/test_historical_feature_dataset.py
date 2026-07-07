from datetime import datetime
from pathlib import Path

from app.history import (
    build_historical_feature_dataset,
    build_labeled_historical_feature_row,
)
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_dataset_uses_started_at_labels_and_chronological_order(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    history = make_historical_match(
        "history",
        started_at=_dt("2026-01-01T10:00:00Z"),
        ended_at=_dt("2026-01-01T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        winner_side="team_a",
    )
    later_team_b_win = make_historical_match(
        "later-team-b-win",
        started_at=_dt("2026-01-12T10:00:00Z"),
        ended_at=_dt("2026-01-12T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        winner_side="team_b",
    )
    earlier_team_a_win = make_historical_match(
        "earlier-team-a-win",
        started_at=_dt("2026-01-10T10:00:00Z"),
        ended_at=_dt("2026-01-10T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        winner_side="team_a",
    )
    unknown_winner = make_historical_match(
        "unknown-winner",
        started_at=_dt("2026-01-11T10:00:00Z"),
        ended_at=_dt("2026-01-11T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        winner_side=None,
    )
    missing_source_id = make_historical_match(
        "missing-source-id",
        started_at=_dt("2026-01-13T10:00:00Z"),
        ended_at=_dt("2026-01-13T12:00:00Z"),
        team_a_source_id=None,
        team_b_source_id="team-b",
        winner_side="team_a",
    )
    for match in (
        history,
        later_team_b_win,
        earlier_team_a_win,
        unknown_winner,
        missing_source_id,
    ):
        repository.save_historical_match(match)

    rows = build_historical_feature_dataset(repository)

    assert [row.feature_row.source_match_id for row in rows] == [
        "history",
        "earlier-team-a-win",
        "later-team-b-win",
    ]
    assert [row.target for row in rows] == [1, 1, 0]
    assert rows[1].feature_row.prediction_timestamp == (
        earlier_team_a_win.started_at
    )
    assert rows[2].feature_row.prediction_timestamp == later_team_b_win.started_at


def test_numeric_features_exclude_ids_and_winner_fields(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    target = make_historical_match(
        "target",
        started_at=_dt("2026-01-10T10:00:00Z"),
        ended_at=_dt("2026-01-10T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        winner_side="team_b",
    )
    repository.save_historical_match(target)

    row = build_labeled_historical_feature_row(target, repository)

    assert row is not None
    numeric = row.numeric_features()
    assert "source_match_id" not in numeric
    assert "team_a_source_id" not in numeric
    assert "winner_side" not in numeric
    assert "winner_source_id" not in numeric
    assert "target" not in numeric


def test_old_labeled_row_is_unchanged_after_future_append(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    target = make_historical_match(
        "target",
        started_at=_dt("2026-01-10T10:00:00Z"),
        ended_at=_dt("2026-01-10T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        winner_side="team_a",
    )
    repository.save_historical_match(
        make_historical_match(
            "history",
            started_at=_dt("2026-01-01T10:00:00Z"),
            ended_at=_dt("2026-01-01T12:00:00Z"),
            team_a_source_id="team-a",
            team_b_source_id="opponent",
            winner_side="team_a",
        )
    )
    repository.save_historical_match(target)

    before = build_labeled_historical_feature_row(target, repository)
    repository.save_historical_match(
        make_historical_match(
            "future",
            started_at=_dt("2026-01-11T10:00:00Z"),
            ended_at=_dt("2026-01-11T12:00:00Z"),
            team_a_source_id="team-a",
            team_b_source_id="team-b",
            winner_side="team_b",
        )
    )
    after = build_labeled_historical_feature_row(target, repository)

    assert before is not None
    assert after is not None
    assert before.feature_row == after.feature_row
    assert before.target == after.target == 1


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
