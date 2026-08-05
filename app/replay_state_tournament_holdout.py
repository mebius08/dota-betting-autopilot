from __future__ import annotations

from collections.abc import Iterable, Sequence
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Literal

from app import replay_state_baseline as baseline
from app.replay_state_training import MANIFEST_FILENAME, TRAINING_FILENAME


EVALUATION_SCHEMA_VERSION = 1
PREDICTIONS_FILENAME = "predictions.csv"
EVALUATION_FILENAME = "evaluation.json"
TARGET_COLUMN = baseline.TARGET_COLUMN
MOMENTUM_COLUMN = baseline.MOMENTUM_COLUMN
PREDICTOR_NAMES = baseline.PREDICTOR_NAMES
PREDICTION_COLUMNS = (
    "match_id",
    "league_id",
    "game_time_seconds",
    "actual",
    "zero_prediction",
    "train_median_prediction",
    "momentum_180_prediction",
    "catboost_prediction",
)
TIME_BUCKETS = ("early", "mid", "late", "very_late")


class ReplayStateTournamentHoldoutError(ValueError):
    """Raised when tournament-holdout evaluation cannot run safely."""


@dataclass(frozen=True)
class ReplayStateTournamentHoldoutResult:
    status: Literal["BUILT", "UNCHANGED"]
    output_dir: Path
    prediction_row_count: int
    prediction_sha256: str
    source_manifest_sha256: str
    lineage_sha256: str


@dataclass(frozen=True)
class _PredictionRow:
    match_id: int
    league_id: int
    game_time_seconds: int
    actual: float
    zero_prediction: float
    train_median_prediction: float
    momentum_180_prediction: float
    catboost_prediction: float


MetricValue = int | float | None
Metrics = dict[str, MetricValue]
PredictorMetrics = dict[str, Metrics]


def build_match_to_league_lineage(
    trajectory_dir: str | Path,
) -> dict[int, int]:
    directory = Path(trajectory_dir)
    if not directory.exists():
        raise ReplayStateTournamentHoldoutError(
            f"Trajectory directory does not exist: {directory.as_posix()}."
        )
    if not directory.is_dir():
        raise ReplayStateTournamentHoldoutError(
            f"Trajectory path is not a directory: {directory.as_posix()}."
        )
    try:
        artifacts = sorted(
            (path for path in directory.iterdir() if path.suffix == ".json"),
            key=lambda path: path.name,
        )
    except OSError as exc:
        raise ReplayStateTournamentHoldoutError(
            "Cannot inspect the trajectory directory."
        ) from exc
    if not artifacts:
        raise ReplayStateTournamentHoldoutError(
            "Trajectory directory contains no canonical JSON artifacts."
        )

    lineage: dict[int, int] = {}
    for artifact in artifacts:
        payload = _load_trajectory_artifact(artifact)
        match_id = _positive_json_integer(payload, "match_id", artifact.name)
        league_id = _positive_json_integer(payload, "league_id", artifact.name)
        existing = lineage.get(match_id)
        if existing is not None and existing != league_id:
            raise ReplayStateTournamentHoldoutError(
                f"Conflicting lineage for match_id {match_id}: "
                f"league_id {existing} and {league_id}."
            )
        lineage[match_id] = league_id
    return dict(sorted(lineage.items()))


def evaluate_replay_state_tournament_holdout(
    training_dir: str | Path,
    trajectory_dir: str | Path,
    train_league_ids: Iterable[int],
    test_league_ids: Iterable[int],
    output_dir: str | Path,
) -> ReplayStateTournamentHoldoutResult:
    train_leagues = _normalize_league_ids(train_league_ids, "train")
    test_leagues = _normalize_league_ids(test_league_ids, "test")
    overlap = set(train_leagues) & set(test_leagues)
    if overlap:
        rendered = ", ".join(str(league_id) for league_id in sorted(overlap))
        raise ReplayStateTournamentHoldoutError(
            f"Train and test league sets overlap: {rendered}."
        )

    source_dir = Path(training_dir)
    try:
        manifest_bytes = baseline._read_source_file(source_dir, MANIFEST_FILENAME)
        manifest = baseline._load_training_manifest(manifest_bytes)
        training_bytes = baseline._read_source_file(source_dir, TRAINING_FILENAME)
        baseline._validate_source_hash(training_bytes, manifest.training_sha256)
        dataset = baseline._load_training_dataset(training_bytes, manifest.row_count)
    except baseline.ReplayStateBaselineError as exc:
        raise ReplayStateTournamentHoldoutError(str(exc)) from exc

    lineage = build_match_to_league_lineage(trajectory_dir)
    training_match_ids = {row.match_id for row in dataset.rows}
    missing_match_ids = sorted(training_match_ids - lineage.keys())
    if missing_match_ids:
        rendered = ", ".join(str(match_id) for match_id in missing_match_ids[:5])
        suffix = "..." if len(missing_match_ids) > 5 else ""
        raise ReplayStateTournamentHoldoutError(
            "Missing canonical lineage for training match_id values: "
            f"{rendered}{suffix}."
        )

    train_league_set = set(train_leagues)
    test_league_set = set(test_leagues)
    train_rows = tuple(
        row for row in dataset.rows if lineage[row.match_id] in train_league_set
    )
    test_rows = tuple(
        row for row in dataset.rows if lineage[row.match_id] in test_league_set
    )
    excluded_rows = tuple(
        row
        for row in dataset.rows
        if lineage[row.match_id] not in train_league_set | test_league_set
    )
    train_match_ids = {row.match_id for row in train_rows}
    test_match_ids = {row.match_id for row in test_rows}
    excluded_match_ids = {row.match_id for row in excluded_rows}
    if train_match_ids & test_match_ids:
        raise ReplayStateTournamentHoldoutError(
            "Train and test match sets must not overlap."
        )
    _require_rows_and_matches(train_rows, train_match_ids, "train")
    _require_rows_and_matches(test_rows, test_match_ids, "test")

    train_rows = tuple(
        sorted(train_rows, key=lambda row: (row.match_id, row.game_time_seconds))
    )
    test_rows = tuple(
        sorted(test_rows, key=lambda row: (row.match_id, row.game_time_seconds))
    )
    training_median = float(
        median(row.values[TARGET_COLUMN] for row in train_rows)
    )
    catboost_predictions = _predict_catboost(
        train_rows, test_rows, dataset.feature_columns
    )
    if len(catboost_predictions) != len(test_rows):
        raise ReplayStateTournamentHoldoutError(
            "CatBoost returned an unexpected prediction count."
        )

    predictions: list[_PredictionRow] = []
    for row, catboost_prediction in zip(
        test_rows, catboost_predictions, strict=True
    ):
        if not math.isfinite(catboost_prediction):
            raise ReplayStateTournamentHoldoutError(
                "CatBoost returned a non-finite prediction."
            )
        predictions.append(
            _PredictionRow(
                match_id=row.match_id,
                league_id=lineage[row.match_id],
                game_time_seconds=row.game_time_seconds,
                actual=row.values[TARGET_COLUMN],
                zero_prediction=0.0,
                train_median_prediction=training_median,
                momentum_180_prediction=row.values[MOMENTUM_COLUMN],
                catboost_prediction=catboost_prediction,
            )
        )

    prediction_bytes = _render_predictions(predictions)
    prediction_sha256 = _sha256(prediction_bytes)
    aggregate_metrics = _metrics_by_predictor(predictions)
    lineage_pairs = tuple(
        (match_id, lineage[match_id])
        for match_id in sorted(train_match_ids | test_match_ids)
    )
    lineage_sha256 = _lineage_sha256(lineage_pairs)
    evaluation_bytes = _render_evaluation(
        source_manifest_sha256=manifest.sha256,
        lineage_sha256=lineage_sha256,
        feature_columns=dataset.feature_columns,
        train_league_ids=train_leagues,
        test_league_ids=test_leagues,
        train_match_count=len(train_match_ids),
        train_row_count=len(train_rows),
        test_match_count=len(test_match_ids),
        test_row_count=len(test_rows),
        excluded_match_count=len(excluded_match_ids),
        excluded_row_count=len(excluded_rows),
        league_counts=_league_counts(dataset.rows, lineage, train_leagues, test_leagues),
        train_target_median=training_median,
        aggregate_metrics=aggregate_metrics,
        time_bucket_metrics=_time_bucket_metrics(predictions),
        prediction_row_count=len(predictions),
        prediction_sha256=prediction_sha256,
    )
    outputs = {
        PREDICTIONS_FILENAME: prediction_bytes,
        EVALUATION_FILENAME: evaluation_bytes,
    }
    destination = Path(output_dir)
    try:
        if baseline._existing_output_set_is_identical(destination, outputs):
            status: Literal["BUILT", "UNCHANGED"] = "UNCHANGED"
        else:
            baseline._replace_output_set(destination, outputs)
            status = "BUILT"
    except baseline.ReplayStateBaselineError as exc:
        raise ReplayStateTournamentHoldoutError(str(exc)) from exc

    return ReplayStateTournamentHoldoutResult(
        status=status,
        output_dir=destination,
        prediction_row_count=len(predictions),
        prediction_sha256=prediction_sha256,
        source_manifest_sha256=manifest.sha256,
        lineage_sha256=lineage_sha256,
    )


def _normalize_league_ids(
    values: Iterable[int], label: str
) -> tuple[int, ...]:
    league_ids = tuple(values)
    if not league_ids:
        raise ReplayStateTournamentHoldoutError(
            f"The {label} league set must be non-empty."
        )
    invalid = [value for value in league_ids if type(value) is not int or value <= 0]
    if invalid:
        raise ReplayStateTournamentHoldoutError(
            f"The {label} league set contains an invalid league ID."
        )
    return tuple(sorted(set(league_ids)))


def _load_trajectory_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayStateTournamentHoldoutError(
            f"Canonical trajectory artifact {path.name} is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ReplayStateTournamentHoldoutError(
            f"Canonical trajectory artifact {path.name} root must be an object."
        )
    return payload


def _positive_json_integer(
    payload: dict[str, Any], field: str, filename: str
) -> int:
    value = payload.get(field)
    if type(value) is not int or value <= 0:
        raise ReplayStateTournamentHoldoutError(
            f"Canonical trajectory artifact {filename} has invalid {field}."
        )
    return value


def _require_rows_and_matches(
    rows: Sequence[baseline._TrainingRow], match_ids: set[int], label: str
) -> None:
    if not match_ids:
        raise ReplayStateTournamentHoldoutError(
            f"The selected {label} leagues contain no training matches."
        )
    if not rows:
        raise ReplayStateTournamentHoldoutError(
            f"The selected {label} leagues contain no training rows."
        )


def _metrics_by_predictor(rows: Sequence[_PredictionRow]) -> PredictorMetrics:
    if not rows:
        return {name: _empty_metrics() for name in PREDICTOR_NAMES}
    actual = [row.actual for row in rows]
    match_ids = [row.match_id for row in rows]
    predictions = {
        "zero": [row.zero_prediction for row in rows],
        "train_median": [row.train_median_prediction for row in rows],
        "momentum_180": [row.momentum_180_prediction for row in rows],
        "catboost": [row.catboost_prediction for row in rows],
    }
    metrics_by_predictor: PredictorMetrics = {}
    for name in PREDICTOR_NAMES:
        metrics: Metrics = {}
        metrics.update(
            baseline.calculate_metrics(actual, predictions[name], match_ids)
        )
        metrics_by_predictor[name] = metrics
    return metrics_by_predictor


def _empty_metrics() -> Metrics:
    return {
        "row_count": 0,
        "match_count": 0,
        "MAE": None,
        "RMSE": None,
        "median_absolute_error": None,
        "sign_accuracy": None,
    }


def _time_bucket_name(game_time_seconds: int) -> str:
    if game_time_seconds < 600:
        return "early"
    if game_time_seconds < 1200:
        return "mid"
    if game_time_seconds < 1800:
        return "late"
    return "very_late"


def _time_bucket_metrics(
    rows: Sequence[_PredictionRow],
) -> dict[str, PredictorMetrics]:
    return {
        bucket: _metrics_by_predictor(
            [row for row in rows if _time_bucket_name(row.game_time_seconds) == bucket]
        )
        for bucket in TIME_BUCKETS
    }


def _league_counts(
    rows: Sequence[baseline._TrainingRow],
    lineage: dict[int, int],
    train_league_ids: Sequence[int],
    test_league_ids: Sequence[int],
) -> list[dict[str, int | str]]:
    matches_by_league: dict[int, set[int]] = {}
    rows_by_league: dict[int, int] = {}
    for row in rows:
        league_id = lineage[row.match_id]
        matches_by_league.setdefault(league_id, set()).add(row.match_id)
        rows_by_league[league_id] = rows_by_league.get(league_id, 0) + 1
    train_set = set(train_league_ids)
    test_set = set(test_league_ids)
    counts: list[dict[str, int | str]] = []
    for league_id in sorted(matches_by_league):
        selection = (
            "train"
            if league_id in train_set
            else "test"
            if league_id in test_set
            else "excluded"
        )
        counts.append(
            {
                "league_id": league_id,
                "selection": selection,
                "match_count": len(matches_by_league[league_id]),
                "row_count": rows_by_league[league_id],
            }
        )
    return counts


def _lineage_sha256(pairs: Sequence[tuple[int, int]]) -> str:
    content = "".join(
        f"{match_id},{league_id}\n" for match_id, league_id in sorted(pairs)
    ).encode("utf-8")
    return _sha256(content)


def _render_predictions(rows: Sequence[_PredictionRow]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=",", lineterminator="\n")
    writer.writerow(PREDICTION_COLUMNS)
    previous_key: tuple[int, int] | None = None
    for row in rows:
        key = (row.match_id, row.game_time_seconds)
        if previous_key is not None and key <= previous_key:
            raise ReplayStateTournamentHoldoutError(
                "Prediction rows are not strictly ordered."
            )
        previous_key = key
        writer.writerow(
            (
                row.match_id,
                row.league_id,
                row.game_time_seconds,
                baseline._format_float(row.actual),
                baseline._format_float(row.zero_prediction),
                baseline._format_float(row.train_median_prediction),
                baseline._format_float(row.momentum_180_prediction),
                baseline._format_float(row.catboost_prediction),
            )
        )
    return stream.getvalue().encode("utf-8")


def _render_evaluation(
    *,
    source_manifest_sha256: str,
    lineage_sha256: str,
    feature_columns: Sequence[str],
    train_league_ids: Sequence[int],
    test_league_ids: Sequence[int],
    train_match_count: int,
    train_row_count: int,
    test_match_count: int,
    test_row_count: int,
    excluded_match_count: int,
    excluded_row_count: int,
    league_counts: Sequence[dict[str, int | str]],
    train_target_median: float,
    aggregate_metrics: PredictorMetrics,
    time_bucket_metrics: dict[str, PredictorMetrics],
    prediction_row_count: int,
    prediction_sha256: str,
) -> bytes:
    numeric_aggregate_metrics = {
        predictor: {
            metric: value
            for metric, value in metrics.items()
            if value is not None
        }
        for predictor, metrics in aggregate_metrics.items()
    }
    payload = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "source_manifest_sha256": source_manifest_sha256,
        "lineage_sha256": lineage_sha256,
        "target": TARGET_COLUMN,
        "feature_columns": list(feature_columns),
        "train_league_ids": list(train_league_ids),
        "test_league_ids": list(test_league_ids),
        "train_match_count": train_match_count,
        "train_row_count": train_row_count,
        "test_match_count": test_match_count,
        "test_row_count": test_row_count,
        "excluded_match_count": excluded_match_count,
        "excluded_row_count": excluded_row_count,
        "league_counts": list(league_counts),
        "train_target_median": train_target_median,
        "catboost_parameters": baseline.catboost_parameters(),
        "aggregate_metrics": aggregate_metrics,
        "time_bucket_metrics": time_bucket_metrics,
        "relative_improvements": baseline._relative_improvements(
            numeric_aggregate_metrics
        ),
        "prediction_file": {
            "filename": PREDICTIONS_FILENAME,
            "row_count": prediction_row_count,
            "sha256": prediction_sha256,
        },
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return f"{text}\n".encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


_predict_catboost = baseline._predict_catboost
