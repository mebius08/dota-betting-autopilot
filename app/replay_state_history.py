from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from numbers import Real
from pathlib import Path
from typing import Literal, TypeAlias, cast

from app import replay_state_model as model
from app import replay_state_training as training


HISTORY_SCHEMA_VERSION = 1
REQUIRED_LAGS_SECONDS = training.REQUIRED_LAGS_SECONDS
CURRENT_FEATURE_COLUMNS = (
    *training.CURRENT_DIFF_COLUMNS,
    *(column for column, _ in training.TOTAL_COLUMNS),
)
HISTORICAL_FEATURE_COLUMNS = tuple(
    f"{column}_change_prev_{lag}"
    for lag in REQUIRED_LAGS_SECONDS
    for column in training.MOMENTUM_DIFF_COLUMNS
)
SUPPORTED_MODEL_FEATURE_COLUMNS = (
    *CURRENT_FEATURE_COLUMNS,
    *HISTORICAL_FEATURE_COLUMNS,
)
SNAPSHOT_FIELDS = frozenset(("game_time_seconds", *CURRENT_FEATURE_COLUMNS))
FORBIDDEN_SNAPSHOT_FIELDS = frozenset(("match_id", *training.FORBIDDEN_COLUMNS))

Numeric: TypeAlias = int | float


class ReplayStateHistoryError(ValueError):
    """Raised when a normalized replay-state history request is invalid."""


@dataclass(frozen=True)
class NormalizedReplayStateSnapshot:
    game_time_seconds: Numeric
    features: Mapping[str, Numeric]


@dataclass(frozen=True)
class ReplayStateHistoryRequest:
    match_id: int
    snapshots: tuple[NormalizedReplayStateSnapshot, ...]


@dataclass(frozen=True)
class ReplayStateHistoryFeatureResult:
    current_snapshot: NormalizedReplayStateSnapshot
    missing_lags_seconds: tuple[int, ...]
    features: Mapping[str, Numeric] | None


@dataclass(frozen=True)
class ReplayStateHistoryScoreResult:
    status: Literal["BUILT", "UNCHANGED"]
    response_status: Literal["scored", "not_ready"]
    output_path: Path


def load_replay_state_history_request(
    input_path: str | Path,
) -> ReplayStateHistoryRequest:
    path = Path(input_path)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ReplayStateHistoryError(
            "Replay state history request JSON cannot be read."
        ) from exc
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayStateHistoryError(
            "Replay state history request is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise ReplayStateHistoryError(
            "Replay state history request root must be an object."
        )
    if set(payload) != {"schema_version", "match_id", "snapshots"}:
        raise ReplayStateHistoryError(
            "Replay state history request fields do not match the schema."
        )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != HISTORY_SCHEMA_VERSION:
        raise ReplayStateHistoryError(
            "Unsupported replay state history request schema_version."
        )
    match_id = payload["match_id"]
    if type(match_id) is not int or match_id <= 0:
        raise ReplayStateHistoryError(
            "Replay state history request match_id must be a positive integer."
        )
    snapshots = validate_normalized_snapshots(payload["snapshots"])
    return ReplayStateHistoryRequest(match_id=match_id, snapshots=snapshots)


def validate_normalized_snapshots(
    snapshots: object,
) -> tuple[NormalizedReplayStateSnapshot, ...]:
    if not isinstance(snapshots, list):
        raise ReplayStateHistoryError("Replay state history snapshots must be a list.")
    if not snapshots:
        raise ReplayStateHistoryError(
            "Replay state history snapshots must be non-empty."
        )

    normalized: list[NormalizedReplayStateSnapshot] = []
    seen_times: set[Numeric] = set()
    for index, raw_snapshot in enumerate(snapshots):
        if not isinstance(raw_snapshot, dict):
            raise ReplayStateHistoryError(
                f"Normalized snapshot {index} must be an object."
            )
        if any(not isinstance(key, str) for key in raw_snapshot):
            raise ReplayStateHistoryError(
                f"Normalized snapshot {index} field names must be strings."
            )
        fields = set(raw_snapshot)
        forbidden = sorted(
            field
            for field in fields
            if field in FORBIDDEN_SNAPSHOT_FIELDS or field.startswith("target_")
        )
        if forbidden:
            raise ReplayStateHistoryError(
                "Target and lineage fields are not accepted in normalized snapshots: "
                + ", ".join(forbidden)
                + "."
            )
        missing = sorted(SNAPSHOT_FIELDS - fields)
        unexpected = sorted(fields - SNAPSHOT_FIELDS)
        if missing:
            raise ReplayStateHistoryError(
                "Missing normalized snapshot fields: " + ", ".join(missing) + "."
            )
        if unexpected:
            raise ReplayStateHistoryError(
                "Unexpected normalized snapshot fields: " + ", ".join(unexpected) + "."
            )

        game_time = _validated_numeric(
            raw_snapshot["game_time_seconds"],
            f"Normalized snapshot {index} game_time_seconds",
        )
        if game_time < 0:
            raise ReplayStateHistoryError(
                f"Normalized snapshot {index} game_time_seconds must be non-negative."
            )
        if game_time in seen_times:
            raise ReplayStateHistoryError(
                f"Duplicate normalized snapshot game_time_seconds: {game_time}."
            )
        seen_times.add(game_time)

        features = {
            column: _validated_numeric(
                raw_snapshot[column],
                f"Normalized snapshot {index} field {column}",
            )
            for column in CURRENT_FEATURE_COLUMNS
        }
        normalized.append(
            NormalizedReplayStateSnapshot(
                game_time_seconds=game_time,
                features=features,
            )
        )

    return tuple(sorted(normalized, key=lambda snapshot: snapshot.game_time_seconds))


def build_replay_state_feature_mapping(
    snapshots: Sequence[NormalizedReplayStateSnapshot],
) -> ReplayStateHistoryFeatureResult:
    if not snapshots:
        raise ReplayStateHistoryError(
            "Replay state history snapshots must be non-empty."
        )
    ordered = tuple(sorted(snapshots, key=lambda snapshot: snapshot.game_time_seconds))
    snapshots_by_time: dict[Numeric, NormalizedReplayStateSnapshot] = {}
    for snapshot in ordered:
        if snapshot.game_time_seconds in snapshots_by_time:
            raise ReplayStateHistoryError(
                "Duplicate normalized snapshot game_time_seconds: "
                f"{snapshot.game_time_seconds}."
            )
        if set(snapshot.features) != set(CURRENT_FEATURE_COLUMNS):
            raise ReplayStateHistoryError(
                "Normalized snapshot feature mapping does not match the contract."
            )
        snapshots_by_time[snapshot.game_time_seconds] = snapshot

    current = ordered[-1]
    lagged = {
        lag: snapshots_by_time.get(current.game_time_seconds - lag)
        for lag in REQUIRED_LAGS_SECONDS
    }
    missing_lags = tuple(lag for lag in REQUIRED_LAGS_SECONDS if lagged[lag] is None)
    if missing_lags:
        return ReplayStateHistoryFeatureResult(
            current_snapshot=current,
            missing_lags_seconds=missing_lags,
            features=None,
        )

    features: dict[str, Numeric] = {
        column: current.features[column] for column in CURRENT_FEATURE_COLUMNS
    }
    for lag in REQUIRED_LAGS_SECONDS:
        previous = lagged[lag]
        if previous is None:
            raise ReplayStateHistoryError("Missing exact lag snapshot.")
        for column in training.MOMENTUM_DIFF_COLUMNS:
            features[f"{column}_change_prev_{lag}"] = (
                current.features[column] - previous.features[column]
            )
    if tuple(features) != SUPPORTED_MODEL_FEATURE_COLUMNS:
        raise ReplayStateHistoryError(
            "Generated replay state features do not match the supported contract."
        )
    return ReplayStateHistoryFeatureResult(
        current_snapshot=current,
        missing_lags_seconds=(),
        features=features,
    )


def validate_model_feature_contract(feature_columns: Sequence[str]) -> None:
    if tuple(feature_columns) != SUPPORTED_MODEL_FEATURE_COLUMNS:
        raise ReplayStateHistoryError(
            "Model feature columns do not match the supported normalized history "
            "contract."
        )


def score_replay_state_history_request(
    request: ReplayStateHistoryRequest,
    scorer: model.ReplayStateModelScorer,
) -> dict[str, object]:
    validate_model_feature_contract(scorer.feature_columns)
    feature_result = build_replay_state_feature_mapping(request.snapshots)
    current_time = feature_result.current_snapshot.game_time_seconds
    if feature_result.missing_lags_seconds:
        return {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "status": "not_ready",
            "match_id": request.match_id,
            "game_time_seconds": current_time,
            "required_lags_seconds": list(REQUIRED_LAGS_SECONDS),
            "missing_lags_seconds": list(feature_result.missing_lags_seconds),
        }

    if feature_result.features is None:
        raise ReplayStateHistoryError("Ready replay state history has no features.")
    ordered_features = {
        column: feature_result.features[column] for column in scorer.feature_columns
    }
    prediction = scorer.score(ordered_features)
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "status": "scored",
        "model_manifest_sha256": scorer.manifest_sha256,
        "match_id": request.match_id,
        "game_time_seconds": current_time,
        "horizon_seconds": model.HORIZON_SECONDS,
        "prediction_net_worth_diff_change": prediction,
        "predicted_direction": model._prediction_direction(prediction),
    }


def score_replay_state_history(
    model_dir: str | Path,
    input_path: str | Path,
    output_path: str | Path,
) -> ReplayStateHistoryScoreResult:
    request = load_replay_state_history_request(input_path)
    scorer = model.load_replay_state_model(model_dir)
    response = score_replay_state_history_request(request, scorer)
    content = model._render_json(response)
    destination = Path(output_path)
    status = model._write_atomic_if_changed(destination, content)
    response_status = cast(Literal["scored", "not_ready"], response["status"])
    return ReplayStateHistoryScoreResult(
        status=status,
        response_status=response_status,
        output_path=destination,
    )


def _validated_numeric(value: object, label: str) -> Numeric:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ReplayStateHistoryError(f"{label} must be a numeric value.")
    try:
        numeric = float(value)
    except OverflowError as exc:
        raise ReplayStateHistoryError(f"{label} must be finite.") from exc
    if not math.isfinite(numeric):
        raise ReplayStateHistoryError(f"{label} must be finite.")
    return cast(Numeric, value)
