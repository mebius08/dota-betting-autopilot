from pathlib import Path

import pytest

from app.reports import build_report_from_repository
from app.storage import SQLiteRepository
from tests.ml_test_helpers import make_bet, make_candidate, make_match, make_session


def test_build_report_empty_db(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    report = build_report_from_repository(repository)

    assert report.total_bets == 0
    assert report.roi_pct == 0.0


def test_build_report_with_open_and_settled_bets(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_bet_bundle(repository, "session-1", "match-1", "candidate-1", "open")
    save_bet_bundle(
        repository,
        "session-1",
        "match-2",
        "candidate-2",
        "win",
        result="win",
        status="settled",
        profit_units=0.32,
    )
    save_bet_bundle(
        repository,
        "session-1",
        "match-3",
        "candidate-3",
        "loss",
        result="loss",
        status="settled",
        profit_units=-0.35,
    )

    report = build_report_from_repository(repository)

    assert report.total_bets == 3
    assert report.open_bets == 1
    assert report.settled_bets == 2
    assert report.wins == 1
    assert report.losses == 1
    assert report.profit_units == pytest.approx(-0.03)


def test_build_report_roi(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_bet_bundle(
        repository,
        "session-1",
        "match-1",
        "candidate-1",
        "win",
        result="win",
        status="settled",
        profit_units=0.35,
    )
    save_bet_bundle(
        repository,
        "session-1",
        "match-2",
        "candidate-2",
        "loss",
        result="loss",
        status="settled",
        profit_units=-0.35,
    )

    report = build_report_from_repository(repository)

    assert report.total_staked_units == pytest.approx(0.7)
    assert report.roi_pct == pytest.approx(0.0)


def test_build_report_by_session(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_bet_bundle(
        repository,
        "session-1",
        "match-1",
        "candidate-1",
        "bet-1",
        result="win",
        status="settled",
        profit_units=0.32,
    )
    save_bet_bundle(
        repository,
        "session-2",
        "match-2",
        "candidate-2",
        "bet-2",
        result="loss",
        status="settled",
        profit_units=-0.35,
    )

    report = build_report_from_repository(repository, session_id="session-1")

    assert report.total_sessions == 1
    assert report.total_bets == 1
    assert report.wins == 1
    assert report.losses == 0


def save_bet_bundle(
    repository: SQLiteRepository,
    session_id: str,
    match_id: str,
    candidate_id: str,
    bet_id: str,
    result: str = "unknown",
    status: str = "placed",
    profit_units: float = 0.0,
) -> None:
    repository.save_session(make_session(session_id))
    repository.save_match(make_match(session_id, match_id))
    repository.save_bet_candidate(make_candidate(session_id, match_id, candidate_id))
    repository.save_bet(
        make_bet(
            session_id,
            match_id,
            candidate_id,
            bet_id,
            result=result,
            status=status,
            profit_units=profit_units,
        )
    )
