from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Literal, Protocol, cast

from app import replay_state_baseline as baseline
from app import replay_state_tournament_holdout as holdout
from app.replay_state_training import (
    MANIFEST_FILENAME as TRAINING_MANIFEST_FILENAME,
    TARGET_COLUMNS,
    TRAINING_FILENAME,
)


BUNDLE_SCHEMA_VERSION = 1
SCORE_SCHEMA_VERSION = 1
MODEL_FORMAT = "catboost_cbm"
MODEL_FILENAME = "model.cbm"
MODEL_MANIFEST_FILENAME = "manifest.json"
BUNDLE_FILENAMES = (MODEL_FILENAME, MODEL_MANIFEST_FILENAME)
TARGET_COLUMN = baseline.TARGET_COLUMN
HORIZON_SECONDS = 180
FORBIDDEN_SCORE_FEATURES = frozenset(
    ("match_id", "league_id", "game_time_seconds", *TARGET_COLUMNS)
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ReplayStateModelError(ValueError):
    """Raised when a replay-state model bundle cannot be built or scored."""


class _PredictionModel(Protocol):
    def predict(self, data: Sequence[Sequence[float]]) -> object: ...


@dataclass(frozen=True)
class ReplayStateModelBuildResult:
    status: Literal["BUILT", "UNCHANGED"]
    output_dir: Path
    source_manifest_sha256: str
    lineage_sha256: str
    match_count: int
    row_count: int


@dataclass(frozen=True)
class ReplayStateModelScoreResult:
    status: Literal["BUILT", "UNCHANGED"]
    output_path: Path
    model_manifest_sha256: str
    prediction: float
    predicted_direction: str


@dataclass(frozen=True)
class _BundleManifest:
    source_manifest_sha256: str
    lineage_sha256: str
    feature_columns: tuple[str, ...]
    league_ids: tuple[int, ...]
    match_count: int
    row_count: int
    model_size_bytes: int
    model_sha256: str


@dataclass(frozen=True)
class ReplayStateModelScorer:
    feature_columns: tuple[str, ...]
    manifest_sha256: str
    _model: _PredictionModel

    def score(self, features: Mapping[str, object]) -> float:
        ordered = _validate_and_order_features(features, self.feature_columns)
        try:
            raw_predictions = self._model.predict([ordered])
            predictions = list(cast(Iterable[object], raw_predictions))
        except Exception as exc:
            raise ReplayStateModelError(
                "CatBoost could not score the feature row."
            ) from exc
        if len(predictions) != 1:
            raise ReplayStateModelError(
                "CatBoost returned an unexpected prediction count."
            )
        raw_prediction = predictions[0]
        if isinstance(raw_prediction, bool) or not isinstance(raw_prediction, Real):
            raise ReplayStateModelError("CatBoost returned a non-numeric prediction.")
        try:
            prediction = float(raw_prediction)
        except OverflowError as exc:
            raise ReplayStateModelError(
                "CatBoost returned a non-finite prediction."
            ) from exc
        if not math.isfinite(prediction):
            raise ReplayStateModelError("CatBoost returned a non-finite prediction.")
        return prediction


def build_replay_state_model(
    training_dir: str | Path,
    trajectory_dir: str | Path,
    league_ids: Iterable[int],
    output_dir: str | Path,
) -> ReplayStateModelBuildResult:
    selected_leagues = _normalize_league_ids(league_ids)
    source_dir = Path(training_dir)
    try:
        manifest_bytes = baseline._read_source_file(
            source_dir, TRAINING_MANIFEST_FILENAME
        )
        training_manifest = baseline._load_training_manifest(manifest_bytes)
        training_bytes = baseline._read_source_file(source_dir, TRAINING_FILENAME)
        baseline._validate_source_hash(
            training_bytes, training_manifest.training_sha256
        )
        dataset = baseline._load_training_dataset(
            training_bytes, training_manifest.row_count
        )
    except baseline.ReplayStateBaselineError as exc:
        raise ReplayStateModelError(str(exc)) from exc

    try:
        lineage = holdout.build_match_to_league_lineage(trajectory_dir)
    except holdout.ReplayStateTournamentHoldoutError as exc:
        raise ReplayStateModelError(str(exc)) from exc

    selected_set = set(selected_leagues)
    selected_rows = tuple(
        sorted(
            (row for row in dataset.rows if lineage.get(row.match_id) in selected_set),
            key=lambda row: (row.match_id, row.game_time_seconds),
        )
    )
    selected_match_ids = tuple(sorted({row.match_id for row in selected_rows}))
    if not selected_match_ids:
        raise ReplayStateModelError(
            "The selected leagues contain no replay state training matches."
        )
    if not selected_rows:
        raise ReplayStateModelError(
            "The selected leagues contain no replay state training rows."
        )
    _validate_feature_contract(dataset.feature_columns)

    lineage_pairs = tuple(
        (match_id, lineage[match_id]) for match_id in selected_match_ids
    )
    lineage_sha256 = holdout._lineage_sha256(lineage_pairs)
    source_manifest_sha256 = training_manifest.sha256
    parameters = baseline.catboost_parameters()
    destination = Path(output_dir)
    expected = _BundleManifest(
        source_manifest_sha256=source_manifest_sha256,
        lineage_sha256=lineage_sha256,
        feature_columns=dataset.feature_columns,
        league_ids=selected_leagues,
        match_count=len(selected_match_ids),
        row_count=len(selected_rows),
        model_size_bytes=0,
        model_sha256="",
    )
    if _existing_bundle_matches(destination, expected):
        status: Literal["BUILT", "UNCHANGED"] = "UNCHANGED"
    else:
        model_bytes = _train_model_bytes(
            selected_rows,
            dataset.feature_columns,
            parameters,
            destination.parent,
        )
        manifest_content = _render_bundle_manifest(
            source_manifest_sha256=source_manifest_sha256,
            lineage_sha256=lineage_sha256,
            feature_columns=dataset.feature_columns,
            league_ids=selected_leagues,
            match_count=len(selected_match_ids),
            row_count=len(selected_rows),
            parameters=parameters,
            model_bytes=model_bytes,
        )
        _replace_bundle(
            destination,
            {
                MODEL_FILENAME: model_bytes,
                MODEL_MANIFEST_FILENAME: manifest_content,
            },
        )
        status = "BUILT"

    return ReplayStateModelBuildResult(
        status=status,
        output_dir=destination,
        source_manifest_sha256=source_manifest_sha256,
        lineage_sha256=lineage_sha256,
        match_count=len(selected_match_ids),
        row_count=len(selected_rows),
    )


def load_replay_state_model(model_dir: str | Path) -> ReplayStateModelScorer:
    directory = Path(model_dir)
    if not directory.exists():
        raise ReplayStateModelError(
            f"Model bundle directory does not exist: {directory.as_posix()}."
        )
    if not directory.is_dir():
        raise ReplayStateModelError(
            f"Model bundle path is not a directory: {directory.as_posix()}."
        )
    _reject_unexpected_bundle_entries(directory)
    manifest_content = _read_bundle_file(directory, MODEL_MANIFEST_FILENAME)
    manifest = _load_bundle_manifest(manifest_content)
    model_path = directory / MODEL_FILENAME
    _validate_model_file(model_path, manifest)
    model = _load_catboost_model(model_path)
    return ReplayStateModelScorer(
        feature_columns=manifest.feature_columns,
        manifest_sha256=_sha256(manifest_content),
        _model=model,
    )


def score_replay_state_model(
    model_dir: str | Path,
    input_path: str | Path,
    output_path: str | Path,
) -> ReplayStateModelScoreResult:
    request = _load_score_request(Path(input_path))
    scorer = load_replay_state_model(model_dir)
    prediction = scorer.score(request["features"])
    direction = _prediction_direction(prediction)
    response = {
        "schema_version": SCORE_SCHEMA_VERSION,
        "model_manifest_sha256": scorer.manifest_sha256,
        "match_id": request["match_id"],
        "game_time_seconds": request["game_time_seconds"],
        "horizon_seconds": HORIZON_SECONDS,
        "prediction_net_worth_diff_change": prediction,
        "predicted_direction": direction,
    }
    content = _render_json(response)
    destination = Path(output_path)
    status = _write_atomic_if_changed(destination, content)
    return ReplayStateModelScoreResult(
        status=status,
        output_path=destination,
        model_manifest_sha256=scorer.manifest_sha256,
        prediction=prediction,
        predicted_direction=direction,
    )


def _normalize_league_ids(values: Iterable[int]) -> tuple[int, ...]:
    league_ids = tuple(values)
    if not league_ids:
        raise ReplayStateModelError("The selected league set must be non-empty.")
    if any(type(value) is not int or value <= 0 for value in league_ids):
        raise ReplayStateModelError(
            "The selected league set contains an invalid league ID."
        )
    return tuple(sorted(set(league_ids)))


def _validate_feature_contract(feature_columns: Sequence[str]) -> None:
    try:
        expected = baseline._select_feature_columns(baseline.TRAINING_COLUMNS)
    except baseline.ReplayStateBaselineError as exc:
        raise ReplayStateModelError(str(exc)) from exc
    if tuple(feature_columns) != expected:
        raise ReplayStateModelError(
            "Model feature columns do not match the fixed training schema."
        )
    forbidden = set(feature_columns) & FORBIDDEN_SCORE_FEATURES
    if forbidden or any(column.startswith("target_") for column in feature_columns):
        raise ReplayStateModelError(
            "Target and lineage columns cannot be used as model features."
        )


def _train_model_bytes(
    rows: Sequence[baseline._TrainingRow],
    feature_columns: Sequence[str],
    parameters: Mapping[str, object],
    output_parent: Path,
) -> bytes:
    try:
        output_parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReplayStateModelError(
            "Cannot create the replay state model parent directory."
        ) from exc
    temporary_dir: Path | None = None
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(dir=output_parent, prefix=".replay-state-model.train.")
        )
        model_path = temporary_dir / MODEL_FILENAME
        _train_model(rows, feature_columns, parameters, model_path)
        model_bytes = model_path.read_bytes()
    except OSError as exc:
        raise ReplayStateModelError("Could not serialize the CatBoost model.") from exc
    finally:
        if temporary_dir is not None and temporary_dir.exists():
            try:
                shutil.rmtree(temporary_dir)
            except OSError as exc:
                raise ReplayStateModelError(
                    "CatBoost training completed but temporary cleanup failed."
                ) from exc
    if not model_bytes:
        raise ReplayStateModelError("CatBoost produced an empty model file.")
    return model_bytes


def _train_model(
    rows: Sequence[baseline._TrainingRow],
    feature_columns: Sequence[str],
    parameters: Mapping[str, object],
    model_path: Path,
) -> None:
    try:
        from catboost import CatBoostRegressor
    except ModuleNotFoundError as exc:
        raise ReplayStateModelError(
            "CatBoost is not installed. Install project dependencies first."
        ) from exc
    model = CatBoostRegressor(**dict(parameters))
    train_features = [
        [row.values[column] for column in feature_columns] for row in rows
    ]
    train_target = [row.values[TARGET_COLUMN] for row in rows]
    try:
        model.fit(train_features, train_target)
        model.save_model(str(model_path), format="cbm")
    except Exception as exc:
        raise ReplayStateModelError("CatBoost model training failed.") from exc


def _render_bundle_manifest(
    *,
    source_manifest_sha256: str,
    lineage_sha256: str,
    feature_columns: Sequence[str],
    league_ids: Sequence[int],
    match_count: int,
    row_count: int,
    parameters: Mapping[str, object],
    model_bytes: bytes,
) -> bytes:
    payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "model_format": MODEL_FORMAT,
        "source_manifest_sha256": source_manifest_sha256,
        "lineage_sha256": lineage_sha256,
        "target": TARGET_COLUMN,
        "horizon_seconds": HORIZON_SECONDS,
        "feature_columns": list(feature_columns),
        "league_ids": list(league_ids),
        "match_count": match_count,
        "row_count": row_count,
        "catboost_parameters": dict(parameters),
        "model_file": {
            "filename": MODEL_FILENAME,
            "size_bytes": len(model_bytes),
            "sha256": _sha256(model_bytes),
        },
    }
    return _render_json(payload)


def _load_bundle_manifest(content: bytes) -> _BundleManifest:
    payload = _decode_json_object(content, "Model manifest")
    expected_keys = {
        "schema_version",
        "model_format",
        "source_manifest_sha256",
        "lineage_sha256",
        "target",
        "horizon_seconds",
        "feature_columns",
        "league_ids",
        "match_count",
        "row_count",
        "catboost_parameters",
        "model_file",
    }
    if set(payload) != expected_keys:
        raise ReplayStateModelError("Model manifest fields do not match the schema.")
    if payload["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise ReplayStateModelError("Unsupported model manifest schema_version.")
    if payload["model_format"] != MODEL_FORMAT:
        raise ReplayStateModelError("Unsupported replay state model format.")
    if payload["target"] != TARGET_COLUMN:
        raise ReplayStateModelError(
            "Model manifest target does not match the contract."
        )
    if payload["horizon_seconds"] != HORIZON_SECONDS:
        raise ReplayStateModelError(
            "Model manifest horizon does not match the contract."
        )
    source_sha256 = _manifest_sha256(payload, "source_manifest_sha256")
    lineage_sha256 = _manifest_sha256(payload, "lineage_sha256")
    feature_columns = _manifest_string_tuple(payload, "feature_columns")
    _validate_feature_contract(feature_columns)
    league_ids = _manifest_league_ids(payload)
    match_count = _positive_manifest_int(payload, "match_count")
    row_count = _positive_manifest_int(payload, "row_count")
    if payload["catboost_parameters"] != baseline.catboost_parameters():
        raise ReplayStateModelError(
            "Model manifest CatBoost parameters do not match the fixed contract."
        )
    model_file = payload["model_file"]
    if not isinstance(model_file, dict) or set(model_file) != {
        "filename",
        "size_bytes",
        "sha256",
    }:
        raise ReplayStateModelError("Model manifest model_file is invalid.")
    if model_file["filename"] != MODEL_FILENAME:
        raise ReplayStateModelError(
            f"Model manifest filename must be {MODEL_FILENAME}."
        )
    model_size_bytes = _positive_manifest_int(model_file, "size_bytes")
    model_sha256 = _manifest_sha256(model_file, "sha256")
    return _BundleManifest(
        source_manifest_sha256=source_sha256,
        lineage_sha256=lineage_sha256,
        feature_columns=feature_columns,
        league_ids=league_ids,
        match_count=match_count,
        row_count=row_count,
        model_size_bytes=model_size_bytes,
        model_sha256=model_sha256,
    )


def _existing_bundle_matches(output_dir: Path, expected: _BundleManifest) -> bool:
    if not output_dir.exists():
        return False
    if not output_dir.is_dir():
        raise ReplayStateModelError(
            f"Output path is not a directory: {output_dir.as_posix()}."
        )
    _reject_unexpected_bundle_entries(output_dir)
    try:
        manifest_content = _read_bundle_file(output_dir, MODEL_MANIFEST_FILENAME)
        manifest = _load_bundle_manifest(manifest_content)
        matches = (
            manifest.source_manifest_sha256 == expected.source_manifest_sha256
            and manifest.lineage_sha256 == expected.lineage_sha256
            and manifest.feature_columns == expected.feature_columns
            and manifest.league_ids == expected.league_ids
            and manifest.match_count == expected.match_count
            and manifest.row_count == expected.row_count
        )
        if not matches:
            return False
        model_path = output_dir / MODEL_FILENAME
        _validate_model_file(model_path, manifest)
        _load_catboost_model(model_path)
    except ReplayStateModelError:
        return False
    return True


def _validate_model_file(path: Path, manifest: _BundleManifest) -> None:
    try:
        if not path.is_file():
            raise ReplayStateModelError(f"Model bundle is missing {MODEL_FILENAME}.")
        size = path.stat().st_size
        content = path.read_bytes()
    except OSError as exc:
        raise ReplayStateModelError("Model file cannot be read.") from exc
    if size != manifest.model_size_bytes:
        raise ReplayStateModelError("Model file size does not match the manifest.")
    if _sha256(content) != manifest.model_sha256:
        raise ReplayStateModelError("Model file SHA-256 does not match the manifest.")


def _load_catboost_model(path: Path) -> _PredictionModel:
    try:
        from catboost import CatBoostRegressor
    except ModuleNotFoundError as exc:
        raise ReplayStateModelError(
            "CatBoost is not installed. Install project dependencies first."
        ) from exc
    model = CatBoostRegressor()
    try:
        model.load_model(str(path), format="cbm")
    except Exception as exc:
        raise ReplayStateModelError("CatBoost model file is corrupted.") from exc
    return model


def _validate_and_order_features(
    features: Mapping[str, object], feature_columns: Sequence[str]
) -> list[float]:
    if not isinstance(features, Mapping):
        raise ReplayStateModelError("Score request features must be an object.")
    keys = set(features)
    if any(not isinstance(key, str) for key in keys):
        raise ReplayStateModelError("Score request feature names must be strings.")
    forbidden = keys & FORBIDDEN_SCORE_FEATURES
    if forbidden or any(
        isinstance(key, str) and key.startswith("target_") for key in keys
    ):
        raise ReplayStateModelError(
            "Target and lineage fields are not accepted as score features."
        )
    expected = set(feature_columns)
    missing = expected - keys
    unexpected = keys - expected
    if missing:
        raise ReplayStateModelError(
            "Missing model features: " + ", ".join(sorted(missing)) + "."
        )
    if unexpected:
        raise ReplayStateModelError(
            "Unexpected model features: " + ", ".join(sorted(unexpected)) + "."
        )
    ordered: list[float] = []
    for column in feature_columns:
        value = features[column]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ReplayStateModelError(f"Feature {column} must be a numeric value.")
        try:
            numeric = float(value)
        except OverflowError as exc:
            raise ReplayStateModelError(f"Feature {column} must be finite.") from exc
        if not math.isfinite(numeric):
            raise ReplayStateModelError(f"Feature {column} must be finite.")
        ordered.append(numeric)
    return ordered


def _load_score_request(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ReplayStateModelError("Score request JSON cannot be read.") from exc
    payload = _decode_json_object(content, "Score request")
    if set(payload) != {
        "schema_version",
        "match_id",
        "game_time_seconds",
        "features",
    }:
        raise ReplayStateModelError("Score request fields do not match the schema.")
    if payload["schema_version"] != SCORE_SCHEMA_VERSION:
        raise ReplayStateModelError("Unsupported score request schema_version.")
    match_id = payload["match_id"]
    if type(match_id) is not int or match_id <= 0:
        raise ReplayStateModelError(
            "Score request match_id must be a positive integer."
        )
    game_time = payload["game_time_seconds"]
    if type(game_time) is not int or game_time < 0:
        raise ReplayStateModelError(
            "Score request game_time_seconds must be a non-negative integer."
        )
    features = payload["features"]
    if not isinstance(features, dict):
        raise ReplayStateModelError("Score request features must be an object.")
    return payload


def _prediction_direction(prediction: float) -> str:
    if prediction > 0.0:
        return "radiant_improving"
    if prediction < 0.0:
        return "dire_improving"
    return "flat"


def _replace_bundle(output_dir: Path, outputs: Mapping[str, bytes]) -> None:
    parent = output_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReplayStateModelError(
            "Cannot create the replay state model parent directory."
        ) from exc
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ReplayStateModelError(
                f"Output path is not a directory: {output_dir.as_posix()}."
            )
        _reject_unexpected_bundle_entries(output_dir)

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
        for filename in BUNDLE_FILENAMES:
            _write_durable(new_output / filename, outputs[filename])
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
        message = "Could not atomically replace replay state model outputs."
        if rollback_failed:
            message += " Automatic rollback also failed."
        raise ReplayStateModelError(message) from exc
    finally:
        if transaction is not None and transaction.exists() and not rollback_failed:
            try:
                shutil.rmtree(transaction)
            except OSError as exc:
                if installed_new:
                    raise ReplayStateModelError(
                        "Model build completed but temporary cleanup failed."
                    ) from exc


def _write_atomic_if_changed(
    path: Path, content: bytes
) -> Literal["BUILT", "UNCHANGED"]:
    if path.exists():
        if not path.is_file():
            raise ReplayStateModelError(
                f"Score output path is not a file: {path.as_posix()}."
            )
        try:
            if path.read_bytes() == content:
                return "UNCHANGED"
        except OSError as exc:
            raise ReplayStateModelError(
                "Existing score response cannot be read."
            ) from exc
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.transaction."
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            _write_durable(temporary_path, content)
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    except OSError as exc:
        raise ReplayStateModelError(
            "Could not atomically write the score response."
        ) from exc
    return "BUILT"


def _reject_unexpected_bundle_entries(output_dir: Path) -> None:
    try:
        unexpected = sorted(
            path.name
            for path in output_dir.iterdir()
            if path.name not in BUNDLE_FILENAMES
        )
    except OSError as exc:
        raise ReplayStateModelError(
            "Cannot inspect the model bundle directory."
        ) from exc
    if unexpected:
        raise ReplayStateModelError(
            "Model bundle directory contains unexpected entries: "
            + ", ".join(unexpected)
            + "."
        )


def _read_bundle_file(directory: Path, filename: str) -> bytes:
    try:
        return (directory / filename).read_bytes()
    except OSError as exc:
        raise ReplayStateModelError(
            f"Cannot read required model bundle file {filename}."
        ) from exc


def _decode_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayStateModelError(f"{label} is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise ReplayStateModelError(f"{label} root must be an object.")
    return payload


def _manifest_sha256(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ReplayStateModelError(
            f"Model manifest {key} must be a lowercase SHA-256 digest."
        )
    return value


def _manifest_string_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ReplayStateModelError(
            f"Model manifest {key} must be a non-empty unique string list."
        )
    return tuple(value)


def _manifest_league_ids(payload: Mapping[str, Any]) -> tuple[int, ...]:
    value = payload.get("league_ids")
    if not isinstance(value, list):
        raise ReplayStateModelError("Model manifest league_ids must be a list.")
    try:
        league_ids = _normalize_league_ids(value)
    except ReplayStateModelError as exc:
        raise ReplayStateModelError("Model manifest league_ids are invalid.") from exc
    if list(league_ids) != value:
        raise ReplayStateModelError(
            "Model manifest league_ids must be sorted and unique."
        )
    return league_ids


def _positive_manifest_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value <= 0:
        raise ReplayStateModelError(f"Model manifest {key} must be a positive integer.")
    return value


def _render_json(payload: object) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    return f"{text}\n".encode("utf-8")


def _write_durable(path: Path, content: bytes) -> None:
    with path.open("wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
