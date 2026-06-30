from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.collectors import (
    MatchCollector,
    OddsCollector,
    TwitchCollector,
    match_in_scope,
)
from app.domain import Bet, BetCandidate, Decision, Match, OddsSnapshot, Session
from app.execution import ExecutionEngine
from app.scoring import ScoreBreakdown, analyze_streamer_messages, score_odds_snapshot
from app.scoring.bet_selector import select_bets


def build_candidate_from_snapshot(
    snapshot: OddsSnapshot,
    score: ScoreBreakdown,
    threshold: float,
) -> BetCandidate:
    decision: Decision
    if score.final_score >= threshold:
        decision = "bet"
    elif score.final_score >= threshold - 10:
        decision = "watch"
    else:
        decision = "skip"

    return BetCandidate(
        id=str(uuid4()),
        session_id=snapshot.session_id,
        match_id=snapshot.match_id,
        market=snapshot.market,
        selection=snapshot.selection,
        line=snapshot.line,
        odds=snapshot.odds,
        phase=snapshot.phase,
        market_score=score.market_score,
        phase_score=score.phase_score,
        line_score=score.line_score,
        streamer_score=score.streamer_score,
        risk_score=score.risk_score,
        final_score=score.final_score,
        decision=decision,
        explanation=score.explanation,
        created_at=datetime.now(timezone.utc),
    )


class AutopilotService:
    def __init__(
        self,
        match_collector: MatchCollector,
        odds_collector: OddsCollector,
        twitch_collector: TwitchCollector,
        execution_engine: ExecutionEngine,
    ) -> None:
        self.match_collector = match_collector
        self.odds_collector = odds_collector
        self.twitch_collector = twitch_collector
        self.execution_engine = execution_engine
        self.last_in_scope_matches: list[Match] = []
        self.last_candidates: list[BetCandidate] = []

    def run_once(self, session: Session, config: Mapping[str, Any]) -> list[Bet]:
        blocked_keywords = list(config.get("session", {}).get("blocked_keywords", []))
        matches = self.match_collector.fetch_matches(session)
        in_scope_matches = [
            match
            for match in matches
            if match_in_scope(match, session.tournament_keyword, blocked_keywords)
        ]
        self.last_in_scope_matches = in_scope_matches

        messages = self.twitch_collector.fetch_recent_messages(session.streamer_channel)
        streamer_signals = analyze_streamer_messages(messages)
        stake_pct = float(config.get("betting", {}).get("default_stake_pct", 0.35))

        created_bets: list[Bet] = []
        self.last_candidates = []

        for match in in_scope_matches:
            snapshots = self.odds_collector.fetch_odds(match)
            candidates = [
                build_candidate_from_snapshot(
                    snapshot=snapshot,
                    score=score_odds_snapshot(snapshot, streamer_signals),
                    threshold=session.score_threshold,
                )
                for snapshot in snapshots
            ]
            self.last_candidates.extend(candidates)

            selected_candidates = select_bets(
                candidates,
                max_bets_per_match=session.max_bets_per_match,
                threshold=session.score_threshold,
            )
            for candidate in selected_candidates:
                bet = self.execution_engine.handle_candidate(
                    candidate,
                    session.execution_mode,
                    stake_pct,
                )
                if bet is not None:
                    created_bets.append(bet)

        return created_bets
