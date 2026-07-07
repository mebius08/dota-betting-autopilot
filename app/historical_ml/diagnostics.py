from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from app.historical_ml.dataset import (
    HistoricalMatchDataset,
    build_historical_ml_dataset,
)
from app.historical_ml.evaluation import (
    HistoricalModelMetrics,
    evaluate_probabilities,
)
from app.historical_ml.model import (
    DEFAULT_HISTORICAL_MODEL_PATH,
    create_historical_model_pipeline,
)
from app.historical_ml.split import (
    HistoricalTemporalSplit,
    HistoricalTemporalSplitPolicy,
    split_historical_dataset,
    split_timestamp_ranges,
)
from app.history import (
    DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    HISTORICAL_COMPETITION_CLASSIFICATION_PRECEDENCE,
    HistoricalCompetitionFamily,
    HistoricalFeaturePolicy,
    RecencyWeightingPolicy,
    build_historical_feature_dataset,
    classify_historical_competition_family,
)
from app.tournaments import CompetitiveStage

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


CATBOOST_RANDOM_SEED = 42
CATBOOST_LEARNING_RATE = 0.03
CATBOOST_ITERATIONS = 300
CATBOOST_EARLY_STOPPING_ROUNDS = 30
CATBOOST_DECAY_CANDIDATES: tuple[float, ...] = (30.0, 60.0, 90.0, 120.0, 180.0)


@dataclass(frozen=True)
class CatBoostCandidateConfig:
    decay_days: float
    depth: int
    l2_leaf_reg: float
    learning_rate: float = CATBOOST_LEARNING_RATE
    iterations: int = CATBOOST_ITERATIONS

    def params(self) -> dict[str, object]:
        return {
            "loss_function": "Logloss",
            "random_seed": CATBOOST_RANDOM_SEED,
            "verbose": False,
            "allow_writing_files": False,
            "learning_rate": self.learning_rate,
            "iterations": self.iterations,
            "depth": self.depth,
            "l2_leaf_reg": self.l2_leaf_reg,
        }


@dataclass(frozen=True)
class ProbabilityBucket:
    lower: float
    upper: float
    rows: int
    mean_predicted_probability: float
    observed_positive_rate: float
    absolute_calibration_gap: float


@dataclass(frozen=True)
class ChronologicalBucket:
    rows: int
    timestamp_start: datetime
    timestamp_end: datetime
    metrics: HistoricalModelMetrics


@dataclass(frozen=True)
class GroupMetrics:
    name: str
    rows: int
    metrics: HistoricalModelMetrics


@dataclass(frozen=True)
class FeatureDrift:
    feature_name: str
    standardized_mean_shift: float
    train_mean: float
    other_mean: float
    train_std: float


@dataclass(frozen=True)
class ModelDiagnostics:
    train_metrics: HistoricalModelMetrics
    validation_metrics: HistoricalModelMetrics
    test_metrics: HistoricalModelMetrics
    test_probability_buckets: tuple[ProbabilityBucket, ...]
    test_chronological_buckets: tuple[ChronologicalBucket, ...]
    test_family_metrics: tuple[GroupMetrics, ...]
    test_stage_metrics: tuple[GroupMetrics, ...]


@dataclass(frozen=True)
class BaselineDiagnostics:
    name: str
    train_probability: float
    diagnostics: ModelDiagnostics


@dataclass(frozen=True)
class CatBoostCandidateDiagnostics:
    config: CatBoostCandidateConfig
    diagnostics: ModelDiagnostics
    feature_importance: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class HistoricalMLDiagnosticResult:
    evaluated: bool
    message: str
    rows: int
    feature_count: int
    split: HistoricalTemporalSplit | None
    timestamp_ranges: Mapping[str, tuple[datetime | None, datetime | None]]
    split_positive_label_rates: Mapping[str, float]
    baselines: tuple[BaselineDiagnostics, ...]
    logistic: ModelDiagnostics | None
    catboost_available: bool
    catboost_candidates: tuple[CatBoostCandidateDiagnostics, ...]
    selected_catboost: CatBoostCandidateDiagnostics | None
    validation_feature_drift: tuple[FeatureDrift, ...]
    test_feature_drift: tuple[FeatureDrift, ...]


@dataclass(frozen=True)
class _RowContext:
    competition_family: HistoricalCompetitionFamily
    stage: CompetitiveStage


class CatBoostUnavailableError(RuntimeError):
    pass


def diagnose_historical_ml_from_repository(
    repository: "SQLiteRepository",
    *,
    logistic_model_path: str | Path = DEFAULT_HISTORICAL_MODEL_PATH,
    split_policy: HistoricalTemporalSplitPolicy | None = None,
    decay_candidates: Sequence[float] = CATBOOST_DECAY_CANDIDATES,
) -> HistoricalMLDiagnosticResult:
    del logistic_model_path  # The diagnostic trains in memory and never overwrites artifacts.
    temporal_policy = split_policy or HistoricalTemporalSplitPolicy()
    reference_bundle = _build_bundle(
        repository,
        decay_days=90.0,
        split_policy=temporal_policy,
    )
    dataset = reference_bundle.dataset
    split = reference_bundle.split
    timestamp_ranges = split_timestamp_ranges(dataset, split)

    if not split.train_indices or not split.validation_indices or not split.test_indices:
        return HistoricalMLDiagnosticResult(
            evaluated=False,
            message="Not enough rows to build train/validation/test diagnostics.",
            rows=len(dataset),
            feature_count=len(dataset.feature_names),
            split=split,
            timestamp_ranges=timestamp_ranges,
            split_positive_label_rates={},
            baselines=(),
            logistic=None,
            catboost_available=_catboost_import_available(),
            catboost_candidates=(),
            selected_catboost=None,
            validation_feature_drift=(),
            test_feature_drift=(),
        )
    if len(set(dataset.y[list(split.train_indices)].tolist())) < 2:
        return HistoricalMLDiagnosticResult(
            evaluated=False,
            message="Need both Team A win and Team B win examples in train split.",
            rows=len(dataset),
            feature_count=len(dataset.feature_names),
            split=split,
            timestamp_ranges=timestamp_ranges,
            split_positive_label_rates=_split_label_rates(dataset, split),
            baselines=(),
            logistic=None,
            catboost_available=_catboost_import_available(),
            catboost_candidates=(),
            selected_catboost=None,
            validation_feature_drift=(),
            test_feature_drift=(),
        )

    baselines = (
        _baseline("CONSTANT_0_5", 0.5, reference_bundle),
        _train_label_prior_baseline(reference_bundle),
    )
    logistic = _fit_logistic_baseline(reference_bundle)
    validation_feature_drift = _top_feature_drift(
        dataset,
        train_indices=split.train_indices,
        other_indices=split.validation_indices,
    )
    test_feature_drift = _top_feature_drift(
        dataset,
        train_indices=split.train_indices,
        other_indices=split.test_indices,
    )

    catboost_available = _catboost_import_available()
    catboost_candidates: tuple[CatBoostCandidateDiagnostics, ...] = ()
    selected_catboost: CatBoostCandidateDiagnostics | None = None
    if catboost_available:
        catboost_candidates = rank_catboost_candidates(
            _evaluate_catboost_candidates(
                repository,
                reference_bundle=reference_bundle,
                split_policy=temporal_policy,
                decay_candidates=decay_candidates,
            )
        )
        selected_catboost = select_catboost_candidate(catboost_candidates)

    return HistoricalMLDiagnosticResult(
        evaluated=True,
        message="Historical ML diagnostics complete.",
        rows=len(dataset),
        feature_count=len(dataset.feature_names),
        split=split,
        timestamp_ranges=timestamp_ranges,
        split_positive_label_rates=_split_label_rates(dataset, split),
        baselines=baselines,
        logistic=logistic,
        catboost_available=catboost_available,
        catboost_candidates=catboost_candidates,
        selected_catboost=selected_catboost,
        validation_feature_drift=validation_feature_drift,
        test_feature_drift=test_feature_drift,
    )


def rank_catboost_candidates(
    candidates: Iterable[CatBoostCandidateDiagnostics],
) -> tuple[CatBoostCandidateDiagnostics, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.diagnostics.validation_metrics.brier_score,
                candidate.diagnostics.validation_metrics.log_loss,
                -candidate.diagnostics.validation_metrics.accuracy,
                candidate.config.decay_days,
                candidate.config.depth,
                candidate.config.l2_leaf_reg,
                candidate.config.learning_rate,
                candidate.config.iterations,
            ),
        )
    )


def select_catboost_candidate(
    candidates: Iterable[CatBoostCandidateDiagnostics],
) -> CatBoostCandidateDiagnostics | None:
    ranked = rank_catboost_candidates(candidates)
    if not ranked:
        return None
    return ranked[0]


def catboost_base_params(config: CatBoostCandidateConfig) -> dict[str, object]:
    return config.params()


@dataclass(frozen=True)
class _DiagnosticBundle:
    dataset: HistoricalMatchDataset
    split: HistoricalTemporalSplit
    row_contexts: tuple[_RowContext, ...]


def _build_bundle(
    repository: "SQLiteRepository",
    *,
    decay_days: float,
    split_policy: HistoricalTemporalSplitPolicy,
) -> _DiagnosticBundle:
    feature_rows = tuple(
        sorted(
            build_historical_feature_dataset(
                repository,
                policy=HistoricalFeaturePolicy(
                    recency=RecencyWeightingPolicy(decay_days=decay_days)
                ),
                competition_scope_policy=DEFAULT_HISTORICAL_COMPETITION_SCOPE,
            ),
            key=lambda row: (
                row.feature_row.prediction_timestamp,
                row.feature_row.source,
                row.feature_row.source_match_id,
            ),
        )
    )
    dataset = build_historical_ml_dataset(feature_rows)
    matches_by_id = {
        match.id: match
        for match in repository.list_historical_matches()
    }
    contexts: list[_RowContext] = []
    for row in feature_rows:
        match = (
            matches_by_id.get(row.feature_row.target_match_id)
            if row.feature_row.target_match_id is not None
            else None
        )
        contexts.append(
            _RowContext(
                competition_family=(
                    classify_historical_competition_family(match)
                    if match is not None
                    else HistoricalCompetitionFamily.UNKNOWN
                ),
                stage=(
                    match.competitive_stage
                    if match is not None
                    else row.feature_row.competitive_stage
                ),
            )
        )

    return _DiagnosticBundle(
        dataset=dataset,
        split=split_historical_dataset(dataset, policy=split_policy),
        row_contexts=tuple(contexts),
    )


def _baseline(
    name: str,
    probability: float,
    bundle: _DiagnosticBundle,
) -> BaselineDiagnostics:
    split = bundle.split
    return BaselineDiagnostics(
        name=name,
        train_probability=probability,
        diagnostics=_diagnostics_from_probabilities(
            bundle,
            train_probabilities=[probability] * split.train_rows,
            validation_probabilities=[probability] * split.validation_rows,
            test_probabilities=[probability] * split.test_rows,
        ),
    )


def _train_label_prior_baseline(
    bundle: _DiagnosticBundle,
) -> BaselineDiagnostics:
    split = bundle.split
    y_train = bundle.dataset.y[list(split.train_indices)]
    probability = float(y_train.mean())
    return _baseline("TRAIN_LABEL_PRIOR", probability, bundle)


def _fit_logistic_baseline(bundle: _DiagnosticBundle) -> ModelDiagnostics:
    dataset = bundle.dataset
    split = bundle.split
    pipeline = create_historical_model_pipeline()
    pipeline.fit(
        dataset.x[list(split.train_indices)],
        dataset.y[list(split.train_indices)],
    )
    return _diagnostics_from_model(bundle, pipeline)


def _evaluate_catboost_candidates(
    repository: "SQLiteRepository",
    *,
    reference_bundle: _DiagnosticBundle,
    split_policy: HistoricalTemporalSplitPolicy,
    decay_candidates: Sequence[float],
) -> tuple[CatBoostCandidateDiagnostics, ...]:
    classifier_cls = _catboost_classifier()
    candidates: list[CatBoostCandidateDiagnostics] = []
    reference_keys = _target_identity_keys(reference_bundle.dataset)
    for decay_days in decay_candidates:
        bundle = _build_bundle(
            repository,
            decay_days=decay_days,
            split_policy=split_policy,
        )
        if _target_identity_keys(bundle.dataset) != reference_keys:
            raise ValueError(
                "CatBoost decay candidates changed target row identities."
            )
        for depth in (4, 6):
            for l2_leaf_reg in (3.0, 10.0):
                config = CatBoostCandidateConfig(
                    decay_days=float(decay_days),
                    depth=depth,
                    l2_leaf_reg=l2_leaf_reg,
                )
                model = classifier_cls(**config.params())
                split = bundle.split
                model.fit(
                    bundle.dataset.x[list(split.train_indices)],
                    bundle.dataset.y[list(split.train_indices)],
                    eval_set=(
                        bundle.dataset.x[list(split.validation_indices)],
                        bundle.dataset.y[list(split.validation_indices)],
                    ),
                    use_best_model=True,
                    early_stopping_rounds=CATBOOST_EARLY_STOPPING_ROUNDS,
                )
                diagnostics = _diagnostics_from_model(bundle, model)
                importance = _feature_importance(model, bundle.dataset.feature_names)
                candidates.append(
                    CatBoostCandidateDiagnostics(
                        config=config,
                        diagnostics=diagnostics,
                        feature_importance=importance,
                    )
                )
    return tuple(candidates)


def _diagnostics_from_model(
    bundle: _DiagnosticBundle,
    model: Any,
) -> ModelDiagnostics:
    split = bundle.split
    return _diagnostics_from_probabilities(
        bundle,
        train_probabilities=_predict_positive_probability(
            model,
            bundle.dataset.x[list(split.train_indices)],
        ),
        validation_probabilities=_predict_positive_probability(
            model,
            bundle.dataset.x[list(split.validation_indices)],
        ),
        test_probabilities=_predict_positive_probability(
            model,
            bundle.dataset.x[list(split.test_indices)],
        ),
    )


def _diagnostics_from_probabilities(
    bundle: _DiagnosticBundle,
    *,
    train_probabilities: Sequence[float],
    validation_probabilities: Sequence[float],
    test_probabilities: Sequence[float],
) -> ModelDiagnostics:
    dataset = bundle.dataset
    split = bundle.split
    train_y = dataset.y[list(split.train_indices)].tolist()
    validation_y = dataset.y[list(split.validation_indices)].tolist()
    test_y = dataset.y[list(split.test_indices)].tolist()
    test_indices = split.test_indices
    return ModelDiagnostics(
        train_metrics=evaluate_probabilities(train_y, train_probabilities),
        validation_metrics=evaluate_probabilities(
            validation_y,
            validation_probabilities,
        ),
        test_metrics=evaluate_probabilities(test_y, test_probabilities),
        test_probability_buckets=_probability_buckets(test_y, test_probabilities),
        test_chronological_buckets=_chronological_buckets(
            bundle,
            probabilities=test_probabilities,
        ),
        test_family_metrics=_group_metrics(
            bundle,
            indices=test_indices,
            probabilities=test_probabilities,
            names=[
                context.competition_family.name
                for context in bundle.row_contexts
            ],
        ),
        test_stage_metrics=_group_metrics(
            bundle,
            indices=test_indices,
            probabilities=test_probabilities,
            names=[context.stage.value for context in bundle.row_contexts],
        ),
    )


def _predict_positive_probability(
    model: Any,
    x: np.ndarray,
) -> tuple[float, ...]:
    probabilities = model.predict_proba(x)
    if probabilities.shape[1] < 2:
        raise ValueError("model predict_proba did not return binary probabilities")
    return tuple(float(value) for value in probabilities[:, 1])


def _probability_buckets(
    y_true: Sequence[int],
    probabilities: Sequence[float],
) -> tuple[ProbabilityBucket, ...]:
    y = np.asarray(y_true, dtype=np.int_)
    p = np.asarray(probabilities, dtype=np.float64)
    buckets: list[ProbabilityBucket] = []
    for index in range(10):
        lower = index / 10
        upper = (index + 1) / 10
        if index == 9:
            mask = (p >= lower) & (p <= upper)
        else:
            mask = (p >= lower) & (p < upper)
        if not bool(mask.any()):
            continue
        bucket_y = y[mask]
        bucket_p = p[mask]
        mean_probability = float(bucket_p.mean())
        observed = float(bucket_y.mean())
        buckets.append(
            ProbabilityBucket(
                lower=lower,
                upper=upper,
                rows=int(mask.sum()),
                mean_predicted_probability=mean_probability,
                observed_positive_rate=observed,
                absolute_calibration_gap=abs(mean_probability - observed),
            )
        )
    return tuple(buckets)


def _chronological_buckets(
    bundle: _DiagnosticBundle,
    *,
    probabilities: Sequence[float],
) -> tuple[ChronologicalBucket, ...]:
    split = bundle.split
    probability_by_index = dict(zip(split.test_indices, probabilities, strict=True))
    groups = _timestamp_groups(split.test_indices, bundle.dataset)
    if not groups:
        return ()
    bucket_count = min(4, len(groups))
    target_group_count = math.ceil(len(groups) / bucket_count)
    buckets: list[tuple[int, ...]] = []
    current: list[int] = []
    for group in groups:
        if len(buckets) < bucket_count - 1 and len(current) >= target_group_count:
            buckets.append(tuple(current))
            current = []
        current.extend(group)
    if current:
        buckets.append(tuple(current))

    result: list[ChronologicalBucket] = []
    for bucket in buckets:
        y_true = bundle.dataset.y[list(bucket)].tolist()
        bucket_probabilities = [probability_by_index[index] for index in bucket]
        timestamps = [
            bundle.dataset.metadata[index].prediction_timestamp
            for index in bucket
        ]
        result.append(
            ChronologicalBucket(
                rows=len(bucket),
                timestamp_start=min(timestamps),
                timestamp_end=max(timestamps),
                metrics=evaluate_probabilities(y_true, bucket_probabilities),
            )
        )
    return tuple(result)


def _group_metrics(
    bundle: _DiagnosticBundle,
    *,
    indices: tuple[int, ...],
    probabilities: Sequence[float],
    names: Sequence[str],
    minimum_rows: int = 10,
) -> tuple[GroupMetrics, ...]:
    grouped: dict[str, list[tuple[int, float]]] = {}
    for index, probability in zip(indices, probabilities, strict=True):
        grouped.setdefault(names[index], []).append((index, float(probability)))

    metrics: list[GroupMetrics] = []
    for name in sorted(grouped):
        rows = grouped[name]
        if len(rows) < minimum_rows:
            continue
        row_indices = [row[0] for row in rows]
        row_probabilities = [row[1] for row in rows]
        metrics.append(
            GroupMetrics(
                name=name,
                rows=len(rows),
                metrics=evaluate_probabilities(
                    bundle.dataset.y[row_indices].tolist(),
                    row_probabilities,
                ),
            )
        )
    return tuple(metrics)


def _timestamp_groups(
    indices: tuple[int, ...],
    dataset: HistoricalMatchDataset,
) -> tuple[tuple[int, ...], ...]:
    sorted_indices = sorted(
        indices,
        key=lambda index: (
            dataset.metadata[index].prediction_timestamp,
            dataset.metadata[index].source,
            dataset.metadata[index].source_match_id,
        ),
    )
    groups: list[list[int]] = []
    current_timestamp: datetime | None = None
    for index in sorted_indices:
        timestamp = dataset.metadata[index].prediction_timestamp
        if current_timestamp is None or timestamp != current_timestamp:
            groups.append([])
            current_timestamp = timestamp
        groups[-1].append(index)
    return tuple(tuple(group) for group in groups)


def _top_feature_drift(
    dataset: HistoricalMatchDataset,
    *,
    train_indices: tuple[int, ...],
    other_indices: tuple[int, ...],
    limit: int = 10,
) -> tuple[FeatureDrift, ...]:
    if not train_indices or not other_indices:
        return ()
    train = dataset.x[list(train_indices)]
    other = dataset.x[list(other_indices)]
    train_means = train.mean(axis=0)
    other_means = other.mean(axis=0)
    train_stds = train.std(axis=0)
    drifts: list[FeatureDrift] = []
    for index, feature_name in enumerate(dataset.feature_names):
        train_std = float(train_stds[index])
        denominator = train_std if train_std > 1e-12 else 1.0
        shift = abs(float(other_means[index] - train_means[index])) / denominator
        drifts.append(
            FeatureDrift(
                feature_name=feature_name,
                standardized_mean_shift=shift,
                train_mean=float(train_means[index]),
                other_mean=float(other_means[index]),
                train_std=train_std,
            )
        )
    return tuple(
        sorted(
            drifts,
            key=lambda drift: (-drift.standardized_mean_shift, drift.feature_name),
        )[:limit]
    )


def _split_label_rates(
    dataset: HistoricalMatchDataset,
    split: HistoricalTemporalSplit,
) -> dict[str, float]:
    return {
        "train": _label_rate(dataset, split.train_indices),
        "validation": _label_rate(dataset, split.validation_indices),
        "test": _label_rate(dataset, split.test_indices),
    }


def _label_rate(
    dataset: HistoricalMatchDataset,
    indices: tuple[int, ...],
) -> float:
    if not indices:
        return 0.0
    return float(dataset.y[list(indices)].mean())


def _target_identity_keys(
    dataset: HistoricalMatchDataset,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (metadata.source, metadata.source_match_id)
        for metadata in dataset.metadata
    )


def _feature_importance(
    model: Any,
    feature_names: Sequence[str],
    limit: int = 15,
) -> tuple[tuple[str, float], ...]:
    values = model.get_feature_importance()
    pairs = [
        (feature_name, float(importance))
        for feature_name, importance in zip(feature_names, values, strict=True)
    ]
    return tuple(sorted(pairs, key=lambda item: (-item[1], item[0]))[:limit])


def _catboost_classifier() -> Any:
    try:
        from catboost import CatBoostClassifier
    except ModuleNotFoundError as exc:
        raise CatBoostUnavailableError(
            "CatBoost is not installed. Install project dependencies first."
        ) from exc
    return CatBoostClassifier


def _catboost_import_available() -> bool:
    try:
        _catboost_classifier()
    except CatBoostUnavailableError:
        return False
    return True


def allowed_competition_family_names() -> tuple[str, ...]:
    return tuple(
        family.name
        for family in HISTORICAL_COMPETITION_CLASSIFICATION_PRECEDENCE
        if family in DEFAULT_HISTORICAL_COMPETITION_SCOPE.allowed_families
    )
