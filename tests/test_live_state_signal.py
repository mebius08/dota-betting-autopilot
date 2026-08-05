from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app import live_state_signal as signal
from app import replay_state_history as history
from app import replay_state_model as model
from app.cli import main


class _FakeModel:
    def __init__(self, prediction: float = 123.45) -> None:
        self.prediction = prediction
        self.rows: list[list[float]] = []

    def predict(self, data: Sequence[Sequence[float]]) -> list[float]:
        self.rows.extend(list(map(list, data)))
        return [self.prediction for _ in data]


def test_new_match_history_and_not_ready_response_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    state_dir = tmp_path / "state"
    output_path = tmp_path / "signal.json"
    _write_json(request_path, _request_payload(match_id=123, game_time=360))

    result = signal.ingest_live_state_snapshot(
        tmp_path / "bundle",
        state_dir,
        request_path,
        output_path,
    )

    history_path = state_dir / "123" / "history.json"
    history_bytes = history_path.read_bytes()
    persisted = json.loads(history_bytes)
    assert result.status == "BUILT"
    assert result.response_status == "not_ready"
    assert result.history_path == history_path
    assert list(persisted) == ["schema_version", "match_id", "snapshots"]
    assert persisted["match_id"] == 123
    assert len(persisted["snapshots"]) == 1
    assert list(persisted["snapshots"][0]) == [
        "game_time_seconds",
        *history.CURRENT_FEATURE_COLUMNS,
    ]
    response = json.loads(output_path.read_bytes())
    assert list(response) == [
        "schema_version",
        "status",
        "match_id",
        "game_time_seconds",
        "snapshot_count",
        "required_lags_seconds",
        "missing_lags_seconds",
        "history_file_sha256",
    ]
    assert response == {
        "schema_version": 1,
        "status": "not_ready",
        "match_id": 123,
        "game_time_seconds": 360,
        "snapshot_count": 1,
        "required_lags_seconds": [60, 120, 180],
        "missing_lags_seconds": [60, 120, 180],
        "history_file_sha256": hashlib.sha256(history_bytes).hexdigest(),
    }
    assert fake_model.rows == []
    assert b"target_" not in history_bytes
    assert b"target_" not in output_path.read_bytes()


def test_out_of_order_exact_history_scores_latest_persisted_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = _install_fake_model(monkeypatch, prediction=-4.0)
    original_score = history.score_replay_state_history_request
    score_calls: list[tuple[int | float, ...]] = []

    def score_spy(
        request: history.ReplayStateHistoryRequest,
        scorer: model.ReplayStateModelScorer,
    ) -> dict[str, object]:
        score_calls.append(
            tuple(snapshot.game_time_seconds for snapshot in request.snapshots)
        )
        return original_score(request, scorer)

    monkeypatch.setattr(
        signal.history,
        "score_replay_state_history_request",
        score_spy,
    )
    request_path = tmp_path / "request.json"
    state_dir = tmp_path / "state"
    output_path = tmp_path / "signal.json"
    response_statuses: list[str] = []
    for game_time in (360, 180, 300, 240):
        _write_json(request_path, _request_payload(match_id=123, game_time=game_time))
        signal.ingest_live_state_snapshot(
            tmp_path / "bundle",
            state_dir,
            request_path,
            output_path,
        )
        response_statuses.append(json.loads(output_path.read_bytes())["status"])

    assert response_statuses == ["not_ready", "not_ready", "not_ready", "scored"]
    persisted = json.loads((state_dir / "123" / "history.json").read_bytes())
    assert [item["game_time_seconds"] for item in persisted["snapshots"]] == [
        180,
        240,
        300,
        360,
    ]
    assert score_calls == [
        (360,),
        (180, 360),
        (180, 300, 360),
        (180, 240, 300, 360),
    ]
    response = json.loads(output_path.read_bytes())
    assert list(response) == [
        "schema_version",
        "status",
        "model_manifest_sha256",
        "match_id",
        "game_time_seconds",
        "snapshot_count",
        "horizon_seconds",
        "prediction_net_worth_diff_change",
        "predicted_direction",
        "history_file_sha256",
    ]
    assert response["game_time_seconds"] == 360
    assert response["snapshot_count"] == 4
    assert response["horizon_seconds"] == 180
    assert response["prediction_net_worth_diff_change"] == -4.0
    assert response["predicted_direction"] == "dire_improving"
    assert len(fake_model.rows) == 1


def test_no_nearest_timestamp_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model = _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "signal.json"
    for game_time in (360, 181, 241, 299):
        _write_json(request_path, _request_payload(match_id=123, game_time=game_time))
        signal.ingest_live_state_snapshot(
            tmp_path / "bundle",
            tmp_path / "state",
            request_path,
            output_path,
        )

    response = json.loads(output_path.read_bytes())
    assert response["game_time_seconds"] == 360
    assert response["missing_lags_seconds"] == [60, 120, 180]
    assert fake_model.rows == []


def test_conflicting_duplicate_is_rejected_without_replacing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    state_dir = tmp_path / "state"
    output_path = tmp_path / "signal.json"
    request = _request_payload(match_id=123, game_time=60)
    _write_json(request_path, request)
    signal.ingest_live_state_snapshot(
        tmp_path / "bundle", state_dir, request_path, output_path
    )
    history_path = state_dir / "123" / "history.json"
    original_history = history_path.read_bytes()
    original_response = output_path.read_bytes()
    request["snapshot"]["kill_diff"] = 999
    _write_json(request_path, request)

    with pytest.raises(signal.LiveStateSignalError, match="Conflicting snapshot"):
        signal.ingest_live_state_snapshot(
            tmp_path / "bundle", state_dir, request_path, output_path
        )

    assert history_path.read_bytes() == original_history
    assert output_path.read_bytes() == original_response


@pytest.mark.parametrize("replacement", [None, b"{corrupted response"])
def test_identical_duplicate_repairs_missing_or_corrupted_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes | None,
) -> None:
    _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    state_dir = tmp_path / "state"
    output_path = tmp_path / "signal.json"
    _write_json(request_path, _request_payload(match_id=123, game_time=60))
    first = signal.ingest_live_state_snapshot(
        tmp_path / "bundle", state_dir, request_path, output_path
    )
    history_path = state_dir / "123" / "history.json"
    history_bytes = history_path.read_bytes()
    expected_response = output_path.read_bytes()
    if replacement is None:
        output_path.unlink()
    else:
        output_path.write_bytes(replacement)

    repaired = signal.ingest_live_state_snapshot(
        tmp_path / "bundle", state_dir, request_path, output_path
    )
    unchanged = signal.ingest_live_state_snapshot(
        tmp_path / "bundle", state_dir, request_path, output_path
    )

    assert first.status == "BUILT"
    assert repaired.status == "BUILT"
    assert unchanged.status == "UNCHANGED"
    assert history_path.read_bytes() == history_bytes
    assert output_path.read_bytes() == expected_response


def test_cli_prints_built_then_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "signal.json"
    _write_json(request_path, _request_payload(match_id=123, game_time=60))
    arguments = [
        "ingest-live-state-snapshot",
        "--model-dir",
        str(tmp_path / "bundle"),
        "--state-dir",
        str(tmp_path / "state"),
        "--input",
        str(request_path),
        "--output",
        str(output_path),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"


def test_match_histories_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    state_dir = tmp_path / "state"
    for match_id, game_time in ((123, 60), (456, 120)):
        _write_json(
            request_path,
            _request_payload(match_id=match_id, game_time=game_time),
        )
        signal.ingest_live_state_snapshot(
            tmp_path / "bundle",
            state_dir,
            request_path,
            tmp_path / f"signal-{match_id}.json",
        )

    first = json.loads((state_dir / "123" / "history.json").read_bytes())
    second = json.loads((state_dir / "456" / "history.json").read_bytes())
    assert first["match_id"] == 123
    assert [item["game_time_seconds"] for item in first["snapshots"]] == [60]
    assert second["match_id"] == 456
    assert [item["game_time_seconds"] for item in second["snapshots"]] == [120]


def test_insertion_order_does_not_change_persisted_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    for label, times in (("forward", (60, 120)), ("reverse", (120, 60))):
        for game_time in times:
            _write_json(
                request_path,
                _request_payload(match_id=123, game_time=game_time),
            )
            signal.ingest_live_state_snapshot(
                tmp_path / "bundle",
                tmp_path / label,
                request_path,
                tmp_path / f"{label}.json",
            )

    assert (tmp_path / "forward" / "123" / "history.json").read_bytes() == (
        tmp_path / "reverse" / "123" / "history.json"
    ).read_bytes()
    assert (tmp_path / "forward.json").read_bytes() == (
        tmp_path / "reverse.json"
    ).read_bytes()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("schema_bool", "schema_version"),
        ("schema_wrong", "schema_version"),
        ("match_bool", "match_id"),
        ("match_zero", "match_id"),
        ("missing_request", "fields"),
        ("unexpected_request", "fields"),
        ("missing_snapshot", "Missing"),
        ("unexpected_snapshot", "Unexpected"),
        ("target", "Target and lineage"),
        ("lineage", "Target and lineage"),
        ("numeric_bool", "numeric"),
        ("numeric_string", "numeric"),
        ("non_finite", "finite"),
        ("negative_time", "non-negative"),
    ],
)
def test_malformed_requests_are_rejected(case: str, message: str) -> None:
    payload = _request_payload(match_id=123, game_time=60)
    if case == "schema_bool":
        payload["schema_version"] = True
    elif case == "schema_wrong":
        payload["schema_version"] = 2
    elif case == "match_bool":
        payload["match_id"] = True
    elif case == "match_zero":
        payload["match_id"] = 0
    elif case == "missing_request":
        del payload["snapshot"]
    elif case == "unexpected_request":
        payload["provider"] = "example"
    elif case == "missing_snapshot":
        del payload["snapshot"]["kill_diff"]
    elif case == "unexpected_snapshot":
        payload["snapshot"]["provider_value"] = 1
    elif case == "target":
        payload["snapshot"]["target_net_worth_diff_change_180"] = 1
    elif case == "lineage":
        payload["snapshot"]["league_id"] = 1
    elif case == "numeric_bool":
        payload["snapshot"]["kill_diff"] = True
    elif case == "numeric_string":
        payload["snapshot"]["total_xp"] = "1"
    elif case == "non_finite":
        payload["snapshot"]["total_xp"] = float("nan")
    elif case == "negative_time":
        payload["snapshot"]["game_time_seconds"] = -1

    with pytest.raises(signal.LiveStateSignalError, match=message):
        signal.validate_live_state_snapshot_request(payload)


def test_corrupted_persisted_history_is_rejected(tmp_path: Path) -> None:
    history_path = tmp_path / "state" / "123" / "history.json"
    history_path.parent.mkdir(parents=True)
    history_path.write_bytes(b"{not json")

    with pytest.raises(signal.LiveStateSignalError, match="UTF-8 JSON"):
        signal.load_persisted_match_history(tmp_path / "state", 123)


def test_persisted_match_id_mismatch_is_rejected(tmp_path: Path) -> None:
    _write_persisted_history(
        tmp_path / "state",
        directory_match_id=123,
        payload_match_id=456,
        snapshots=[_snapshot(60)],
    )

    with pytest.raises(signal.LiveStateSignalError, match="does not match"):
        signal.load_persisted_match_history(tmp_path / "state", 123)


def test_persisted_duplicate_timestamp_is_rejected(tmp_path: Path) -> None:
    _write_persisted_history(
        tmp_path / "state",
        directory_match_id=123,
        payload_match_id=123,
        snapshots=[_snapshot(60), _snapshot(60)],
    )

    with pytest.raises(signal.LiveStateSignalError, match="Duplicate"):
        signal.load_persisted_match_history(tmp_path / "state", 123)


def test_unsorted_persisted_history_is_rejected(tmp_path: Path) -> None:
    _write_persisted_history(
        tmp_path / "state",
        directory_match_id=123,
        payload_match_id=123,
        snapshots=[_snapshot(120), _snapshot(60)],
    )

    with pytest.raises(signal.LiveStateSignalError, match="sorted numerically"):
        signal.load_persisted_match_history(tmp_path / "state", 123)


@pytest.mark.parametrize("field", ["target_value", "league_id"])
def test_invalid_persisted_snapshot_is_rejected(tmp_path: Path, field: str) -> None:
    snapshot = _snapshot(60)
    snapshot[field] = 1
    _write_persisted_history(
        tmp_path / "state",
        directory_match_id=123,
        payload_match_id=123,
        snapshots=[snapshot],
    )

    with pytest.raises(signal.LiveStateSignalError, match="Target and lineage"):
        signal.load_persisted_match_history(tmp_path / "state", 123)


def test_history_and_response_are_replaced_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_model(monkeypatch)
    request_path = tmp_path / "request.json"
    state_dir = tmp_path / "state"
    output_path = tmp_path / "signal.json"
    _write_json(request_path, _request_payload(match_id=123, game_time=60))
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def replace_spy(source: Path, destination: Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.exists()
        assert source_path.parent == destination_path.parent
        replacements.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(signal.os, "replace", replace_spy)

    signal.ingest_live_state_snapshot(
        tmp_path / "bundle", state_dir, request_path, output_path
    )

    assert [destination for _, destination in replacements] == [
        state_dir / "123" / "history.json",
        output_path,
    ]
    assert not list(tmp_path.rglob("*.transaction.*"))


def test_corrupted_model_bundle_is_rejected_before_state_is_written(
    tmp_path: Path,
) -> None:
    request_path = tmp_path / "request.json"
    state_dir = tmp_path / "state"
    output_path = tmp_path / "signal.json"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_json(request_path, _request_payload(match_id=123, game_time=60))
    expected_model = b"synthetic-model"
    manifest = model._render_bundle_manifest(
        source_manifest_sha256="1" * 64,
        lineage_sha256="2" * 64,
        feature_columns=history.SUPPORTED_MODEL_FEATURE_COLUMNS,
        league_ids=[10],
        match_count=1,
        row_count=1,
        parameters=model.baseline.catboost_parameters(),
        model_bytes=expected_model,
    )
    (bundle / model.MODEL_MANIFEST_FILENAME).write_bytes(manifest)
    (bundle / model.MODEL_FILENAME).write_bytes(b"X" + expected_model[1:])

    with pytest.raises(model.ReplayStateModelError, match="SHA-256"):
        signal.ingest_live_state_snapshot(
            bundle,
            state_dir,
            request_path,
            output_path,
        )

    assert not (state_dir / "123" / "history.json").exists()
    assert not output_path.exists()


def _install_fake_model(
    monkeypatch: pytest.MonkeyPatch,
    prediction: float = 123.45,
) -> _FakeModel:
    fake_model = _FakeModel(prediction)
    scorer = model.ReplayStateModelScorer(
        feature_columns=history.SUPPORTED_MODEL_FEATURE_COLUMNS,
        manifest_sha256="1" * 64,
        _model=fake_model,
    )
    monkeypatch.setattr(
        signal.model,
        "load_replay_state_model",
        lambda path: scorer,
    )
    return fake_model


def _request_payload(*, match_id: int, game_time: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "match_id": match_id,
        "snapshot": _snapshot(game_time),
    }


def _snapshot(game_time: int) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"game_time_seconds": game_time}
    snapshot.update(
        {
            column: game_time + index + 1
            for index, column in enumerate(history.CURRENT_FEATURE_COLUMNS)
        }
    )
    return snapshot


def _write_persisted_history(
    state_dir: Path,
    *,
    directory_match_id: int,
    payload_match_id: int,
    snapshots: list[Mapping[str, object]],
) -> None:
    path = state_dir / str(directory_match_id) / "history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        path,
        {
            "schema_version": 1,
            "match_id": payload_match_id,
            "snapshots": snapshots,
        },
    )


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    path.write_text(f"{content}\n", encoding="utf-8", newline="\n")
