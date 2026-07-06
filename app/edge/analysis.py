from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Literal, Protocol

from app.domain import BetCandidate, OddsSnapshot, StreamerUtterance
from app.edge.market_probability import (
    MarketProbabilityError,
    calculate_expected_value_units,
    calculate_probability_edge,
    calculate_two_way_market_probabilities,
    decimal_odds_to_implied_probability,
    validate_probability,
)


SUPPORTED_EDGE_MARKETS = ("map_winner",)

CandidateEdgeStatus = Literal[
    "available",
    "model_probability_unavailable",
    "incomplete_market",
    "invalid_odds",
    "unsupported_market",
]


class CandidateProbabilityPredictor(Protocol):
    def is_available(self) -> bool:
        ...

    def predict_good_bet_probability(
        self,
        candidate: BetCandidate,
        utterances: list[StreamerUtterance],
    ) -> float | None:
        ...


@dataclass(frozen=True)
class ModelProbability:
    probability: float | None
    source: str
    reason: str | None


@dataclass(frozen=True)
class CandidateEdgeAnalysis:
    match_id: str
    market: str
    selection: str
    bookmaker: str | None
    decimal_odds: float
    raw_implied_probability: float | None
    fair_market_probability: float | None
    model_probability: float | None
    probability_source: str
    edge: float | None
    expected_value_units: float | None
    status: CandidateEdgeStatus
    reason: str


def extract_model_probability(
    *,
    candidate: BetCandidate,
    utterances: list[StreamerUtterance],
    predictor: CandidateProbabilityPredictor | None,
) -> ModelProbability:
    if predictor is None or not predictor.is_available():
        return ModelProbability(
            probability=None,
            source="unavailable",
            reason="model probability unavailable",
        )

    try:
        probability = predictor.predict_good_bet_probability(candidate, utterances)
    except Exception:
        return ModelProbability(
            probability=None,
            source="unavailable",
            reason="model probability unavailable",
        )

    if probability is None:
        return ModelProbability(
            probability=None,
            source="unavailable",
            reason="model probability unavailable",
        )

    try:
        parsed = validate_probability(probability, "model_probability")
    except MarketProbabilityError:
        return ModelProbability(
            probability=None,
            source="unavailable",
            reason="model probability unavailable",
        )

    return ModelProbability(
        probability=parsed,
        source="ml_predict_proba",
        reason=None,
    )


def analyze_candidate_edge(
    *,
    candidate: BetCandidate,
    snapshots: Sequence[OddsSnapshot],
    utterances: list[StreamerUtterance],
    predictor: CandidateProbabilityPredictor | None = None,
    bookmaker: str | None = None,
) -> CandidateEdgeAnalysis:
    try:
        raw_probability = decimal_odds_to_implied_probability(candidate.odds)
    except MarketProbabilityError as exc:
        return _analysis(
            candidate=candidate,
            bookmaker=None,
            raw_implied_probability=None,
            fair_market_probability=None,
            model_probability=None,
            probability_source="unavailable",
            edge=None,
            expected_value_units=None,
            status="invalid_odds",
            reason=str(exc),
        )

    if candidate.market not in SUPPORTED_EDGE_MARKETS:
        return _analysis(
            candidate=candidate,
            bookmaker=None,
            raw_implied_probability=raw_probability,
            fair_market_probability=None,
            model_probability=None,
            probability_source="unavailable",
            edge=None,
            expected_value_units=None,
            status="unsupported_market",
            reason=f"unsupported market: {candidate.market}",
        )

    candidate_snapshot = _find_candidate_snapshot(candidate, snapshots, bookmaker)
    if candidate_snapshot is None:
        return _analysis(
            candidate=candidate,
            bookmaker=bookmaker,
            raw_implied_probability=raw_probability,
            fair_market_probability=None,
            model_probability=None,
            probability_source="unavailable",
            edge=None,
            expected_value_units=None,
            status="incomplete_market",
            reason="complete two-way market pair not found",
        )

    pair = _find_two_way_pair(candidate_snapshot, snapshots)
    if pair is None:
        return _analysis(
            candidate=candidate,
            bookmaker=candidate_snapshot.bookmaker,
            raw_implied_probability=raw_probability,
            fair_market_probability=None,
            model_probability=None,
            probability_source="unavailable",
            edge=None,
            expected_value_units=None,
            status="incomplete_market",
            reason="complete two-way market pair not found",
        )

    other_snapshot = pair
    try:
        market_probability = calculate_two_way_market_probabilities(
            selection_a=candidate_snapshot.selection,
            odds_a=candidate_snapshot.odds,
            selection_b=other_snapshot.selection,
            odds_b=other_snapshot.odds,
        )
    except MarketProbabilityError as exc:
        return _analysis(
            candidate=candidate,
            bookmaker=candidate_snapshot.bookmaker,
            raw_implied_probability=raw_probability,
            fair_market_probability=None,
            model_probability=None,
            probability_source="unavailable",
            edge=None,
            expected_value_units=None,
            status="invalid_odds",
            reason=str(exc),
        )

    fair_probability = market_probability.fair_probability_a
    model_probability = extract_model_probability(
        candidate=candidate,
        utterances=utterances,
        predictor=predictor,
    )
    if model_probability.probability is None:
        return _analysis(
            candidate=candidate,
            bookmaker=candidate_snapshot.bookmaker,
            raw_implied_probability=raw_probability,
            fair_market_probability=fair_probability,
            model_probability=None,
            probability_source=model_probability.source,
            edge=None,
            expected_value_units=None,
            status="model_probability_unavailable",
            reason=model_probability.reason or "model probability unavailable",
        )

    edge = calculate_probability_edge(
        model_probability=model_probability.probability,
        fair_market_probability=fair_probability,
    )
    expected_value = calculate_expected_value_units(
        model_probability=model_probability.probability,
        decimal_odds=candidate_snapshot.odds,
    )
    return _analysis(
        candidate=candidate,
        bookmaker=candidate_snapshot.bookmaker,
        raw_implied_probability=raw_probability,
        fair_market_probability=fair_probability,
        model_probability=model_probability.probability,
        probability_source=model_probability.source,
        edge=edge,
        expected_value_units=expected_value,
        status="available",
        reason="edge available",
    )


def build_edge_analyses(
    *,
    candidates: Sequence[BetCandidate],
    snapshots: Sequence[OddsSnapshot],
    utterances_by_match: Mapping[str, list[StreamerUtterance]],
    predictor: CandidateProbabilityPredictor | None = None,
    match_id: str | None = None,
    bookmaker: str | None = None,
    min_edge: float | None = None,
    limit: int | None = None,
) -> list[CandidateEdgeAnalysis]:
    analyses: list[CandidateEdgeAnalysis] = []
    for candidate in candidates:
        if match_id is not None and candidate.match_id != match_id:
            continue
        if bookmaker is not None and not _candidate_has_bookmaker_snapshot(
            candidate,
            snapshots,
            bookmaker,
        ):
            continue

        analysis = analyze_candidate_edge(
            candidate=candidate,
            snapshots=snapshots,
            utterances=utterances_by_match.get(candidate.match_id, []),
            predictor=predictor,
            bookmaker=bookmaker,
        )
        if min_edge is not None and (
            analysis.edge is None or analysis.edge < min_edge
        ):
            continue

        analyses.append(analysis)
        if limit is not None and len(analyses) >= limit:
            break

    return analyses


def _analysis(
    *,
    candidate: BetCandidate,
    bookmaker: str | None,
    raw_implied_probability: float | None,
    fair_market_probability: float | None,
    model_probability: float | None,
    probability_source: str,
    edge: float | None,
    expected_value_units: float | None,
    status: CandidateEdgeStatus,
    reason: str,
) -> CandidateEdgeAnalysis:
    return CandidateEdgeAnalysis(
        match_id=candidate.match_id,
        market=candidate.market,
        selection=candidate.selection,
        bookmaker=bookmaker,
        decimal_odds=float(candidate.odds),
        raw_implied_probability=raw_implied_probability,
        fair_market_probability=fair_market_probability,
        model_probability=model_probability,
        probability_source=probability_source,
        edge=edge,
        expected_value_units=expected_value_units,
        status=status,
        reason=reason,
    )


def _find_candidate_snapshot(
    candidate: BetCandidate,
    snapshots: Sequence[OddsSnapshot],
    bookmaker: str | None,
) -> OddsSnapshot | None:
    matches = [
        snapshot
        for snapshot in snapshots
        if _snapshot_matches_candidate(snapshot, candidate)
        and (bookmaker is None or snapshot.bookmaker == bookmaker)
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _find_two_way_pair(
    candidate_snapshot: OddsSnapshot,
    snapshots: Sequence[OddsSnapshot],
) -> OddsSnapshot | None:
    same_market_time = [
        snapshot
        for snapshot in snapshots
        if snapshot.match_id == candidate_snapshot.match_id
        and snapshot.market == candidate_snapshot.market
        and snapshot.bookmaker == candidate_snapshot.bookmaker
        and snapshot.created_at == candidate_snapshot.created_at
        and snapshot.line == candidate_snapshot.line
    ]
    selections = {snapshot.selection for snapshot in same_market_time}
    if len(same_market_time) != 2 or len(selections) != 2:
        return None

    for snapshot in same_market_time:
        if snapshot.id != candidate_snapshot.id:
            return snapshot
    return None


def _candidate_has_bookmaker_snapshot(
    candidate: BetCandidate,
    snapshots: Sequence[OddsSnapshot],
    bookmaker: str,
) -> bool:
    return any(
        _snapshot_matches_candidate(snapshot, candidate)
        and snapshot.bookmaker == bookmaker
        for snapshot in snapshots
    )


def _snapshot_matches_candidate(
    snapshot: OddsSnapshot,
    candidate: BetCandidate,
) -> bool:
    return (
        snapshot.match_id == candidate.match_id
        and snapshot.market == candidate.market
        and snapshot.selection == candidate.selection
        and snapshot.line == candidate.line
        and math.isclose(snapshot.odds, candidate.odds, rel_tol=1e-12, abs_tol=1e-12)
    )
