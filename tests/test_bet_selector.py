from datetime import datetime, timezone

from app.domain import BetCandidate, Decision
from app.scoring import select_bets


def make_candidate(
    *,
    candidate_id: str,
    final_score: float,
    decision: Decision = "bet",
    market: str = "total_kills",
    selection: str = "over",
    line: float | None = 48.5,
) -> BetCandidate:
    return BetCandidate(
        id=candidate_id,
        session_id="session-1",
        match_id="match-1",
        market=market,
        selection=selection,
        line=line,
        odds=1.9,
        phase="after_draft",
        market_score=25,
        phase_score=20,
        line_score=10,
        streamer_score=4,
        risk_score=5,
        final_score=final_score,
        decision=decision,
        explanation="test candidate",
        created_at=datetime.now(timezone.utc),
    )


def test_select_bets_returns_top_n_by_score() -> None:
    candidates = [
        make_candidate(candidate_id="low", final_score=65, line=46.5),
        make_candidate(candidate_id="top", final_score=80, line=47.5),
        make_candidate(candidate_id="mid", final_score=70, line=48.5),
    ]

    selected = select_bets(candidates, max_bets_per_match=2, threshold=62)

    assert [candidate.id for candidate in selected] == ["top", "mid"]


def test_select_bets_rejects_below_threshold() -> None:
    candidates = [
        make_candidate(candidate_id="watch", final_score=61),
        make_candidate(candidate_id="skip", final_score=40, decision="skip"),
    ]

    assert select_bets(candidates, max_bets_per_match=3, threshold=62) == []


def test_select_bets_does_not_exceed_max_per_match() -> None:
    candidates = [
        make_candidate(candidate_id="one", final_score=80, line=45.5),
        make_candidate(candidate_id="two", final_score=79, line=46.5),
        make_candidate(candidate_id="three", final_score=78, line=47.5),
    ]

    selected = select_bets(candidates, max_bets_per_match=1, threshold=62)

    assert len(selected) == 1
    assert selected[0].id == "one"


def test_select_bets_removes_duplicate_market_line() -> None:
    candidates = [
        make_candidate(candidate_id="one", final_score=80, line=48.5),
        make_candidate(candidate_id="two", final_score=79, line=48.5),
    ]

    selected = select_bets(candidates, max_bets_per_match=3, threshold=62)

    assert [candidate.id for candidate in selected] == ["one"]
