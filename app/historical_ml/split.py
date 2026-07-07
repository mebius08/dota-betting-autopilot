from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import math

from app.historical_ml.dataset import HistoricalMatchDataset


class HistoricalTrainingDataError(ValueError):
    pass


@dataclass(frozen=True)
class HistoricalTemporalSplitPolicy:
    train_fraction: float = 0.70
    validation_fraction: float = 0.15
    test_fraction: float = 0.15

    def __post_init__(self) -> None:
        fractions = (
            self.train_fraction,
            self.validation_fraction,
            self.test_fraction,
        )
        if any(not math.isfinite(value) or value < 0 for value in fractions):
            raise ValueError("temporal split fractions must be finite non-negative")
        total = sum(fractions)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("temporal split fractions must sum to 1.0")
        if self.train_fraction <= 0 or self.validation_fraction <= 0:
            raise ValueError("train and validation fractions must be positive")
        if self.test_fraction <= 0:
            raise ValueError("test fraction must be positive")

    def as_dict(self) -> dict[str, float]:
        return {
            "train_fraction": self.train_fraction,
            "validation_fraction": self.validation_fraction,
            "test_fraction": self.test_fraction,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> HistoricalTemporalSplitPolicy:
        return cls(
            train_fraction=_mapping_float(value["train_fraction"]),
            validation_fraction=_mapping_float(value["validation_fraction"]),
            test_fraction=_mapping_float(value["test_fraction"]),
        )


@dataclass(frozen=True)
class HistoricalMinimumRowsPolicy:
    minimum_total_rows: int = 100
    minimum_train_rows: int = 60
    minimum_validation_rows: int = 15
    minimum_test_rows: int = 15

    def __post_init__(self) -> None:
        values = (
            self.minimum_total_rows,
            self.minimum_train_rows,
            self.minimum_validation_rows,
            self.minimum_test_rows,
        )
        if any(value < 1 for value in values):
            raise ValueError("minimum row counts must be positive")

    def as_dict(self) -> dict[str, int]:
        return {
            "minimum_total_rows": self.minimum_total_rows,
            "minimum_train_rows": self.minimum_train_rows,
            "minimum_validation_rows": self.minimum_validation_rows,
            "minimum_test_rows": self.minimum_test_rows,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> HistoricalMinimumRowsPolicy:
        return cls(
            minimum_total_rows=_mapping_int(value["minimum_total_rows"]),
            minimum_train_rows=_mapping_int(value["minimum_train_rows"]),
            minimum_validation_rows=_mapping_int(value["minimum_validation_rows"]),
            minimum_test_rows=_mapping_int(value["minimum_test_rows"]),
        )


@dataclass(frozen=True)
class HistoricalTemporalSplit:
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    test_indices: tuple[int, ...]

    @property
    def train_rows(self) -> int:
        return len(self.train_indices)

    @property
    def validation_rows(self) -> int:
        return len(self.validation_indices)

    @property
    def test_rows(self) -> int:
        return len(self.test_indices)

    def row_counts(self) -> dict[str, int]:
        return {
            "train": self.train_rows,
            "validation": self.validation_rows,
            "test": self.test_rows,
        }


def split_historical_dataset(
    dataset: HistoricalMatchDataset,
    *,
    policy: HistoricalTemporalSplitPolicy | None = None,
) -> HistoricalTemporalSplit:
    split_policy = policy or HistoricalTemporalSplitPolicy()
    sorted_indices = tuple(
        sorted(
            range(len(dataset)),
            key=lambda index: (
                dataset.metadata[index].prediction_timestamp,
                dataset.metadata[index].source,
                dataset.metadata[index].source_match_id,
            ),
        )
    )
    total_rows = len(sorted_indices)
    train_target = int(total_rows * split_policy.train_fraction)
    validation_end_target = int(
        total_rows
        * (split_policy.train_fraction + split_policy.validation_fraction)
    )

    train: list[int] = []
    validation: list[int] = []
    test: list[int] = []
    consumed = 0
    for group in _timestamp_groups(sorted_indices, dataset):
        if consumed < train_target:
            train.extend(group)
        elif consumed < validation_end_target:
            validation.extend(group)
        else:
            test.extend(group)
        consumed += len(group)

    return HistoricalTemporalSplit(
        train_indices=tuple(train),
        validation_indices=tuple(validation),
        test_indices=tuple(test),
    )


def validate_minimum_training_rows(
    dataset: HistoricalMatchDataset,
    split: HistoricalTemporalSplit,
    *,
    policy: HistoricalMinimumRowsPolicy | None = None,
) -> None:
    minimums = policy or HistoricalMinimumRowsPolicy()
    total_rows = len(dataset)
    if total_rows < minimums.minimum_total_rows:
        raise HistoricalTrainingDataError(
            "Not enough usable historical feature rows: "
            f"{total_rows} found, {minimums.minimum_total_rows} required."
        )
    if split.train_rows < minimums.minimum_train_rows:
        raise HistoricalTrainingDataError(
            "Not enough train rows after temporal split: "
            f"{split.train_rows} found, {minimums.minimum_train_rows} required."
        )
    if split.validation_rows < minimums.minimum_validation_rows:
        raise HistoricalTrainingDataError(
            "Not enough validation rows after temporal split: "
            f"{split.validation_rows} found, "
            f"{minimums.minimum_validation_rows} required."
        )
    if split.test_rows < minimums.minimum_test_rows:
        raise HistoricalTrainingDataError(
            "Not enough test rows after temporal split: "
            f"{split.test_rows} found, {minimums.minimum_test_rows} required."
        )


def split_timestamp_ranges(
    dataset: HistoricalMatchDataset,
    split: HistoricalTemporalSplit,
) -> dict[str, tuple[datetime | None, datetime | None]]:
    return {
        "train": _timestamp_range(dataset, split.train_indices),
        "validation": _timestamp_range(dataset, split.validation_indices),
        "test": _timestamp_range(dataset, split.test_indices),
    }


def _timestamp_groups(
    sorted_indices: Sequence[int],
    dataset: HistoricalMatchDataset,
) -> tuple[tuple[int, ...], ...]:
    groups: list[list[int]] = []
    previous_timestamp: datetime | None = None
    for index in sorted_indices:
        timestamp = dataset.metadata[index].prediction_timestamp
        if previous_timestamp is None or timestamp != previous_timestamp:
            groups.append([])
            previous_timestamp = timestamp
        groups[-1].append(index)
    return tuple(tuple(group) for group in groups)


def _timestamp_range(
    dataset: HistoricalMatchDataset,
    indices: tuple[int, ...],
) -> tuple[datetime | None, datetime | None]:
    if not indices:
        return (None, None)
    timestamps = tuple(dataset.metadata[index].prediction_timestamp for index in indices)
    return (min(timestamps), max(timestamps))


def _mapping_float(value: object) -> float:
    if isinstance(value, int | float | str):
        return float(value)
    raise ValueError("expected numeric artifact value")


def _mapping_int(value: object) -> int:
    if isinstance(value, int | str):
        return int(value)
    raise ValueError("expected integer artifact value")
