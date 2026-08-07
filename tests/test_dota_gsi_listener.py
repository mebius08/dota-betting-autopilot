from __future__ import annotations

from collections.abc import Mapping, Sequence
import http.client
import json
from pathlib import Path
import socket
import threading
from typing import Any

import pytest

from app import cli
from app import dota_gsi_listener as listener
from app import live_state_signal as signal
from app import replay_state_history as history
from app import replay_state_model as model
from app.dota_gsi_probe import MAX_REQUEST_BODY_BYTES


TOKEN = "listener-test-token"


class _FakeModel:
    def __init__(self, prediction: float) -> None:
        self.prediction = prediction

    def predict(self, data: Sequence[Sequence[float]]) -> list[float]:
        return [self.prediction for _ in data]


class _ListenerThread:
    def __init__(
        self,
        root: Path,
        *,
        max_payloads: int = 1,
        idle_timeout_seconds: float = 2.0,
    ) -> None:
        self.lines: list[str] = []
        self.result: listener.DotaGSIListenerResult | None = None
        self.error: BaseException | None = None
        self.port = 0
        self._listening = threading.Event()

        def emit(line: str) -> None:
            self.lines.append(line)
            if line.startswith("LISTENING "):
                self.port = int(line.rsplit("=", 1)[1])
                self._listening.set()

        def run() -> None:
            try:
                self.result = listener.run_dota_gsi_live(
                    host="127.0.0.1",
                    port=0,
                    model_dir=root / "bundle",
                    state_dir=root / "state",
                    signal_dir=root / "signals",
                    max_payloads=max_payloads,
                    idle_timeout_seconds=idle_timeout_seconds,
                    expected_token=TOKEN,
                    emit=emit,
                )
            except BaseException as exc:
                self.error = exc
                self._listening.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self._listening.wait(5), "listener did not start"
        if self.error is not None:
            raise self.error

    def join(self) -> listener.DotaGSIListenerResult:
        self._thread.join(10)
        assert not self._thread.is_alive(), "listener did not stop"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def join_error(self) -> BaseException:
        self._thread.join(10)
        assert not self._thread.is_alive(), "listener did not stop"
        assert self.error is not None
        return self.error


def _request(
    port: int,
    *,
    method: str = "POST",
    path: str = "/",
    body: bytes = b"",
) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _post(port: int, payload: object, token: str = TOKEN) -> int:
    body = json.dumps(
        {"auth": {"token": token}, **dict(cast_mapping(payload))},
        allow_nan=False,
    ).encode("utf-8")
    return _request(port, body=body)


def cast_mapping(payload: object) -> Mapping[str, object]:
    assert isinstance(payload, Mapping)
    return payload


def _fake_ignored_result(
    *,
    status: str = "BUILT",
) -> listener.live.InMemoryDotaGSILiveResult:
    assert status in {"BUILT", "UNCHANGED"}
    return listener.live.InMemoryDotaGSILiveResult(
        status=status,  # type: ignore[arg-type]
        response_status="ignored",
        output_path=None,
        response={
            "schema_version": 1,
            "status": "ignored",
            "reason": "game_not_in_progress",
            "match_id": None,
            "game_time_seconds": -1,
        },
    )


def _install_fake_adapter(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[object] | None = None,
) -> None:
    def ingest(
        _model_dir: str | Path,
        _state_dir: str | Path,
        _signal_dir: str | Path,
        payload: object,
    ) -> listener.live.InMemoryDotaGSILiveResult:
        if calls is not None:
            calls.append(payload)
        return _fake_ignored_result()

    monkeypatch.setattr(listener.live, "ingest_dota_gsi_payload_in_memory", ingest)


def _install_fake_model(
    monkeypatch: pytest.MonkeyPatch,
    prediction: float = -123.45,
) -> None:
    scorer = model.ReplayStateModelScorer(
        feature_columns=history.SUPPORTED_MODEL_FEATURE_COLUMNS,
        manifest_sha256="1" * 64,
        _model=_FakeModel(prediction),
    )
    monkeypatch.setattr(signal.model, "load_replay_state_model", lambda _path: scorer)


def test_loopback_listener_starts_and_idle_timeout_is_bounded(tmp_path: Path) -> None:
    lines: list[str] = []

    result = listener.run_dota_gsi_live(
        host="127.0.0.1",
        port=0,
        model_dir=tmp_path / "bundle",
        state_dir=tmp_path / "state",
        signal_dir=tmp_path / "signals",
        max_payloads=1,
        idle_timeout_seconds=0.03,
        expected_token=TOKEN,
        emit=lines.append,
    )

    assert result.termination_reason == "idle_timeout"
    assert result.summary == "SUMMARY accepted=0 ignored=0 not_ready=0 scored=0"
    assert len(lines) == 1
    assert lines[0].startswith("LISTENING host=127.0.0.1 port=")


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "example.test", ""])
def test_non_loopback_bind_is_rejected(host: str, tmp_path: Path) -> None:
    with pytest.raises(listener.DotaGSIListenerError, match="non_loopback_host"):
        listener.run_dota_gsi_live(
            host=host,
            port=3000,
            model_dir=tmp_path,
            state_dir=tmp_path,
            signal_dir=tmp_path,
            max_payloads=1,
            idle_timeout_seconds=1,
            expected_token=TOKEN,
            emit=lambda _line: None,
        )


def test_authenticated_payload_calls_adapter_directly_with_auth_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    _install_fake_adapter(monkeypatch, calls)

    def no_staging(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("raw payload staging is forbidden")

    monkeypatch.setattr(listener.live.tempfile, "mkstemp", no_staging)
    running = _ListenerThread(tmp_path)

    assert _post(running.port, {"map": {"clock_time": 1}}) == 200
    result = running.join()

    assert result.accepted == result.ignored == 1
    assert calls == [{"map": {"clock_time": 1}}]
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        (b"{}", 401, "authentication_failed"),
        (b'{"auth":{"token":"wrong"}}', 401, "authentication_failed"),
        (b"{bad-json", 400, "invalid_json"),
        (
            b'{"auth":{"token":"listener-test-token"},"map":{},"map":{}}',
            400,
            "duplicate_json_key",
        ),
        (b"\xff", 400, "invalid_utf8"),
    ],
)
def test_bad_http_payloads_do_not_stop_or_increment_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    status: int,
    reason: str,
) -> None:
    _install_fake_adapter(monkeypatch)
    running = _ListenerThread(tmp_path)

    assert _request(running.port, body=body) == status
    assert _post(running.port, {"map": {}}) == 200
    result = running.join()

    assert result.accepted == 1
    assert f"REJECTED reason={reason}" in running.lines


def test_body_limit_wrong_method_and_wrong_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_adapter(monkeypatch)
    running = _ListenerThread(tmp_path)
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=10)
    try:
        connection.putrequest("POST", "/")
        connection.putheader(
            "Content-Length", str(MAX_REQUEST_BODY_BYTES + 1)
        )
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        assert response.status == 413
    finally:
        connection.close()

    assert _request(running.port, method="GET") == 405
    assert _request(running.port, path="/other", body=b"{}") == 404
    assert _post(running.port, {}) == 200
    running.join()
    assert "REJECTED reason=body_too_large" in running.lines
    assert "REJECTED reason=wrong_method" in running.lines
    assert "REJECTED reason=wrong_path" in running.lines


def test_peer_connection_abort_is_request_local_but_handler_error_is_terminal(
    tmp_path: Path,
) -> None:
    server = listener._DotaGSILiveHTTPServer(
        ("127.0.0.1", 0),
        listener._DotaGSILiveRequestHandler,
        expected_token=TOKEN,
        model_dir=tmp_path / "bundle",
        state_dir=tmp_path / "state",
        signal_dir=tmp_path / "signals",
        max_payloads=1,
        emit=lambda _line: None,
    )
    with server, socket.socket() as request:
        try:
            raise ConnectionAbortedError
        except ConnectionAbortedError:
            server.handle_error(request, ("127.0.0.1", 1))
        assert server.fatal_error is None

        try:
            raise RuntimeError("handler failed")
        except RuntimeError:
            server.handle_error(request, ("127.0.0.1", 1))
        assert server.fatal_error is not None
        assert server.fatal_error.reason == "internal_failure"


def test_authenticated_invalid_gsi_payload_is_stable_and_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    running = _ListenerThread(tmp_path)

    assert _post(running.port, {"map": {}}) == 422
    assert _post(running.port, _spectator_payload(clock_time=1)) == 200
    result = running.join()

    assert result == listener.DotaGSIListenerResult(1, 1, 0, 0, "max_payloads")
    assert "REJECTED reason=invalid_gsi_payload" in running.lines


def test_end_to_end_ignored_not_ready_and_scored_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    running = _ListenerThread(tmp_path, max_payloads=6)

    assert _post(
        running.port,
        _spectator_payload(game_state="DOTA_GAMERULES_STATE_PRE_GAME", clock_time=-56),
    ) == 200
    assert _post(running.port, _spectator_payload(clock_time=1)) == 200
    for game_time in (0, 60, 120, 180):
        assert _post(running.port, _spectator_payload(clock_time=game_time)) == 200

    result = running.join()
    assert result == listener.DotaGSIListenerResult(6, 2, 3, 1, "max_payloads")
    assert running.lines[1:] == [
        "IGNORED match_id=123 game_time=-56 reason=game_not_in_progress",
        "STATE match_id=123 game_time=0 status=not_ready snapshots=1",
        "STATE match_id=123 game_time=60 status=not_ready snapshots=2",
        "STATE match_id=123 game_time=120 status=not_ready snapshots=3",
        "SIGNAL match_id=123 game_time=180 prediction=-123.45 "
        "direction=dire_improving",
    ]

    signal_path = tmp_path / "signals" / "123.json"
    response = json.loads(signal_path.read_bytes())
    assert response["status"] == "scored"
    assert response["prediction_net_worth_diff_change"] == -123.45
    history_payload = json.loads(
        (tmp_path / "state" / "123" / "history.json").read_bytes()
    )
    assert [item["game_time_seconds"] for item in history_payload["snapshots"]] == [
        0,
        60,
        120,
        180,
    ]
    persisted = signal_path.read_bytes() + json.dumps(history_payload).encode()
    for forbidden in (TOKEN, "steam-secret", "account-secret", "player-secret", "auth"):
        assert forbidden.encode() not in persisted
    assert not list(tmp_path.rglob(".dota-gsi-live-request.*"))


def test_ignored_null_match_id_creates_no_signal_file(tmp_path: Path) -> None:
    running = _ListenerThread(tmp_path)

    assert _post(
        running.port,
        _spectator_payload(
            game_state="DOTA_GAMERULES_STATE_PRE_GAME",
            clock_time=-20,
            matchid="unavailable",
        ),
    ) == 200
    result = running.join()

    assert result.ignored == 1
    assert not (tmp_path / "signals").exists()
    assert running.lines[1:] == []


def test_duplicate_boundary_is_idempotent_and_console_is_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    replacements: list[Path] = []
    real_replace = signal.os.replace

    def replace(source: Path, destination: Path) -> None:
        replacements.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(signal.os, "replace", replace)
    running = _ListenerThread(tmp_path, max_payloads=2)
    payload = _spectator_payload(clock_time=60)

    assert _post(running.port, payload) == 200
    assert _post(running.port, payload) == 200
    result = running.join()

    assert result.not_ready == 2
    assert running.lines.count(
        "STATE match_id=123 game_time=60 status=not_ready snapshots=1"
    ) == 1
    assert replacements == [
        tmp_path / "state" / "123" / "history.json",
        tmp_path / "signals" / "123.json",
    ]
    assert not list(tmp_path.rglob("*.transaction.*"))


def test_terminal_persistence_failure_preserves_committed_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = listener.live.ingest_dota_gsi_payload_in_memory
    calls = 0

    def ingest(*args: object, **kwargs: object) -> listener.live.InMemoryDotaGSILiveResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise signal.LiveStateSignalError("secret path must not escape")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(listener.live, "ingest_dota_gsi_payload_in_memory", ingest)
    running = _ListenerThread(tmp_path, max_payloads=2)

    assert _post(running.port, _spectator_payload(clock_time=1)) == 200
    assert _post(running.port, _spectator_payload(clock_time=2)) == 500
    error = running.join_error()

    assert isinstance(error, listener.DotaGSIListenerError)
    assert str(error) == "persistence_failed"
    committed = json.loads((tmp_path / "signals" / "123.json").read_bytes())
    assert committed["game_time_seconds"] == 1
    assert running.lines[-1] == "REJECTED reason=persistence_failed"
    assert "secret" not in "\n".join(running.lines)


def test_keyboard_interrupt_closes_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(_self: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(listener._DotaGSILiveHTTPServer, "handle_request", interrupt)
    result = listener.run_dota_gsi_live(
        host="127.0.0.1",
        port=0,
        model_dir=tmp_path,
        state_dir=tmp_path,
        signal_dir=tmp_path,
        max_payloads=1,
        idle_timeout_seconds=1,
        expected_token=TOKEN,
        emit=lambda _line: None,
    )

    assert result.termination_reason == "interrupted"
    assert result.accepted == 0


def test_no_outbound_connection_is_attempted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("outbound connection attempted")

    monkeypatch.setattr(socket, "create_connection", fail)
    result = listener.run_dota_gsi_live(
        host="127.0.0.1",
        port=0,
        model_dir=tmp_path,
        state_dir=tmp_path,
        signal_dir=tmp_path,
        max_payloads=1,
        idle_timeout_seconds=0.03,
        expected_token=TOKEN,
        emit=lambda _line: None,
    )
    assert result.accepted == 0


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            listener.DotaGSIListenerResult(4, 1, 2, 1, "max_payloads"),
            "SUMMARY accepted=4 ignored=1 not_ready=2 scored=1\nDONE\n",
        ),
        (
            listener.DotaGSIListenerResult(0, 0, 0, 0, "idle_timeout"),
            "SUMMARY accepted=0 ignored=0 not_ready=0 scored=0\nNO_PAYLOADS\n",
        ),
    ],
)
def test_cli_prints_concise_final_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: listener.DotaGSIListenerResult,
    expected: str,
) -> None:
    monkeypatch.setenv("DOTA_GSI_TOKEN", TOKEN)
    monkeypatch.setattr(listener, "run_dota_gsi_live", lambda **_kwargs: result)

    exit_code = cli.main(
        [
            "run-dota-gsi-live",
            "--model-dir",
            str(tmp_path / "bundle"),
            "--state-dir",
            str(tmp_path / "state"),
            "--signal-dir",
            str(tmp_path / "signals"),
            "--auth-token-env",
            "DOTA_GSI_TOKEN",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == expected
    assert captured.err == ""


def test_cli_requires_auth_token_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DOTA_GSI_TOKEN", raising=False)

    exit_code = cli.main(
        [
            "run-dota-gsi-live",
            "--model-dir",
            str(tmp_path),
            "--state-dir",
            str(tmp_path),
            "--signal-dir",
            str(tmp_path),
            "--auth-token-env",
            "DOTA_GSI_TOKEN",
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().err == "ERROR reason=auth_token_environment_missing\n"


def _spectator_payload(
    *,
    game_state: str = listener.live.IN_PROGRESS_STATE,
    clock_time: int = 1,
    matchid: object = 123,
) -> dict[str, Any]:
    players: dict[str, dict[str, dict[str, object]]] = {"team2": {}, "team3": {}}
    heroes: dict[str, dict[str, dict[str, object]]] = {"team2": {}, "team3": {}}
    for team, name, start in (("team2", "radiant", 0), ("team3", "dire", 5)):
        for offset in range(5):
            index = start + offset
            label = f"player{index}"
            players[team][label] = {
                "kills": index + 1,
                "deaths": index + 2,
                "assists": index + 3,
                "last_hits": index + 10,
                "denies": index + 1,
                "gold": index + 100,
                "net_worth": index + 1000,
                "team_name": name,
                "steamid": "steam-secret",
                "accountid": "account-secret",
                "name": "player-secret",
            }
            heroes[team][label] = {
                "xp": index + 500,
                "name": "player-secret",
            }
    return {
        "map": {
            "matchid": matchid,
            "clock_time": clock_time,
            "game_state": game_state,
        },
        "player": players,
        "hero": heroes,
    }
