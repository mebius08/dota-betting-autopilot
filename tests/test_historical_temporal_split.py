from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.historical_ml import (
    HistoricalMatchDataset,
    HistoricalMatchRowMetadata,
    HistoricalMinimumRowsPolicy,
    HistoricalTemporalSplitPolicy,
    HistoricalTrainingDataError,
    split_historical_dataset,
    validate_minimum_training_rows,
)


def test_temporal_split_is_chronological_and_keeps_timestamp_groups() -> None:
    dataset = _dataset(
        [
            _metadata("late-a", 4),
            _metadata("early", 1),
            _metadata("middle-a", 2),
            _metadata("middle-b", 2),
            _metadata("late-b", 4),
            _metadata("latest", 5),
        ]
    )

    split = split_historical_dataset(
        dataset,
        policy=HistoricalTemporalSplitPolicy(
            train_fraction=0.5,
            validation_fraction=0.25,
            test_fraction=0.25,
        ),
    )

    assert _ids(dataset, split.train_indices) == ["early", "middle-a", "middle-b"]
    assert _ids(dataset, split.validation_indices) == ["late-a", "late-b"]
    assert _ids(dataset, split.test_indices) == ["latest"]
    middle_indices = {
        index
        for index, metadata in enumerate(dataset.metadata)
        if metadata.source_match_id.startswith("middle")
    }
    assert middle_indices.issubset(set(split.train_indices))


def test_minimum_sample_validation_fails_clearly() -> None:
    dataset = _dataset([_metadata("one", 1), _metadata("two", 2)])
    split = split_historical_dataset(dataset)

    with pytest.raises(HistoricalTrainingDataError, match="Not enough usable"):
        validate_minimum_training_rows(
            dataset,
            split,
            policy=HistoricalMinimumRowsPolicy(
                minimum_total_rows=3,
                minimum_train_rows=1,
                minimum_validation_rows=1,
                minimum_test_rows=1,
            ),
        )


def _dataset(metadata: list[HistoricalMatchRowMetadata]) -> HistoricalMatchDataset:
    return HistoricalMatchDataset(
        x=np.zeros((len(metadata), 1)),
        y=np.asarray([index % 2 for index in range(len(metadata))]),
        metadata=tuple(metadata),
        feature_names=("feature",),
    )


def _metadata(source_match_id: str, day_offset: int) -> HistoricalMatchRowMetadata:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=day_offset
    )
    return HistoricalMatchRowMetadata(
        source="pandascore",
        source_match_id=source_match_id,
        prediction_timestamp=timestamp,
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        target_match_id=source_match_id,
        tournament_source_id=None,
        tournament_name=None,
    )


def _ids(dataset: HistoricalMatchDataset, indices: tuple[int, ...]) -> list[str]:
    return [dataset.metadata[index].source_match_id for index in indices]
