from pathlib import Path

import numpy as np

from app.historical_ml import HISTORICAL_ML_FEATURE_NAMES, build_historical_ml_dataset
from app.history import build_labeled_historical_feature_row
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_historical_ml_dataset_is_chronological_numeric_and_metadata_free(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    earlier = make_historical_match("earlier", winner_side="team_a")
    later = make_historical_match(
        "later",
        started_at=earlier.started_at.replace(day=2),
        ended_at=earlier.ended_at.replace(day=2) if earlier.ended_at else None,
        winner_side="team_b",
    )
    for match in (later, earlier):
        repository.save_historical_match(match)

    rows = [
        row
        for match in (later, earlier)
        if (row := build_labeled_historical_feature_row(match, repository))
        is not None
    ]
    dataset = build_historical_ml_dataset(rows)

    assert [metadata.source_match_id for metadata in dataset.metadata] == [
        "earlier",
        "later",
    ]
    assert dataset.y.tolist() == [1, 0]
    assert dataset.x.shape == (2, len(HISTORICAL_ML_FEATURE_NAMES))
    assert np.isfinite(dataset.x).all()
    excluded = {
        "source",
        "source_match_id",
        "prediction_timestamp",
        "team_a_source_id",
        "team_b_source_id",
        "winner_name",
        "winner_source_id",
        "winner_side",
        "target",
    }
    assert excluded.isdisjoint(HISTORICAL_ML_FEATURE_NAMES)
    assert "stage_group" in HISTORICAL_ML_FEATURE_NAMES


def test_target_label_permutation_does_not_change_x(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match = make_historical_match("target", winner_side="team_a")
    repository.save_historical_match(match)
    row = build_labeled_historical_feature_row(match, repository)
    assert row is not None

    original = build_historical_ml_dataset([row])
    permuted = build_historical_ml_dataset(
        [type(row)(feature_row=row.feature_row, target=1 - row.target)]
    )

    assert np.array_equal(original.x, permuted.x)
    assert original.y.tolist() == [1]
    assert permuted.y.tolist() == [0]
