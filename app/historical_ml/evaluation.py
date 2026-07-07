from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from sklearn.metrics import (  # type: ignore[import-untyped]
    accuracy_score,
    brier_score_loss,
    log_loss,
)

from app.historical_ml.dataset import (
    HISTORICAL_ML_FEATURE_NAMES,
    build_historical_ml_dataset,
)
from app.historical_ml.model import (
    DEFAULT_HISTORICAL_MODEL_PATH,
    HistoricalMatchWinModel,
    load_historical_model,
)
from app.historical_ml.split import (
    HistoricalTemporalSplit,
    HistoricalTemporalSplitPolicy,
    split_historical_dataset,
)
from app.history import (
    HistoricalFeaturePolicy,
    RecencyWeightingPolicy,
    build_historical_feature_dataset,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class HistoricalModelMetrics:
    row_count: int
    positive_label_rate: float
    average_predicted_probability: float
    brier_score: float
    log_loss: float
    accuracy: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "row_count": self.row_count,
            "positive_label_rate": self.positive_label_rate,
            "average_predicted_probability": (
                self.average_predicted_probability
            ),
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "accuracy": self.accuracy,
        }


@dataclass(frozen=True)
class HistoricalCurrentEvaluationResult:
    evaluated: bool
    rows: int
    feature_count: int
    split: HistoricalTemporalSplit | None
    train_metrics: HistoricalModelMetrics | None
    validation_metrics: HistoricalModelMetrics | None
    test_metrics: HistoricalModelMetrics | None
    recorded_metrics: Mapping[str, Mapping[str, object]]
    message: str


def evaluate_probabilities(
    y_true: Sequence[int],
    probabilities: Sequence[float],
) -> HistoricalModelMetrics:
    y = np.asarray(y_true, dtype=np.int_)
    predicted_probabilities = np.asarray(probabilities, dtype=np.float64)
    if len(y) == 0:
        raise ValueError("cannot evaluate an empty partition")
    if len(y) != len(predicted_probabilities):
        raise ValueError("labels and probabilities must have the same length")
    for probability in predicted_probabilities:
        if not math.isfinite(float(probability)):
            raise ValueError("predicted probabilities must be finite")
        if probability < 0 or probability > 1:
            raise ValueError("predicted probabilities must be in [0, 1]")

    predicted_labels = (predicted_probabilities >= 0.5).astype(int)
    return HistoricalModelMetrics(
        row_count=len(y),
        positive_label_rate=float(y.mean()),
        average_predicted_probability=float(predicted_probabilities.mean()),
        brier_score=float(brier_score_loss(y, predicted_probabilities)),
        log_loss=float(log_loss(y, predicted_probabilities, labels=[0, 1])),
        accuracy=float(accuracy_score(y, predicted_labels)),
    )


def evaluate_model_partition(
    model: HistoricalMatchWinModel,
    x_rows: Sequence[Sequence[float]] | np.ndarray,
    y_true: Sequence[int],
) -> HistoricalModelMetrics:
    probabilities = model.predict_team_a_probabilities(x_rows)
    return evaluate_probabilities(y_true, probabilities)


def evaluate_historical_model_from_repository(
    repository: "SQLiteRepository",
    *,
    model_path: str | Path = DEFAULT_HISTORICAL_MODEL_PATH,
    decay_days: float = 90.0,
) -> HistoricalCurrentEvaluationResult:
    model = load_historical_model(model_path)
    feature_policy = HistoricalFeaturePolicy(
        recency=RecencyWeightingPolicy(decay_days=decay_days)
    )
    feature_rows = build_historical_feature_dataset(
        repository,
        policy=feature_policy,
    )
    dataset = build_historical_ml_dataset(feature_rows)
    split_policy = HistoricalTemporalSplitPolicy.from_mapping(
        model.temporal_split_policy
    )
    split = split_historical_dataset(dataset, policy=split_policy)
    if not split.train_indices or not split.validation_indices or not split.test_indices:
        return HistoricalCurrentEvaluationResult(
            evaluated=False,
            rows=len(dataset),
            feature_count=len(HISTORICAL_ML_FEATURE_NAMES),
            split=split,
            train_metrics=None,
            validation_metrics=None,
            test_metrics=None,
            recorded_metrics=model.evaluation_metrics,
            message="Not enough current historical rows to evaluate all partitions.",
        )

    return HistoricalCurrentEvaluationResult(
        evaluated=True,
        rows=len(dataset),
        feature_count=len(HISTORICAL_ML_FEATURE_NAMES),
        split=split,
        train_metrics=_evaluate_indices(model, dataset.x, dataset.y, split.train_indices),
        validation_metrics=_evaluate_indices(
            model,
            dataset.x,
            dataset.y,
            split.validation_indices,
        ),
        test_metrics=_evaluate_indices(model, dataset.x, dataset.y, split.test_indices),
        recorded_metrics=model.evaluation_metrics,
        message="Current historical dataset evaluation complete.",
    )


def _evaluate_indices(
    model: HistoricalMatchWinModel,
    x: np.ndarray,
    y: np.ndarray,
    indices: tuple[int, ...],
) -> HistoricalModelMetrics:
    return evaluate_model_partition(
        model,
        x[list(indices)],
        y[list(indices)].tolist(),
    )
