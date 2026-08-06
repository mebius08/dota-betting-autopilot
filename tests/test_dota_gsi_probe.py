from __future__ import annotations

from collections.abc import Mapping
import http.client
import json
import os
from pathlib import Path
import socket
import threading
from typing import Any

import pytest

from app import cli
from app import dota_gsi_probe as gsi


TOKEN = "test-only-gsi-secret"


class _CaptureThread:
    def __init__(
        self,
        output_dir: Path,
        *,
        max_payloads: int = 1,
        idle_timeout_seconds: float = 2.0,
    ) -> None:
        self.port = _unused_port()
        self.lines: list[str] = []
        self.result: gsi.CaptureResult | None = None
        self.error: BaseException | None = None
        self._listening = threading.Event()

        def emit(line: str) -> None:
            self.lines.append(line)
            if line.startswith("LISTENING "):
                self._listening.set()

        def run() -> None:
            try:
                self.result = gsi.capture_dota_gsi_probe(
                    host="127.0.0.1",
                    port=self.port,
                    output_dir=output_dir,
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
        assert self._listening.wait(2), "probe did not start listening"
        if self.error is not None:
            raise self.error

    def join(self) -> gsi.CaptureResult:
        self._thread.join(3)
        assert not self._thread.is_alive(), "probe did not stop"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    port: int,
    *,
    method: str = "POST",
    path: str = "/",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _payload_bytes(
    token: str = TOKEN,
    **extra: object,
) -> bytes:
    payload = {"auth": {"token": token}, **extra}
    return json.dumps(payload).encode("utf-8")


def _post_valid(port: int, **extra: object) -> int:
    payload = {"auth": {"token": TOKEN}, **extra}
    return _request(port, body=json.dumps(payload).encode("utf-8"))


def _record(
    sequence: int,
    payload: dict[str, object],
    sha256: str | None = None,
) -> gsi.PayloadRecord:
    return gsi.PayloadRecord(
        sequence=sequence,
        filename=f"payloads/{sequence:06d}.json",
        sha256=sha256 or str(sequence) * 64,
        payload=payload,
    )


def test_loopback_hosts_are_accepted() -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        gsi.validate_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.4", "example.test", ""])
def test_non_loopback_hosts_are_rejected(host: str, tmp_path: Path) -> None:
    with pytest.raises(gsi.DotaGSIProbeError, match="non_loopback_host"):
        gsi.capture_dota_gsi_probe(
            host=host,
            port=3000,
            output_dir=tmp_path,
            max_payloads=1,
            idle_timeout_seconds=1,
            expected_token=TOKEN,
            emit=lambda _line: None,
        )


def test_valid_authenticated_post_is_persisted_and_audited(tmp_path: Path) -> None:
    capture = _CaptureThread(tmp_path)
    status = _post_valid(
        capture.port,
        map={"matchid": "20", "clock_time": 42},
        player={"kills": 3},
        hero={"experience": 900},
    )

    result = capture.join()

    assert status == 200
    assert result.status == "CAPTURED"
    assert capture.lines == [
        f"LISTENING host=127.0.0.1 port={capture.port}",
        "ACCEPTED sequence=1",
    ]
    persisted = json.loads((tmp_path / "payloads" / "000001.json").read_text())
    assert "auth" not in persisted
    audit = json.loads((tmp_path / "probe.json").read_text())
    assert audit["observed_match_ids"] == [20]
    assert audit["observed_game_times"] == [42]
    assert audit["field_presence_counts"]["player.kills"] == 1
    assert audit["field_presence_counts"]["hero.experience"] == 1


@pytest.mark.parametrize(
    ("body", "expected_status", "reason"),
    [
        (b'{"map":{}}', 401, "authentication_failed"),
        (_payload_bytes("wrong-token"), 401, "authentication_failed"),
        (b"{not-json", 400, "invalid_json"),
        (
            b'{"auth":{"token":"test-only-gsi-secret"},"map":{},"map":{}}',
            400,
            "duplicate_json_key",
        ),
        (b"[]", 400, "json_not_object"),
        (b"\xff", 400, "invalid_utf8"),
    ],
)
def test_invalid_posts_are_rejected_without_stopping_listener(
    tmp_path: Path,
    body: bytes,
    expected_status: int,
    reason: str,
) -> None:
    capture = _CaptureThread(tmp_path)

    assert _request(capture.port, body=body) == expected_status
    assert _post_valid(capture.port, map={}) == 200
    assert capture.join().payload_count == 1
    assert f"REJECTED reason={reason}" in capture.lines


def test_token_comparison_uses_constant_time_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return True

    monkeypatch.setattr(gsi.hmac, "compare_digest", compare)

    sanitized = gsi.validate_and_sanitize_payload(
        {"auth": {"token": "provided"}, "map": {}},
        "expected",
    )

    assert calls == [(b"provided", b"expected")]
    assert sanitized == {"map": {}}


def test_auth_token_is_never_persisted_or_printed(tmp_path: Path) -> None:
    capture = _CaptureThread(tmp_path)

    assert _request(capture.port, body=_payload_bytes("wrong-token")) == 401
    assert _post_valid(capture.port, player={"gold": 12}) == 200
    capture.join()

    all_bytes = b"".join(path.read_bytes() for path in tmp_path.rglob("*.*"))
    assert TOKEN.encode() not in all_bytes
    assert b"wrong-token" not in all_bytes
    assert all(TOKEN not in line and "wrong-token" not in line for line in capture.lines)


def test_body_larger_than_four_mib_is_rejected(tmp_path: Path) -> None:
    capture = _CaptureThread(tmp_path)
    connection = http.client.HTTPConnection("127.0.0.1", capture.port, timeout=2)
    try:
        connection.putrequest("POST", "/")
        connection.putheader("Content-Length", str(gsi.MAX_REQUEST_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        assert response.status == 413
    finally:
        connection.close()

    assert _post_valid(capture.port) == 200
    capture.join()
    assert "REJECTED reason=body_too_large" in capture.lines


def test_wrong_method_and_path_are_rejected(tmp_path: Path) -> None:
    capture = _CaptureThread(tmp_path)

    assert _request(capture.port, method="GET") == 405
    assert _request(capture.port, path="/other", body=_payload_bytes()) == 404
    assert _post_valid(capture.port) == 200
    capture.join()

    assert "REJECTED reason=wrong_method" in capture.lines
    assert "REJECTED reason=wrong_path" in capture.lines


def test_canonical_payload_serialization_is_sorted_and_newline_terminated() -> None:
    payload = {"z": 1, "a": {"d": 4, "c": "кириллица"}}

    content = gsi.canonical_payload_bytes(payload)

    assert content == (
        '{\n  "a": {\n    "c": "кириллица",\n    "d": 4\n  },\n  "z": 1\n}\n'
    ).encode("utf-8")


def test_payload_numbering_continues_deterministically(tmp_path: Path) -> None:
    first = gsi.persist_payload(tmp_path, {"map": {"game_time": 1}}, 1)
    second = gsi.persist_payload(tmp_path, {"map": {"game_time": 2}}, 2)

    assert first.filename == "payloads/000001.json"
    assert second.filename == "payloads/000002.json"
    assert [record.sequence for record in gsi.validate_existing_payloads(tmp_path)] == [
        1,
        2,
    ]


def test_capture_continues_after_existing_payload(tmp_path: Path) -> None:
    gsi.persist_payload(tmp_path, {"map": {"game_time": 1}}, 1)
    capture = _CaptureThread(tmp_path)

    assert _post_valid(capture.port, map={"game_time": 2}) == 200
    capture.join()

    assert (tmp_path / "payloads" / "000001.json").is_file()
    assert (tmp_path / "payloads" / "000002.json").is_file()
    assert "ACCEPTED sequence=2" in capture.lines


def test_payload_persistence_never_overwrites_existing_file(tmp_path: Path) -> None:
    original = gsi.persist_payload(tmp_path, {"value": "original"}, 1)
    original_bytes = (tmp_path / original.filename).read_bytes()

    with pytest.raises(gsi.DotaGSIProbeError, match="persistence_failed"):
        gsi.persist_payload(tmp_path, {"value": "replacement"}, 1)

    assert (tmp_path / original.filename).read_bytes() == original_bytes


def test_payload_is_published_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_link = os.link
    observations: list[tuple[bool, bool, bytes]] = []

    def observing_link(source: str | os.PathLike[str], destination: Any) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        observations.append(
            (source_path.is_file(), destination_path.exists(), source_path.read_bytes())
        )
        real_link(source, destination)

    monkeypatch.setattr(gsi.os, "link", observing_link)

    record = gsi.persist_payload(tmp_path, {"value": 1}, 1)

    final_bytes = (tmp_path / record.filename).read_bytes()
    assert observations == [(True, False, final_bytes)]
    assert not list((tmp_path / "payloads").glob("*.tmp"))


def test_existing_payload_numbering_gap_is_rejected(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    content = gsi.canonical_payload_bytes({"value": 1})
    (payload_dir / "000001.json").write_bytes(content)
    (payload_dir / "000003.json").write_bytes(content)

    with pytest.raises(gsi.DotaGSIProbeError, match="existing_payloads_invalid"):
        gsi.validate_existing_payloads(tmp_path)


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("000001.json", b"not-json"),
        ("000001.json", b'{"a":1,"a":2}\n'),
        ("000001.json", b"\xff"),
        ("000001.json", b'{"value":1}\n'),
        ("payload.json", b"{}\n"),
    ],
)
def test_malformed_or_unexpected_existing_payload_is_rejected(
    tmp_path: Path,
    filename: str,
    content: bytes,
) -> None:
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    (payload_dir / filename).write_bytes(content)

    with pytest.raises(gsi.DotaGSIProbeError, match="existing_payloads_invalid"):
        gsi.validate_existing_payloads(tmp_path)


def test_existing_payload_with_auth_field_is_rejected(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir()
    content = gsi.canonical_payload_bytes({"auth": {}, "map": {}})
    (payload_dir / "000001.json").write_bytes(content)

    with pytest.raises(gsi.DotaGSIProbeError, match="existing_payloads_invalid"):
        gsi.validate_existing_payloads(tmp_path)


def test_structural_audit_is_deterministic_sorted_and_recursive() -> None:
    first = _record(
        1,
        {
            "z": {},
            "map": {"matchid": "10", "clock_time": 10},
            "player": {"kills": 1, "deaths": 2, "gold": 3},
            "hero": {"experience": 4},
        },
        "a" * 64,
    )
    second = _record(
        2,
        {
            "a": {},
            "map": {"match_id": 2, "game_time": 2.5},
            "player": {
                "team2": {
                    "player0": {
                        "assists": 1,
                        "last_hits": 2,
                        "denies": 3,
                        "net_worth": 4,
                    }
                }
            },
            "hero": {"team2": {"player0": {"experience": 5}}},
        },
        "b" * 64,
    )

    audit = gsi.build_structural_audit([second, first])

    assert audit == gsi.build_structural_audit([first, second])
    assert audit["payload_files"] == [
        {"filename": "payloads/000001.json", "sha256": "a" * 64},
        {"filename": "payloads/000002.json", "sha256": "b" * 64},
    ]
    assert audit["top_level_keys"] == ["a", "hero", "map", "player", "z"]
    assert audit["map_keys"] == ["clock_time", "game_time", "match_id", "matchid"]
    assert audit["player_keys"] == ["deaths", "gold", "kills", "team2"]
    assert audit["hero_keys"] == ["experience", "team2"]
    assert audit["observed_match_ids"] == [2, 10]
    assert audit["observed_game_times"] == [2.5, 10]
    assert audit["shape_counts"] == {
        "player_single_object": 1,
        "player_team_mapping": 1,
        "hero_single_object": 1,
        "hero_team_mapping": 1,
    }
    assert audit["field_presence_counts"] == {
        "map.matchid": 1,
        "map.match_id": 1,
        "map.clock_time": 1,
        "map.game_time": 1,
        "player.kills": 1,
        "player.deaths": 1,
        "player.assists": 1,
        "player.last_hits": 1,
        "player.denies": 1,
        "player.gold": 1,
        "player.net_worth": 1,
        "hero.experience": 2,
    }


def test_audit_is_rebuilt_from_existing_payload_files(tmp_path: Path) -> None:
    record = gsi.persist_payload(
        tmp_path,
        {"map": {"matchid": "44", "game_time": 8}},
        1,
    )
    (tmp_path / "probe.json").write_text('{"stale":true}\n', encoding="utf-8")

    result = gsi.capture_dota_gsi_probe(
        host="127.0.0.1",
        port=0,
        output_dir=tmp_path,
        max_payloads=1,
        idle_timeout_seconds=0.03,
        expected_token=TOKEN,
        emit=lambda _line: None,
    )

    assert result.status == "NO_PAYLOADS"
    audit_bytes = (tmp_path / "probe.json").read_bytes()
    audit = json.loads(audit_bytes)
    assert audit["payload_count"] == 1
    assert audit["payload_files"] == [
        {"filename": record.filename, "sha256": record.sha256}
    ]
    assert audit["observed_match_ids"] == [44]
    assert audit_bytes == gsi.canonical_payload_bytes(audit)


def test_capture_exits_at_bounded_maximum(tmp_path: Path) -> None:
    capture = _CaptureThread(tmp_path, max_payloads=2)

    assert _post_valid(capture.port, map={"game_time": 1}) == 200
    assert _post_valid(capture.port, map={"game_time": 2}) == 200

    result = capture.join()
    assert result.payload_count == 2
    assert result.status == "CAPTURED"


def test_capture_exits_at_idle_timeout_without_payload(tmp_path: Path) -> None:
    lines: list[str] = []

    result = gsi.capture_dota_gsi_probe(
        host="127.0.0.1",
        port=0,
        output_dir=tmp_path,
        max_payloads=1,
        idle_timeout_seconds=0.03,
        expected_token=TOKEN,
        emit=lines.append,
    )

    assert result.status == "NO_PAYLOADS"
    assert len(lines) == 1
    assert lines[0].startswith("LISTENING host=127.0.0.1 port=")


def test_capture_makes_no_outbound_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_outbound(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected outbound connection")

    monkeypatch.setattr(socket, "create_connection", fail_outbound)

    result = gsi.capture_dota_gsi_probe(
        host="127.0.0.1",
        port=0,
        output_dir=tmp_path,
        max_payloads=1,
        idle_timeout_seconds=0.03,
        expected_token=TOKEN,
        emit=lambda _line: None,
    )

    assert result.status == "NO_PAYLOADS"


@pytest.mark.parametrize(
    ("result", "final_line"),
    [
        (gsi.CaptureResult(payload_count=1), "CAPTURED"),
        (gsi.CaptureResult(payload_count=0), "NO_PAYLOADS"),
    ],
)
def test_cli_prints_final_capture_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: gsi.CaptureResult,
    final_line: str,
) -> None:
    def capture(**_kwargs: object) -> gsi.CaptureResult:
        return result

    monkeypatch.setattr(gsi, "capture_dota_gsi_probe", capture)
    monkeypatch.setenv("DOTA_GSI_TOKEN", TOKEN)

    exit_code = cli.main(
        [
            "capture-dota-gsi-probe",
            "--output-dir",
            str(tmp_path),
            "--auth-token-env",
            "DOTA_GSI_TOKEN",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"{final_line}\n"
    assert captured.err == ""


def test_cli_rejects_missing_or_empty_auth_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DOTA_GSI_TOKEN", raising=False)

    exit_code = cli.main(
        [
            "capture-dota-gsi-probe",
            "--output-dir",
            str(tmp_path),
            "--auth-token-env",
            "DOTA_GSI_TOKEN",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "ERROR reason=auth_token_environment_missing\n"


def test_duplicate_json_decoder_checks_nested_objects() -> None:
    with pytest.raises(gsi.DotaGSIProbeError, match="duplicate_json_key"):
        gsi.decode_json_object(b'{"outer":{"value":1,"value":2}}')
