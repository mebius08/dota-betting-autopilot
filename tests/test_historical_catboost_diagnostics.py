from pathlib import Path
from datetime import datetime, timezone

import joblib
import numpy as np
import pytest

from app.historical_ml import (
    DEFAULT_HISTORICAL_CATBOOST_MODEL_PATH,
    CatBoostCandidateConfig,
    CatBoostCandidateDiagnostics,
    HISTORICAL_CATBOOST_MODEL_TYPE,
    HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS,
    HISTORICAL_FEATURE_SCHEMA_VERSION,
    HISTORICAL_ML_FEATURE_NAMES,
    HISTORICAL_PREDICTION_MODE_PRE_MATCH,
    HistoricalCatBoostMatchWinModel,
    HistoricalModelCompatibilityError,
    ModelDiagnostics,
    catboost_base_params,
    load_historical_catboost_model,
    rank_catboost_candidates,
    save_historical_catboost_model,
    select_catboost_candidate,
)
from app.historical_ml.evaluation import HistoricalModelMetrics
from app.history import EWC_2026_BASELINE_SCOPE


def test_catboost_params_are_deterministic_and_do_not_write_files() -> None:
    config = CatBoostCandidateConfig(decay_days=90.0, depth=4, l2_leaf_reg=3.0)

    params = catboost_base_params(config)

    assert params["loss_function"] == "Logloss"
    assert params["random_seed"] == 42
    assert params["verbose"] is False
    assert params["allow_writing_files"] is False
    assert params["learning_rate"] == 0.03
    assert "StandardScaler" not in params


def test_catboost_predict_proba_label_one_and_no_training_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from catboost import CatBoostClassifier

    monkeypatch.chdir(tmp_path)
    config = CatBoostCandidateConfig(
        decay_days=90.0,
        depth=2,
        l2_leaf_reg=3.0,
        iterations=2,
    )
    model = CatBoostClassifier(**catboost_base_params(config))
    x = np.asarray([[0.0], [1.0], [0.1], [0.9]], dtype=np.float64)
    y = np.asarray([0, 1, 0, 1])

    model.fit(x, y)
    probabilities = model.predict_proba(x)

    assert probabilities.shape == (4, 2)
    assert list(model.classes_) == [0, 1]
    assert all(0.0 <= value <= 1.0 for value in probabilities[:, 1])
    assert not (tmp_path / "catboost_info").exists()


def test_pre_match_catboost_artifact_has_separate_identity(
    tmp_path: Path,
) -> None:
    artifact = _catboost_artifact(model=object())

    path = save_historical_catboost_model(
        artifact,
        tmp_path / DEFAULT_HISTORICAL_CATBOOST_MODEL_PATH.name,
    )
    loaded = load_historical_catboost_model(path)

    assert path.name == "historical_match_win_catboost.joblib"
    assert loaded.model_type == HISTORICAL_CATBOOST_MODEL_TYPE
    assert loaded.prediction_mode == HISTORICAL_PREDICTION_MODE_PRE_MATCH


def test_pre_match_catboost_model_kind_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    artifact = _catboost_artifact(model=object(), model_type="wrong-kind")
    path = tmp_path / "bad-catboost.joblib"
    joblib.dump(artifact, path)

    with pytest.raises(HistoricalModelCompatibilityError, match="model type"):
        load_historical_catboost_model(path)


def test_catboost_selection_uses_validation_brier_first() -> None:
    worse_test_better_validation = _candidate(
        validation_brier=0.20,
        validation_log_loss=0.60,
        validation_accuracy=0.50,
        test_brier=0.90,
        depth=6,
    )
    better_test_worse_validation = _candidate(
        validation_brier=0.21,
        validation_log_loss=0.10,
        validation_accuracy=0.99,
        test_brier=0.10,
        depth=4,
    )

    selected = select_catboost_candidate(
        [better_test_worse_validation, worse_test_better_validation]
    )

    assert selected is worse_test_better_validation


def test_catboost_ranked_list_sorts_generation_order_by_validation_policy() -> None:
    generated_first_but_worse = _candidate(
        validation_brier=0.204,
        validation_log_loss=0.61,
        validation_accuracy=0.50,
        test_brier=0.01,
        depth=4,
        decay_days=30.0,
    )
    selected_best = _candidate(
        validation_brier=0.202,
        validation_log_loss=0.59,
        validation_accuracy=0.70,
        test_brier=0.99,
        depth=4,
        decay_days=120.0,
    )
    middle = _candidate(
        validation_brier=0.203,
        validation_log_loss=0.60,
        validation_accuracy=0.60,
        test_brier=0.50,
        depth=4,
        decay_days=60.0,
    )

    ranked = rank_catboost_candidates(
        [generated_first_but_worse, middle, selected_best]
    )

    assert ranked == (selected_best, middle, generated_first_but_worse)
    assert select_catboost_candidate(ranked) is selected_best


def test_catboost_selection_tie_breaks_without_test_metrics() -> None:
    depth_6 = _candidate(
        validation_brier=0.20,
        validation_log_loss=0.60,
        validation_accuracy=0.50,
        test_brier=0.01,
        depth=6,
    )
    depth_4 = _candidate(
        validation_brier=0.20,
        validation_log_loss=0.60,
        validation_accuracy=0.50,
        test_brier=0.99,
        depth=4,
    )

    selected = select_catboost_candidate([depth_6, depth_4])

    assert selected is depth_4


def _candidate(
    *,
    validation_brier: float,
    validation_log_loss: float,
    validation_accuracy: float,
    test_brier: float,
    depth: int,
    decay_days: float = 90.0,
) -> CatBoostCandidateDiagnostics:
    return CatBoostCandidateDiagnostics(
        config=CatBoostCandidateConfig(
            decay_days=decay_days,
            depth=depth,
            l2_leaf_reg=3.0,
        ),
        diagnostics=ModelDiagnostics(
            train_metrics=_metrics(0.30, 0.70, 0.50),
            validation_metrics=_metrics(
                validation_brier,
                validation_log_loss,
                validation_accuracy,
            ),
            test_metrics=_metrics(test_brier, 0.40, 0.50),
            test_probability_buckets=(),
            test_chronological_buckets=(),
            test_family_metrics=(),
            test_stage_metrics=(),
        ),
    )


def _metrics(
    brier: float,
    log_loss: float,
    accuracy: float,
) -> HistoricalModelMetrics:
    return HistoricalModelMetrics(
        row_count=10,
        positive_label_rate=0.5,
        average_predicted_probability=0.5,
        brier_score=brier,
        log_loss=log_loss,
        accuracy=accuracy,
    )


def _catboost_artifact(
    *,
    model: object,
    model_type: str = HISTORICAL_CATBOOST_MODEL_TYPE,
) -> HistoricalCatBoostMatchWinModel:
    return HistoricalCatBoostMatchWinModel(
        model=model,
        feature_schema_version=HISTORICAL_FEATURE_SCHEMA_VERSION,
        feature_names=HISTORICAL_ML_FEATURE_NAMES,
        model_type=model_type,
        prediction_mode=HISTORICAL_PREDICTION_MODE_PRE_MATCH,
        training_timestamp=datetime.now(timezone.utc),
        recency_decay_days=90.0,
        catboost_params=catboost_base_params(
            CatBoostCandidateConfig(decay_days=90.0, depth=4, l2_leaf_reg=3.0)
        ),
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
        competition_scope_policy=EWC_2026_BASELINE_SCOPE.as_dict(),
        feature_history_scope_policy=EWC_2026_BASELINE_SCOPE.as_dict(),
        feature_history_scope_semantics=HISTORICAL_FEATURE_HISTORY_SCOPE_SEMANTICS,
    )
