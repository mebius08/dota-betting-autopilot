from __future__ import annotations

from collections.abc import Iterable, Sequence
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
from statistics import median
import tempfile
from typing import Any, Literal, cast

from app.replay_state_training import (
    MANIFEST_FILENAME,
    TARGET_COLUMNS,
    TRAINING_COLUMNS,
    TRAINING_FILENAME,
)


EVALUATION_SCHEMA_VERSION = 1
PREDICTIONS_FILENAME = "predictions.csv"
EVALUATION_FILENAME = "evaluation.json"
OUTPUT_FILENAMES = (PREDICTIONS_FILENAME, EVALUATION_FILENAME)
TARGET_COLUMN = "target_net_worth_diff_change_180"
MOMENTUM_COLUMN = "net_worth_diff_change_prev_180"
LINEAGE_COLUMNS = ("match_id", "game_time_seconds")
EXCLUDED_FEATURE_COLUMNS = frozenset((*LINEAGE_COLUMNS, *TARGET_COLUMNS))
PREDICTOR_NAMES = ("zero", "train_median", "momentum_180", "catboost")
PREDICTION_COLUMNS = (
    "match_id",
    "game_time_seconds",
    "fold",
    "actual",
    "zero_prediction",
    "train_median_prediction",
    "momentum_180_prediction",
    "catboost_prediction",
)
TRAIN_MATCH_COUNTS = (50, 60, 70, 80, 90)
TEST_MATCH_COUNT = 10
MINIMUM_MATCH_COUNT = 100
_INTEGER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ReplayStateBaselineError(ValueError):
    """Raised when replay state baseline evaluation cannot run safely."""


@dataclass(frozen=True)
class ReplayStateBaselineResult:
    status: Literal["BUILT", "UNCHANGED"]
    output_dir: Path
    prediction_row_count: int
    prediction_sha256: str
    source_manifest_sha256: str


@dataclass(frozen=True)
class MatchSplit:
    fold: int
    train_match_ids: tuple[int, ...]
    test_match_ids: tuple[int, ...]


@dataclass(frozen=True)
class _TrainingManifest:
    sha256: str
    row_count: int
    training_sha256: str


@dataclass(frozen=True)
class _TrainingRow:
    match_id: int
    game_time_seconds: int
    values: dict[str, float]


@dataclass(frozen=True)
class _Dataset:
    feature_columns: tuple[str, ...]
    rows: tuple[_TrainingRow, ...]


@dataclass(frozen=True)
class _PredictionRow:
    match_id: int
    game_time_seconds: int
    fold: int
    actual: float
    zero_prediction: float
    train_median_prediction: float
    momentum_180_prediction: float
    catboost_prediction: float


def catboost_parameters() -> dict[str, object]:
    return {
        "loss_function": "RMSE",
        "iterations": 40,
        "depth": 4,
        "learning_rate": 0.05,
        "random_seed": 42,
        "thread_count": 1,
        "allow_writing_files": False,
        "verbose": False,
    }


def build_expanding_match_splits(match_ids: Iterable[int]) -> tuple[MatchSplit, ...]:
    ordered = tuple(sorted(set(match_ids)))
    if len(ordered) < MINIMUM_MATCH_COUNT:
        raise ReplayStateBaselineError(
            "Replay state baseline evaluation requires at least 100 unique matches; "
            f"got {len(ordered)}."
        )
    return tuple(
        MatchSplit(
            fold=fold,
            train_match_ids=ordered[:train_count],
            test_match_ids=ordered[train_count : train_count + TEST_MATCH_COUNT],
        )
        for fold, train_count in enumerate(TRAIN_MATCH_COUNTS, start=1)
    )


def calculate_metrics(
    actual: Sequence[float],
    predicted: Sequence[float],
    match_ids: Sequence[int],
) -> dict[str, int | float]:
    if not actual or len(actual) != len(predicted) or len(actual) != len(match_ids):
        raise ReplayStateBaselineError(
            "Metrics require equally sized, non-empty actual, prediction, and match lists."
        )
    absolute_errors = [abs(observed - estimate) for observed, estimate in zip(actual, predicted, strict=True)]
    squared_errors = [
        (observed - estimate) ** 2
        for observed, estimate in zip(actual, predicted, strict=True)
    ]
    sign_correct = sum(
        _sign_is_correct(observed, estimate)
        for observed, estimate in zip(actual, predicted, strict=True)
    )
    count = len(actual)
    return {
        "row_count": count,
        "match_count": len(set(match_ids)),
        "MAE": sum(absolute_errors) / count,
        "RMSE": math.sqrt(sum(squared_errors) / count),
        "median_absolute_error": float(median(absolute_errors)),
        "sign_accuracy": sign_correct / count,
    }


def evaluate_replay_state_baseline(
    training_dir: str | Path,
    output_dir: str | Path,
) -> ReplayStateBaselineResult:
    source_dir = Path(training_dir)
    manifest_bytes = _read_source_file(source_dir, MANIFEST_FILENAME)
    manifest = _load_training_manifest(manifest_bytes)
    training_bytes = _read_source_file(source_dir, TRAINING_FILENAME)
    _validate_source_hash(training_bytes, manifest.training_sha256)
    dataset = _load_training_dataset(training_bytes, manifest.row_count)
    splits = build_expanding_match_splits(row.match_id for row in dataset.rows)

    predictions: list[_PredictionRow] = []
    folds: list[dict[str, object]] = []
    for split in splits:
        train_ids = set(split.train_match_ids)
        test_ids = set(split.test_match_ids)
        if train_ids & test_ids:
            raise ReplayStateBaselineError(
                f"Fold {split.fold} has overlapping train and test matches."
            )
        train_rows = tuple(row for row in dataset.rows if row.match_id in train_ids)
        test_rows = tuple(row for row in dataset.rows if row.match_id in test_ids)
        if not train_rows or not test_rows:
            raise ReplayStateBaselineError(
                f"Fold {split.fold} has no training or test rows."
            )
        fold_predictions, training_median = _evaluate_fold(
            split.fold,
            train_rows,
            test_rows,
            dataset.feature_columns,
        )
        predictions.extend(fold_predictions)
        folds.append(
            {
                "fold": split.fold,
                "train_match_ids": list(split.train_match_ids),
                "test_match_ids": list(split.test_match_ids),
                "train_row_count": len(train_rows),
                "test_row_count": len(test_rows),
                "train_target_median": training_median,
                "metrics": _metrics_by_predictor(fold_predictions),
            }
        )

    predictions.sort(key=lambda row: (row.fold, row.match_id, row.game_time_seconds))
    prediction_bytes = _render_predictions(predictions)
    prediction_sha256 = _sha256(prediction_bytes)
    aggregate_metrics = _metrics_by_predictor(predictions)
    evaluation_bytes = _render_evaluation(
        source_manifest_sha256=manifest.sha256,
        feature_columns=dataset.feature_columns,
        folds=folds,
        aggregate_metrics=aggregate_metrics,
        prediction_row_count=len(predictions),
        prediction_sha256=prediction_sha256,
    )
    outputs = {
        PREDICTIONS_FILENAME: prediction_bytes,
        EVALUATION_FILENAME: evaluation_bytes,
    }
    destination = Path(output_dir)
    if _existing_output_set_is_identical(destination, outputs):
        status: Literal["BUILT", "UNCHANGED"] = "UNCHANGED"
    else:
        _replace_output_set(destination, outputs)
        status = "BUILT"
    return ReplayStateBaselineResult(
        status=status,
        output_dir=destination,
        prediction_row_count=len(predictions),
        prediction_sha256=prediction_sha256,
        source_manifest_sha256=manifest.sha256,
    )


def _select_feature_columns(columns: Sequence[str]) -> tuple[str, ...]:
    if tuple(columns) != TRAINING_COLUMNS:
        raise ReplayStateBaselineError(
            "replay_state_training.csv does not match the fixed training schema."
        )
    features = tuple(
        column for column in columns if column not in EXCLUDED_FEATURE_COLUMNS
    )
    if not features or MOMENTUM_COLUMN not in features:
        raise ReplayStateBaselineError("Replay state feature schema is incomplete.")
    if any(column.startswith("target_") for column in features):
        raise ReplayStateBaselineError("Target columns cannot be used as features.")
    return features


def _evaluate_fold(
    fold: int,
    train_rows: Sequence[_TrainingRow],
    test_rows: Sequence[_TrainingRow],
    feature_columns: Sequence[str],
) -> tuple[list[_PredictionRow], float]:
    training_median = float(median(row.values[TARGET_COLUMN] for row in train_rows))
    catboost_predictions = _predict_catboost(train_rows, test_rows, feature_columns)
    if len(catboost_predictions) != len(test_rows):
        raise ReplayStateBaselineError("CatBoost returned an unexpected prediction count.")
    predictions: list[_PredictionRow] = []
    for row, catboost_prediction in zip(
        test_rows, catboost_predictions, strict=True
    ):
        if not math.isfinite(catboost_prediction):
            raise ReplayStateBaselineError("CatBoost returned a non-finite prediction.")
        predictions.append(
            _PredictionRow(
                match_id=row.match_id,
                game_time_seconds=row.game_time_seconds,
                fold=fold,
                actual=row.values[TARGET_COLUMN],
                zero_prediction=0.0,
                train_median_prediction=training_median,
                momentum_180_prediction=row.values[MOMENTUM_COLUMN],
                catboost_prediction=catboost_prediction,
            )
        )
    return predictions, training_median


def _predict_catboost(
    train_rows: Sequence[_TrainingRow],
    test_rows: Sequence[_TrainingRow],
    feature_columns: Sequence[str],
) -> list[float]:
    try:
        from catboost import CatBoostRegressor
    except ModuleNotFoundError as exc:
        raise ReplayStateBaselineError(
            "CatBoost is not installed. Install project dependencies first."
        ) from exc
    model = CatBoostRegressor(**catboost_parameters())
    train_features = [
        [row.values[column] for column in feature_columns] for row in train_rows
    ]
    train_target = [row.values[TARGET_COLUMN] for row in train_rows]
    test_features = [
        [row.values[column] for column in feature_columns] for row in test_rows
    ]
    model.fit(train_features, train_target)
    raw_predictions = model.predict(test_features)
    return [float(value) for value in raw_predictions]


def _metrics_by_predictor(
    rows: Sequence[_PredictionRow],
) -> dict[str, dict[str, int | float]]:
    actual = [row.actual for row in rows]
    match_ids = [row.match_id for row in rows]
    predictions = {
        "zero": [row.zero_prediction for row in rows],
        "train_median": [row.train_median_prediction for row in rows],
        "momentum_180": [row.momentum_180_prediction for row in rows],
        "catboost": [row.catboost_prediction for row in rows],
    }
    return {
        name: calculate_metrics(actual, predictions[name], match_ids)
        for name in PREDICTOR_NAMES
    }


def _relative_improvements(
    aggregate_metrics: dict[str, dict[str, int | float]],
) -> dict[str, dict[str, float | None]]:
    catboost = aggregate_metrics["catboost"]
    improvements: dict[str, dict[str, float | None]] = {}
    for baseline_name in ("zero", "momentum_180"):
        baseline = aggregate_metrics[baseline_name]
        values: dict[str, float | None] = {}
        for metric in ("MAE", "RMSE"):
            baseline_value = float(baseline[metric])
            catboost_value = float(catboost[metric])
            values[metric] = (
                None
                if baseline_value == 0.0
                else (baseline_value - catboost_value) / baseline_value
            )
        improvements[f"catboost_vs_{baseline_name}"] = values
    return improvements


def _sign_is_correct(actual: float, predicted: float) -> bool:
    if actual == 0.0:
        return predicted == 0.0
    if predicted == 0.0:
        return False
    return (actual > 0.0) == (predicted > 0.0)


def _load_training_manifest(content: bytes) -> _TrainingManifest:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayStateBaselineError(
            "Training manifest is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ReplayStateBaselineError("Training manifest root must be an object.")
    schema_version = payload.get("schema_version")
    if schema_version != 1:
        raise ReplayStateBaselineError(
            f"Unsupported training manifest schema_version {schema_version}."
        )
    row_count = _manifest_nonnegative_int(payload, "training_row_count")
    training_file = payload.get("training_file")
    if not isinstance(training_file, dict):
        raise ReplayStateBaselineError(
            "Training manifest training_file must be an object."
        )
    filename = training_file.get("filename")
    if filename != TRAINING_FILENAME:
        raise ReplayStateBaselineError(
            f"Training manifest filename must be {TRAINING_FILENAME}."
        )
    file_row_count = _manifest_nonnegative_int(training_file, "row_count")
    if file_row_count != row_count:
        raise ReplayStateBaselineError(
            "Training manifest row counts are inconsistent."
        )
    training_sha256 = training_file.get("sha256")
    if not isinstance(training_sha256, str) or _SHA256_PATTERN.fullmatch(
        training_sha256
    ) is None:
        raise ReplayStateBaselineError(
            "Training manifest sha256 must be a lowercase SHA-256 digest."
        )
    return _TrainingManifest(
        sha256=_sha256(content),
        row_count=row_count,
        training_sha256=training_sha256,
    )


def _manifest_nonnegative_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise ReplayStateBaselineError(
            f"Training manifest {key} must be a non-negative integer."
        )
    return value


def _load_training_dataset(content: bytes, expected_row_count: int) -> _Dataset:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReplayStateBaselineError(
            f"{TRAINING_FILENAME} is not valid UTF-8."
        ) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = tuple(reader.fieldnames or ())
    feature_columns = _select_feature_columns(columns)
    rows: list[_TrainingRow] = []
    keys: set[tuple[int, int]] = set()
    for row_number, raw_row in enumerate(reader, start=2):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise ReplayStateBaselineError(
                f"Malformed row {row_number} in {TRAINING_FILENAME}."
            )
        row = cast(dict[str, str], raw_row)
        match_id = _parse_integer(row["match_id"], "match_id", row_number)
        game_time = _parse_integer(
            row["game_time_seconds"], "game_time_seconds", row_number
        )
        key = (match_id, game_time)
        if key in keys:
            raise ReplayStateBaselineError(
                "Duplicate replay state training key "
                f"(match_id={match_id}, game_time_seconds={game_time})."
            )
        keys.add(key)
        values = {
            column: _parse_number(row[column], column, row_number)
            for column in TRAINING_COLUMNS
            if column not in LINEAGE_COLUMNS
        }
        rows.append(
            _TrainingRow(
                match_id=match_id,
                game_time_seconds=game_time,
                values=values,
            )
        )
    if len(rows) != expected_row_count:
        raise ReplayStateBaselineError(
            f"Training row-count mismatch: expected {expected_row_count}, "
            f"got {len(rows)}."
        )
    rows.sort(key=lambda row: (row.match_id, row.game_time_seconds))
    return _Dataset(feature_columns=feature_columns, rows=tuple(rows))


def _parse_integer(value: str, column: str, row_number: int) -> int:
    if _INTEGER_PATTERN.fullmatch(value) is None:
        raise ReplayStateBaselineError(
            f"Malformed numeric value for {column} in row {row_number}."
        )
    return int(value)


def _parse_number(value: str, column: str, row_number: int) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise ReplayStateBaselineError(
            f"Malformed numeric value for {column} in row {row_number}."
        ) from exc
    if not math.isfinite(result):
        raise ReplayStateBaselineError(
            f"Malformed numeric value for {column} in row {row_number}."
        )
    return result


def _read_source_file(directory: Path, filename: str) -> bytes:
    if not directory.exists():
        raise ReplayStateBaselineError(
            f"Training directory does not exist: {directory.as_posix()}."
        )
    if not directory.is_dir():
        raise ReplayStateBaselineError(
            f"Training path is not a directory: {directory.as_posix()}."
        )
    try:
        return (directory / filename).read_bytes()
    except OSError as exc:
        raise ReplayStateBaselineError(
            f"Cannot read required source file {filename}."
        ) from exc


def _validate_source_hash(content: bytes, expected: str) -> None:
    actual = _sha256(content)
    if actual != expected:
        raise ReplayStateBaselineError(
            f"Source hash mismatch for {TRAINING_FILENAME}: "
            f"expected {expected}, got {actual}."
        )


def _render_predictions(rows: Sequence[_PredictionRow]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=",", lineterminator="\n")
    writer.writerow(PREDICTION_COLUMNS)
    previous_key: tuple[int, int, int] | None = None
    for row in rows:
        key = (row.fold, row.match_id, row.game_time_seconds)
        if previous_key is not None and key <= previous_key:
            raise ReplayStateBaselineError("Prediction rows are not strictly ordered.")
        previous_key = key
        writer.writerow(
            (
                row.match_id,
                row.game_time_seconds,
                row.fold,
                _format_float(row.actual),
                _format_float(row.zero_prediction),
                _format_float(row.train_median_prediction),
                _format_float(row.momentum_180_prediction),
                _format_float(row.catboost_prediction),
            )
        )
    return stream.getvalue().encode("utf-8")


def _format_float(value: float) -> str:
    if value == 0.0:
        return "0"
    if value.is_integer():
        return str(int(value))
    return repr(value)


def _render_evaluation(
    *,
    source_manifest_sha256: str,
    feature_columns: Sequence[str],
    folds: Sequence[dict[str, object]],
    aggregate_metrics: dict[str, dict[str, int | float]],
    prediction_row_count: int,
    prediction_sha256: str,
) -> bytes:
    payload = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "source_manifest_sha256": source_manifest_sha256,
        "target": TARGET_COLUMN,
        "feature_columns": list(feature_columns),
        "split_strategy": {
            "name": "deterministic_expanding_window_by_match_id",
            "match_order": "numeric_ascending",
            "fold_count": len(TRAIN_MATCH_COUNTS),
            "minimum_unique_matches": MINIMUM_MATCH_COUNT,
            "train_match_counts": list(TRAIN_MATCH_COUNTS),
            "test_match_count": TEST_MATCH_COUNT,
            "shuffle": False,
        },
        "catboost_parameters": catboost_parameters(),
        "folds": list(folds),
        "aggregate_metrics": aggregate_metrics,
        "relative_improvements": _relative_improvements(aggregate_metrics),
        "prediction_file": {
            "filename": PREDICTIONS_FILENAME,
            "row_count": prediction_row_count,
            "sha256": prediction_sha256,
        },
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return f"{text}\n".encode("utf-8")


def _existing_output_set_is_identical(
    output_dir: Path,
    outputs: dict[str, bytes],
) -> bool:
    if not output_dir.exists():
        return False
    if not output_dir.is_dir():
        raise ReplayStateBaselineError(
            f"Output path is not a directory: {output_dir.as_posix()}."
        )
    _reject_unexpected_output_entries(output_dir)
    try:
        return all(
            (output_dir / filename).is_file()
            and (output_dir / filename).read_bytes() == content
            for filename, content in outputs.items()
        )
    except OSError as exc:
        raise ReplayStateBaselineError(
            "Existing replay state baseline outputs cannot be read."
        ) from exc


def _replace_output_set(output_dir: Path, outputs: dict[str, bytes]) -> None:
    parent = output_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReplayStateBaselineError(
            "Cannot create the replay state baseline parent directory."
        ) from exc
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ReplayStateBaselineError(
                f"Output path is not a directory: {output_dir.as_posix()}."
            )
        _reject_unexpected_output_entries(output_dir)

    transaction: Path | None = None
    old_output: Path | None = None
    installed_new = False
    rollback_failed = False
    try:
        transaction = Path(
            tempfile.mkdtemp(dir=parent, prefix=f".{output_dir.name}.transaction.")
        )
        new_output = transaction / "new"
        new_output.mkdir()
        for filename, content in outputs.items():
            _write_durable(new_output / filename, content)
        old_output = transaction / "old"
        if output_dir.exists():
            os.replace(output_dir, old_output)
        else:
            old_output = None
        try:
            os.replace(new_output, output_dir)
            installed_new = True
            if old_output is not None:
                shutil.rmtree(old_output)
                old_output = None
        except OSError:
            if installed_new and output_dir.exists():
                os.replace(output_dir, transaction / "failed-new")
                installed_new = False
            if old_output is not None and old_output.exists():
                os.replace(old_output, output_dir)
                old_output = None
            raise
    except OSError as exc:
        if old_output is not None and old_output.exists() and not output_dir.exists():
            try:
                os.replace(old_output, output_dir)
                old_output = None
            except OSError:
                rollback_failed = True
        message = "Could not atomically replace replay state baseline outputs."
        if rollback_failed:
            message += " Automatic rollback also failed."
        raise ReplayStateBaselineError(message) from exc
    finally:
        if transaction is not None and transaction.exists() and not rollback_failed:
            try:
                shutil.rmtree(transaction)
            except OSError as exc:
                if installed_new:
                    raise ReplayStateBaselineError(
                        "Baseline evaluation completed but temporary cleanup failed."
                    ) from exc


def _reject_unexpected_output_entries(output_dir: Path) -> None:
    try:
        unexpected = sorted(
            path.name
            for path in output_dir.iterdir()
            if path.name not in OUTPUT_FILENAMES
        )
    except OSError as exc:
        raise ReplayStateBaselineError(
            "Cannot inspect the replay state baseline output directory."
        ) from exc
    if unexpected:
        raise ReplayStateBaselineError(
            "Output directory contains unexpected entries; refusing to replace it: "
            f"{', '.join(unexpected)}."
        )


def _write_durable(path: Path, content: bytes) -> None:
    with path.open("wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
