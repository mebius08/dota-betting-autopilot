from datetime import datetime, timezone
from uuid import uuid4

from app.domain import BET_RESULTS, Bet, BetCandidate, BetResult, ExecutionMode


class PaperExecutor:
    def __init__(self) -> None:
        self.bets: list[Bet] = []

    def place(
        self,
        candidate: BetCandidate,
        stake_pct: float,
        mode: ExecutionMode = "paper",
    ) -> Bet:
        bet = Bet(
            id=str(uuid4()),
            session_id=candidate.session_id,
            match_id=candidate.match_id,
            candidate_id=candidate.id,
            mode=mode,
            market=candidate.market,
            selection=candidate.selection,
            line=candidate.line,
            odds=candidate.odds,
            stake_pct=stake_pct,
            status="placed",
            result="unknown",
            profit_units=0.0,
            created_at=datetime.now(timezone.utc),
            settled_at=None,
        )
        self.bets.append(bet)
        return bet

    def settle_bet(self, bet_id: str, result: BetResult) -> Bet:
        if result == "unknown":
            raise ValueError("Cannot settle a bet with unknown result")
        if result not in BET_RESULTS:
            raise ValueError(f"Unsupported bet result: {result}")

        bet = self._find_bet(bet_id)
        if result == "win":
            bet.profit_units = (bet.odds - 1.0) * bet.stake_pct
        elif result == "loss":
            bet.profit_units = -bet.stake_pct
        else:
            bet.profit_units = 0.0

        bet.status = "settled"
        bet.result = result
        bet.settled_at = datetime.now(timezone.utc)
        return bet

    def _find_bet(self, bet_id: str) -> Bet:
        for bet in self.bets:
            if bet.id == bet_id:
                return bet
        raise ValueError(f"Bet not found: {bet_id}")
