from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import math

from app.draft_ml.dataset import DraftMapDataset


@dataclass(frozen=True)
class DraftTemporalSplitPolicy:
    train_fraction: float = 0.70
    validation_fraction: float = 0.15
    test_fraction: float = 0.15

    def __post_init__(self) -> None:
        values = (self.train_fraction, self.validation_fraction, self.test_fraction)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("draft split fractions must be finite and positive")
        if not math.isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("draft split fractions must sum to 1.0")

    def as_dict(self) -> dict[str, float]:
        return {
            "train_fraction": self.train_fraction,
            "validation_fraction": self.validation_fraction,
            "test_fraction": self.test_fraction,
        }


@dataclass(frozen=True)
class DraftMinimumRowsPolicy:
    minimum_total_rows: int = 100
    minimum_train_rows: int = 60
    minimum_validation_rows: int = 15
    minimum_test_rows: int = 15

    def as_dict(self) -> dict[str, int]:
        return {
            "minimum_total_rows": self.minimum_total_rows,
            "minimum_train_rows": self.minimum_train_rows,
            "minimum_validation_rows": self.minimum_validation_rows,
            "minimum_test_rows": self.minimum_test_rows,
        }


@dataclass(frozen=True)
class DraftTemporalSplit:
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


class DraftTrainingDataError(ValueError):
    pass


def split_draft_dataset(
    dataset: DraftMapDataset,
    *,
    policy: DraftTemporalSplitPolicy | None = None,
) -> DraftTemporalSplit:
    split_policy = policy or DraftTemporalSplitPolicy()
    sorted_indices = tuple(
        sorted(
            range(len(dataset)),
            key=lambda index: (
                dataset.metadata[index].prediction_timestamp,
                dataset.metadata[index].source,
                dataset.metadata[index].source_game_id,
            ),
        )
    )
    train_target = int(len(sorted_indices) * split_policy.train_fraction)
    validation_end = int(
        len(sorted_indices)
        * (split_policy.train_fraction + split_policy.validation_fraction)
    )
    train: list[int] = []
    validation: list[int] = []
    test: list[int] = []
    consumed = 0
    for group in _timestamp_groups(sorted_indices, dataset):
        if consumed < train_target:
            train.extend(group)
        elif consumed < validation_end:
            validation.extend(group)
        else:
            test.extend(group)
        consumed += len(group)
    return DraftTemporalSplit(tuple(train), tuple(validation), tuple(test))


def validate_minimum_draft_training_rows(
    dataset: DraftMapDataset,
    split: DraftTemporalSplit,
    *,
    policy: DraftMinimumRowsPolicy | None = None,
) -> None:
    minimums = policy or DraftMinimumRowsPolicy()
    if len(dataset) < minimums.minimum_total_rows:
        raise DraftTrainingDataError(
            "Not enough usable post-draft rows: "
            f"{len(dataset)} found, {minimums.minimum_total_rows} required."
        )
    if split.train_rows < minimums.minimum_train_rows:
        raise DraftTrainingDataError(
            "Not enough draft train rows after temporal split: "
            f"{split.train_rows} found, {minimums.minimum_train_rows} required."
        )
    if split.validation_rows < minimums.minimum_validation_rows:
        raise DraftTrainingDataError(
            "Not enough draft validation rows after temporal split: "
            f"{split.validation_rows} found, {minimums.minimum_validation_rows} required."
        )
    if split.test_rows < minimums.minimum_test_rows:
        raise DraftTrainingDataError(
            "Not enough draft test rows after temporal split: "
            f"{split.test_rows} found, {minimums.minimum_test_rows} required."
        )


def _timestamp_groups(
    indices: Sequence[int],
    dataset: DraftMapDataset,
) -> tuple[tuple[int, ...], ...]:
    groups: list[list[int]] = []
    previous: datetime | None = None
    for index in indices:
        timestamp = dataset.metadata[index].prediction_timestamp
        if previous is None or timestamp != previous:
            groups.append([])
            previous = timestamp
        groups[-1].append(index)
    return tuple(tuple(group) for group in groups)
