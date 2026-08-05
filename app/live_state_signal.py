from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Literal, cast

from app import replay_state_history as history
from app import replay_state_model as model


LIVE_STATE_SCHEMA_VERSION = 1
HISTORY_FILENAME = "history.json"


class LiveStateSignalError(ValueError):
    """Raised when live state ingestion or persisted history is invalid."""


@dataclass(frozen=True)
class LiveStateSnapshotRequest:
    match_id: int
    snapshot: history.NormalizedReplayStateSnapshot


@dataclass(frozen=True)
class PersistedMatchHistory:
    match_id: int
    snapshots: tuple[history.NormalizedReplayStateSnapshot, ...]


@dataclass(frozen=True)
class HistoryPersistenceResult:
    status: Literal["BUILT", "UNCHANGED"]
    path: Path
    sha256: str


@dataclass(frozen=True)
class LiveStateSignalResult:
    status: Literal["BUILT", "UNCHANGED"]
    response_status: Literal["scored", "not_ready"]
    history_path: Path
    output_path: Path
    history_file_sha256: str


def load_live_state_snapshot_request(
    input_path: str | Path,
) -> LiveStateSnapshotRequest:
    path = Path(input_path)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LiveStateSignalError(
            "Live state snapshot request JSON cannot be read."
        ) from exc
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveStateSignalError(
            "Live state snapshot request is not valid UTF-8 JSON."
        ) from exc
    return validate_live_state_snapshot_request(payload)


def validate_live_state_snapshot_request(payload: object) -> LiveStateSnapshotRequest:
    if not isinstance(payload, dict):
        raise LiveStateSignalError("Live state snapshot request root must be an object.")
    if any(not isinstance(key, str) for key in payload):
        raise LiveStateSignalError(
            "Live state snapshot request field names must be strings."
        )
    if set(payload) != {"schema_version", "match_id", "snapshot"}:
        raise LiveStateSignalError(
            "Live state snapshot request fields do not match the schema."
        )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != LIVE_STATE_SCHEMA_VERSION:
        raise LiveStateSignalError(
            "Unsupported live state snapshot request schema_version."
        )
    match_id = payload["match_id"]
    if type(match_id) is not int or match_id <= 0:
        raise LiveStateSignalError(
            "Live state snapshot request match_id must be a positive integer."
        )
    try:
        snapshots = history.validate_normalized_snapshots([payload["snapshot"]])
    except history.ReplayStateHistoryError as exc:
        raise LiveStateSignalError(
            f"Live state snapshot request is invalid: {exc}"
        ) from exc
    return LiveStateSnapshotRequest(match_id=match_id, snapshot=snapshots[0])


def match_history_path(state_dir: str | Path, match_id: int) -> Path:
    _validate_match_id(match_id)
    return Path(state_dir) / str(match_id) / HISTORY_FILENAME


def load_persisted_match_history(
    state_dir: str | Path,
    match_id: int,
) -> PersistedMatchHistory:
    path = match_history_path(state_dir, match_id)
    if not path.exists():
        return PersistedMatchHistory(match_id=match_id, snapshots=())
    if not path.is_file():
        raise LiveStateSignalError(
            f"Persisted match history path is not a file: {path.as_posix()}."
        )
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LiveStateSignalError("Persisted match history cannot be read.") from exc
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveStateSignalError(
            "Persisted match history is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise LiveStateSignalError("Persisted match history root must be an object.")
    if any(not isinstance(key, str) for key in payload):
        raise LiveStateSignalError(
            "Persisted match history field names must be strings."
        )
    if set(payload) != {"schema_version", "match_id", "snapshots"}:
        raise LiveStateSignalError(
            "Persisted match history fields do not match the schema."
        )
    schema_version = payload["schema_version"]
    if type(schema_version) is not int or schema_version != LIVE_STATE_SCHEMA_VERSION:
        raise LiveStateSignalError(
            "Unsupported persisted match history schema_version."
        )
    persisted_match_id = payload["match_id"]
    if type(persisted_match_id) is not int or persisted_match_id <= 0:
        raise LiveStateSignalError(
            "Persisted match history match_id must be a positive integer."
        )
    if persisted_match_id != match_id:
        raise LiveStateSignalError(
            "Persisted match history match_id does not match its directory."
        )
    raw_snapshots = payload["snapshots"]
    if not isinstance(raw_snapshots, list):
        raise LiveStateSignalError("Persisted match history snapshots must be a list.")
    try:
        snapshots = (
            history.validate_normalized_snapshots(raw_snapshots)
            if raw_snapshots
            else ()
        )
    except history.ReplayStateHistoryError as exc:
        raise LiveStateSignalError(f"Persisted match history is invalid: {exc}") from exc
    raw_game_times = tuple(
        raw_snapshot["game_time_seconds"] for raw_snapshot in raw_snapshots
    )
    normalized_game_times = tuple(
        snapshot.game_time_seconds for snapshot in snapshots
    )
    if raw_game_times != normalized_game_times:
        raise LiveStateSignalError(
            "Persisted match history snapshots must be sorted numerically by "
            "game_time_seconds."
        )
    return PersistedMatchHistory(match_id=match_id, snapshots=snapshots)


def insert_live_state_snapshot(
    persisted: PersistedMatchHistory,
    snapshot: history.NormalizedReplayStateSnapshot,
) -> tuple[PersistedMatchHistory, bool]:
    for existing in persisted.snapshots:
        if existing.game_time_seconds != snapshot.game_time_seconds:
            continue
        if existing == snapshot:
            return persisted, False
        raise LiveStateSignalError(
            "Conflicting snapshot already exists for match_id "
            f"{persisted.match_id} at game_time_seconds "
            f"{snapshot.game_time_seconds}."
        )
    snapshots = tuple(
        sorted(
            (*persisted.snapshots, snapshot),
            key=lambda item: item.game_time_seconds,
        )
    )
    return PersistedMatchHistory(
        match_id=persisted.match_id,
        snapshots=snapshots,
    ), True


def persist_match_history(
    state_dir: str | Path,
    persisted: PersistedMatchHistory,
) -> HistoryPersistenceResult:
    content = render_persisted_match_history(persisted)
    path = match_history_path(state_dir, persisted.match_id)
    status = _write_atomic_if_changed(path, content, "persisted match history")
    return HistoryPersistenceResult(
        status=status,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def render_persisted_match_history(persisted: PersistedMatchHistory) -> bytes:
    _validate_match_id(persisted.match_id)
    snapshot_payloads = [_snapshot_payload(snapshot) for snapshot in persisted.snapshots]
    if snapshot_payloads:
        try:
            validated = history.validate_normalized_snapshots(snapshot_payloads)
        except history.ReplayStateHistoryError as exc:
            raise LiveStateSignalError(
                f"Persisted match history is invalid: {exc}"
            ) from exc
        snapshot_payloads = [_snapshot_payload(snapshot) for snapshot in validated]
    payload = {
        "schema_version": LIVE_STATE_SCHEMA_VERSION,
        "match_id": persisted.match_id,
        "snapshots": snapshot_payloads,
    }
    return _render_json(payload)


def build_live_state_signal_response(
    persisted: PersistedMatchHistory,
    scorer: model.ReplayStateModelScorer,
    history_file_sha256: str,
) -> dict[str, object]:
    request = history.ReplayStateHistoryRequest(
        match_id=persisted.match_id,
        snapshots=persisted.snapshots,
    )
    scored = history.score_replay_state_history_request(request, scorer)
    response_status = scored["status"]
    if response_status == "not_ready":
        return {
            "schema_version": LIVE_STATE_SCHEMA_VERSION,
            "status": "not_ready",
            "match_id": persisted.match_id,
            "game_time_seconds": scored["game_time_seconds"],
            "snapshot_count": len(persisted.snapshots),
            "required_lags_seconds": scored["required_lags_seconds"],
            "missing_lags_seconds": scored["missing_lags_seconds"],
            "history_file_sha256": history_file_sha256,
        }
    if response_status == "scored":
        return {
            "schema_version": LIVE_STATE_SCHEMA_VERSION,
            "status": "scored",
            "model_manifest_sha256": scored["model_manifest_sha256"],
            "match_id": persisted.match_id,
            "game_time_seconds": scored["game_time_seconds"],
            "snapshot_count": len(persisted.snapshots),
            "horizon_seconds": scored["horizon_seconds"],
            "prediction_net_worth_diff_change": scored[
                "prediction_net_worth_diff_change"
            ],
            "predicted_direction": scored["predicted_direction"],
            "history_file_sha256": history_file_sha256,
        }
    raise LiveStateSignalError("Replay state history scorer returned invalid status.")


def persist_live_state_signal_response(
    output_path: str | Path,
    response: object,
) -> Literal["BUILT", "UNCHANGED"]:
    content = _render_json(response)
    return _write_atomic_if_changed(
        Path(output_path), content, "live state signal response"
    )


def ingest_live_state_snapshot(
    model_dir: str | Path,
    state_dir: str | Path,
    input_path: str | Path,
    output_path: str | Path,
) -> LiveStateSignalResult:
    request = load_live_state_snapshot_request(input_path)
    existing = load_persisted_match_history(state_dir, request.match_id)
    updated, _ = insert_live_state_snapshot(existing, request.snapshot)

    history_content = render_persisted_match_history(updated)
    history_file_sha256 = hashlib.sha256(history_content).hexdigest()
    scorer = model.load_replay_state_model(model_dir)
    response = build_live_state_signal_response(
        updated,
        scorer,
        history_file_sha256,
    )
    response_content = _render_json(response)

    history_path = match_history_path(state_dir, request.match_id)
    history_status = _write_atomic_if_changed(
        history_path,
        history_content,
        "persisted match history",
    )
    destination = Path(output_path)
    signal_status = _write_atomic_if_changed(
        destination,
        response_content,
        "live state signal response",
    )
    status: Literal["BUILT", "UNCHANGED"] = (
        "BUILT"
        if history_status == "BUILT" or signal_status == "BUILT"
        else "UNCHANGED"
    )
    return LiveStateSignalResult(
        status=status,
        response_status=cast(Literal["scored", "not_ready"], response["status"]),
        history_path=history_path,
        output_path=destination,
        history_file_sha256=history_file_sha256,
    )


def _snapshot_payload(
    snapshot: history.NormalizedReplayStateSnapshot,
) -> dict[str, history.Numeric]:
    return {
        "game_time_seconds": snapshot.game_time_seconds,
        **{
            column: snapshot.features[column]
            for column in history.CURRENT_FEATURE_COLUMNS
        },
    }


def _validate_match_id(match_id: object) -> None:
    if type(match_id) is not int or match_id <= 0:
        raise LiveStateSignalError("match_id must be a positive integer.")


def _render_json(payload: object) -> bytes:
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LiveStateSignalError("Live state JSON could not be rendered.") from exc
    return f"{text}\n".encode("utf-8")


def _write_atomic_if_changed(
    path: Path,
    content: bytes,
    label: str,
) -> Literal["BUILT", "UNCHANGED"]:
    if path.exists():
        if not path.is_file():
            raise LiveStateSignalError(
                f"{label.capitalize()} path is not a file: {path.as_posix()}."
            )
        try:
            if path.read_bytes() == content:
                return "UNCHANGED"
        except OSError as exc:
            raise LiveStateSignalError(f"Existing {label} cannot be read.") from exc
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.transaction.",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        with temporary_path.open("wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise LiveStateSignalError(f"Could not atomically write {label}.") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return "BUILT"
