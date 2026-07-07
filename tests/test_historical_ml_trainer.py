from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.historical_ml import (
    HISTORICAL_ML_FEATURE_NAMES,
    HistoricalMatchDataset,
    HistoricalMatchRowMetadata,
    HistoricalMinimumRowsPolicy,
    HistoricalTemporalSplitPolicy,
    load_historical_model,
    train_historical_model_from_repository,
)
from app.history import EWC_2026_BASELINE_SCOPE
import app.historical_ml.trainer as trainer_module


def test_historical_trainer_saves_artifact_with_small_explicit_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset = _dataset([0, 1, 0, 1, 0, 1, 0, 1])
    monkeypatch.setattr(
        trainer_module,
        "build_historical_feature_dataset",
        lambda repository, policy, competition_scope_policy: [],
    )
    monkeypatch.setattr(
        trainer_module,
        "build_historical_ml_dataset",
        lambda rows: dataset,
    )
    model_path = tmp_path / "historical.joblib"

    result = train_historical_model_from_repository(
        object(),
        model_path=model_path,
        split_policy=HistoricalTemporalSplitPolicy(
            train_fraction=0.5,
            validation_fraction=0.25,
            test_fraction=0.25,
        ),
        minimum_rows_policy=HistoricalMinimumRowsPolicy(
            minimum_total_rows=8,
            minimum_train_rows=4,
            minimum_validation_rows=2,
            minimum_test_rows=2,
        ),
    )

    assert result.trained is True
    assert result.split is not None
    assert result.split.train_rows == 4
    assert result.train_metrics is not None
    assert result.validation_metrics is not None
    assert result.test_metrics is not None
    assert model_path.exists()
    assert (
        load_historical_model(model_path).competition_scope_policy
        == EWC_2026_BASELINE_SCOPE.as_dict()
    )
    assert (
        load_historical_model(model_path).feature_history_scope_policy
        == EWC_2026_BASELINE_SCOPE.as_dict()
    )


def test_historical_trainer_rejects_single_class_train_split(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset = _dataset([1, 1, 1, 1, 0, 0, 0, 0])
    monkeypatch.setattr(
        trainer_module,
        "build_historical_feature_dataset",
        lambda repository, policy, competition_scope_policy: [],
    )
    monkeypatch.setattr(
        trainer_module,
        "build_historical_ml_dataset",
        lambda rows: dataset,
    )
    model_path = tmp_path / "historical.joblib"

    result = train_historical_model_from_repository(
        object(),
        model_path=model_path,
        split_policy=HistoricalTemporalSplitPolicy(
            train_fraction=0.5,
            validation_fraction=0.25,
            test_fraction=0.25,
        ),
        minimum_rows_policy=HistoricalMinimumRowsPolicy(
            minimum_total_rows=8,
            minimum_train_rows=4,
            minimum_validation_rows=2,
            minimum_test_rows=2,
        ),
    )

    assert result.trained is False
    assert "Need both Team A win and Team B win" in result.message
    assert not model_path.exists()


def _dataset(labels: list[int]) -> HistoricalMatchDataset:
    row_count = len(labels)
    feature_count = len(HISTORICAL_ML_FEATURE_NAMES)
    x = np.asarray(
        [
            [
                (row_index + 1) * (feature_index + 1) / 100
                for feature_index in range(feature_count)
            ]
            for row_index in range(row_count)
        ],
        dtype=np.float64,
    )
    return HistoricalMatchDataset(
        x=x,
        y=np.asarray(labels),
        metadata=tuple(_metadata(index) for index in range(row_count)),
        feature_names=HISTORICAL_ML_FEATURE_NAMES,
    )


def _metadata(index: int) -> HistoricalMatchRowMetadata:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    return HistoricalMatchRowMetadata(
        source="pandascore",
        source_match_id=f"match-{index}",
        prediction_timestamp=timestamp,
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        target_match_id=f"match-{index}",
        tournament_source_id=None,
        tournament_name=None,
    )
