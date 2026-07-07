from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.historical_ml.dataset import (
    HISTORICAL_ML_FEATURE_NAMES,
    build_historical_ml_dataset,
)
from app.historical_ml.model import (
    DEFAULT_HISTORICAL_MODEL_PATH,
    HistoricalModelCompatibilityError,
    load_historical_model,
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
    DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    HistoricalCompetitionScopePolicy,
    HistoricalFeaturePolicy,
    RecencyWeightingPolicy,
    build_historical_feature_dataset,
    is_historical_match_scope_eligible,
    is_historical_match_scope_eligible_target,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class HistoricalMLStatus:
    historical_matches: int
    raw_usable_winner_records: int
    competition_scope_policy: HistoricalCompetitionScopePolicy
    scope_eligible_target_matches: int
    scope_eligible_feature_history_matches: int
    usable_feature_rows: int
    feature_count: int
    minimum_rows_policy: HistoricalMinimumRowsPolicy
    split: HistoricalTemporalSplit
    split_ready: bool
    readiness_reason: str
    model_artifact_path: Path
    model_artifact_exists: bool
    model_artifact_compatible: bool
    artifact_incompatibility_reason: str | None
    artifact_feature_schema_version: int | None
    artifact_training_timestamp: datetime | None
    artifact_competition_scope_policy: Mapping[str, object] | None
    artifact_feature_history_scope_policy: Mapping[str, object] | None
    artifact_feature_history_scope_semantics: str | None
    artifact_recorded_metrics: Mapping[str, Mapping[str, object]]


def build_historical_ml_status(
    repository: "SQLiteRepository",
    *,
    model_path: str | Path = DEFAULT_HISTORICAL_MODEL_PATH,
    decay_days: float = 90.0,
    split_policy: HistoricalTemporalSplitPolicy | None = None,
    minimum_rows_policy: HistoricalMinimumRowsPolicy | None = None,
) -> HistoricalMLStatus:
    temporal_policy = split_policy or HistoricalTemporalSplitPolicy()
    minimum_policy = minimum_rows_policy or HistoricalMinimumRowsPolicy()
    feature_policy = HistoricalFeaturePolicy(
        recency=RecencyWeightingPolicy(decay_days=decay_days)
    )
    scope_policy = DEFAULT_HISTORICAL_COMPETITION_SCOPE
    historical_matches_raw = tuple(repository.list_historical_matches())
    historical_matches = len(historical_matches_raw)
    scope_target_matches = sum(
        1
        for match in historical_matches_raw
        if is_historical_match_scope_eligible_target(match, scope_policy)
    )
    scope_feature_history_matches = sum(
        1
        for match in historical_matches_raw
        if is_historical_match_scope_eligible(match, scope_policy)
    )
    feature_rows = build_historical_feature_dataset(
        repository,
        policy=feature_policy,
        competition_scope_policy=scope_policy,
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
        split_ready = False
        readiness_reason = str(exc)
    else:
        split_ready = True
        readiness_reason = "Ready to train."

    artifact_path = Path(model_path)
    artifact_exists = artifact_path.exists()
    artifact_compatible = False
    incompatibility_reason: str | None = None
    artifact_feature_schema_version: int | None = None
    artifact_training_timestamp: datetime | None = None
    artifact_competition_scope_policy: Mapping[str, object] | None = None
    artifact_feature_history_scope_policy: Mapping[str, object] | None = None
    artifact_feature_history_scope_semantics: str | None = None
    artifact_recorded_metrics: Mapping[str, Mapping[str, object]] = {}
    if artifact_exists:
        try:
            artifact = load_historical_model(artifact_path)
        except (FileNotFoundError, HistoricalModelCompatibilityError) as exc:
            incompatibility_reason = str(exc)
        else:
            artifact_compatible = True
            artifact_feature_schema_version = artifact.feature_schema_version
            artifact_training_timestamp = artifact.training_timestamp
            artifact_competition_scope_policy = artifact.competition_scope_policy
            artifact_feature_history_scope_policy = (
                artifact.feature_history_scope_policy
            )
            artifact_feature_history_scope_semantics = (
                artifact.feature_history_scope_semantics
            )
            artifact_recorded_metrics = artifact.evaluation_metrics

    return HistoricalMLStatus(
        historical_matches=historical_matches,
        raw_usable_winner_records=sum(
            1
            for match in historical_matches_raw
            if match.usable_for_match_winner_training
        ),
        competition_scope_policy=scope_policy,
        scope_eligible_target_matches=scope_target_matches,
        scope_eligible_feature_history_matches=scope_feature_history_matches,
        usable_feature_rows=len(dataset),
        feature_count=len(HISTORICAL_ML_FEATURE_NAMES),
        minimum_rows_policy=minimum_policy,
        split=split,
        split_ready=split_ready,
        readiness_reason=readiness_reason,
        model_artifact_path=artifact_path,
        model_artifact_exists=artifact_exists,
        model_artifact_compatible=artifact_compatible,
        artifact_incompatibility_reason=incompatibility_reason,
        artifact_feature_schema_version=artifact_feature_schema_version,
        artifact_training_timestamp=artifact_training_timestamp,
        artifact_competition_scope_policy=artifact_competition_scope_policy,
        artifact_feature_history_scope_policy=artifact_feature_history_scope_policy,
        artifact_feature_history_scope_semantics=(
            artifact_feature_history_scope_semantics
        ),
        artifact_recorded_metrics=artifact_recorded_metrics,
    )
