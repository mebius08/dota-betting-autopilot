import pytest

from app.domain import BetCandidate, StreamerUtterance
from app.scoring.hybrid_scorer import apply_ml_score
from tests.ml_test_helpers import make_candidate


class FakePredictor:
    def __init__(self, score: float | None, available: bool = True) -> None:
        self.score = score
        self.available = available

    def is_available(self) -> bool:
        return self.available

    def predict_ml_score(
        self,
        candidate: BetCandidate,
        utterances: list[StreamerUtterance],
    ) -> float | None:
        return self.score


def test_apply_ml_score_without_predictor_leaves_candidate_unchanged() -> None:
    candidate = make_candidate(final_score=50)

    result = apply_ml_score(candidate, [], None)

    assert result is candidate
    assert candidate.final_score == 50
    assert "ML score applied" not in candidate.explanation


def test_apply_ml_score_unavailable_keeps_candidate() -> None:
    candidate = make_candidate(final_score=50)

    apply_ml_score(candidate, [], FakePredictor(100, available=False))

    assert candidate.final_score == 50


def test_apply_ml_score_none_prediction_keeps_candidate() -> None:
    candidate = make_candidate(final_score=50)

    apply_ml_score(candidate, [], FakePredictor(None))

    assert candidate.final_score == 50


def test_apply_ml_score_can_raise_score() -> None:
    candidate = make_candidate(final_score=50)

    apply_ml_score(candidate, [], FakePredictor(100), ml_weight=0.5)

    assert candidate.final_score == 75
    assert "ML score applied" in candidate.explanation


def test_apply_ml_score_can_lower_score() -> None:
    candidate = make_candidate(final_score=80)

    apply_ml_score(candidate, [], FakePredictor(0), ml_weight=0.5)

    assert candidate.final_score == 40
    assert "ML score applied" in candidate.explanation


def test_apply_ml_score_updates_explanation() -> None:
    candidate = make_candidate(final_score=50)

    apply_ml_score(candidate, [], FakePredictor(100), ml_weight=0.5)

    assert "ML score applied" in candidate.explanation


@pytest.mark.parametrize("ml_weight", [-1.0, 2.0])
def test_apply_ml_score_validates_weight(ml_weight: float) -> None:
    with pytest.raises(ValueError, match="ml_weight"):
        apply_ml_score(make_candidate(), [], FakePredictor(100), ml_weight=ml_weight)
