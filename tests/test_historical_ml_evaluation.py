import pytest

from app.historical_ml import evaluate_probabilities


def test_evaluation_metrics_for_perfect_predictions() -> None:
    metrics = evaluate_probabilities([0, 1], [0.0, 1.0])

    assert metrics.row_count == 2
    assert metrics.positive_label_rate == 0.5
    assert metrics.average_predicted_probability == 0.5
    assert metrics.brier_score == 0.0
    assert metrics.log_loss == pytest.approx(0.0)
    assert metrics.accuracy == 1.0


def test_evaluation_rejects_invalid_probabilities() -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        evaluate_probabilities([1], [1.2])
