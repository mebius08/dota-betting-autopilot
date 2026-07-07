from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import numpy as np
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]

from app.historical_ml.dataset import (
    HISTORICAL_FEATURE_SCHEMA_VERSION,
    HISTORICAL_ML_FEATURE_NAMES,
    feature_mapping_to_vector,
)
from app.history.competition_scope import (
    DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    validate_historical_scope_compatible,
)


DEFAULT_HISTORICAL_MODEL_PATH = (
    Path("data") / "models" / "historical_match_win.joblib"
)
DEFAULT_HISTORICAL_CATBOOST_MODEL_PATH = (
    Path("data") / "models" / "historical_match_win_catboost.joblib"
)
HISTORICAL_MODEL_TYPE = "historical_match_win_logistic_regression_v1"
HISTORICAL_CATBOOST_MODEL_TYPE = "historical_match_win_catboost_v1"
HISTORICAL_PREDICTION_MODE_PRE_MATCH = "PRE_MATCH_SERIES"
HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS = "same_as_target_scope_v1"


class HistoricalModelCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalMatchWinModel:
    pipeline: Any
    feature_schema_version: int
    feature_names: tuple[str, ...]
    model_type: str
    training_timestamp: datetime
    recency_decay_days: float
    temporal_split_policy: Mapping[str, object]
    minimum_rows_policy: Mapping[str, object]
    row_counts: Mapping[str, int]
    evaluation_metrics: Mapping[str, Mapping[str, object]]
    competition_scope_policy: Mapping[str, object] = field(
        default_factory=DEFAULT_HISTORICAL_COMPETITION_SCOPE.as_dict
    )
    feature_history_scope_policy: Mapping[str, object] = field(
        default_factory=DEFAULT_HISTORICAL_COMPETITION_SCOPE.as_dict
    )
    feature_history_scope_semantics: str = (
        HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS
    )

    def validate_compatible(self) -> None:
        if self.feature_schema_version != HISTORICAL_FEATURE_SCHEMA_VERSION:
            raise HistoricalModelCompatibilityError(
                "Historical model feature schema version mismatch: "
                f"artifact={self.feature_schema_version}, "
                f"expected={HISTORICAL_FEATURE_SCHEMA_VERSION}."
            )
        if tuple(self.feature_names) != HISTORICAL_ML_FEATURE_NAMES:
            raise HistoricalModelCompatibilityError(
                "Historical model feature names/order mismatch."
            )
        if self.model_type != HISTORICAL_MODEL_TYPE:
            raise HistoricalModelCompatibilityError(
                "Historical model type mismatch: "
                f"artifact={self.model_type}, expected={HISTORICAL_MODEL_TYPE}."
            )
        try:
            validate_historical_scope_compatible(
                getattr(self, "competition_scope_policy", None)
            )
        except ValueError as exc:
            raise HistoricalModelCompatibilityError(str(exc)) from exc
        if (
            getattr(self, "feature_history_scope_semantics", None)
            != HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS
        ):
            raise HistoricalModelCompatibilityError(
                "Historical model feature history scope semantics mismatch: "
                f"artifact={getattr(self, 'feature_history_scope_semantics', None)}, "
                f"expected={HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS}."
            )
        try:
            validate_historical_scope_compatible(
                getattr(self, "feature_history_scope_policy", None)
            )
        except ValueError as exc:
            raise HistoricalModelCompatibilityError(
                "Historical model feature history scope mismatch: "
                f"{exc}"
            ) from exc

    def predict_team_a_probability(
        self,
        features: Mapping[str, object] | Sequence[float],
    ) -> float:
        if isinstance(features, Mapping):
            vector = feature_mapping_to_vector(
                features,
                feature_names=self.feature_names,
            )
        else:
            vector = tuple(float(value) for value in features)
        if len(vector) != len(self.feature_names):
            raise ValueError(
                "feature vector length does not match historical model schema"
            )
        probability = float(
            self.pipeline.predict_proba(
                np.asarray([vector], dtype=np.float64)
            )[0][1]
        )
        return max(0.0, min(1.0, probability))

    def predict_team_a_probabilities(
        self,
        rows: Sequence[Sequence[float]] | np.ndarray,
    ) -> tuple[float, ...]:
        x = np.asarray(rows, dtype=np.float64)
        if x.ndim == 1:
            x = x.reshape(1, -1)
        if x.shape[1] != len(self.feature_names):
            raise ValueError(
                "feature matrix width does not match historical model schema"
            )
        probabilities = self.pipeline.predict_proba(x)[:, 1]
        return tuple(max(0.0, min(1.0, float(value))) for value in probabilities)


@dataclass(frozen=True)
class HistoricalCatBoostMatchWinModel:
    model: Any
    feature_schema_version: int
    feature_names: tuple[str, ...]
    model_type: str
    prediction_mode: str
    training_timestamp: datetime
    recency_decay_days: float
    catboost_params: Mapping[str, object]
    temporal_split_policy: Mapping[str, object]
    minimum_rows_policy: Mapping[str, object]
    row_counts: Mapping[str, int]
    evaluation_metrics: Mapping[str, Mapping[str, object]]
    competition_scope_policy: Mapping[str, object] = field(
        default_factory=DEFAULT_HISTORICAL_COMPETITION_SCOPE.as_dict
    )
    feature_history_scope_policy: Mapping[str, object] = field(
        default_factory=DEFAULT_HISTORICAL_COMPETITION_SCOPE.as_dict
    )
    feature_history_scope_semantics: str = (
        HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS
    )

    def validate_compatible(self) -> None:
        if self.feature_schema_version != HISTORICAL_FEATURE_SCHEMA_VERSION:
            raise HistoricalModelCompatibilityError(
                "Historical CatBoost feature schema version mismatch: "
                f"artifact={self.feature_schema_version}, "
                f"expected={HISTORICAL_FEATURE_SCHEMA_VERSION}."
            )
        if tuple(self.feature_names) != HISTORICAL_ML_FEATURE_NAMES:
            raise HistoricalModelCompatibilityError(
                "Historical CatBoost feature names/order mismatch."
            )
        if self.model_type != HISTORICAL_CATBOOST_MODEL_TYPE:
            raise HistoricalModelCompatibilityError(
                "Historical CatBoost model type mismatch: "
                f"artifact={self.model_type}, "
                f"expected={HISTORICAL_CATBOOST_MODEL_TYPE}."
            )
        if self.prediction_mode != HISTORICAL_PREDICTION_MODE_PRE_MATCH:
            raise HistoricalModelCompatibilityError(
                "Historical CatBoost prediction mode mismatch: "
                f"artifact={self.prediction_mode}, "
                f"expected={HISTORICAL_PREDICTION_MODE_PRE_MATCH}."
            )
        try:
            validate_historical_scope_compatible(
                getattr(self, "competition_scope_policy", None)
            )
        except ValueError as exc:
            raise HistoricalModelCompatibilityError(str(exc)) from exc
        if (
            getattr(self, "feature_history_scope_semantics", None)
            != HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS
        ):
            raise HistoricalModelCompatibilityError(
                "Historical CatBoost feature history scope semantics mismatch."
            )
        try:
            validate_historical_scope_compatible(
                getattr(self, "feature_history_scope_policy", None)
            )
        except ValueError as exc:
            raise HistoricalModelCompatibilityError(
                "Historical CatBoost feature history scope mismatch: "
                f"{exc}"
            ) from exc

    def predict_team_a_probabilities(
        self,
        rows: Sequence[Sequence[float]] | np.ndarray,
    ) -> tuple[float, ...]:
        x = np.asarray(rows, dtype=np.float64)
        if x.ndim == 1:
            x = x.reshape(1, -1)
        if x.shape[1] != len(self.feature_names):
            raise ValueError(
                "feature matrix width does not match historical CatBoost schema"
            )
        probabilities = self.model.predict_proba(x)[:, 1]
        return tuple(max(0.0, min(1.0, float(value))) for value in probabilities)


def create_historical_model_pipeline() -> Pipeline:
    return Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    max_iter=1000,
                    random_state=42,
                ),
            ),
        ]
    )


def save_historical_model(
    model: HistoricalMatchWinModel,
    path: str | Path = DEFAULT_HISTORICAL_MODEL_PATH,
) -> Path:
    model.validate_compatible()
    artifact_path = Path(path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, artifact_path)
    return artifact_path


def load_historical_model(
    path: str | Path = DEFAULT_HISTORICAL_MODEL_PATH,
) -> HistoricalMatchWinModel:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(
            f"Historical ML model artifact not found: {artifact_path.as_posix()}"
        )
    try:
        model = joblib.load(artifact_path)
    except Exception as exc:
        raise HistoricalModelCompatibilityError(
            f"Could not load historical model artifact: {exc}"
        ) from exc
    if not isinstance(model, HistoricalMatchWinModel):
        raise HistoricalModelCompatibilityError(
            "Historical model artifact has an unexpected format."
        )
    model.validate_compatible()
    return model


def save_historical_catboost_model(
    model: HistoricalCatBoostMatchWinModel,
    path: str | Path = DEFAULT_HISTORICAL_CATBOOST_MODEL_PATH,
) -> Path:
    model.validate_compatible()
    artifact_path = Path(path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, artifact_path)
    return artifact_path


def load_historical_catboost_model(
    path: str | Path = DEFAULT_HISTORICAL_CATBOOST_MODEL_PATH,
) -> HistoricalCatBoostMatchWinModel:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(
            "Historical CatBoost model artifact not found: "
            f"{artifact_path.as_posix()}"
        )
    try:
        model = joblib.load(artifact_path)
    except Exception as exc:
        raise HistoricalModelCompatibilityError(
            f"Could not load historical CatBoost model artifact: {exc}"
        ) from exc
    if not isinstance(model, HistoricalCatBoostMatchWinModel):
        raise HistoricalModelCompatibilityError(
            "Historical CatBoost model artifact has an unexpected format."
        )
    model.validate_compatible()
    return model
