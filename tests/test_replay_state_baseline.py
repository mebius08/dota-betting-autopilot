from __future__ import annotations

from collections.abc import Callable, Sequence
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

import pytest

from app import replay_state_baseline as baseline
from app.cli import main
from app.replay_state_training import TARGET_COLUMNS, TRAINING_COLUMNS


def test_exact_expanding_window_match_splits_have_no_overlap() -> None:
    match_ids = list(reversed(range(100, 205)))

    splits = baseline.build_expanding_match_splits(match_ids)

    assert len(splits) == 5
    for fold, train_count in enumerate((50, 60, 70, 80, 90), start=1):
        split = splits[fold - 1]
        assert split.fold == fold
        assert split.train_match_ids == tuple(range(100, 100 + train_count))
        assert split.test_match_ids == tuple(
            range(100 + train_count, 110 + train_count)
        )
        assert set(split.train_match_ids).isdisjoint(split.test_match_ids)


def test_split_requires_one_hundred_unique_matches() -> None:
    with pytest.raises(baseline.ReplayStateBaselineError, match="at least 100"):
        baseline.build_expanding_match_splits([*range(99), 98])


def test_target_and_lineage_columns_are_excluded_from_features() -> None:
    features = baseline._select_feature_columns(TRAINING_COLUMNS)

    assert len(features) == 26
    assert "match_id" not in features
    assert "game_time_seconds" not in features
    assert set(TARGET_COLUMNS).isdisjoint(features)
    assert all(not column.startswith("target_") for column in features)


def test_fold_predictors_use_zero_training_median_and_momentum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    train_rows = (
        _unit_row(1, 180, target=-2.0, momentum=3.0),
        _unit_row(2, 180, target=4.0, momentum=-5.0),
    )
    test_rows = (_unit_row(3, 180, target=8.0, momentum=7.0),)
    monkeypatch.setattr(
        baseline,
        "_predict_catboost",
        lambda train, test, features: [0.5],
    )

    predictions, training_median = baseline._evaluate_fold(
        1,
        train_rows,
        test_rows,
        (baseline.MOMENTUM_COLUMN,),
    )

    assert training_median == 1.0
    assert predictions[0].zero_prediction == 0.0
    assert predictions[0].train_median_prediction == 1.0
    assert predictions[0].momentum_180_prediction == 7.0
    assert predictions[0].catboost_prediction == 0.5


def test_metric_calculations_and_sign_rules() -> None:
    metrics = baseline.calculate_metrics(
        [2.0, -2.0, 0.0, 3.0],
        [1.0, 0.0, 0.0, -1.0],
        [1, 1, 2, 2],
    )

    assert metrics["row_count"] == 4
    assert metrics["match_count"] == 2
    assert metrics["MAE"] == 1.75
    assert metrics["RMSE"] == pytest.approx(math.sqrt(21.0 / 4.0))
    assert metrics["median_absolute_error"] == 1.5
    assert metrics["sign_accuracy"] == 0.5


def test_source_hash_rejection(tmp_path: Path) -> None:
    training_dir = _write_training_dir(tmp_path / "training")
    path = training_dir / baseline.TRAINING_FILENAME
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(baseline.ReplayStateBaselineError, match="hash mismatch"):
        baseline.evaluate_replay_state_baseline(training_dir, tmp_path / "output")


def test_source_count_rejection(tmp_path: Path) -> None:
    training_dir = _write_training_dir(tmp_path / "training")
    manifest = _read_manifest(training_dir)
    manifest["training_row_count"] += 1
    manifest["training_file"]["row_count"] += 1
    _write_manifest(training_dir, manifest)

    with pytest.raises(baseline.ReplayStateBaselineError, match="row-count mismatch"):
        baseline.evaluate_replay_state_baseline(training_dir, tmp_path / "output")


def test_manifest_filename_rejection(tmp_path: Path) -> None:
    training_dir = _write_training_dir(tmp_path / "training")
    manifest = _read_manifest(training_dir)
    manifest["training_file"]["filename"] = "other.csv"
    _write_manifest(training_dir, manifest)

    with pytest.raises(baseline.ReplayStateBaselineError, match="filename must be"):
        baseline.evaluate_replay_state_baseline(training_dir, tmp_path / "output")


def test_duplicate_key_rejection(tmp_path: Path) -> None:
    training_dir = _write_training_dir(tmp_path / "training")

    def duplicate(rows: list[dict[str, str]]) -> None:
        rows.append(dict(rows[0]))

    _rewrite_training_csv(training_dir, duplicate)

    with pytest.raises(baseline.ReplayStateBaselineError, match="Duplicate"):
        baseline.evaluate_replay_state_baseline(training_dir, tmp_path / "output")


def test_malformed_numeric_rejection(tmp_path: Path) -> None:
    training_dir = _write_training_dir(tmp_path / "training")

    def make_non_finite(rows: list[dict[str, str]]) -> None:
        rows[0]["kill_diff"] = "nan"

    _rewrite_training_csv(training_dir, make_non_finite)

    with pytest.raises(baseline.ReplayStateBaselineError, match="Malformed numeric"):
        baseline.evaluate_replay_state_baseline(training_dir, tmp_path / "output")


def test_deterministic_cli_rerun_prediction_order_hash_and_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    training_dir = _write_training_dir(tmp_path / "training", reverse=True)
    output_dir = tmp_path / "output"
    monkeypatch.setattr(baseline, "_predict_catboost", _fake_catboost)
    arguments = [
        "evaluate-replay-state-baseline",
        "--training-dir",
        str(training_dir),
        "--output-dir",
        str(output_dir),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    first_prediction_bytes = (output_dir / baseline.PREDICTIONS_FILENAME).read_bytes()
    first_evaluation_bytes = (output_dir / baseline.EVALUATION_FILENAME).read_bytes()

    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"
    assert (output_dir / baseline.PREDICTIONS_FILENAME).read_bytes() == first_prediction_bytes
    assert (output_dir / baseline.EVALUATION_FILENAME).read_bytes() == first_evaluation_bytes

    prediction_rows = _read_csv(output_dir / baseline.PREDICTIONS_FILENAME)
    prediction_keys = [
        (int(row["fold"]), int(row["match_id"]), int(row["game_time_seconds"]))
        for row in prediction_rows
    ]
    assert prediction_keys == sorted(prediction_keys)
    assert all(row["zero_prediction"] == "0" for row in prediction_rows)

    evaluation = json.loads(first_evaluation_bytes)
    prediction_file = evaluation["prediction_file"]
    assert prediction_file["filename"] == baseline.PREDICTIONS_FILENAME
    assert prediction_file["row_count"] == len(prediction_rows) == 50
    assert prediction_file["sha256"] == hashlib.sha256(
        first_prediction_bytes
    ).hexdigest()
    assert evaluation["target"] == baseline.TARGET_COLUMN
    assert set(TARGET_COLUMNS).isdisjoint(evaluation["feature_columns"])
    assert evaluation["catboost_parameters"]["thread_count"] == 1


def _unit_row(
    match_id: int,
    game_time_seconds: int,
    *,
    target: float,
    momentum: float,
) -> baseline._TrainingRow:
    return baseline._TrainingRow(
        match_id=match_id,
        game_time_seconds=game_time_seconds,
        values={
            baseline.TARGET_COLUMN: target,
            baseline.MOMENTUM_COLUMN: momentum,
        },
    )


def _fake_catboost(
    train_rows: Sequence[baseline._TrainingRow],
    test_rows: Sequence[baseline._TrainingRow],
    feature_columns: Sequence[str],
) -> list[float]:
    assert train_rows
    assert feature_columns
    return [row.values[baseline.TARGET_COLUMN] + 0.25 for row in test_rows]


def _write_training_dir(
    directory: Path,
    *,
    reverse: bool = False,
) -> Path:
    rows: list[dict[str, object]] = []
    for index, match_id in enumerate(range(1000, 1100)):
        row: dict[str, object] = {}
        for column_number, column in enumerate(TRAINING_COLUMNS):
            if column == "match_id":
                value: object = match_id
            elif column == "game_time_seconds":
                value = 180
            elif column == baseline.TARGET_COLUMN:
                value = (index % 9) - 4
            elif column == baseline.MOMENTUM_COLUMN:
                value = (index % 7) - 3
            elif column in TARGET_COLUMNS:
                value = (index % 5) - 2
            else:
                value = index + column_number
            row[column] = value
        rows.append(row)
    if reverse:
        rows.reverse()
    directory.mkdir()
    training_bytes = _csv_bytes(TRAINING_COLUMNS, rows)
    (directory / baseline.TRAINING_FILENAME).write_bytes(training_bytes)
    manifest = {
        "schema_version": 1,
        "training_row_count": len(rows),
        "training_file": {
            "filename": baseline.TRAINING_FILENAME,
            "row_count": len(rows),
            "sha256": hashlib.sha256(training_bytes).hexdigest(),
        },
    }
    _write_manifest(directory, manifest)
    return directory


def _rewrite_training_csv(
    directory: Path,
    change: Callable[[list[dict[str, str]]], None],
) -> None:
    path = directory / baseline.TRAINING_FILENAME
    rows = _read_csv(path)
    change(rows)
    training_bytes = _csv_bytes(TRAINING_COLUMNS, rows)
    path.write_bytes(training_bytes)
    manifest = _read_manifest(directory)
    manifest["training_row_count"] = len(rows)
    manifest["training_file"]["row_count"] = len(rows)
    manifest["training_file"]["sha256"] = hashlib.sha256(
        training_bytes
    ).hexdigest()
    _write_manifest(directory, manifest)


def _csv_bytes(
    columns: Sequence[str],
    rows: Sequence[dict[str, object] | dict[str, str]],
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=",", lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row[column] for column in columns)
    return stream.getvalue().encode("utf-8")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _read_manifest(directory: Path) -> dict[str, Any]:
    return json.loads((directory / baseline.MANIFEST_FILENAME).read_bytes())


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    text = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)
    (directory / baseline.MANIFEST_FILENAME).write_text(
        f"{text}\n", encoding="utf-8", newline="\n"
    )
