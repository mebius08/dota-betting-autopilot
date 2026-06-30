from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.domain import Bet, BetCandidate, Match, OddsSnapshot, Session
from app.storage import SQLiteRepository, get_connection, init_db


def test_storage_init_creates_db(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "test.db"

    init_db(db_path)

    assert db_path.exists()


def test_save_and_list_bets(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")
    match = make_match(session.id, "match-1")
    candidate = make_candidate(session.id, match.id, "candidate-1")
    bet = make_bet(session.id, match.id, candidate.id, "bet-1")

    repository.save_session(session)
    repository.save_match(match)
    repository.save_bet_candidate(candidate)
    repository.save_bet(bet)

    bets = repository.list_bets()

    assert len(bets) == 1
    assert bets[0].market == "total_kills"
    assert bets[0].odds == 1.92
    assert bets[0].stake_pct == 0.35
    assert bets[0].result == "unknown"


def test_save_session(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")

    repository.save_session(session)

    sessions = repository.list_sessions()
    assert sessions == [session]


def test_list_bets_by_session(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    first_session = make_session("session-1")
    second_session = make_session("session-2")
    first_match = make_match(first_session.id, "match-1")
    second_match = make_match(second_session.id, "match-2")
    first_candidate = make_candidate(first_session.id, first_match.id, "candidate-1")
    second_candidate = make_candidate(second_session.id, second_match.id, "candidate-2")
    first_bet = make_bet(first_session.id, first_match.id, first_candidate.id, "bet-1")
    second_bet = make_bet(
        second_session.id,
        second_match.id,
        second_candidate.id,
        "bet-2",
    )

    for session in (first_session, second_session):
        repository.save_session(session)
    for match in (first_match, second_match):
        repository.save_match(match)
    for candidate in (first_candidate, second_candidate):
        repository.save_bet_candidate(candidate)
    for bet in (first_bet, second_bet):
        repository.save_bet(bet)

    bets = repository.list_bets_by_session(first_session.id)

    assert bets == [first_bet]


def test_save_match(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")
    match = make_match(session.id, "match-1")

    repository.save_session(session)
    repository.save_match(match)

    matches = repository.list_matches_by_session(session.id)
    assert matches == [match]


def test_save_odds_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    session = make_session("session-1")
    match = make_match(session.id, "match-1")
    snapshot = make_snapshot(session.id, match.id, "snapshot-1")

    repository.save_session(session)
    repository.save_match(match)
    repository.save_odds_snapshot(snapshot)

    with closing(get_connection(db_path)) as connection:
        row = connection.execute(
            "SELECT market, odds, is_live FROM odds_snapshots WHERE id = ?",
            (snapshot.id,),
        ).fetchone()

    assert row is not None
    assert row["market"] == "total_kills"
    assert row["odds"] == 1.92
    assert row["is_live"] == 0


def make_session(session_id: str) -> Session:
    return Session(
        id=session_id,
        name="DreamLeague",
        tournament_keyword="DreamLeague",
        streamer_channel="streamer_name",
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
        streamer_score=4,
        risk_score=5,
        final_score=64,
        decision="bet",
        explanation="A-tier market; after draft phase",
        created_at=datetime(2026, 6, 30, 8, 5, tzinfo=timezone.utc),
    )


def make_bet(
    session_id: str,
    match_id: str,
    candidate_id: str,
    bet_id: str,
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
        status="placed",
        result="unknown",
        profit_units=0.0,
        created_at=datetime(2026, 6, 30, 8, 10, tzinfo=timezone.utc),
        settled_at=None,
    )


def make_snapshot(
    session_id: str,
    match_id: str,
    snapshot_id: str,
) -> OddsSnapshot:
    return OddsSnapshot(
        id=snapshot_id,
        session_id=session_id,
        match_id=match_id,
        external_market_id="external-market",
        market="total_kills",
        selection="over",
        line=48.5,
        odds=1.92,
        phase="after_draft",
        is_live=False,
        is_suspended=False,
        bookmaker="fakebook",
        created_at=datetime(2026, 6, 30, 8, 3, tzinfo=timezone.utc),
    )
