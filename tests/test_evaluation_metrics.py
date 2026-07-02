import pytest

from app.evaluation.metrics import (
    calculate_accuracy,
    calculate_average_profit_units,
    calculate_roi_pct,
)


def test_calculate_accuracy_counts_correct_predictions() -> None:
    accuracy = calculate_accuracy([1, 0, 1], [1, 1, 1])

    assert accuracy == pytest.approx(2 / 3)


def test_profit_metrics_handle_values() -> None:
    profit_units = [0.35, -0.35, 0.0]
    stake_units = [0.35, 0.35, 0.35]

    assert calculate_average_profit_units(profit_units) == pytest.approx(0.0)
    assert calculate_roi_pct(profit_units, stake_units) == pytest.approx(0.0)


def test_empty_metrics_do_not_fail() -> None:
    assert calculate_accuracy([], []) is None
    assert calculate_average_profit_units([]) == 0.0
    assert calculate_roi_pct([], []) == 0.0


def test_accuracy_requires_equal_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        calculate_accuracy([1], [1, 0])
