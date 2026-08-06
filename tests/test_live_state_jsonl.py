from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app import live_state_jsonl as bridge
from app import live_state_signal as signal
from app import replay_state_history as history
from app import replay_state_model as model
from app.cli import main


class _FakeModel:
    def __init__(self, prediction: float = 12.5) -> None:
        self.prediction = prediction

    def predict(self, data: Sequence[Sequence[float]]) -> list[float]:
        return [self.prediction for _ in data]


def test_first_run_processes_complete_records_in_physical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    records = [
        _request_payload(match_id=123, game_time=120),
        _request_payload(match_id=123, game_time=60),
        _request_payload(match_id=456, game_time=30),
    ]
    _write_jsonl(paths["input"], records)
    calls: list[tuple[int, int]] = []
    real_ingest = signal.ingest_live_state_snapshot

    def ingest_spy(
        model_dir: str | Path,
        state_dir: str | Path,
        input_path: str | Path,
        output_path: str | Path,
    ) -> signal.LiveStateSignalResult:
        payload = json.loads(Path(input_path).read_bytes())
        calls.append((payload["match_id"], payload["snapshot"]["game_time_seconds"]))
        return real_ingest(model_dir, state_dir, input_path, output_path)

    monkeypatch.setattr(bridge.signal, "ingest_live_state_snapshot", ingest_spy)

    result = _sync(paths)

    input_bytes = paths["input"].read_bytes()
    cursor = _read_json(paths["cursor"])
    summary = _read_json(paths["output"])
    assert result.status == "BUILT"
    assert calls == [(123, 120), (123, 60), (456, 30)]
    assert cursor == {
        "schema_version": 1,
        "input_filename": "events.jsonl",
        "byte_offset": len(input_bytes),
        "processed_line_count": 3,
        "processed_prefix_sha256": hashlib.sha256(input_bytes).hexdigest(),
    }
    assert summary["status"] == "processed"
    assert summary["start_line"] == 1
    assert summary["end_line"] == 3
    assert summary["processed_event_count"] == 3
    assert summary["changed_event_count"] == 3
    assert summary["not_ready_event_count"] == 3
    assert (paths["signals"] / "123.json").is_file()
    assert (paths["signals"] / "456.json").is_file()


def test_second_run_without_new_bytes_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(paths["input"], [_request_payload(123, 60)])

    assert _sync(paths).status == "BUILT"
    summary_bytes = paths["output"].read_bytes()
    cursor_bytes = paths["cursor"].read_bytes()
    assert _sync(paths).status == "UNCHANGED"
    assert paths["output"].read_bytes() == summary_bytes
    assert paths["cursor"].read_bytes() == cursor_bytes


def test_cli_prints_built_then_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(paths["input"], [_request_payload(123, 60)])
    arguments = _cli_arguments(paths)

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"


def test_appended_complete_record_processes_only_new_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    first = _jsonl_line(_request_payload(123, 60))
    second = _jsonl_line(_request_payload(456, 120))
    paths["input"].write_bytes(first)
    _sync(paths)
    first_history = (paths["state"] / "123" / "history.json").read_bytes()

    with paths["input"].open("ab") as file:
        file.write(second)
    _sync(paths)

    summary = _read_json(paths["output"])
    cursor = _read_json(paths["cursor"])
    assert summary["start_line"] == 2
    assert summary["end_line"] == 2
    assert summary["processed_event_count"] == 1
    assert (paths["state"] / "123" / "history.json").read_bytes() == first_history
    assert (paths["state"] / "456" / "history.json").is_file()
    assert cursor["byte_offset"] == len(first + second)
    assert cursor["processed_line_count"] == 2


def test_partial_record_waits_then_processes_once_when_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    complete_line = _jsonl_line(_request_payload(123, 60))
    split_at = len(complete_line) // 2
    paths["input"].write_bytes(complete_line[:split_at])
    calls = 0
    real_ingest = signal.ingest_live_state_snapshot

    def ingest_spy(*args: object, **kwargs: object) -> signal.LiveStateSignalResult:
        nonlocal calls
        calls += 1
        return real_ingest(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bridge.signal, "ingest_live_state_snapshot", ingest_spy)

    assert _sync(paths).status == "BUILT"
    waiting = _read_json(paths["output"])
    cursor_before = _read_json(paths["cursor"])
    assert waiting["status"] == "waiting_for_complete_record"
    assert waiting["processed_event_count"] == 0
    assert waiting["pending_partial_record"] is True
    assert cursor_before["byte_offset"] == 0
    assert calls == 0

    with paths["input"].open("ab") as file:
        file.write(complete_line[split_at:])
    _sync(paths)

    assert calls == 1
    assert _read_json(paths["output"])["processed_event_count"] == 1
    assert _read_json(paths["cursor"])["byte_offset"] == len(complete_line)


def test_complete_records_are_processed_before_pending_partial_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    complete = _jsonl_line(_request_payload(123, 60))
    partial = _jsonl_line(_request_payload(456, 60))[:-1]
    paths["input"].write_bytes(complete + partial)

    _sync(paths)

    summary = _read_json(paths["output"])
    assert summary["status"] == "processed"
    assert summary["processed_event_count"] == 1
    assert summary["pending_partial_record"] is True
    assert _read_json(paths["cursor"])["byte_offset"] == len(complete)
    assert not (paths["state"] / "456").exists()


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (b"\n", "Blank JSONL record at physical line 1"),
        (b"{bad json\n", "Malformed JSON at physical line 1"),
        (b"[]\n", "physical line 1 must be an object"),
        (b"123\n", "physical line 1 must be an object"),
        (b"\xff\n", "Invalid UTF-8 at physical line 1"),
    ],
)
def test_invalid_complete_line_is_rejected_without_cursor_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    line: bytes,
    message: str,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    paths["input"].write_bytes(line)

    with pytest.raises(bridge.LiveStateJsonlError, match=message):
        _sync(paths)

    assert not paths["cursor"].exists()
    assert not paths["state"].exists()


def test_failure_stops_before_later_lines_and_keeps_earlier_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    first = _jsonl_line(_request_payload(123, 60))
    failed = b"{malformed\n"
    later = _jsonl_line(_request_payload(456, 60))
    paths["input"].write_bytes(first + failed + later)

    with pytest.raises(bridge.LiveStateJsonlError, match="physical line 2"):
        _sync(paths)

    cursor = _read_json(paths["cursor"])
    assert cursor["byte_offset"] == len(first)
    assert cursor["processed_line_count"] == 1
    assert cursor["processed_prefix_sha256"] == hashlib.sha256(first).hexdigest()
    assert (paths["state"] / "123" / "history.json").is_file()
    assert not (paths["state"] / "456").exists()


def test_request_uses_existing_validation_and_ingestion_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    invalid = _request_payload(123, 60)
    invalid["provider"] = "forbidden"
    paths["input"].write_bytes(_jsonl_line(invalid))
    validation_calls = 0
    ingestion_calls = 0
    real_validate = signal.validate_live_state_snapshot_request
    real_ingest = signal.ingest_live_state_snapshot

    def validate_spy(payload: object) -> signal.LiveStateSnapshotRequest:
        nonlocal validation_calls
        validation_calls += 1
        return real_validate(payload)

    def ingest_spy(*args: object) -> signal.LiveStateSignalResult:
        nonlocal ingestion_calls
        ingestion_calls += 1
        raise AssertionError("ingestion must not run after validation failure")

    monkeypatch.setattr(bridge.signal, "validate_live_state_snapshot_request", validate_spy)
    monkeypatch.setattr(bridge.signal, "ingest_live_state_snapshot", ingest_spy)

    with pytest.raises(bridge.LiveStateJsonlError, match="fields do not match"):
        _sync(paths)
    assert validation_calls == 1
    assert ingestion_calls == 0

    valid_paths = _paths(tmp_path / "valid")
    _write_jsonl(valid_paths["input"], [_request_payload(123, 60)])
    monkeypatch.setattr(bridge.signal, "validate_live_state_snapshot_request", real_validate)

    def valid_ingest_spy(
        *args: object, **kwargs: object
    ) -> signal.LiveStateSignalResult:
        nonlocal ingestion_calls
        ingestion_calls += 1
        return real_ingest(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bridge.signal, "ingest_live_state_snapshot", valid_ingest_spy)
    _sync(valid_paths)
    assert ingestion_calls == 1


def test_duplicate_line_counts_unchanged_but_advances_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    line = _jsonl_line(_request_payload(123, 60))
    paths["input"].write_bytes(line)
    _sync(paths)

    with paths["input"].open("ab") as file:
        file.write(line)
    _sync(paths)

    summary = _read_json(paths["output"])
    cursor = _read_json(paths["cursor"])
    assert summary["processed_event_count"] == 1
    assert summary["changed_event_count"] == 0
    assert summary["unchanged_event_count"] == 1
    assert summary["not_ready_event_count"] == 1
    assert cursor["byte_offset"] == len(line) * 2
    assert cursor["processed_line_count"] == 2


def test_scored_and_not_ready_summary_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(
        paths["input"],
        [
            _request_payload(123, 360),
            _request_payload(123, 180),
            _request_payload(123, 300),
            _request_payload(123, 240),
        ],
    )

    _sync(paths)

    summary = _read_json(paths["output"])
    assert summary["processed_event_count"] == 4
    assert summary["changed_event_count"] == 4
    assert summary["unchanged_event_count"] == 0
    assert summary["scored_event_count"] == 1
    assert summary["not_ready_event_count"] == 3
    assert summary["processed_event_count"] == (
        summary["changed_event_count"] + summary["unchanged_event_count"]
    )
    assert summary["processed_event_count"] == (
        summary["scored_event_count"] + summary["not_ready_event_count"]
    )
    assert _read_json(paths["signals"] / "123.json")["status"] == "scored"


def test_match_histories_and_current_signals_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(
        paths["input"],
        [_request_payload(123, 60), _request_payload(456, 120)],
    )

    _sync(paths)

    first_history = _read_json(paths["state"] / "123" / "history.json")
    second_history = _read_json(paths["state"] / "456" / "history.json")
    first_signal = _read_json(paths["signals"] / "123.json")
    second_signal = _read_json(paths["signals"] / "456.json")
    assert first_history["match_id"] == first_signal["match_id"] == 123
    assert second_history["match_id"] == second_signal["match_id"] == 456
    assert [item["game_time_seconds"] for item in first_history["snapshots"]] == [60]
    assert [item["game_time_seconds"] for item in second_history["snapshots"]] == [120]


def test_input_truncation_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    line = _jsonl_line(_request_payload(123, 60))
    paths["input"].write_bytes(line)
    _sync(paths)
    paths["input"].write_bytes(line[:-1])

    with pytest.raises(bridge.LiveStateJsonlError, match="truncated"):
        _sync(paths)


def test_processed_prefix_mutation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    line = _jsonl_line(_request_payload(123, 60))
    paths["input"].write_bytes(line)
    _sync(paths)
    mutated = bytearray(line)
    mutated[0] = ord("[")
    paths["input"].write_bytes(mutated)

    with pytest.raises(bridge.LiveStateJsonlError, match="were modified"):
        _sync(paths)


def test_cursor_input_basename_mismatch_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(paths["input"], [_request_payload(123, 60)])
    _sync(paths)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(paths["input"].read_bytes())

    with pytest.raises(bridge.LiveStateJsonlError, match="filename does not match"):
        bridge.sync_live_state_jsonl(
            paths["model"],
            paths["state"],
            replacement,
            paths["signals"],
            paths["cursor"],
            paths["output"],
        )


@pytest.mark.parametrize(
    "cursor_bytes",
    [
        b"{bad json",
        b"[]\n",
        b'{"schema_version":1}\n',
        (
            b'{"schema_version":1,"input_filename":"events.jsonl",'
            b'"byte_offset":true,"processed_line_count":0,'
            b'"processed_prefix_sha256":"'
            + (b"0" * 64)
            + b'"}\n'
        ),
    ],
)
def test_corrupted_cursor_is_rejected(
    tmp_path: Path,
    cursor_bytes: bytes,
) -> None:
    paths = _paths(tmp_path)
    paths["input"].write_bytes(b"")
    paths["cursor"].write_bytes(cursor_bytes)

    with pytest.raises(bridge.LiveStateJsonlError, match="cursor is corrupted"):
        _sync(paths)


def test_cursor_rejects_offset_not_after_newline_and_wrong_line_count(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths["input"].write_bytes(b"abc\n")
    for offset, count, expected in (
        (3, 0, "does not follow"),
        (4, 0, "line count does not match"),
    ):
        prefix = paths["input"].read_bytes()[:offset]
        _write_json(
            paths["cursor"],
            {
                "schema_version": 1,
                "input_filename": "events.jsonl",
                "byte_offset": offset,
                "processed_line_count": count,
                "processed_prefix_sha256": hashlib.sha256(prefix).hexdigest(),
            },
        )
        with pytest.raises(bridge.LiveStateJsonlError, match=expected):
            _sync(paths)


def test_summary_and_cursor_field_order_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(paths["input"], [_request_payload(123, 60)])
    _sync(paths)

    cursor = _read_json(paths["cursor"])
    summary = _read_json(paths["output"])
    assert list(cursor) == [
        "schema_version",
        "input_filename",
        "byte_offset",
        "processed_line_count",
        "processed_prefix_sha256",
    ]
    assert list(summary) == [
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
    ]
    assert list(summary["cursor"]) == [
        "byte_offset",
        "processed_line_count",
        "processed_prefix_sha256",
    ]
    assert paths["cursor"].read_bytes().endswith(b"\n")
    assert paths["output"].read_bytes().endswith(b"\n")


def test_cursor_and_summary_are_written_by_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(paths["input"], [_request_payload(123, 60)])
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def replace_spy(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.exists()
        assert source_path.parent == destination_path.parent
        replacements.append((source_path, destination_path))
        real_replace(source_path, destination_path)

    monkeypatch.setattr(bridge.os, "replace", replace_spy)
    _sync(paths)

    destinations = [destination for _, destination in replacements]
    assert paths["cursor"] in destinations
    assert paths["output"] in destinations
    assert not list(tmp_path.rglob("*.transaction.*"))


def test_missing_summary_is_rebuilt_without_moving_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(paths["input"], [_request_payload(123, 60)])
    _sync(paths)
    cursor_bytes = paths["cursor"].read_bytes()
    paths["output"].unlink()

    assert _sync(paths).status == "BUILT"
    summary = _read_json(paths["output"])
    assert summary["status"] == "idle"
    assert summary["start_line"] is None
    assert summary["end_line"] is None
    assert summary["processed_event_count"] == 0
    assert paths["cursor"].read_bytes() == cursor_bytes
    assert _sync(paths).status == "UNCHANGED"


def test_empty_input_builds_idle_cursor_and_summary(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths["input"].write_bytes(b"")

    assert _sync(paths).status == "BUILT"
    summary = _read_json(paths["output"])
    cursor = _read_json(paths["cursor"])
    assert summary["status"] == "idle"
    assert summary["pending_partial_record"] is False
    assert cursor["byte_offset"] == 0
    assert cursor["processed_line_count"] == 0
    assert cursor["processed_prefix_sha256"] == hashlib.sha256(b"").hexdigest()


def test_outputs_do_not_emit_provider_betting_or_target_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    paths = _paths(tmp_path)
    _write_jsonl(paths["input"], [_request_payload(123, 60)])
    _sync(paths)

    artifacts = [
        paths["state"] / "123" / "history.json",
        paths["signals"] / "123.json",
        paths["cursor"],
        paths["output"],
    ]
    forbidden = (b"provider", b"target_", b"odds", b"stake", b"cashout", b"betting")
    for artifact in artifacts:
        content = artifact.read_bytes().lower()
        assert all(field not in content for field in forbidden)


def test_cli_error_has_physical_line_without_environment_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    paths["input"].write_bytes(b"{bad\n")

    assert main(_cli_arguments(paths)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "physical line 1" in captured.err
    assert str(paths["model"]) not in captured.err
    assert str(tmp_path) not in captured.err


def _install_fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    scorer = model.ReplayStateModelScorer(
        feature_columns=history.SUPPORTED_MODEL_FEATURE_COLUMNS,
        manifest_sha256="1" * 64,
        _model=_FakeModel(),
    )
    monkeypatch.setattr(signal.model, "load_replay_state_model", lambda path: scorer)


def _paths(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    return {
        "model": root / "model",
        "state": root / "state",
        "input": root / "events.jsonl",
        "signals": root / "signals",
        "cursor": root / "cursor.json",
        "output": root / "summary.json",
    }


def _sync(paths: dict[str, Path]) -> bridge.LiveStateJsonlSyncResult:
    return bridge.sync_live_state_jsonl(
        paths["model"],
        paths["state"],
        paths["input"],
        paths["signals"],
        paths["cursor"],
        paths["output"],
    )


def _cli_arguments(paths: dict[str, Path]) -> list[str]:
    return [
        "sync-live-state-jsonl",
        "--model-dir",
        str(paths["model"]),
        "--state-dir",
        str(paths["state"]),
        "--input",
        str(paths["input"]),
        "--signal-dir",
        str(paths["signals"]),
        "--cursor",
        str(paths["cursor"]),
        "--output",
        str(paths["output"]),
    ]


def _request_payload(match_id: int, game_time: int) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"game_time_seconds": game_time}
    snapshot.update(
        {
            column: game_time + index + 1
            for index, column in enumerate(history.CURRENT_FEATURE_COLUMNS)
        }
    )
    return {"schema_version": 1, "match_id": match_id, "snapshot": snapshot}


def _jsonl_line(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_jsonl(path: Path, payloads: Sequence[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_jsonl_line(payload) for payload in payloads))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_bytes())
    assert isinstance(payload, dict)
    return payload
