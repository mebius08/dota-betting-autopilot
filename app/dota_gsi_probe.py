from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from typing import Any, NoReturn, cast


MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_PAYLOAD_FILENAME = re.compile(r"^(?P<sequence>[0-9]{6})\.json$")

_FIELD_PATHS = (
    "map.matchid",
    "map.match_id",
    "map.clock_time",
    "map.game_time",
    "player.kills",
    "player.deaths",
    "player.assists",
    "player.last_hits",
    "player.denies",
    "player.gold",
    "player.net_worth",
    "hero.experience",
)


class DotaGSIProbeError(RuntimeError):
    """A safe, stable failure from probe configuration or persistence."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class DuplicateJSONKeyError(ValueError):
    """Raised when JSON contains the same key more than once in an object."""


class _RequestRejected(Exception):
    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PayloadRecord:
    sequence: int
    filename: str
    sha256: str
    payload: dict[str, object] = field(repr=False)


@dataclass(frozen=True)
class CaptureResult:
    payload_count: int

    @property
    def status(self) -> str:
        return "CAPTURED" if self.payload_count else "NO_PAYLOADS"


def validate_loopback_host(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise DotaGSIProbeError("non_loopback_host")


def decode_json_object(content: bytes) -> dict[str, object]:
    """Decode strict UTF-8 JSON while rejecting duplicate object keys."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DotaGSIProbeError("invalid_utf8") from exc

    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except DuplicateJSONKeyError as exc:
        raise DotaGSIProbeError("duplicate_json_key") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise DotaGSIProbeError("invalid_json") from exc

    if not isinstance(decoded, dict):
        raise DotaGSIProbeError("json_not_object")
    return cast(dict[str, object], decoded)


def constant_time_token_matches(provided: object, expected: str) -> bool:
    if not isinstance(provided, str):
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def validate_and_sanitize_payload(
    payload: Mapping[str, object],
    expected_token: str,
) -> dict[str, object]:
    """Authenticate a payload and remove its complete top-level auth object."""

    if not expected_token:
        raise DotaGSIProbeError("empty_auth_token")
    auth = payload.get("auth")
    provided_token = auth.get("token") if isinstance(auth, Mapping) else None
    if not constant_time_token_matches(provided_token, expected_token):
        raise DotaGSIProbeError("authentication_failed")

    sanitized = dict(payload)
    sanitized.pop("auth", None)
    return sanitized


def canonical_payload_bytes(payload: Mapping[str, object]) -> bytes:
    """Return deterministic UTF-8 JSON bytes for one sanitized payload."""

    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DotaGSIProbeError("payload_not_serializable") from exc
    return f"{rendered}\n".encode("utf-8")


def validate_existing_payloads(output_dir: Path) -> list[PayloadRecord]:
    """Validate and load the complete canonical payload sequence."""

    payload_dir = output_dir / "payloads"
    if not payload_dir.exists():
        return []
    if not payload_dir.is_dir():
        raise DotaGSIProbeError("existing_payloads_invalid")

    numbered_paths: list[tuple[int, Path]] = []
    try:
        entries = list(payload_dir.iterdir())
    except OSError as exc:
        raise DotaGSIProbeError("existing_payloads_invalid") from exc

    for path in entries:
        match = _PAYLOAD_FILENAME.fullmatch(path.name)
        if match is None or not path.is_file():
            raise DotaGSIProbeError("existing_payloads_invalid")
        numbered_paths.append((int(match.group("sequence")), path))

    numbered_paths.sort(key=lambda item: item[0])
    expected_sequences = list(range(1, len(numbered_paths) + 1))
    if [sequence for sequence, _ in numbered_paths] != expected_sequences:
        raise DotaGSIProbeError("existing_payloads_invalid")

    records: list[PayloadRecord] = []
    for sequence, path in numbered_paths:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise DotaGSIProbeError("existing_payloads_invalid") from exc
        try:
            payload = decode_json_object(content)
            if "auth" in payload or canonical_payload_bytes(payload) != content:
                raise DotaGSIProbeError("existing_payloads_invalid")
        except DotaGSIProbeError as exc:
            raise DotaGSIProbeError("existing_payloads_invalid") from exc
        records.append(
            PayloadRecord(
                sequence=sequence,
                filename=f"payloads/{path.name}",
                sha256=hashlib.sha256(content).hexdigest(),
                payload=payload,
            )
        )
    return records


def persist_payload(
    output_dir: Path,
    payload: Mapping[str, object],
    sequence: int,
) -> PayloadRecord:
    """Atomically install a new canonical payload without overwriting a file."""

    if sequence < 1 or sequence > 999_999 or "auth" in payload:
        raise DotaGSIProbeError("persistence_failed")
    content = canonical_payload_bytes(payload)
    payload_dir = output_dir / "payloads"
    destination = payload_dir / f"{sequence:06d}.json"
    temporary: Path | None = None
    try:
        payload_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise DotaGSIProbeError("persistence_failed")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=payload_dir,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        # A hard link publishes the fully-written bytes atomically and fails if
        # the final name already exists, so an external writer cannot be lost.
        os.link(temporary, destination)
    except DotaGSIProbeError:
        raise
    except OSError as exc:
        raise DotaGSIProbeError("persistence_failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    return PayloadRecord(
        sequence=sequence,
        filename=f"payloads/{destination.name}",
        sha256=hashlib.sha256(content).hexdigest(),
        payload=dict(payload),
    )


def build_structural_audit(records: Sequence[PayloadRecord]) -> dict[str, object]:
    """Build the fixed capability-audit schema from persisted payload records."""

    top_level_keys: set[str] = set()
    map_keys: set[str] = set()
    player_keys: set[str] = set()
    hero_keys: set[str] = set()
    match_ids: set[int] = set()
    game_times: list[int | float] = []
    shape_counts = {
        "player_single_object": 0,
        "player_team_mapping": 0,
        "hero_single_object": 0,
        "hero_team_mapping": 0,
    }
    field_counts = {path: 0 for path in _FIELD_PATHS}

    ordered_records = sorted(records, key=lambda record: record.filename)
    for record in ordered_records:
        payload = record.payload
        top_level_keys.update(payload)
        map_section = _mapping(payload.get("map"))
        player_section = _mapping(payload.get("player"))
        hero_section = _mapping(payload.get("hero"))

        if map_section is not None:
            map_keys.update(map_section)
            for key in ("matchid", "match_id"):
                if key in map_section:
                    field_counts[f"map.{key}"] += 1
                    match_id = _numeric_match_id(map_section[key])
                    if match_id is not None:
                        match_ids.add(match_id)
            for key in ("clock_time", "game_time"):
                if key in map_section:
                    field_counts[f"map.{key}"] += 1
                    game_time = _finite_number(map_section[key])
                    if game_time is not None and not any(
                        game_time == existing for existing in game_times
                    ):
                        game_times.append(game_time)

        _observe_entity_section(
            "player", player_section, player_keys, shape_counts, field_counts
        )
        _observe_entity_section(
            "hero", hero_section, hero_keys, shape_counts, field_counts
        )

    game_times.sort()
    return {
        "schema_version": 1,
        "payload_count": len(ordered_records),
        "payload_files": [
            {"filename": record.filename, "sha256": record.sha256}
            for record in ordered_records
        ],
        "top_level_keys": sorted(top_level_keys),
        "map_keys": sorted(map_keys),
        "player_keys": sorted(player_keys),
        "hero_keys": sorted(hero_keys),
        "observed_match_ids": sorted(match_ids),
        "observed_game_times": game_times,
        "shape_counts": shape_counts,
        "field_presence_counts": field_counts,
    }


def rebuild_structural_audit(
    output_dir: Path,
    records: Sequence[PayloadRecord],
) -> None:
    audit = build_structural_audit(records)
    content = canonical_payload_bytes(audit)
    _replace_file_atomically(output_dir / "probe.json", content)


def capture_dota_gsi_probe(
    *,
    host: str,
    port: int,
    output_dir: Path,
    max_payloads: int,
    idle_timeout_seconds: float,
    expected_token: str,
    emit: Callable[[str], None] = print,
) -> CaptureResult:
    """Run one sequential, bounded local GSI HTTP capture."""

    validate_loopback_host(host)
    if (
        not 0 <= port <= 65_535
        or max_payloads < 1
        or not math.isfinite(idle_timeout_seconds)
        or idle_timeout_seconds <= 0
        or not expected_token
    ):
        raise DotaGSIProbeError("invalid_configuration")

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DotaGSIProbeError("persistence_failed") from exc
    if not output_dir.is_dir():
        raise DotaGSIProbeError("persistence_failed")
    records = validate_existing_payloads(output_dir)
    rebuild_structural_audit(output_dir, records)

    server_class = _IPv6ProbeHTTPServer if host == "::1" else _ProbeHTTPServer
    try:
        server = server_class(
            (host, port),
            _ProbeRequestHandler,
            expected_token=expected_token,
            output_dir=output_dir,
            records=records,
            max_payloads=max_payloads,
            emit=emit,
        )
    except OSError as exc:
        raise DotaGSIProbeError("binding_failed") from exc

    with server:
        bound_port = int(server.server_address[1])
        emit(f"LISTENING host={host} port={bound_port}")
        started = time.monotonic()
        server.last_accepted_at = started
        while server.accepted_count < max_payloads and server.fatal_error is None:
            remaining = idle_timeout_seconds - (
                time.monotonic() - server.last_accepted_at
            )
            if remaining <= 0:
                break
            server.timeout = remaining
            server.handle_request()

        if server.fatal_error is not None:
            raise server.fatal_error
        return CaptureResult(payload_count=server.accepted_count)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError
    return parsed


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return cast(Mapping[str, object], value)


def _numeric_match_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _finite_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _observe_entity_section(
    name: str,
    section: Mapping[str, object] | None,
    observed_keys: set[str],
    shape_counts: dict[str, int],
    field_counts: dict[str, int],
) -> None:
    if section is None:
        return
    observed_keys.update(section)
    if section:
        shape = "team_mapping" if any(
            isinstance(value, Mapping) for value in section.values()
        ) else "single_object"
        shape_counts[f"{name}_{shape}"] += 1

    fields = (
        ("kills", "deaths", "assists", "last_hits", "denies", "gold", "net_worth")
        if name == "player"
        else ("experience",)
    )
    for field_name in fields:
        if _contains_key_recursively(section, field_name):
            field_counts[f"{name}.{field_name}"] += 1


def _contains_key_recursively(value: Mapping[str, object], key: str) -> bool:
    if key in value:
        return True
    return any(
        _contains_key_recursively(nested, key)
        for item in value.values()
        if (nested := _mapping(item)) is not None
    )


def _replace_file_atomically(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_file() and path.read_bytes() == content:
            return
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise DotaGSIProbeError("persistence_failed") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class _ProbeHTTPServer(HTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        expected_token: str,
        output_dir: Path,
        records: list[PayloadRecord],
        max_payloads: int,
        emit: Callable[[str], None],
    ) -> None:
        self.expected_token = expected_token
        self.output_dir = output_dir
        self.records = records
        self.max_payloads = max_payloads
        self.emit = emit
        self.accepted_count = 0
        self.last_accepted_at = 0.0
        self.fatal_error: DotaGSIProbeError | None = None
        super().__init__(server_address, handler_class)


class _IPv6ProbeHTTPServer(_ProbeHTTPServer):
    address_family = socket.AF_INET6


class _ProbeRequestHandler(BaseHTTPRequestHandler):
    server: _ProbeHTTPServer

    def do_POST(self) -> None:
        if self.path != "/":
            self.close_connection = True
            self._reject(404, "wrong_path")
            return
        if self.server.accepted_count >= self.server.max_payloads:
            self._reject(503, "capture_complete")
            return
        try:
            content = self._read_request_body()
            payload = _decode_request(content)
            sanitized = _authenticate_request(payload, self.server.expected_token)
            sequence = len(self.server.records) + 1
            record = persist_payload(self.server.output_dir, sanitized, sequence)
            updated_records = [*self.server.records, record]
            rebuild_structural_audit(self.server.output_dir, updated_records)
        except _RequestRejected as exc:
            self._reject(exc.status, exc.reason)
            return
        except DotaGSIProbeError as exc:
            reason = (
                "persistence_failed"
                if exc.reason in {"persistence_failed", "payload_not_serializable"}
                else "internal_failure"
            )
            self.server.fatal_error = DotaGSIProbeError(reason)
            self._reject(500, reason)
            return
        except Exception:
            self.server.fatal_error = DotaGSIProbeError("internal_failure")
            self._reject(500, "internal_failure")
            return

        self.server.records.append(record)
        self.server.accepted_count += 1
        self.server.last_accepted_at = time.monotonic()
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.server.emit(f"ACCEPTED sequence={sequence}")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self._reject(405, "wrong_method")

    def _read_request_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _RequestRejected(400, "unsupported_transfer_encoding")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _RequestRejected(411, "content_length_required")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise _RequestRejected(400, "invalid_content_length") from exc
        if content_length < 0:
            raise _RequestRejected(400, "invalid_content_length")
        if content_length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            raise _RequestRejected(413, "body_too_large")
        content = self.rfile.read(content_length)
        if len(content) != content_length:
            raise _RequestRejected(400, "incomplete_body")
        return content

    def _reject(self, status: int, reason: str) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.server.emit(f"REJECTED reason={reason}")

    def log_message(self, _format: str, *args: object) -> None:
        return


def _decode_request(content: bytes) -> dict[str, object]:
    try:
        return decode_json_object(content)
    except DotaGSIProbeError as exc:
        statuses = {
            "invalid_utf8": 400,
            "invalid_json": 400,
            "duplicate_json_key": 400,
            "json_not_object": 400,
        }
        raise _RequestRejected(statuses.get(exc.reason, 400), exc.reason) from exc


def _authenticate_request(
    payload: Mapping[str, object],
    expected_token: str,
) -> dict[str, object]:
    try:
        return validate_and_sanitize_payload(payload, expected_token)
    except DotaGSIProbeError as exc:
        if exc.reason == "authentication_failed":
            raise _RequestRejected(401, "authentication_failed") from exc
        raise
