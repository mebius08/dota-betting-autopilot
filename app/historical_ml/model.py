from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


DEFAULT_HISTORICAL_MODEL_PATH = (
    Path("data") / "models" / "historical_match_win.joblib"
)
HISTORICAL_MODEL_TYPE = "historical_match_win_logistic_regression_v1"


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
