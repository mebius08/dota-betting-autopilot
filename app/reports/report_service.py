from dataclasses import dataclass

from app.domain import Bet


@dataclass
class BettingReport:
    total_bets: int
    wins: int
    losses: int
    pushes: int
    voids: int
    profit_units: float
    average_bets_per_match: float


def build_report(bets: list[Bet], matches_count: int) -> BettingReport:
    total_bets = len(bets)
    return BettingReport(
        total_bets=total_bets,
        wins=sum(1 for bet in bets if bet.result == "win"),
        losses=sum(1 for bet in bets if bet.result == "loss"),
        pushes=sum(1 for bet in bets if bet.result == "push"),
        voids=sum(1 for bet in bets if bet.result == "void"),
        profit_units=sum(bet.profit_units for bet in bets),
        average_bets_per_match=total_bets / matches_count if matches_count else 0.0,
    )
