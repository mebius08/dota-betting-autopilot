from datetime import datetime, timezone
from pathlib import Path

from app.data_io import format_dataset_inspection_report, inspect_dataset
from app.domain import OddsSnapshot
from app.evaluation import build_evaluation_dataset
from app.storage import SQLiteRepository
from tests.ml_test_helpers import (
    make_bet,
    make_candidate,
    make_match,
    make_session,
    make_utterance,
)


def test_inspect_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)

    report = inspect_dataset(repository, db_path)

    assert report.sessions == 0
    assert report.matches == 0
    assert report.odds_snapshots == 0
    assert report.paper_bets == 0
    assert report.open_bets == 0
    assert report.settled_bets == 0
    assert report.outcomes == {"win": 0, "loss": 0, "push": 0, "void": 0}
    assert report.streamer_utterances == 0
    assert report.usable_ml_records == 0
    assert report.readiness == "not_enough_data"


def test_inspect_counts_synthetic_data(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    _save_bundle(repository, 1, "win", "settled")
    _save_bundle(repository, 2, "loss", "settled")
    _save_bundle(repository, 3, "push", "settled")
    _save_bundle(repository, 4, "void", "settled")
    _save_bundle(repository, 5, "unknown", "placed")

    report = inspect_dataset(repository, db_path)
    evaluation_dataset = build_evaluation_dataset(repository)

    assert report.sessions == 5
    assert report.matches == 5
    assert report.odds_snapshots == 5
    assert report.bet_candidates == 5
    assert report.paper_bets == 5
    assert report.open_bets == 1
    assert report.settled_bets == 4
    assert report.outcomes == {"win": 1, "loss": 1, "push": 1, "void": 1}
    assert report.streamer_utterances == 5
    assert report.usable_ml_records == evaluation_dataset.usable_records
    assert report.usable_ml_records == 2
    assert report.readiness == "smoke_training_ready"


def test_inspect_marks_evaluation_ready_at_existing_threshold(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    for index in range(1, 11):
        result = "win" if index % 2 else "loss"
        _save_bundle(repository, index, result, "settled")

    report = inspect_dataset(repository, db_path)

    assert report.usable_ml_records == 10
    assert report.readiness == "evaluation_ready"


def test_format_dataset_inspection_report(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)

    output = format_dataset_inspection_report(inspect_dataset(repository, db_path))

    assert f"Database: {db_path.as_posix()}" in output
    assert "Sessions: 0" in output
    assert "Outcomes:" in output
    assert "  win: 0" in output
    assert "Usable ML records: 0" in output
    assert "Readiness: not_enough_data" in output


def _save_bundle(
    repository: SQLiteRepository,
    index: int,
    result: str,
    status: str,
) -> None:
    session_id = f"session-{index}"
    match_id = f"match-{index}"
    candidate_id = f"candidate-{index}"
    repository.save_session(make_session(session_id))
    repository.save_match(make_match(session_id, match_id))
    repository.save_odds_snapshot(_make_snapshot(session_id, match_id, index))
    repository.save_bet_candidate(make_candidate(session_id, match_id, candidate_id))
    repository.save_bet(
        make_bet(
            session_id,
            match_id,
            candidate_id,
            f"bet-{index}",
            result=result,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            profit_units=0.32 if result == "win" else -0.35,
        )
    )
    repository.save_streamer_utterance(make_utterance(session_id, match_id))


def _make_snapshot(
    session_id: str,
    match_id: str,
    index: int,
) -> OddsSnapshot:
    return OddsSnapshot(
        id=f"odds-{index}",
        session_id=session_id,
        match_id=match_id,
        external_market_id=f"market-{index}",
        market="total_kills",
        selection="over",
        line=48.5,
        odds=1.92,
        phase="after_draft",
        is_live=False,
        is_suspended=False,
        bookmaker="fake",
        created_at=datetime(2026, 6, 30, 8, index, tzinfo=timezone.utc),
    )
