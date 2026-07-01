from datetime import datetime, timezone

from app.domain import Bet, BetCandidate, BetResult, BetStatus, Match, Session
from app.domain import StreamerUtterance
from app.storage import SQLiteRepository


NOW = datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc)


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
        created_at=NOW,
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
        status="finished",
        start_time=NOW,
        external_id=f"{match_id}-external",
    )


def make_candidate(
    session_id: str = "session-1",
    match_id: str = "match-1",
    candidate_id: str = "candidate-1",
    final_score: float = 67.0,
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
        final_score=final_score,
        decision="bet",
        explanation="A-tier market",
        created_at=NOW,
    )


def make_bet(
    session_id: str,
    match_id: str,
    candidate_id: str,
    bet_id: str,
    result: BetResult = "win",
    status: BetStatus = "settled",
    profit_units: float = 0.32,
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
        created_at=NOW,
        settled_at=NOW if status == "settled" else None,
    )


def make_utterance(
    session_id: str = "session-1",
    match_id: str = "match-1",
    text: str = "over kills looks good",
    signal_type: str | None = "over_kills",
    strength: float = 7.0,
    confidence: float = 0.8,
    hype_flag: bool = False,
) -> StreamerUtterance:
    return StreamerUtterance(
        id=f"utterance-{session_id}-{match_id}-{text}",
        session_id=session_id,
        match_id=match_id,
        source="manual_transcript",
        text=text,
        detected_market="total_kills",
        detected_selection="over",
        detected_team=None,
        signal_type=signal_type,
        strength=strength,
        confidence=confidence,
        hype_flag=hype_flag,
        created_at=NOW,
    )


def save_training_bundle(
    repository: SQLiteRepository,
    index: int,
    result: BetResult,
) -> None:
    session_id = f"session-{index}"
    match_id = f"match-{index}"
    candidate_id = f"candidate-{index}"
    repository.save_session(make_session(session_id))
    repository.save_match(make_match(session_id, match_id))
    repository.save_bet_candidate(
        make_candidate(session_id, match_id, candidate_id, final_score=55 + index)
    )
    repository.save_bet(
        make_bet(
            session_id=session_id,
            match_id=match_id,
            candidate_id=candidate_id,
            bet_id=f"bet-{index}",
            result=result,
            profit_units=0.35 if result == "win" else -0.35,
        )
    )
    repository.save_streamer_utterance(
        make_utterance(
            session_id=session_id,
            match_id=match_id,
            strength=float(index % 10),
            confidence=0.5 + (index % 5) / 10,
            hype_flag=index % 3 == 0,
        )
    )
