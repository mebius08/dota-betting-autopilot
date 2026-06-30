from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.collectors import (
    MatchCollector,
    OddsCollector,
    StreamerSpeechCollector,
    match_in_scope,
)
from app.domain import (
    Bet,
    BetCandidate,
    Decision,
    Match,
    OddsSnapshot,
    Session,
    StreamerUtterance,
)
from app.execution import ExecutionEngine
from app.scoring import (
    ScoreBreakdown,
    map_raw_utterances_to_entities,
    score_odds_snapshot,
)
from app.scoring.bet_selector import select_bets
from app.storage import SQLiteRepository


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
        streamer_speech_collector: StreamerSpeechCollector,
        execution_engine: ExecutionEngine,
        repository: SQLiteRepository | None = None,
    ) -> None:
        self.match_collector = match_collector
        self.odds_collector = odds_collector
        self.streamer_speech_collector = streamer_speech_collector
        self.execution_engine = execution_engine
        self.repository = repository
        self.last_in_scope_matches: list[Match] = []
        self.last_candidates: list[BetCandidate] = []
        self.last_streamer_utterances: list[StreamerUtterance] = []

    def run_once(self, session: Session, config: Mapping[str, Any]) -> list[Bet]:
        blocked_keywords = list(config.get("session", {}).get("blocked_keywords", []))
        matches = self.match_collector.fetch_matches(session)
        in_scope_matches = [
            match
            for match in matches
            if match_in_scope(match, session.tournament_keyword, blocked_keywords)
        ]
        self.last_in_scope_matches = in_scope_matches

        raw_utterances = self.streamer_speech_collector.fetch_recent_utterances()
        stake_pct = float(config.get("betting", {}).get("default_stake_pct", 0.35))

        created_bets: list[Bet] = []
        self.last_candidates = []
        self.last_streamer_utterances = []

        for match in in_scope_matches:
            if self.repository is not None:
                self.repository.save_match(match)

            utterances = map_raw_utterances_to_entities(
                raw_utterances,
                session_id=session.id,
                match_id=match.id,
            )
            self.last_streamer_utterances.extend(utterances)
            if self.repository is not None:
                self.repository.save_streamer_utterances(utterances)

            snapshots = self.odds_collector.fetch_odds(match)
            if self.repository is not None:
                for snapshot in snapshots:
                    self.repository.save_odds_snapshot(snapshot)

            candidates = [
                build_candidate_from_snapshot(
                    snapshot=snapshot,
                    score=score_odds_snapshot(snapshot, utterances),
                    threshold=session.score_threshold,
                )
                for snapshot in snapshots
            ]
            self.last_candidates.extend(candidates)
            if self.repository is not None:
                for candidate in candidates:
                    self.repository.save_bet_candidate(candidate)

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
                    if self.repository is not None:
                        self.repository.save_bet(bet)
                    created_bets.append(bet)

        return created_bets
