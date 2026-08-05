from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

import pytest

from app import replay_state_history as history
from app import replay_state_model as model
from app import replay_state_training as training
from app.cli import main


class _FakeModel:
    def __init__(self, prediction: float = 123.45) -> None:
        self.prediction = prediction
        self.rows: list[list[float]] = []

    def predict(self, data: Sequence[Sequence[float]]) -> list[float]:
        self.rows.extend(list(map(list, data)))
        return [self.prediction for _ in data]


def test_request_sorting_latest_selection_and_exact_feature_set(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    _write_json(request_path, _request_payload(times=(180, 0, 120, 60)))

    request = history.load_replay_state_history_request(request_path)
    result = history.build_replay_state_feature_mapping(request.snapshots)

    assert [snapshot.game_time_seconds for snapshot in request.snapshots] == [
        0,
        60,
        120,
        180,
    ]
    assert result.current_snapshot.game_time_seconds == 180
    assert result.missing_lags_seconds == ()
    assert result.features is not None
    assert tuple(result.features) == history.SUPPORTED_MODEL_FEATURE_COLUMNS
    assert len(result.features) == 26


def test_duplicate_timestamp_and_empty_history_are_rejected() -> None:
    duplicate = [_snapshot(0), _snapshot(0)]
    with pytest.raises(history.ReplayStateHistoryError, match="Duplicate"):
        history.validate_normalized_snapshots(duplicate)

    with pytest.raises(history.ReplayStateHistoryError, match="non-empty"):
        history.validate_normalized_snapshots([])


def test_exact_normalized_snapshot_fields_are_accepted() -> None:
    snapshots = history.validate_normalized_snapshots([_snapshot(0)])

    assert len(snapshots) == 1
    assert snapshots[0].game_time_seconds == 0
    assert tuple(snapshots[0].features) == history.CURRENT_FEATURE_COLUMNS


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("remove", "kill_diff"), "Missing"),
        (("add", "mystery"), "Unexpected"),
        (("add", "target_net_worth_diff_change_180"), "Target and lineage"),
        (("add", "league_id"), "Target and lineage"),
    ],
)
def test_missing_unexpected_target_and_lineage_fields_are_rejected(
    change: tuple[str, str], message: str
) -> None:
    snapshot = _snapshot(0)
    operation, field = change
    if operation == "remove":
        del snapshot[field]
    else:
        snapshot[field] = 1

    with pytest.raises(history.ReplayStateHistoryError, match=message):
        history.validate_normalized_snapshots([snapshot])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kill_diff", True, "numeric"),
        ("game_time_seconds", False, "numeric"),
        ("net_worth_diff", "1", "numeric"),
        ("total_xp", float("nan"), "finite"),
        ("total_xp", float("inf"), "finite"),
        ("game_time_seconds", -1, "non-negative"),
    ],
)
def test_invalid_normalized_numeric_values_are_rejected(
    field: str, value: object, message: str
) -> None:
    snapshot = _snapshot(0)
    snapshot[field] = value

    with pytest.raises(history.ReplayStateHistoryError, match=message):
        history.validate_normalized_snapshots([snapshot])


@pytest.mark.parametrize("schema_version", [True, "1", 0, 2])
def test_malformed_schema_version_is_rejected(
    tmp_path: Path, schema_version: object
) -> None:
    request_path = tmp_path / "request.json"
    payload = _request_payload(times=(0,))
    payload["schema_version"] = schema_version
    _write_json(request_path, payload)

    with pytest.raises(history.ReplayStateHistoryError, match="schema_version"):
        history.load_replay_state_history_request(request_path)


@pytest.mark.parametrize("lag", history.REQUIRED_LAGS_SECONDS)
def test_exact_lag_lookup_and_change_semantics(lag: int) -> None:
    snapshots = history.validate_normalized_snapshots(
        [_snapshot(time) for time in (0, 60, 120, 180)]
    )

    result = history.build_replay_state_feature_mapping(snapshots)

    assert result.features is not None
    for column in training.MOMENTUM_DIFF_COLUMNS:
        assert result.features[f"{column}_change_prev_{lag}"] == lag


def test_no_nearest_timestamp_fallback_and_missing_lags_are_sorted() -> None:
    snapshots = history.validate_normalized_snapshots(
        [_snapshot(time) for time in (1, 59, 61, 119, 121, 180)]
    )

    result = history.build_replay_state_feature_mapping(snapshots)

    assert result.current_snapshot.game_time_seconds == 180
    assert result.features is None
    assert result.missing_lags_seconds == (60, 120, 180)


def test_lag_feature_parity_with_training_set_builder() -> None:
    times = (0, 60, 120, 180, 360)
    decisions: dict[training.DecisionKey, training._Decision] = {}
    for game_time in times:
        snapshot = _snapshot(game_time)
        values = {
            column: int(snapshot[column]) for column in training.CURRENT_DIFF_COLUMNS
        }
        for total_column, stem in training.TOTAL_COLUMNS:
            total = int(snapshot[total_column])
            values[f"team_{stem}"] = total // 2
            values[f"opponent_{stem}"] = total - (total // 2)
        decisions[(1, game_time, "RADIANT")] = training._Decision(
            match_id=1,
            game_time_seconds=game_time,
            team="RADIANT",
            values=values,
        )
    outcomes: dict[training.OutcomeKey, training._Outcome] = {
        (1, 180, "RADIANT", 180): training._Outcome(
            match_id=1,
            game_time_seconds=180,
            team="RADIANT",
            horizon_seconds=180,
            values={
                "net_worth_diff_change": 1,
                "total_xp_diff_change": 2,
                "kill_diff_change": 3,
            },
        )
    }
    training_row = training._construct_training_rows(decisions, outcomes)[0]
    normalized = history.validate_normalized_snapshots(
        [_snapshot(time) for time in times if time <= 180]
    )

    result = history.build_replay_state_feature_mapping(normalized)

    assert result.features == {
        column: training_row[column]
        for column in history.SUPPORTED_MODEL_FEATURE_COLUMNS
    }


def test_model_feature_contract_must_match_exactly() -> None:
    history.validate_model_feature_contract(history.SUPPORTED_MODEL_FEATURE_COLUMNS)

    with pytest.raises(history.ReplayStateHistoryError, match="contract"):
        history.validate_model_feature_contract(
            tuple(reversed(history.SUPPORTED_MODEL_FEATURE_COLUMNS))
        )


@pytest.mark.parametrize(
    ("prediction", "direction"),
    [(123.45, "radiant_improving"), (-4.0, "dire_improving"), (0.0, "flat")],
)
def test_ready_history_uses_existing_scorer_in_manifest_order(
    prediction: float, direction: str
) -> None:
    fake_model = _FakeModel(prediction)
    scorer = _scorer(fake_model)
    request = history.ReplayStateHistoryRequest(
        match_id=123,
        snapshots=history.validate_normalized_snapshots(
            [_snapshot(time) for time in (180, 0, 120, 60)]
        ),
    )

    response = history.score_replay_state_history_request(request, scorer)

    assert list(response) == [
        "schema_version",
        "status",
        "model_manifest_sha256",
        "match_id",
        "game_time_seconds",
        "horizon_seconds",
        "prediction_net_worth_diff_change",
        "predicted_direction",
    ]
    assert response == {
        "schema_version": 1,
        "status": "scored",
        "model_manifest_sha256": "1" * 64,
        "match_id": 123,
        "game_time_seconds": 180,
        "horizon_seconds": 180,
        "prediction_net_worth_diff_change": prediction,
        "predicted_direction": direction,
    }
    built = history.build_replay_state_feature_mapping(request.snapshots)
    assert built.features is not None
    assert fake_model.rows == [
        [float(built.features[column]) for column in scorer.feature_columns]
    ]


def test_not_ready_response_is_ordered_and_does_not_invoke_scorer() -> None:
    fake_model = _FakeModel()
    scorer = _scorer(fake_model)
    request = history.ReplayStateHistoryRequest(
        match_id=123,
        snapshots=history.validate_normalized_snapshots(
            [_snapshot(time) for time in (120, 60, 0)]
        ),
    )

    response = history.score_replay_state_history_request(request, scorer)

    assert list(response) == [
        "schema_version",
        "status",
        "match_id",
        "game_time_seconds",
        "required_lags_seconds",
        "missing_lags_seconds",
    ]
    assert response == {
        "schema_version": 1,
        "status": "not_ready",
        "match_id": 123,
        "game_time_seconds": 120,
        "required_lags_seconds": [60, 120, 180],
        "missing_lags_seconds": [180],
    }
    assert fake_model.rows == []


@pytest.mark.parametrize(
    ("times", "response_status"),
    [((0, 60, 120, 180), "scored"), ((0, 60, 120), "not_ready")],
)
def test_cli_response_is_atomic_deterministic_and_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    times: tuple[int, ...],
    response_status: str,
) -> None:
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    _write_json(request_path, _request_payload(times=times))
    fake_model = _FakeModel()
    monkeypatch.setattr(
        history.model,
        "load_replay_state_model",
        lambda path: _scorer(fake_model),
    )
    arguments = [
        "score-replay-state-history",
        "--model-dir",
        str(tmp_path / "bundle"),
        "--input",
        str(request_path),
        "--output",
        str(response_path),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    first_response = response_path.read_bytes()
    response = json.loads(first_response)
    assert response["status"] == response_status
    assert b"target_" not in first_response
    assert b"E:\\" not in first_response
    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"
    assert response_path.read_bytes() == first_response


def test_corrupted_model_bundle_is_rejected(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_json(request_path, _request_payload(times=(0, 60, 120, 180)))
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
        history.score_replay_state_history(bundle, request_path, response_path)

    assert not response_path.exists()


def _scorer(fake_model: _FakeModel) -> model.ReplayStateModelScorer:
    return model.ReplayStateModelScorer(
        feature_columns=history.SUPPORTED_MODEL_FEATURE_COLUMNS,
        manifest_sha256="1" * 64,
        _model=fake_model,
    )


def _request_payload(*, times: Sequence[int]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "match_id": 123,
        "snapshots": [_snapshot(game_time) for game_time in times],
    }


def _snapshot(game_time: int) -> dict[str, object]:
    snapshot: dict[str, object] = {"game_time_seconds": game_time}
    snapshot.update(
        {
            column: game_time + index + 1
            for index, column in enumerate(history.CURRENT_FEATURE_COLUMNS)
        }
    )
    return snapshot


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    path.write_text(f"{content}\n", encoding="utf-8", newline="\n")
