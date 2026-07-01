from typing import Protocol

from app.domain import BetCandidate, StreamerUtterance


class BetScorePredictor(Protocol):
    def is_available(self) -> bool:
        ...

    def predict_ml_score(
        self,
        candidate: BetCandidate,
        utterances: list[StreamerUtterance],
    ) -> float | None:
        ...


def apply_ml_score(
    candidate: BetCandidate,
    utterances: list[StreamerUtterance],
    predictor: BetScorePredictor | None,
    ml_weight: float = 0.5,
) -> BetCandidate:
    if ml_weight < 0 or ml_weight > 1:
        raise ValueError("ml_weight must be between 0 and 1")

    if predictor is None or not predictor.is_available():
        return candidate

    ml_score = predictor.predict_ml_score(candidate, utterances)
    if ml_score is None:
        return candidate

    rule_score = candidate.final_score
    hybrid_score = (1 - ml_weight) * rule_score + ml_weight * ml_score
    candidate.final_score = hybrid_score
    candidate.explanation = (
        f"{candidate.explanation}; ML score applied: "
        f"ml={ml_score:.2f}, rule={rule_score:.2f}, hybrid={hybrid_score:.2f}"
    )
    return candidate


def update_candidate_decision(
    candidate: BetCandidate,
    threshold: float,
) -> BetCandidate:
    if candidate.final_score >= threshold:
        candidate.decision = "bet"
    elif candidate.final_score >= threshold - 10:
        candidate.decision = "watch"
    else:
        candidate.decision = "skip"
    return candidate
