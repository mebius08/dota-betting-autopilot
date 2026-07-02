from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ClassificationMetrics:
    total_records: int
    positive_records: int
    negative_records: int
    predicted_positive_records: int
    accuracy: float | None
    total_profit_units: float
    average_profit_units: float
    roi_pct: float


def calculate_accuracy(
    actual_labels: Sequence[int],
    predicted_labels: Sequence[int],
) -> float | None:
    if len(actual_labels) != len(predicted_labels):
        raise ValueError("actual_labels and predicted_labels must have the same length")

    if not actual_labels:
        return None

    correct = sum(
        1
        for actual_label, predicted_label in zip(actual_labels, predicted_labels)
        if actual_label == predicted_label
    )
    return correct / len(actual_labels)


def calculate_average_profit_units(profit_units: Sequence[float]) -> float:
    if not profit_units:
        return 0.0
    return sum(profit_units) / len(profit_units)


def calculate_roi_pct(
    profit_units: Sequence[float],
    stake_units: Sequence[float],
) -> float:
    if len(profit_units) != len(stake_units):
        raise ValueError("profit_units and stake_units must have the same length")

    total_stake = sum(stake_units)
    if total_stake == 0:
        return 0.0
    return sum(profit_units) / total_stake * 100


def build_classification_metrics(
    actual_labels: Sequence[int],
    predicted_labels: Sequence[int],
    selected_profit_units: Sequence[float],
    selected_stake_units: Sequence[float],
) -> ClassificationMetrics:
    accuracy = calculate_accuracy(actual_labels, predicted_labels)
    positive_records = sum(1 for label in actual_labels if label == 1)
    negative_records = sum(1 for label in actual_labels if label == 0)
    predicted_positive_records = sum(1 for label in predicted_labels if label == 1)

    return ClassificationMetrics(
        total_records=len(actual_labels),
        positive_records=positive_records,
        negative_records=negative_records,
        predicted_positive_records=predicted_positive_records,
        accuracy=accuracy,
        total_profit_units=sum(selected_profit_units),
        average_profit_units=calculate_average_profit_units(selected_profit_units),
        roi_pct=calculate_roi_pct(selected_profit_units, selected_stake_units),
    )
