from datetime import timedelta

import pytest

from app.domain import BetCandidate, OddsSnapshot, StreamerUtterance
from app.edge import analyze_candidate_edge, build_edge_analyses
from tests.ml_test_helpers import NOW, make_utterance


class FixedProbabilityPredictor:
    def __init__(self, probability: float | None, available: bool = True) -> None:
        self.probability = probability
        self.available = available

    def is_available(self) -> bool:
        return self.available

    def predict_good_bet_probability(
        self,
        candidate: BetCandidate,
        utterances: list[StreamerUtterance],
    ) -> float | None:
        return self.probability


class SelectionProbabilityPredictor:
    def __init__(self, probabilities: dict[str, float]) -> None:
        self.probabilities = probabilities

    def is_available(self) -> bool:
        return True

    def predict_good_bet_probability(
        self,
        candidate: BetCandidate,
        utterances: list[StreamerUtterance],
    ) -> float | None:
        return self.probabilities.get(candidate.selection)


def test_analyze_candidate_edge_available_for_complete_two_way_market() -> None:
    candidate = make_edge_candidate(odds=2.0)
    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[
            make_snapshot("snapshot-a", "Team Spirit", 2.0),
            make_snapshot("snapshot-b", "PARIVISION", 2.0),
        ],
        utterances=[make_utterance()],
        predictor=FixedProbabilityPredictor(0.58),
    )

    assert analysis.status == "available"
    assert analysis.bookmaker == "fakebook"
    assert analysis.raw_implied_probability == pytest.approx(0.5)
    assert analysis.fair_market_probability == pytest.approx(0.5)
    assert analysis.model_probability == pytest.approx(0.58)
    assert analysis.probability_source == "ml_predict_proba"
    assert analysis.edge == pytest.approx(0.08)
    assert analysis.expected_value_units == pytest.approx(0.16)


def test_analyze_candidate_edge_keeps_fair_probability_when_model_unavailable() -> None:
    candidate = make_edge_candidate()
    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[
            make_snapshot("snapshot-a", "Team Spirit", 2.10),
            make_snapshot("snapshot-b", "PARIVISION", 1.80),
        ],
        utterances=[],
        predictor=FixedProbabilityPredictor(None),
    )

    assert analysis.status == "model_probability_unavailable"
    assert analysis.fair_market_probability is not None
    assert analysis.model_probability is None
    assert analysis.edge is None
    assert analysis.expected_value_units is None
    assert analysis.probability_source == "unavailable"


def test_analyze_candidate_edge_does_not_infer_missing_market_side() -> None:
    candidate = make_edge_candidate()

    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[make_snapshot("snapshot-a", "Team Spirit", 2.10)],
        utterances=[],
        predictor=FixedProbabilityPredictor(0.58),
    )

    assert analysis.status == "incomplete_market"
    assert analysis.fair_market_probability is None
    assert analysis.edge is None


def test_analyze_candidate_edge_does_not_mix_different_bookmakers() -> None:
    candidate = make_edge_candidate()

    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[
            make_snapshot("snapshot-a", "Team Spirit", 2.10, bookmaker="book-a"),
            make_snapshot("snapshot-b", "PARIVISION", 1.80, bookmaker="book-b"),
        ],
        utterances=[],
        predictor=FixedProbabilityPredictor(0.58),
    )

    assert analysis.status == "incomplete_market"
    assert analysis.edge is None


def test_analyze_candidate_edge_does_not_mix_different_timestamps() -> None:
    candidate = make_edge_candidate()

    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[
            make_snapshot("snapshot-a", "Team Spirit", 2.10),
            make_snapshot(
                "snapshot-b",
                "PARIVISION",
                1.80,
                created_at=NOW + timedelta(minutes=1),
            ),
        ],
        utterances=[],
        predictor=FixedProbabilityPredictor(0.58),
    )

    assert analysis.status == "incomplete_market"
    assert analysis.edge is None


def test_analyze_candidate_edge_handles_unsupported_market() -> None:
    candidate = make_edge_candidate(market="total_kills", selection="over")

    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[make_snapshot("snapshot-a", "over", 2.10, market="total_kills")],
        utterances=[],
        predictor=FixedProbabilityPredictor(0.58),
    )

    assert analysis.status == "unsupported_market"
    assert analysis.model_probability is None
    assert analysis.edge is None


def test_analyze_candidate_edge_handles_invalid_odds() -> None:
    candidate = make_edge_candidate(odds=1.0)

    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[],
        utterances=[],
        predictor=FixedProbabilityPredictor(0.58),
    )

    assert analysis.status == "invalid_odds"
    assert analysis.raw_implied_probability is None
    assert analysis.edge is None


def test_analyze_candidate_edge_supports_negative_edge() -> None:
    candidate = make_edge_candidate(odds=2.0)

    analysis = analyze_candidate_edge(
        candidate=candidate,
        snapshots=[
            make_snapshot("snapshot-a", "Team Spirit", 2.0),
            make_snapshot("snapshot-b", "PARIVISION", 2.0),
        ],
        utterances=[],
        predictor=FixedProbabilityPredictor(0.45),
    )

    assert analysis.status == "available"
    assert analysis.edge == pytest.approx(-0.05)


def test_build_edge_analyses_filters_by_bookmaker_and_min_edge() -> None:
    candidates = [
        make_edge_candidate(candidate_id="candidate-a", odds=2.0),
        make_edge_candidate(
            candidate_id="candidate-b",
            selection="PARIVISION",
            odds=2.0,
        ),
    ]
    snapshots = [
        make_snapshot("snapshot-a", "Team Spirit", 2.0),
        make_snapshot("snapshot-b", "PARIVISION", 2.0),
    ]

    analyses = build_edge_analyses(
        candidates=candidates,
        snapshots=snapshots,
        utterances_by_match={"match-1": []},
        predictor=SelectionProbabilityPredictor(
            {
                "Team Spirit": 0.58,
                "PARIVISION": 0.45,
            }
        ),
        bookmaker="fakebook",
        min_edge=0.05,
    )

    assert [analysis.selection for analysis in analyses] == ["Team Spirit"]


def make_edge_candidate(
    *,
    candidate_id: str = "candidate-1",
    market: str = "map_winner",
    selection: str = "Team Spirit",
    odds: float = 2.10,
) -> BetCandidate:
    return BetCandidate(
        id=candidate_id,
        session_id="session-1",
        match_id="match-1",
        market=market,
        selection=selection,
        line=None,
        odds=odds,
        phase="pre_match",
        market_score=25,
        phase_score=10,
        line_score=10,
        streamer_score=0,
        risk_score=5,
        final_score=50,
        decision="watch",
        explanation="candidate",
        created_at=NOW,
    )


def make_snapshot(
    snapshot_id: str,
    selection: str,
    odds: float,
    *,
    market: str = "map_winner",
    bookmaker: str = "fakebook",
    created_at=NOW,
) -> OddsSnapshot:
    return OddsSnapshot(
        id=snapshot_id,
        session_id="session-1",
        match_id="match-1",
        external_market_id="market-1",
        market=market,
        selection=selection,
        line=None,
        odds=odds,
        phase="pre_match",
        is_live=False,
        is_suspended=False,
        bookmaker=bookmaker,
        created_at=created_at,
    )
