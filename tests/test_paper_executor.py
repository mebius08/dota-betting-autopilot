from datetime import datetime, timezone
from typing import cast

import pytest

from app.domain import BetCandidate, BetResult
from app.execution import PaperExecutor


def make_candidate(odds: float = 2.0) -> BetCandidate:
    return BetCandidate(
        id="candidate-1",
        session_id="session-1",
        match_id="match-1",
        market="total_kills",
        selection="over",
        line=48.5,
        odds=odds,
        phase="after_draft",
        market_score=25,
        phase_score=20,
        line_score=10,
        streamer_score=4,
        risk_score=5,
        final_score=64,
        decision="bet",
        explanation="test candidate",
        created_at=datetime.now(timezone.utc),
    )


def test_place_creates_paper_bet() -> None:
    executor = PaperExecutor()

    bet = executor.place(make_candidate(), stake_pct=0.35)

    assert bet.status == "placed"
    assert bet.result == "unknown"
    assert bet.profit_units == 0
    assert executor.bets == [bet]


def test_win_settlement_calculates_profit() -> None:
    executor = PaperExecutor()
    bet = executor.place(make_candidate(odds=2.2), stake_pct=0.5)

    settled = executor.settle_bet(bet.id, "win")

    assert settled.status == "settled"
    assert settled.result == "win"
    assert settled.profit_units == pytest.approx(0.6)


def test_loss_settlement_calculates_profit() -> None:
    executor = PaperExecutor()
    bet = executor.place(make_candidate(), stake_pct=0.5)

    settled = executor.settle_bet(bet.id, "loss")

    assert settled.profit_units == pytest.approx(-0.5)


@pytest.mark.parametrize("result", ["push", "void"])
def test_push_and_void_settlement_are_zero(result: str) -> None:
    executor = PaperExecutor()
    bet = executor.place(make_candidate(), stake_pct=0.5)

    settled = executor.settle_bet(bet.id, cast(BetResult, result))

    assert settled.profit_units == 0
