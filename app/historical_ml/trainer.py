from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from app.historical_ml.dataset import (
    HISTORICAL_FEATURE_SCHEMA_VERSION,
    HISTORICAL_ML_FEATURE_NAMES,
    build_historical_ml_dataset,
)
from app.historical_ml.evaluation import (
    HistoricalModelMetrics,
    evaluate_model_partition,
)
from app.historical_ml.model import (
    DEFAULT_HISTORICAL_MODEL_PATH,
    HISTORICAL_MODEL_TYPE,
    HistoricalMatchWinModel,
    create_historical_model_pipeline,
    save_historical_model,
)
from app.historical_ml.split import (
    HistoricalMinimumRowsPolicy,
    HistoricalTemporalSplit,
    HistoricalTemporalSplitPolicy,
    HistoricalTrainingDataError,
    split_historical_dataset,
    validate_minimum_training_rows,
)
from app.history import (
    HistoricalFeaturePolicy,
    RecencyWeightingPolicy,
    build_historical_feature_dataset,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class HistoricalTrainingResult:
    trained: bool
    rows: int
    feature_count: int
    model_path: Path | None
    split: HistoricalTemporalSplit | None
    train_metrics: HistoricalModelMetrics | None
    validation_metrics: HistoricalModelMetrics | None
    test_metrics: HistoricalModelMetrics | None
    message: str


def train_historical_model_from_repository(
    repository: "SQLiteRepository",
    *,
    model_path: str | Path = DEFAULT_HISTORICAL_MODEL_PATH,
    decay_days: float = 90.0,
    split_policy: HistoricalTemporalSplitPolicy | None = None,
    minimum_rows_policy: HistoricalMinimumRowsPolicy | None = None,
) -> HistoricalTrainingResult:
    temporal_policy = split_policy or HistoricalTemporalSplitPolicy()
    minimum_policy = minimum_rows_policy or HistoricalMinimumRowsPolicy()
    feature_policy = HistoricalFeaturePolicy(
        recency=RecencyWeightingPolicy(decay_days=decay_days)
    )
    feature_rows = build_historical_feature_dataset(
        repository,
        policy=feature_policy,
    )
    dataset = build_historical_ml_dataset(feature_rows)
    split = split_historical_dataset(dataset, policy=temporal_policy)
    try:
        validate_minimum_training_rows(
            dataset,
            split,
            policy=minimum_policy,
        )
    except HistoricalTrainingDataError as exc:
        return HistoricalTrainingResult(
            trained=False,
            rows=len(dataset),
            feature_count=len(dataset.feature_names),
            model_path=None,
            split=split,
            train_metrics=None,
            validation_metrics=None,
            test_metrics=None,
            message=str(exc),
        )

    y_train = dataset.y[list(split.train_indices)]
    if len(set(y_train.tolist())) < 2:
        return HistoricalTrainingResult(
            trained=False,
            rows=len(dataset),
            feature_count=len(dataset.feature_names),
            model_path=None,
            split=split,
            train_metrics=None,
            validation_metrics=None,
            test_metrics=None,
            message="Need both Team A win and Team B win examples in train split.",
        )

    pipeline = create_historical_model_pipeline()
    pipeline.fit(dataset.x[list(split.train_indices)], y_train)

    train_metrics = evaluate_model_partition(
        _temporary_model(pipeline),
        dataset.x[list(split.train_indices)],
        y_train.tolist(),
    )
    validation_metrics = evaluate_model_partition(
        _temporary_model(pipeline),
        dataset.x[list(split.validation_indices)],
        dataset.y[list(split.validation_indices)].tolist(),
    )
    test_metrics = evaluate_model_partition(
        _temporary_model(pipeline),
        dataset.x[list(split.test_indices)],
        dataset.y[list(split.test_indices)].tolist(),
    )
    artifact = HistoricalMatchWinModel(
        pipeline=pipeline,
        feature_schema_version=HISTORICAL_FEATURE_SCHEMA_VERSION,
        feature_names=HISTORICAL_ML_FEATURE_NAMES,
        model_type=HISTORICAL_MODEL_TYPE,
        training_timestamp=datetime.now(timezone.utc),
        recency_decay_days=decay_days,
        temporal_split_policy=temporal_policy.as_dict(),
        minimum_rows_policy=minimum_policy.as_dict(),
        row_counts=split.row_counts(),
        evaluation_metrics={
            "train": train_metrics.as_dict(),
            "validation": validation_metrics.as_dict(),
            "test": test_metrics.as_dict(),
        },
    )
    saved_path = save_historical_model(artifact, model_path)
    return HistoricalTrainingResult(
        trained=True,
        rows=len(dataset),
        feature_count=len(dataset.feature_names),
        model_path=saved_path,
        split=split,
        train_metrics=train_metrics,
        validation_metrics=validation_metrics,
        test_metrics=test_metrics,
        message="Historical ML v2 model trained successfully.",
    )


def _temporary_model(pipeline: object) -> HistoricalMatchWinModel:
    return HistoricalMatchWinModel(
        pipeline=pipeline,
        feature_schema_version=HISTORICAL_FEATURE_SCHEMA_VERSION,
        feature_names=HISTORICAL_ML_FEATURE_NAMES,
        model_type=HISTORICAL_MODEL_TYPE,
        training_timestamp=datetime.now(timezone.utc),
        recency_decay_days=90.0,
        temporal_split_policy=HistoricalTemporalSplitPolicy().as_dict(),
        minimum_rows_policy=HistoricalMinimumRowsPolicy().as_dict(),
        row_counts={},
        evaluation_metrics={},
    )
