from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import math

import numpy as np
from numpy.typing import NDArray

from app.history import HISTORICAL_NUMERIC_FEATURE_COLUMNS
from app.history.features import HistoricalFeatureRow, LabeledHistoricalFeatureRow


HISTORICAL_FEATURE_SCHEMA_VERSION = 1
HISTORICAL_ML_FEATURE_NAMES: tuple[str, ...] = tuple(
    HISTORICAL_NUMERIC_FEATURE_COLUMNS
)


@dataclass(frozen=True)
class HistoricalMatchRowMetadata:
    source: str
    source_match_id: str
    prediction_timestamp: datetime
    team_a_source_id: str
    team_b_source_id: str
    target_match_id: str | None
    tournament_source_id: str | None
    tournament_name: str | None


@dataclass(frozen=True)
class HistoricalMatchDataset:
    x: NDArray[np.float64]
    y: NDArray[np.int_]
    metadata: tuple[HistoricalMatchRowMetadata, ...]
    feature_names: tuple[str, ...] = HISTORICAL_ML_FEATURE_NAMES

    def __len__(self) -> int:
        return len(self.y)


def build_historical_ml_dataset(
    rows: Iterable[LabeledHistoricalFeatureRow],
    *,
    feature_names: Sequence[str] = HISTORICAL_ML_FEATURE_NAMES,
) -> HistoricalMatchDataset:
    ordered_rows = tuple(
        sorted(rows, key=lambda row: _metadata_order_key(row.feature_row))
    )
    names = tuple(feature_names)
    x_values = [
        row_to_feature_vector(row.feature_row, feature_names=names)
        for row in ordered_rows
    ]
    x = np.asarray(x_values, dtype=np.float64)
    if not x_values:
        x = np.empty((0, len(names)), dtype=np.float64)

    return HistoricalMatchDataset(
        x=x,
        y=np.asarray([row.target for row in ordered_rows], dtype=np.int_),
        metadata=tuple(
            _metadata_from_feature_row(row.feature_row)
            for row in ordered_rows
        ),
        feature_names=names,
    )


def row_to_feature_vector(
    row: HistoricalFeatureRow,
    *,
    feature_names: Sequence[str] = HISTORICAL_ML_FEATURE_NAMES,
) -> tuple[float, ...]:
    return feature_mapping_to_vector(
        row.numeric_features(),
        feature_names=feature_names,
    )


def feature_mapping_to_vector(
    features: Mapping[str, object],
    *,
    feature_names: Sequence[str] = HISTORICAL_ML_FEATURE_NAMES,
) -> tuple[float, ...]:
    return tuple(
        _finite_numeric_value(features[name], feature_name=name)
        for name in feature_names
    )


def _finite_numeric_value(value: object, *, feature_name: str) -> float:
    if not isinstance(value, int | float):
        raise ValueError(f"Historical ML feature is not numeric: {feature_name}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Historical ML feature is not finite: {feature_name}")
    return result


def _metadata_from_feature_row(
    row: HistoricalFeatureRow,
) -> HistoricalMatchRowMetadata:
    return HistoricalMatchRowMetadata(
        source=row.source,
        source_match_id=row.source_match_id,
        prediction_timestamp=row.prediction_timestamp,
        team_a_source_id=row.team_a_source_id,
        team_b_source_id=row.team_b_source_id,
        target_match_id=row.target_match_id,
        tournament_source_id=row.tournament_source_id,
        tournament_name=row.tournament_name,
    )


def _metadata_order_key(
    row: HistoricalFeatureRow,
) -> tuple[datetime, str, str]:
    return (row.prediction_timestamp, row.source, row.source_match_id)
