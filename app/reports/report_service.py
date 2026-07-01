from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain import Bet

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass
class BettingReport:
    total_sessions: int
    total_bets: int
    settled_bets: int
    open_bets: int
    wins: int
    losses: int
    pushes: int
    voids: int
    profit_units: float
    total_staked_units: float
    roi_pct: float
    average_bets_per_match: float


def build_report(
    bets: list[Bet],
    matches_count: int,
    total_sessions: int | None = None,
) -> BettingReport:
    total_bets = len(bets)
    settled = [bet for bet in bets if bet.status == "settled"]
    open_bets = sum(
        1 for bet in bets if bet.result == "unknown" or bet.status != "settled"
    )
    settled_bets = len(settled)
    profit_units = sum(bet.profit_units for bet in bets)
    total_staked_units = sum(bet.stake_pct for bet in settled)
    return BettingReport(
        total_sessions=(
            total_sessions
            if total_sessions is not None
            else len({bet.session_id for bet in bets})
        ),
        total_bets=total_bets,
        settled_bets=settled_bets,
        open_bets=open_bets,
        wins=sum(1 for bet in bets if bet.result == "win"),
        losses=sum(1 for bet in bets if bet.result == "loss"),
        pushes=sum(1 for bet in bets if bet.result == "push"),
        voids=sum(1 for bet in bets if bet.result == "void"),
        profit_units=profit_units,
        total_staked_units=total_staked_units,
        roi_pct=(
            profit_units / total_staked_units * 100 if total_staked_units else 0.0
        ),
        average_bets_per_match=total_bets / matches_count if matches_count else 0.0,
    )


def build_report_from_repository(
    repository: SQLiteRepository,
    session_id: str | None = None,
) -> BettingReport:
    if session_id is not None:
        bets = repository.list_bets_by_session(session_id)
        matches = repository.list_matches_by_session(session_id)
        matches_count = len(matches) or len({bet.match_id for bet in bets})
        total_sessions = 1 if repository.get_session(session_id) is not None else 0
        return build_report(
            bets,
            matches_count=matches_count,
            total_sessions=total_sessions,
        )

    sessions = repository.list_sessions()
    bets = repository.list_bets()
    matches_count = sum(
        len(repository.list_matches_by_session(session.id)) for session in sessions
    )
    if matches_count == 0:
        matches_count = len({bet.match_id for bet in bets})

    return build_report(
        bets,
        matches_count=matches_count,
        total_sessions=len(sessions),
    )
