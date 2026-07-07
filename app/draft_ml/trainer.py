from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.draft_ml.dataset import (
    DRAFT_CATEGORICAL_FEATURE_NAMES,
    DRAFT_FEATURE_NAMES,
    DRAFT_FEATURE_SCHEMA_VERSION,
    DRAFT_PREDICTION_MODE,
    build_draft_map_dataset,
)
from app.draft_ml.model import (
    DEFAULT_DRAFT_MODEL_PATH,
    DRAFT_HERO_IDENTITY_NAMESPACE,
    DRAFT_HISTORY_CUTOFF_SEMANTICS,
    DRAFT_MODEL_TYPE,
    DRAFT_PATCH_SEMANTICS,
    DRAFT_SOURCE_PROVIDER,
    DraftMapWinModel,
    catboost_categorical_indices,
    create_draft_catboost_model,
    save_draft_model,
)
from app.draft_ml.split import (
    DraftMinimumRowsPolicy,
    DraftTemporalSplit,
    DraftTemporalSplitPolicy,
    DraftTrainingDataError,
    split_draft_dataset,
    validate_minimum_draft_training_rows,
)
from app.historical_ml.diagnostics import (
    CATBOOST_EARLY_STOPPING_ROUNDS,
    CatBoostCandidateConfig,
    catboost_base_params,
)
from app.historical_ml.evaluation import HistoricalModelMetrics, evaluate_probabilities
from app.history import DEFAULT_HISTORICAL_COMPETITION_SCOPE

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class DraftCandidateResult:
    config: CatBoostCandidateConfig
    train_metrics: HistoricalModelMetrics
    validation_metrics: HistoricalModelMetrics
    test_metrics: HistoricalModelMetrics
    model: Any


@dataclass(frozen=True)
class DraftTrainingResult:
    trained: bool
    rows: int
    categorical_feature_count: int
    numeric_feature_count: int
    split: DraftTemporalSplit | None
    selected_candidate: DraftCandidateResult | None
    model_path: Path | None
    message: str


def train_draft_model_from_repository(
    repository: "SQLiteRepository",
    *,
    model_path: str | Path = DEFAULT_DRAFT_MODEL_PATH,
    split_policy: DraftTemporalSplitPolicy | None = None,
    minimum_rows_policy: DraftMinimumRowsPolicy | None = None,
) -> DraftTrainingResult:
    temporal_policy = split_policy or DraftTemporalSplitPolicy()
    minimum_policy = minimum_rows_policy or DraftMinimumRowsPolicy()
    dataset = build_draft_map_dataset(repository)
    split = split_draft_dataset(dataset, policy=temporal_policy)
    try:
        validate_minimum_draft_training_rows(dataset, split, policy=minimum_policy)
    except DraftTrainingDataError as exc:
        return DraftTrainingResult(
            trained=False,
            rows=len(dataset),
            categorical_feature_count=len(DRAFT_CATEGORICAL_FEATURE_NAMES),
            numeric_feature_count=len(dataset.numeric_feature_names),
            split=split,
            selected_candidate=None,
            model_path=None,
            message=str(exc),
        )
    y_train = dataset.y[list(split.train_indices)]
    if len(set(y_train.tolist())) < 2:
        return DraftTrainingResult(
            trained=False,
            rows=len(dataset),
            categorical_feature_count=len(DRAFT_CATEGORICAL_FEATURE_NAMES),
            numeric_feature_count=len(dataset.numeric_feature_names),
            split=split,
            selected_candidate=None,
            model_path=None,
            message="Need both Team A win and Team B win examples in draft train split.",
        )

    candidates = []
    for depth in (4, 6):
        for l2_leaf_reg in (3.0, 10.0):
            config = CatBoostCandidateConfig(
                decay_days=0.0,
                depth=depth,
                l2_leaf_reg=l2_leaf_reg,
            )
            params = catboost_base_params(config)
            model = create_draft_catboost_model(params)
            model.fit(
                dataset.x.iloc[list(split.train_indices)],
                y_train,
                cat_features=list(catboost_categorical_indices()),
                eval_set=(
                    dataset.x.iloc[list(split.validation_indices)],
                    dataset.y[list(split.validation_indices)],
                ),
                use_best_model=True,
                early_stopping_rounds=CATBOOST_EARLY_STOPPING_ROUNDS,
            )
            candidates.append(
                DraftCandidateResult(
                    config=config,
                    train_metrics=_metrics(
                        model,
                        dataset.x.iloc[list(split.train_indices)],
                        dataset.y[list(split.train_indices)].tolist(),
                    ),
                    validation_metrics=_metrics(
                        model,
                        dataset.x.iloc[list(split.validation_indices)],
                        dataset.y[list(split.validation_indices)].tolist(),
                    ),
                    test_metrics=_metrics(
                        model,
                        dataset.x.iloc[list(split.test_indices)],
                        dataset.y[list(split.test_indices)].tolist(),
                    ),
                    model=model,
                )
            )
    selected = _select_draft_candidate(candidates)
    if selected is None:
        return DraftTrainingResult(
            trained=False,
            rows=len(dataset),
            categorical_feature_count=len(DRAFT_CATEGORICAL_FEATURE_NAMES),
            numeric_feature_count=len(dataset.numeric_feature_names),
            split=split,
            selected_candidate=None,
            model_path=None,
            message="No draft CatBoost candidates were evaluated.",
        )
    artifact = DraftMapWinModel(
        model=selected.model,
        model_type=DRAFT_MODEL_TYPE,
        prediction_mode=DRAFT_PREDICTION_MODE,
        feature_schema_version=DRAFT_FEATURE_SCHEMA_VERSION,
        feature_names=DRAFT_FEATURE_NAMES,
        categorical_feature_names=DRAFT_CATEGORICAL_FEATURE_NAMES,
        draft_source_provider=DRAFT_SOURCE_PROVIDER,
        hero_identity_namespace=DRAFT_HERO_IDENTITY_NAMESPACE,
        competition_scope_policy=DEFAULT_HISTORICAL_COMPETITION_SCOPE.as_dict(),
        history_cutoff_semantics=DRAFT_HISTORY_CUTOFF_SEMANTICS,
        patch_semantics=DRAFT_PATCH_SEMANTICS,
        training_timestamp=datetime.now(timezone.utc),
        temporal_split_policy=temporal_policy.as_dict(),
        row_counts=split.row_counts(),
        evaluation_metrics={
            "train": selected.train_metrics.as_dict(),
            "validation": selected.validation_metrics.as_dict(),
            "test": selected.test_metrics.as_dict(),
        },
        catboost_params=catboost_base_params(selected.config),
    )
    saved_path = save_draft_model(artifact, model_path)
    return DraftTrainingResult(
        trained=True,
        rows=len(dataset),
        categorical_feature_count=len(DRAFT_CATEGORICAL_FEATURE_NAMES),
        numeric_feature_count=len(dataset.numeric_feature_names),
        split=split,
        selected_candidate=selected,
        model_path=saved_path,
        message="POST_DRAFT map model trained successfully.",
    )


def _metrics(
    model: Any,
    x_rows: Any,
    y_true: list[int],
) -> HistoricalModelMetrics:
    probabilities = tuple(float(value) for value in model.predict_proba(x_rows)[:, 1])
    return evaluate_probabilities(y_true, probabilities)


def _select_draft_candidate(
    candidates: list[DraftCandidateResult],
) -> DraftCandidateResult | None:
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda candidate: (
            candidate.validation_metrics.brier_score,
            candidate.validation_metrics.log_loss,
            -candidate.validation_metrics.accuracy,
            candidate.config.depth,
            candidate.config.l2_leaf_reg,
        ),
    )
