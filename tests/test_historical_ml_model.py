from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pytest

from app.historical_ml import (
    HISTORICAL_FEATURE_SCHEMA_VERSION,
    HISTORICAL_ML_FEATURE_NAMES,
    HISTORICAL_MODEL_TYPE,
    HistoricalMatchWinModel,
    HistoricalModelCompatibilityError,
    create_historical_model_pipeline,
    load_historical_model,
    save_historical_model,
)
from app.history import EWC_2026_BASELINE_SCOPE, HistoricalCompetitionFamily


def test_historical_model_probability_and_artifact_round_trip(
    tmp_path: Path,
) -> None:
    pipeline = create_historical_model_pipeline()
    x = np.vstack(
        [
            np.zeros(len(HISTORICAL_ML_FEATURE_NAMES)),
            np.ones(len(HISTORICAL_ML_FEATURE_NAMES)),
            np.full(len(HISTORICAL_ML_FEATURE_NAMES), 0.25),
            np.full(len(HISTORICAL_ML_FEATURE_NAMES), 0.75),
        ]
    )
    y = np.asarray([0, 1, 0, 1])
    pipeline.fit(x, y)
    model = _model(pipeline)

    probability = model.predict_team_a_probability(x[0])
    assert 0 <= probability <= 1

    path = save_historical_model(model, tmp_path / "historical.joblib")
    loaded = load_historical_model(path)

    assert loaded.predict_team_a_probability(x[0]) == pytest.approx(probability)
    assert loaded.competition_scope_policy == EWC_2026_BASELINE_SCOPE.as_dict()
    assert loaded.competition_scope_policy["exclude_qualifiers"] is True


def test_incompatible_feature_schema_is_rejected(tmp_path: Path) -> None:
    model = _model(create_historical_model_pipeline(), feature_names=("wrong",))
    path = tmp_path / "bad.joblib"
    joblib.dump(model, path)

    with pytest.raises(HistoricalModelCompatibilityError):
        load_historical_model(path)


def test_incompatible_competition_scope_start_is_rejected(tmp_path: Path) -> None:
    scope = EWC_2026_BASELINE_SCOPE.as_dict()
    scope["target_start_at"] = "2025-07-09T00:00:00Z"
    model = _model(
        create_historical_model_pipeline(),
        competition_scope_policy=scope,
    )
    path = tmp_path / "bad-scope-start.joblib"
    joblib.dump(model, path)

    with pytest.raises(HistoricalModelCompatibilityError, match="start mismatch"):
        load_historical_model(path)


def test_incompatible_competition_scope_family_set_is_rejected(
    tmp_path: Path,
) -> None:
    scope = EWC_2026_BASELINE_SCOPE.as_dict()
    scope["allowed_families"] = [
        family
        for family in scope["allowed_families"]
        if family != HistoricalCompetitionFamily.PGL.value
    ]
    model = _model(
        create_historical_model_pipeline(),
        competition_scope_policy=scope,
    )
    path = tmp_path / "bad-scope-family.joblib"
    joblib.dump(model, path)

    with pytest.raises(HistoricalModelCompatibilityError, match="family set"):
        load_historical_model(path)


def _model(
    pipeline: object,
    *,
    feature_names: tuple[str, ...] = HISTORICAL_ML_FEATURE_NAMES,
    competition_scope_policy: dict[str, object] | None = None,
) -> HistoricalMatchWinModel:
    return HistoricalMatchWinModel(
        pipeline=pipeline,
        feature_schema_version=HISTORICAL_FEATURE_SCHEMA_VERSION,
        feature_names=feature_names,
        model_type=HISTORICAL_MODEL_TYPE,
        training_timestamp=datetime.now(timezone.utc),
        recency_decay_days=90.0,
        temporal_split_policy={
            "train_fraction": 0.70,
            "validation_fraction": 0.15,
            "test_fraction": 0.15,
        },
        minimum_rows_policy={
            "minimum_total_rows": 100,
            "minimum_train_rows": 60,
            "minimum_validation_rows": 15,
            "minimum_test_rows": 15,
        },
        row_counts={"train": 70, "validation": 15, "test": 15},
        evaluation_metrics={},
        competition_scope_policy=(
            competition_scope_policy or EWC_2026_BASELINE_SCOPE.as_dict()
        ),
    )
