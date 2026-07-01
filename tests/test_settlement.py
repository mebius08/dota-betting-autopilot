from pathlib import Path

import pytest

from app.storage import SQLiteRepository, calculate_profit_units
from tests.ml_test_helpers import make_bet, make_candidate, make_match, make_session


def test_calculate_profit_win() -> None:
    assert calculate_profit_units("win", odds=2.0, stake_pct=0.5) == 0.5


def test_calculate_profit_loss() -> None:
    assert calculate_profit_units("loss", odds=2.0, stake_pct=0.5) == -0.5


def test_calculate_profit_push_void() -> None:
    assert calculate_profit_units("push", odds=2.0, stake_pct=0.5) == 0.0
    assert calculate_profit_units("void", odds=2.0, stake_pct=0.5) == 0.0


def test_settle_bet_win(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    bet = save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")

    settled = repository.settle_bet(bet.id, "win")

    assert settled.status == "settled"
    assert settled.result == "win"
    assert settled.profit_units == pytest.approx(bet.stake_pct * (bet.odds - 1))
    assert settled.settled_at is not None


def test_settle_bet_loss(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    bet = save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")

    settled = repository.settle_bet(bet.id, "loss")

    assert settled.status == "settled"
    assert settled.result == "loss"
    assert settled.profit_units == -bet.stake_pct


def test_settle_bet_missing_id_raises(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    with pytest.raises(ValueError, match="Bet not found"):
        repository.settle_bet("missing", "win")


def test_list_open_bets(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    open_bet = save_open_bet(
        repository,
        "session-1",
        "match-1",
        "candidate-1",
        "bet-open",
    )
    save_settled_bet(repository, "session-1", "match-2", "candidate-2", "bet-settled")

    open_bets = repository.list_open_bets()

    assert open_bets == [open_bet]


def test_list_open_bets_by_session(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    first_bet = save_open_bet(
        repository,
        "session-1",
        "match-1",
        "candidate-1",
        "bet-1",
    )
    save_open_bet(repository, "session-2", "match-2", "candidate-2", "bet-2")

    open_bets = repository.list_open_bets_by_session("session-1")

    assert open_bets == [first_bet]


def save_open_bet(
    repository: SQLiteRepository,
    session_id: str,
    match_id: str,
    candidate_id: str,
    bet_id: str,
):
    bet = make_bet(
        session_id,
        match_id,
        candidate_id,
        bet_id,
        result="unknown",
        status="placed",
        profit_units=0.0,
    )
    save_bet_bundle(repository, session_id, match_id, candidate_id, bet)
    return bet


def save_settled_bet(
    repository: SQLiteRepository,
    session_id: str,
    match_id: str,
    candidate_id: str,
    bet_id: str,
) -> None:
    bet = make_bet(
        session_id,
        match_id,
        candidate_id,
        bet_id,
        result="win",
        status="settled",
        profit_units=0.32,
    )
    save_bet_bundle(repository, session_id, match_id, candidate_id, bet)


def save_bet_bundle(
    repository: SQLiteRepository,
    session_id: str,
    match_id: str,
    candidate_id: str,
    bet,
) -> None:
    repository.save_session(make_session(session_id))
    repository.save_match(make_match(session_id, match_id))
    repository.save_bet_candidate(make_candidate(session_id, match_id, candidate_id))
    repository.save_bet(bet)
