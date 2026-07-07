from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]

from app.draft_ml.dataset import (
    DRAFT_CATEGORICAL_FEATURE_NAMES,
    DRAFT_FEATURE_NAMES,
    DRAFT_FEATURE_SCHEMA_VERSION,
    DRAFT_PREDICTION_MODE,
    DraftMapDataset,
    draft_categorical_feature_indices,
)
from app.history import DEFAULT_HISTORICAL_COMPETITION_SCOPE


DEFAULT_DRAFT_MODEL_PATH = (
    Path("data") / "models" / "historical_draft_map_win_catboost.joblib"
)
DRAFT_MODEL_TYPE = "historical_draft_map_win_catboost_v1"
DRAFT_SOURCE_PROVIDER = "opendota"
DRAFT_HERO_IDENTITY_NAMESPACE = "provider_hero_id"
DRAFT_HISTORY_CUTOFF_SEMANTICS = "strict_ended_at_before_target_started_at"
DRAFT_PATCH_SEMANTICS = "trusted_provider_patch_or_missing"


class DraftModelCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftMapWinModel:
    model: Any
    model_type: str
    prediction_mode: str
    feature_schema_version: int
    feature_names: tuple[str, ...]
    categorical_feature_names: tuple[str, ...]
    draft_source_provider: str
    hero_identity_namespace: str
    competition_scope_policy: Mapping[str, object]
    history_cutoff_semantics: str
    patch_semantics: str
    training_timestamp: datetime
    temporal_split_policy: Mapping[str, object]
    row_counts: Mapping[str, int]
    evaluation_metrics: Mapping[str, Mapping[str, object]]
    catboost_params: Mapping[str, object] = field(default_factory=dict)

    def validate_compatible(self) -> None:
        if self.model_type != DRAFT_MODEL_TYPE:
            raise DraftModelCompatibilityError(
                "Draft model kind mismatch: "
                f"artifact={self.model_type}, expected={DRAFT_MODEL_TYPE}."
            )
        if self.prediction_mode != DRAFT_PREDICTION_MODE:
            raise DraftModelCompatibilityError(
                "Draft model prediction mode mismatch: "
                f"artifact={self.prediction_mode}, expected={DRAFT_PREDICTION_MODE}."
            )
        if self.feature_schema_version != DRAFT_FEATURE_SCHEMA_VERSION:
            raise DraftModelCompatibilityError("Draft feature schema mismatch.")
        if tuple(self.feature_names) != DRAFT_FEATURE_NAMES:
            raise DraftModelCompatibilityError("Draft feature names/order mismatch.")
        if tuple(self.categorical_feature_names) != DRAFT_CATEGORICAL_FEATURE_NAMES:
            raise DraftModelCompatibilityError(
                "Draft categorical feature names/order mismatch."
            )
        if self.draft_source_provider != DRAFT_SOURCE_PROVIDER:
            raise DraftModelCompatibilityError("Draft source provider mismatch.")
        if self.hero_identity_namespace != DRAFT_HERO_IDENTITY_NAMESPACE:
            raise DraftModelCompatibilityError("Draft hero namespace mismatch.")
        if self.history_cutoff_semantics != DRAFT_HISTORY_CUTOFF_SEMANTICS:
            raise DraftModelCompatibilityError("Draft cutoff semantics mismatch.")
        if self.patch_semantics != DRAFT_PATCH_SEMANTICS:
            raise DraftModelCompatibilityError("Draft patch semantics mismatch.")
        if (
            self.competition_scope_policy.get("scope_id")
            != DEFAULT_HISTORICAL_COMPETITION_SCOPE.scope_id
        ):
            raise DraftModelCompatibilityError("Draft competition scope mismatch.")

    def predict_team_a_probabilities(
        self,
        dataset: DraftMapDataset,
    ) -> tuple[float, ...]:
        if tuple(dataset.x.columns) != DRAFT_FEATURE_NAMES:
            raise ValueError("draft feature matrix does not match schema")
        probabilities = self.model.predict_proba(dataset.x)[:, 1]
        return tuple(max(0.0, min(1.0, float(value))) for value in probabilities)


def catboost_categorical_indices() -> tuple[int, ...]:
    return draft_categorical_feature_indices()


def create_draft_catboost_model(params: Mapping[str, object]) -> Any:
    try:
        from catboost import CatBoostClassifier
    except ModuleNotFoundError as exc:
        raise DraftModelCompatibilityError(
            "CatBoost is not installed. Install project dependencies first."
        ) from exc
    return CatBoostClassifier(**dict(params))


def save_draft_model(
    model: DraftMapWinModel,
    path: str | Path = DEFAULT_DRAFT_MODEL_PATH,
) -> Path:
    model.validate_compatible()
    artifact_path = Path(path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, artifact_path)
    return artifact_path


def load_draft_model(
    path: str | Path = DEFAULT_DRAFT_MODEL_PATH,
) -> DraftMapWinModel:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(
            f"Draft ML model artifact not found: {artifact_path.as_posix()}"
        )
    try:
        model = joblib.load(artifact_path)
    except Exception as exc:
        raise DraftModelCompatibilityError(
            f"Could not load draft model artifact: {exc}"
        ) from exc
    if not isinstance(model, DraftMapWinModel):
        raise DraftModelCompatibilityError(
            "Draft model artifact has an unexpected format."
        )
    model.validate_compatible()
    return model
