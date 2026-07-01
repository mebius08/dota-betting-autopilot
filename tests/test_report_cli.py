from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import cli
from app.domain import (
    Bet,
    BetCandidate,
    BetResult,
    BetStatus,
    Match,
    Session,
    StreamerUtterance,
)
from app.reports import build_report_from_repository
from app.storage import SQLiteRepository, init_db


def test_report_command_empty_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["report", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Bets: 0" in output


def test_report_command_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["report", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code != 0
    assert "Database not found" in output


def test_report_command_with_bets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = make_repository_with_bet(db_path, session_id="session-1")

    exit_code = cli.main(["report", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Bets: 1" in output
    assert "Open bets: 1" in output
    assert "Settled bets: 0" in output
    assert "ROI: 0.00%" in output
    assert "total_kills" in output
    assert "Profit units" in output
    assert repository.list_bets()


def test_report_show_utterances(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = make_repository_with_bet(db_path, session_id="session-1")
    repository.save_streamer_utterance(make_utterance("session-1", "session-1-match"))

    exit_code = cli.main(["report", "--db", str(db_path), "--show-utterances"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Recent streamer utterances" in output
    assert "over kills looks playable" in output


def test_build_report_from_repository_all_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    repository = make_repository_with_bet(db_path, session_id="session-1")
    add_bet_bundle(
        repository,
        session_id="session-2",
        bet_id="bet-2",
        result="win",
        profit_units=0.32,
        status="settled",
    )

    report = build_report_from_repository(repository)

    assert report.total_sessions == 2
    assert report.total_bets == 2
    assert report.open_bets == 1
    assert report.settled_bets == 1
    assert report.roi_pct == pytest.approx(91.43, abs=0.01)
    assert report.average_bets_per_match == 1.0


def test_build_report_from_repository_one_session(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    repository = make_repository_with_bet(db_path, session_id="session-1")
    add_bet_bundle(
        repository,
        session_id="session-2",
        bet_id="bet-2",
        result="win",
        profit_units=0.32,
        status="settled",
    )

    report = build_report_from_repository(repository, session_id="session-1")

    assert report.total_sessions == 1
    assert report.total_bets == 1
    assert report.open_bets == 1
    assert report.settled_bets == 0
    assert report.average_bets_per_match == 1.0


def make_repository_with_bet(db_path: Path, session_id: str) -> SQLiteRepository:
    repository = SQLiteRepository(db_path)
    add_bet_bundle(repository, session_id=session_id, bet_id="bet-1")
    return repository


def add_bet_bundle(
    repository: SQLiteRepository,
    session_id: str,
    bet_id: str,
    result: BetResult = "unknown",
    profit_units: float = 0.0,
    status: BetStatus = "placed",
) -> None:
    match_id = f"{session_id}-match"
    candidate_id = f"{session_id}-candidate"
    session = make_session(session_id)
    match = make_match(session_id, match_id)
    candidate = make_candidate(session_id, match_id, candidate_id)
    bet = make_bet(
        session_id,
        match_id,
        candidate_id,
        bet_id,
        result=result,
        profit_units=profit_units,
        status=status,
    )

    repository.save_session(session)
    repository.save_match(match)
    repository.save_bet_candidate(candidate)
    repository.save_bet(bet)


def make_session(session_id: str) -> Session:
    return Session(
        id=session_id,
        name="DreamLeague",
        tournament_keyword="DreamLeague",
        streamer_channel="manual_transcript",
        execution_mode="paper",
        target_bets_per_match=1.2,
        max_bets_per_match=3,
        score_threshold=62,
        active=True,
        created_at=datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc),
        ended_at=None,
    )


def make_match(session_id: str, match_id: str) -> Match:
    return Match(
        id=match_id,
        session_id=session_id,
        tournament_name="DreamLeague Season 25",
        team_a="Team Spirit",
        team_b="Gaimin Gladiators",
        format="bo3",
        status="upcoming",
        start_time=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
        external_id="external-match",
    )


def make_candidate(
    session_id: str,
    match_id: str,
    candidate_id: str,
) -> BetCandidate:
    return BetCandidate(
        id=candidate_id,
        session_id=session_id,
        match_id=match_id,
        market="total_kills",
        selection="over",
        line=48.5,
        odds=1.92,
        phase="after_draft",
        market_score=25,
        phase_score=20,
        line_score=10,
        streamer_score=7,
        risk_score=5,
        final_score=67,
        decision="bet",
        explanation="A-tier market",
        created_at=datetime(2026, 6, 30, 8, 5, tzinfo=timezone.utc),
    )


def make_bet(
    session_id: str,
    match_id: str,
    candidate_id: str,
    bet_id: str,
    result: BetResult = "unknown",
    profit_units: float = 0.0,
    status: BetStatus = "placed",
) -> Bet:
    return Bet(
        id=bet_id,
        session_id=session_id,
        match_id=match_id,
        candidate_id=candidate_id,
        mode="paper",
        market="total_kills",
        selection="over",
        line=48.5,
        odds=1.92,
        stake_pct=0.35,
        status=status,
        result=result,
        profit_units=profit_units,
        created_at=datetime(2026, 6, 30, 8, 10, tzinfo=timezone.utc),
        settled_at=None,
    )


def make_utterance(session_id: str, match_id: str) -> StreamerUtterance:
    return StreamerUtterance(
        id="utterance-1",
        session_id=session_id,
        match_id=match_id,
        source="manual_transcript",
        text="over kills looks playable",
        detected_market="total_kills",
        detected_selection="over",
        detected_team=None,
        signal_type="over_kills",
        strength=7.0,
        confidence=0.8,
        hype_flag=False,
        created_at=datetime(2026, 6, 30, 8, 4, tzinfo=timezone.utc),
    )
