from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Literal, cast

from app import live_state_signal as signal
from app import replay_state_history as history
from app import replay_state_model as model


JSONL_SCHEMA_VERSION = 1
_EMPTY_PREFIX_SHA256 = hashlib.sha256(b"").hexdigest()
_CURSOR_FIELDS = {
    "schema_version",
    "input_filename",
    "byte_offset",
    "processed_line_count",
    "processed_prefix_sha256",
}
_SUMMARY_FIELDS = {
    "schema_version",
    "status",
    "input_filename",
    "start_line",
    "end_line",
    "processed_event_count",
    "changed_event_count",
    "unchanged_event_count",
    "scored_event_count",
    "not_ready_event_count",
    "pending_partial_record",
    "cursor",
}
_SUMMARY_CURSOR_FIELDS = {
    "byte_offset",
    "processed_line_count",
    "processed_prefix_sha256",
}


class LiveStateJsonlError(ValueError):
    """Raised when JSONL bridge input or persistent state is invalid."""


@dataclass(frozen=True)
class LiveStateJsonlCursor:
    input_filename: str
    byte_offset: int
    processed_line_count: int
    processed_prefix_sha256: str


@dataclass(frozen=True)
class CompleteJsonlLine:
    line_number: int
    start_offset: int
    end_offset: int
    content: bytes


@dataclass(frozen=True)
class NewJsonlRecords:
    lines: tuple[CompleteJsonlLine, ...]
    pending_partial_record: bool
    has_new_bytes: bool


@dataclass(frozen=True)
class LiveStateJsonlSyncResult:
    status: Literal["BUILT", "UNCHANGED"]
    cursor: LiveStateJsonlCursor


def load_live_state_jsonl_cursor(
    cursor_path: str | Path,
    input_path: str | Path,
) -> tuple[LiveStateJsonlCursor, bool]:
    """Load a validated cursor, or return the deterministic initial cursor."""
    path = Path(cursor_path)
    input_filename = Path(input_path).name
    if not path.exists():
        return (
            LiveStateJsonlCursor(
                input_filename=input_filename,
                byte_offset=0,
                processed_line_count=0,
                processed_prefix_sha256=_EMPTY_PREFIX_SHA256,
            ),
            False,
        )
    if not path.is_file():
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    try:
        content = path.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.") from exc
    if not isinstance(payload, dict) or set(payload) != _CURSOR_FIELDS:
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    if any(not isinstance(key, str) for key in payload):
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    if payload["schema_version"] != JSONL_SCHEMA_VERSION or type(
        payload["schema_version"]
    ) is not int:
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    saved_filename = payload["input_filename"]
    if not isinstance(saved_filename, str):
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    if saved_filename != input_filename:
        raise LiveStateJsonlError(
            "Live state JSONL cursor input filename does not match."
        )
    byte_offset = payload["byte_offset"]
    processed_line_count = payload["processed_line_count"]
    prefix_sha256 = payload["processed_prefix_sha256"]
    if type(byte_offset) is not int or byte_offset < 0:
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    if type(processed_line_count) is not int or processed_line_count < 0:
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    if not _is_sha256(prefix_sha256):
        raise LiveStateJsonlError("Live state JSONL cursor is corrupted.")
    return (
        LiveStateJsonlCursor(
            input_filename=saved_filename,
            byte_offset=byte_offset,
            processed_line_count=processed_line_count,
            processed_prefix_sha256=cast(str, prefix_sha256),
        ),
        True,
    )


def verify_processed_prefix(
    input_path: str | Path,
    cursor: LiveStateJsonlCursor,
) -> bytes:
    """Read input and verify that every cursor-committed byte is unchanged."""
    path = Path(input_path)
    if not path.is_file():
        raise LiveStateJsonlError("Live state JSONL input cannot be read.")
    try:
        input_bytes = path.read_bytes()
    except OSError as exc:
        raise LiveStateJsonlError("Live state JSONL input cannot be read.") from exc
    if len(input_bytes) < cursor.byte_offset:
        raise LiveStateJsonlError("Live state JSONL input was truncated.")
    prefix = input_bytes[: cursor.byte_offset]
    if hashlib.sha256(prefix).hexdigest() != cursor.processed_prefix_sha256:
        raise LiveStateJsonlError(
            "Previously processed live state JSONL bytes were modified."
        )
    if cursor.byte_offset > 0 and prefix[-1:] != b"\n":
        raise LiveStateJsonlError(
            "Live state JSONL cursor does not follow a processed newline."
        )
    if prefix.count(b"\n") != cursor.processed_line_count:
        raise LiveStateJsonlError(
            "Live state JSONL cursor line count does not match its prefix."
        )
    return input_bytes


def discover_complete_jsonl_records(
    input_bytes: bytes,
    cursor: LiveStateJsonlCursor,
) -> NewJsonlRecords:
    """Discover complete LF-terminated records after the saved cursor."""
    new_bytes = input_bytes[cursor.byte_offset :]
    if not new_bytes:
        return NewJsonlRecords(
            lines=(), pending_partial_record=False, has_new_bytes=False
        )

    lines: list[CompleteJsonlLine] = []
    relative_start = 0
    line_number = cursor.processed_line_count + 1
    while True:
        relative_newline = new_bytes.find(b"\n", relative_start)
        if relative_newline < 0:
            break
        end_offset = cursor.byte_offset + relative_newline + 1
        lines.append(
            CompleteJsonlLine(
                line_number=line_number,
                start_offset=cursor.byte_offset + relative_start,
                end_offset=end_offset,
                content=new_bytes[relative_start : relative_newline + 1],
            )
        )
        line_number += 1
        relative_start = relative_newline + 1
    return NewJsonlRecords(
        lines=tuple(lines),
        pending_partial_record=relative_start < len(new_bytes),
        has_new_bytes=True,
    )


def live_state_signal_path(signal_dir: str | Path, match_id: int) -> Path:
    """Select the stable current-signal path for one validated match."""
    if type(match_id) is not int or match_id <= 0:
        raise LiveStateJsonlError("Live state JSONL match_id is invalid.")
    return Path(signal_dir) / f"{match_id}.json"


def advance_live_state_jsonl_cursor(
    cursor: LiveStateJsonlCursor,
    line: CompleteJsonlLine,
    processed_prefix_sha256: str,
) -> LiveStateJsonlCursor:
    """Return the cursor immediately after one successfully processed line."""
    if line.start_offset != cursor.byte_offset:
        raise LiveStateJsonlError("Live state JSONL cursor advancement is invalid.")
    if line.line_number != cursor.processed_line_count + 1:
        raise LiveStateJsonlError("Live state JSONL line advancement is invalid.")
    if not line.content.endswith(b"\n") or not _is_sha256(
        processed_prefix_sha256
    ):
        raise LiveStateJsonlError("Live state JSONL cursor advancement is invalid.")
    return LiveStateJsonlCursor(
        input_filename=cursor.input_filename,
        byte_offset=line.end_offset,
        processed_line_count=line.line_number,
        processed_prefix_sha256=processed_prefix_sha256,
    )


def persist_live_state_jsonl_cursor(
    cursor_path: str | Path,
    cursor: LiveStateJsonlCursor,
) -> Literal["BUILT", "UNCHANGED"]:
    payload = {
        "schema_version": JSONL_SCHEMA_VERSION,
        "input_filename": cursor.input_filename,
        "byte_offset": cursor.byte_offset,
        "processed_line_count": cursor.processed_line_count,
        "processed_prefix_sha256": cursor.processed_prefix_sha256,
    }
    return _write_atomic_if_changed(
        Path(cursor_path),
        _render_json(payload),
        "live state JSONL cursor",
    )


def build_live_state_jsonl_summary(
    *,
    status: Literal["processed", "idle", "waiting_for_complete_record"],
    cursor: LiveStateJsonlCursor,
    start_line: int | None,
    end_line: int | None,
    processed_event_count: int,
    changed_event_count: int,
    unchanged_event_count: int,
    scored_event_count: int,
    not_ready_event_count: int,
    pending_partial_record: bool,
) -> dict[str, object]:
    if processed_event_count != changed_event_count + unchanged_event_count:
        raise LiveStateJsonlError("Live state JSONL summary accounting is invalid.")
    if processed_event_count != scored_event_count + not_ready_event_count:
        raise LiveStateJsonlError("Live state JSONL summary accounting is invalid.")
    if status == "processed":
        if (
            processed_event_count <= 0
            or type(start_line) is not int
            or type(end_line) is not int
            or end_line - start_line + 1 != processed_event_count
        ):
            raise LiveStateJsonlError("Live state JSONL summary range is invalid.")
    elif (
        processed_event_count != 0
        or start_line is not None
        or end_line is not None
        or pending_partial_record != (status == "waiting_for_complete_record")
    ):
        raise LiveStateJsonlError("Live state JSONL summary state is invalid.")
    return {
        "schema_version": JSONL_SCHEMA_VERSION,
        "status": status,
        "input_filename": cursor.input_filename,
        "start_line": start_line,
        "end_line": end_line,
        "processed_event_count": processed_event_count,
        "changed_event_count": changed_event_count,
        "unchanged_event_count": unchanged_event_count,
        "scored_event_count": scored_event_count,
        "not_ready_event_count": not_ready_event_count,
        "pending_partial_record": pending_partial_record,
        "cursor": {
            "byte_offset": cursor.byte_offset,
            "processed_line_count": cursor.processed_line_count,
            "processed_prefix_sha256": cursor.processed_prefix_sha256,
        },
    }


def persist_live_state_jsonl_summary(
    output_path: str | Path,
    summary: object,
) -> Literal["BUILT", "UNCHANGED"]:
    return _write_atomic_if_changed(
        Path(output_path),
        _render_json(summary),
        "live state JSONL summary",
    )


def sync_live_state_jsonl(
    model_dir: str | Path,
    state_dir: str | Path,
    input_path: str | Path,
    signal_dir: str | Path,
    cursor_path: str | Path,
    output_path: str | Path,
) -> LiveStateJsonlSyncResult:
    """Perform one bounded append-only JSONL synchronization pass."""
    cursor, cursor_exists = load_live_state_jsonl_cursor(cursor_path, input_path)
    input_bytes = verify_processed_prefix(input_path, cursor)
    discovered = discover_complete_jsonl_records(input_bytes, cursor)

    if not discovered.has_new_bytes:
        cursor_status = (
            "UNCHANGED"
            if cursor_exists
            else persist_live_state_jsonl_cursor(cursor_path, cursor)
        )
        if _has_current_summary(output_path, cursor, pending_partial_record=False):
            summary_status: Literal["BUILT", "UNCHANGED"] = "UNCHANGED"
        else:
            summary = build_live_state_jsonl_summary(
                status="idle",
                cursor=cursor,
                start_line=None,
                end_line=None,
                processed_event_count=0,
                changed_event_count=0,
                unchanged_event_count=0,
                scored_event_count=0,
                not_ready_event_count=0,
                pending_partial_record=False,
            )
            summary_status = persist_live_state_jsonl_summary(output_path, summary)
        return LiveStateJsonlSyncResult(
            status=(
                "BUILT"
                if cursor_status == "BUILT" or summary_status == "BUILT"
                else "UNCHANGED"
            ),
            cursor=cursor,
        )

    if not discovered.lines:
        cursor_status = (
            "UNCHANGED"
            if cursor_exists
            else persist_live_state_jsonl_cursor(cursor_path, cursor)
        )
        summary = build_live_state_jsonl_summary(
            status="waiting_for_complete_record",
            cursor=cursor,
            start_line=None,
            end_line=None,
            processed_event_count=0,
            changed_event_count=0,
            unchanged_event_count=0,
            scored_event_count=0,
            not_ready_event_count=0,
            pending_partial_record=True,
        )
        summary_status = persist_live_state_jsonl_summary(output_path, summary)
        return LiveStateJsonlSyncResult(
            status=(
                "BUILT"
                if cursor_status == "BUILT" or summary_status == "BUILT"
                else "UNCHANGED"
            ),
            cursor=cursor,
        )

    initial_line = discovered.lines[0].line_number
    changed_event_count = 0
    unchanged_event_count = 0
    scored_event_count = 0
    not_ready_event_count = 0
    prefix_hasher = hashlib.sha256(input_bytes[: cursor.byte_offset])

    for line in discovered.lines:
        result = _process_complete_line(
            line,
            model_dir=Path(model_dir),
            state_dir=Path(state_dir),
            signal_dir=Path(signal_dir),
        )
        if result.status == "BUILT":
            changed_event_count += 1
        else:
            unchanged_event_count += 1
        if result.response_status == "scored":
            scored_event_count += 1
        else:
            not_ready_event_count += 1

        prefix_hasher.update(line.content)
        cursor = advance_live_state_jsonl_cursor(
            cursor,
            line,
            prefix_hasher.hexdigest(),
        )
        try:
            persist_live_state_jsonl_cursor(cursor_path, cursor)
        except LiveStateJsonlError as exc:
            raise LiveStateJsonlError(
                f"JSONL record at physical line {line.line_number} was ingested "
                "but its cursor could not be advanced."
            ) from exc

    processed_event_count = len(discovered.lines)
    summary = build_live_state_jsonl_summary(
        status="processed",
        cursor=cursor,
        start_line=initial_line,
        end_line=discovered.lines[-1].line_number,
        processed_event_count=processed_event_count,
        changed_event_count=changed_event_count,
        unchanged_event_count=unchanged_event_count,
        scored_event_count=scored_event_count,
        not_ready_event_count=not_ready_event_count,
        pending_partial_record=discovered.pending_partial_record,
    )
    persist_live_state_jsonl_summary(output_path, summary)
    return LiveStateJsonlSyncResult(status="BUILT", cursor=cursor)


def _process_complete_line(
    line: CompleteJsonlLine,
    *,
    model_dir: Path,
    state_dir: Path,
    signal_dir: Path,
) -> signal.LiveStateSignalResult:
    record_bytes = line.content[:-1]
    try:
        record_text = record_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LiveStateJsonlError(
            f"Invalid UTF-8 at physical line {line.line_number}."
        ) from exc
    if not record_text.strip():
        raise LiveStateJsonlError(
            f"Blank JSONL record at physical line {line.line_number}."
        )
    try:
        payload = json.loads(record_text)
    except json.JSONDecodeError as exc:
        raise LiveStateJsonlError(
            f"Malformed JSON at physical line {line.line_number}."
        ) from exc
    if not isinstance(payload, dict):
        raise LiveStateJsonlError(
            f"JSONL record at physical line {line.line_number} must be an object."
        )
    try:
        request = signal.validate_live_state_snapshot_request(payload)
    except signal.LiveStateSignalError as exc:
        raise LiveStateJsonlError(
            f"Invalid JSONL record at physical line {line.line_number}: {exc}"
        ) from exc

    request_path: Path | None = None
    try:
        descriptor, request_name = tempfile.mkstemp(
            prefix=".live-state-jsonl-request.", suffix=".json"
        )
        request_path = Path(request_name)
        with os.fdopen(descriptor, "wb") as file:
            file.write(record_bytes)
            file.flush()
            os.fsync(file.fileno())
        return signal.ingest_live_state_snapshot(
            model_dir,
            state_dir,
            request_path,
            live_state_signal_path(signal_dir, request.match_id),
        )
    except (
        OSError,
        signal.LiveStateSignalError,
        history.ReplayStateHistoryError,
        model.ReplayStateModelError,
    ) as exc:
        raise LiveStateJsonlError(
            f"JSONL record at physical line {line.line_number} could not be ingested."
        ) from exc
    finally:
        if request_path is not None:
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass


def _has_current_summary(
    output_path: str | Path,
    cursor: LiveStateJsonlCursor,
    *,
    pending_partial_record: bool,
) -> bool:
    path = Path(output_path)
    if not path.is_file():
        return False
    try:
        content = path.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or set(payload) != _SUMMARY_FIELDS:
        return False
    if any(not isinstance(key, str) for key in payload):
        return False
    summary_cursor = payload["cursor"]
    if not isinstance(summary_cursor, dict) or set(summary_cursor) != (
        _SUMMARY_CURSOR_FIELDS
    ):
        return False
    expected_cursor = {
        "byte_offset": cursor.byte_offset,
        "processed_line_count": cursor.processed_line_count,
        "processed_prefix_sha256": cursor.processed_prefix_sha256,
    }
    if summary_cursor != expected_cursor:
        return False
    if payload["schema_version"] != JSONL_SCHEMA_VERSION:
        return False
    if payload["input_filename"] != cursor.input_filename:
        return False
    status = payload["status"]
    if status not in {"processed", "idle", "waiting_for_complete_record"}:
        return False
    counts = (
        payload["processed_event_count"],
        payload["changed_event_count"],
        payload["unchanged_event_count"],
        payload["scored_event_count"],
        payload["not_ready_event_count"],
    )
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    processed, changed, unchanged, scored, not_ready = counts
    if processed != changed + unchanged or processed != scored + not_ready:
        return False
    if type(payload["pending_partial_record"]) is not bool:
        return False
    if payload["pending_partial_record"] != pending_partial_record:
        return False
    start_line = payload["start_line"]
    end_line = payload["end_line"]
    if status == "processed":
        if (
            processed <= 0
            or type(start_line) is not int
            or type(end_line) is not int
            or end_line - start_line + 1 != processed
            or end_line != cursor.processed_line_count
        ):
            return False
    elif processed != 0 or start_line is not None or end_line is not None:
        return False
    if status == "waiting_for_complete_record" and not pending_partial_record:
        return False
    try:
        return _render_json(payload) == content
    except LiveStateJsonlError:
        return False


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _render_json(payload: object) -> bytes:
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LiveStateJsonlError(
            "Live state JSONL state could not be rendered."
        ) from exc
    return f"{text}\n".encode("utf-8")


def _write_atomic_if_changed(
    path: Path,
    content: bytes,
    label: str,
) -> Literal["BUILT", "UNCHANGED"]:
    if path.exists():
        if not path.is_file():
            raise LiveStateJsonlError(f"Could not atomically write {label}.")
        try:
            if path.read_bytes() == content:
                return "UNCHANGED"
        except OSError as exc:
            raise LiveStateJsonlError(f"Could not atomically write {label}.") from exc
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
        raise LiveStateJsonlError(f"Could not atomically write {label}.") from exc
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
    return "BUILT"
