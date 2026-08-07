from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import math
from pathlib import Path
import socket
import sys
import time
from typing import Any, Literal, cast

from app import dota_gsi_live as live
from app.dota_gsi_probe import (
    DotaGSIProbeError,
    _ProbeRequestHandler,
    _RequestRejected,
    _authenticate_request,
    _decode_request,
    validate_loopback_host,
)
from app.live_state_signal import LiveStateSignalError
from app.replay_state_history import ReplayStateHistoryError
from app.replay_state_model import ReplayStateModelError


TerminationReason = Literal["max_payloads", "idle_timeout", "interrupted"]


class DotaGSIListenerError(RuntimeError):
    """A safe, stable terminal failure from the live listener."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class DotaGSIListenerResult:
    accepted: int
    ignored: int
    not_ready: int
    scored: int
    termination_reason: TerminationReason

    def __post_init__(self) -> None:
        if self.accepted != self.ignored + self.not_ready + self.scored:
            raise ValueError("Live listener accounting is inconsistent.")

    @property
    def summary(self) -> str:
        return (
            f"SUMMARY accepted={self.accepted} ignored={self.ignored} "
            f"not_ready={self.not_ready} scored={self.scored}"
        )


def run_dota_gsi_live(
    *,
    host: str,
    port: int,
    model_dir: str | Path,
    state_dir: str | Path,
    signal_dir: str | Path,
    max_payloads: int,
    idle_timeout_seconds: float,
    expected_token: str,
    emit: Callable[[str], None] = print,
) -> DotaGSIListenerResult:
    """Run one sequential, bounded local Dota GSI live listener."""

    try:
        validate_loopback_host(host)
    except DotaGSIProbeError as exc:
        raise DotaGSIListenerError(exc.reason) from exc
    if (
        not 0 <= port <= 65_535
        or max_payloads < 1
        or not math.isfinite(idle_timeout_seconds)
        or idle_timeout_seconds <= 0
        or not expected_token
    ):
        raise DotaGSIListenerError("invalid_configuration")

    server_class = (
        _IPv6DotaGSILiveHTTPServer
        if host == "::1"
        else _DotaGSILiveHTTPServer
    )
    try:
        server = server_class(
            (host, port),
            _DotaGSILiveRequestHandler,
            expected_token=expected_token,
            model_dir=Path(model_dir),
            state_dir=Path(state_dir),
            signal_dir=Path(signal_dir),
            max_payloads=max_payloads,
            emit=emit,
        )
    except OSError as exc:
        raise DotaGSIListenerError("binding_failed") from exc

    termination_reason: TerminationReason = "max_payloads"
    with server:
        bound_port = int(server.server_address[1])
        emit(f"LISTENING host={host} port={bound_port}")
        server.last_accepted_at = time.monotonic()
        try:
            while (
                server.accepted_count < max_payloads
                and server.fatal_error is None
            ):
                remaining = idle_timeout_seconds - (
                    time.monotonic() - server.last_accepted_at
                )
                if remaining <= 0:
                    termination_reason = "idle_timeout"
                    break
                server.timeout = remaining
                server.handle_request()
        except KeyboardInterrupt:
            termination_reason = "interrupted"

        if server.fatal_error is not None:
            raise server.fatal_error
        return DotaGSIListenerResult(
            accepted=server.accepted_count,
            ignored=server.status_counts["ignored"],
            not_ready=server.status_counts["not_ready"],
            scored=server.status_counts["scored"],
            termination_reason=termination_reason,
        )


class _DotaGSILiveHTTPServer(HTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        expected_token: str,
        model_dir: Path,
        state_dir: Path,
        signal_dir: Path,
        max_payloads: int,
        emit: Callable[[str], None],
    ) -> None:
        self.expected_token = expected_token
        self.model_dir = model_dir
        self.state_dir = state_dir
        self.signal_dir = signal_dir
        self.max_payloads = max_payloads
        self.emit = emit
        self.accepted_count = 0
        self.last_accepted_at = 0.0
        self.status_counts = {"ignored": 0, "not_ready": 0, "scored": 0}
        self.emitted_lines: set[str] = set()
        self.fatal_error: DotaGSIListenerError | None = None
        super().__init__(server_address, handler_class)

    def record_accepted(self, result: live.InMemoryDotaGSILiveResult) -> None:
        response_status = result.response_status
        self.accepted_count += 1
        self.status_counts[response_status] += 1
        self.last_accepted_at = time.monotonic()

        line = _console_line(result)
        if line is not None and line not in self.emitted_lines:
            self.emitted_lines.add(line)
            self.emit(line)

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        del request, client_address
        if isinstance(sys.exception(), ConnectionError):
            return
        self.fatal_error = DotaGSIListenerError("internal_failure")


class _IPv6DotaGSILiveHTTPServer(_DotaGSILiveHTTPServer):
    address_family = socket.AF_INET6


class _DotaGSILiveRequestHandler(_ProbeRequestHandler):
    def do_POST(self) -> None:
        server = cast(_DotaGSILiveHTTPServer, self.server)
        if self.path != "/":
            self.close_connection = True
            self._reject(404, "wrong_path")
            return
        if server.accepted_count >= server.max_payloads:
            self._reject(503, "listener_complete")
            return

        try:
            content = self._read_request_body()
            payload = _decode_request(content)
            sanitized = _authenticate_request(payload, server.expected_token)
        except _RequestRejected as exc:
            self._reject(exc.status, exc.reason)
            return

        try:
            result = live.ingest_dota_gsi_payload_in_memory(
                server.model_dir,
                server.state_dir,
                server.signal_dir,
                sanitized,
            )
            server.record_accepted(result)
        except live.DotaGSILiveError:
            self._reject(422, "invalid_gsi_payload")
            return
        except LiveStateSignalError:
            self._terminal_failure("persistence_failed")
            return
        except ReplayStateHistoryError:
            self._terminal_failure("state_failure")
            return
        except ReplayStateModelError:
            self._terminal_failure("model_failure")
            return
        except Exception:
            self._terminal_failure("internal_failure")
            return

        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _terminal_failure(self, reason: str) -> None:
        server = cast(_DotaGSILiveHTTPServer, self.server)
        server.fatal_error = DotaGSIListenerError(reason)
        self._reject(500, reason)


def _console_line(result: live.InMemoryDotaGSILiveResult) -> str | None:
    if result.status != "BUILT":
        return None
    response = result.response
    response_status = response.get("status")
    if response_status != result.response_status:
        raise DotaGSIListenerError("invalid_adapter_response")

    match_id = response.get("match_id")
    game_time = response.get("game_time_seconds")
    if result.response_status == "ignored":
        if response.get("reason") != live.IGNORE_REASON or match_id is None:
            return None
        return (
            f"IGNORED match_id={match_id} game_time={game_time} "
            f"reason={response['reason']}"
        )
    if result.response_status == "not_ready":
        return (
            f"STATE match_id={match_id} game_time={game_time} "
            f"status=not_ready snapshots={response.get('snapshot_count')}"
        )
    if result.response_status == "scored":
        return (
            f"SIGNAL match_id={match_id} game_time={game_time} "
            "prediction="
            f"{response.get('prediction_net_worth_diff_change')} direction="
            f"{response.get('predicted_direction')}"
        )
    raise DotaGSIListenerError("invalid_adapter_response")
