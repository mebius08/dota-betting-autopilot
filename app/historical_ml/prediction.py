from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.historical_ml.dataset import row_to_feature_vector
from app.historical_ml.model import (
    DEFAULT_HISTORICAL_MODEL_PATH,
    load_historical_model,
)
from app.history import (
    HistoricalFeaturePolicy,
    HistoricalPredictionContext,
    build_historical_match_features,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class HistoricalMatchPrediction:
    source: str
    source_match_id: str
    prediction_timestamp: datetime
    team_a_source_id: str
    team_b_source_id: str
    team_a_win_probability: float
    team_b_win_probability: float
    model_type: str
    feature_schema_version: int
    training_timestamp: datetime


def predict_historical_match(
    repository: "SQLiteRepository",
    context: HistoricalPredictionContext,
    *,
    model_path: str | Path = DEFAULT_HISTORICAL_MODEL_PATH,
    policy: HistoricalFeaturePolicy | None = None,
) -> HistoricalMatchPrediction:
    model = load_historical_model(model_path)
    feature_row = build_historical_match_features(
        repository,
        context,
        policy=policy,
    )
    probability = model.predict_team_a_probability(
        row_to_feature_vector(feature_row, feature_names=model.feature_names)
    )
    return HistoricalMatchPrediction(
        source=context.source,
        source_match_id=context.source_match_id,
        prediction_timestamp=context.prediction_timestamp,
        team_a_source_id=context.team_a_source_id,
        team_b_source_id=context.team_b_source_id,
        team_a_win_probability=probability,
        team_b_win_probability=1.0 - probability,
        model_type=model.model_type,
        feature_schema_version=model.feature_schema_version,
        training_timestamp=model.training_timestamp,
    )
