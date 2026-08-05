from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from app.cli import main
from app import replay_state_model as model
from app.replay_state_training import TARGET_COLUMNS, TRAINING_COLUMNS


class _FakeModel:
    def __init__(self, prediction: float = 12.5) -> None:
        self.prediction = prediction
        self.rows: list[list[float]] = []

    def predict(self, data: Sequence[Sequence[float]]) -> list[float]:
        self.rows = [list(row) for row in data]
        return [self.prediction for _ in data]


def test_build_selection_order_contract_manifest_and_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path, reverse=True)
    output_dir = tmp_path / "bundle"
    captured: dict[str, object] = {}

    def fake_train(
        rows: Sequence[object],
        feature_columns: Sequence[str],
        parameters: Mapping[str, object],
        path: Path,
    ) -> None:
        captured["keys"] = [
            (getattr(row, "match_id"), getattr(row, "game_time_seconds"))
            for row in rows
        ]
        captured["features"] = tuple(feature_columns)
        captured["parameters"] = dict(parameters)
        path.write_bytes(b"synthetic-catboost-model")

    fake_model = _FakeModel()
    monkeypatch.setattr(model, "_train_model", fake_train)
    monkeypatch.setattr(model, "_load_catboost_model", lambda path: fake_model)
    arguments = [
        "build-replay-state-model",
        "--training-dir",
        str(training_dir),
        "--trajectory-dir",
        str(trajectory_dir),
        "--league-id",
        "20",
        "--league-id",
        "10",
        "--output-dir",
        str(output_dir),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    first_model_bytes = (output_dir / model.MODEL_FILENAME).read_bytes()
    first_manifest_bytes = (output_dir / model.MODEL_MANIFEST_FILENAME).read_bytes()

    assert captured["keys"] == [(100, 0), (100, 60), (200, 0)]
    features = captured["features"]
    assert features == model.baseline._select_feature_columns(TRAINING_COLUMNS)
    assert isinstance(features, tuple)
    assert set(features).isdisjoint(model.FORBIDDEN_SCORE_FEATURES)
    assert set(features).isdisjoint(TARGET_COLUMNS)
    assert captured["parameters"] == model.baseline.catboost_parameters()

    manifest = json.loads(first_manifest_bytes)
    assert set(manifest) == {
        "schema_version",
        "model_format",
        "source_manifest_sha256",
        "lineage_sha256",
        "target",
        "horizon_seconds",
        "feature_columns",
        "league_ids",
        "match_count",
        "row_count",
        "catboost_parameters",
        "model_file",
    }
    assert manifest["schema_version"] == 1
    assert manifest["model_format"] == model.MODEL_FORMAT
    assert manifest["target"] == model.TARGET_COLUMN
    assert manifest["horizon_seconds"] == 180
    assert manifest["feature_columns"] == list(features)
    assert manifest["league_ids"] == [10, 20]
    assert manifest["match_count"] == 2
    assert manifest["row_count"] == 3
    assert manifest["catboost_parameters"] == model.baseline.catboost_parameters()
    assert manifest["lineage_sha256"] == model.holdout._lineage_sha256(
        ((100, 10), (200, 20))
    )
    assert manifest["model_file"] == {
        "filename": model.MODEL_FILENAME,
        "size_bytes": len(first_model_bytes),
        "sha256": hashlib.sha256(first_model_bytes).hexdigest(),
    }
    assert all(needle not in first_manifest_bytes for needle in (b"E:\\", b"/tmp"))

    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"
    assert (output_dir / model.MODEL_FILENAME).read_bytes() == first_model_bytes
    assert (
        output_dir / model.MODEL_MANIFEST_FILENAME
    ).read_bytes() == first_manifest_bytes


@pytest.mark.parametrize("failure", ["filename", "count", "hash"])
def test_source_manifest_validation(tmp_path: Path, failure: str) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    manifest_path = training_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if failure == "filename":
        manifest["training_file"]["filename"] = "other.csv"
    elif failure == "count":
        manifest["training_row_count"] += 1
        manifest["training_file"]["row_count"] += 1
    else:
        manifest["training_file"]["sha256"] = "0" * 64
    _write_json(manifest_path, manifest)

    with pytest.raises(model.ReplayStateModelError):
        model.build_replay_state_model(
            training_dir, trajectory_dir, [10], tmp_path / "bundle"
        )


def test_lineage_conflict_duplicate_key_and_non_finite_are_rejected(
    tmp_path: Path,
) -> None:
    conflict_root = tmp_path / "conflict"
    conflict_root.mkdir()
    training_dir, trajectory_dir = _write_standard_fixture(conflict_root)
    _write_trajectory(trajectory_dir / "conflict.json", 100, 99)
    with pytest.raises(model.ReplayStateModelError, match="Conflicting lineage"):
        model.build_replay_state_model(
            training_dir, trajectory_dir, [10], conflict_root / "bundle"
        )

    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    training_dir, trajectory_dir = _write_standard_fixture(duplicate_root)
    _rewrite_training(training_dir, lambda rows: rows.append(dict(rows[0])))
    with pytest.raises(model.ReplayStateModelError, match="Duplicate"):
        model.build_replay_state_model(
            training_dir, trajectory_dir, [10], duplicate_root / "bundle"
        )

    non_finite_root = tmp_path / "non-finite"
    non_finite_root.mkdir()
    training_dir, trajectory_dir = _write_standard_fixture(non_finite_root)

    def make_non_finite(rows: list[dict[str, str]]) -> None:
        rows[-1]["kill_diff"] = "inf"

    _rewrite_training(training_dir, make_non_finite)
    with pytest.raises(model.ReplayStateModelError, match="Malformed numeric"):
        model.build_replay_state_model(
            training_dir, trajectory_dir, [10], non_finite_root / "bundle"
        )


def test_selected_leagues_must_be_nonempty_and_contain_rows(
    tmp_path: Path,
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    with pytest.raises(model.ReplayStateModelError, match="non-empty"):
        model.build_replay_state_model(
            training_dir, trajectory_dir, [], tmp_path / "empty"
        )
    with pytest.raises(model.ReplayStateModelError, match="no replay state"):
        model.build_replay_state_model(
            training_dir, trajectory_dir, [999], tmp_path / "missing"
        )


@pytest.mark.parametrize("change", ["source", "league"])
def test_changed_source_or_config_forces_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    output_dir = tmp_path / "bundle"
    training_calls = 0

    def fake_train(
        rows: Sequence[object],
        features: Sequence[str],
        parameters: Mapping[str, object],
        path: Path,
    ) -> None:
        nonlocal training_calls
        training_calls += 1
        path.write_bytes(f"model-{training_calls}".encode())

    monkeypatch.setattr(model, "_train_model", fake_train)
    monkeypatch.setattr(model, "_load_catboost_model", lambda path: _FakeModel())
    first = model.build_replay_state_model(
        training_dir, trajectory_dir, [10], output_dir
    )
    if change == "source":

        def change_source(rows: list[dict[str, str]]) -> None:
            rows[0]["kill_diff"] = str(int(rows[0]["kill_diff"]) + 1)

        _rewrite_training(training_dir, change_source)
        league_ids = [10]
    else:
        league_ids = [20]

    second = model.build_replay_state_model(
        training_dir, trajectory_dir, league_ids, output_dir
    )

    assert first.status == "BUILT"
    assert second.status == "BUILT"
    assert training_calls == 2


def test_model_file_size_hash_and_corruption_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    output_dir = tmp_path / "bundle"
    _patch_fake_build(monkeypatch)
    model.build_replay_state_model(training_dir, trajectory_dir, [10], output_dir)
    model_path = output_dir / model.MODEL_FILENAME
    original_model = model_path.read_bytes()
    model_path.write_bytes(original_model + b"corrupt")

    with pytest.raises(model.ReplayStateModelError, match="size"):
        model.load_replay_state_model(output_dir)

    model_path.write_bytes(b"X" + original_model[1:])
    with pytest.raises(model.ReplayStateModelError, match="SHA-256"):
        model.load_replay_state_model(output_dir)

    rebuilt = model.build_replay_state_model(
        training_dir, trajectory_dir, [10], output_dir
    )
    assert rebuilt.status == "BUILT"


def test_self_consistent_but_invalid_catboost_file_is_rejected(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    model_bytes = b"not-a-catboost-model"
    (bundle / model.MODEL_FILENAME).write_bytes(model_bytes)
    manifest = model._render_bundle_manifest(
        source_manifest_sha256="1" * 64,
        lineage_sha256="2" * 64,
        feature_columns=model.baseline._select_feature_columns(TRAINING_COLUMNS),
        league_ids=[10],
        match_count=1,
        row_count=1,
        parameters=model.baseline.catboost_parameters(),
        model_bytes=model_bytes,
    )
    (bundle / model.MODEL_MANIFEST_FILENAME).write_bytes(manifest)

    with pytest.raises(model.ReplayStateModelError, match="corrupted"):
        model.load_replay_state_model(bundle)


def test_successful_load_score_and_exact_feature_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    output_dir = tmp_path / "bundle"
    fake = _FakeModel(prediction=-7.25)
    _patch_fake_build(monkeypatch, fake)
    model.build_replay_state_model(training_dir, trajectory_dir, [10], output_dir)
    scorer = model.load_replay_state_model(output_dir)
    reversed_features = {
        column: index + 0.5
        for index, column in reversed(list(enumerate(scorer.feature_columns)))
    }

    prediction = scorer.score(reversed_features)

    assert prediction == -7.25
    assert fake.rows == [
        [float(reversed_features[column]) for column in scorer.feature_columns]
    ]


@pytest.mark.parametrize(
    ("features", "message"),
    [
        ({"a": 1.0}, "Missing"),
        ({"a": 1.0, "b": 2.0, "c": 3.0}, "Unexpected"),
        ({"a": "1", "b": 2.0}, "numeric"),
        ({"a": float("nan"), "b": 2.0}, "finite"),
        ({"a": float("inf"), "b": 2.0}, "finite"),
        ({"a": 10**1000, "b": 2.0}, "finite"),
        ({"a": 1.0, "b": 2.0, "match_id": 10}, "lineage"),
        (
            {"a": 1.0, "b": 2.0, model.TARGET_COLUMN: 3.0},
            "Target",
        ),
    ],
)
def test_score_feature_validation(features: dict[str, object], message: str) -> None:
    scorer = model.ReplayStateModelScorer(
        feature_columns=("a", "b"),
        manifest_sha256="1" * 64,
        _model=_FakeModel(),
    )

    with pytest.raises(model.ReplayStateModelError, match=message):
        scorer.score(features)


@pytest.mark.parametrize(
    ("prediction", "direction"),
    [
        (1.0, "radiant_improving"),
        (-1.0, "dire_improving"),
        (0.0, "flat"),
    ],
)
def test_direction_semantics(prediction: float, direction: str) -> None:
    assert model._prediction_direction(prediction) == direction


def test_cli_score_response_is_deterministic_and_manifest_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    bundle = tmp_path / "bundle"
    _patch_fake_build(monkeypatch, _FakeModel(prediction=123.45))
    model.build_replay_state_model(training_dir, trajectory_dir, [10], bundle)
    manifest_bytes = (bundle / model.MODEL_MANIFEST_FILENAME).read_bytes()
    feature_columns = _read_json(bundle / model.MODEL_MANIFEST_FILENAME)[
        "feature_columns"
    ]
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    _write_json(
        request_path,
        {
            "schema_version": 1,
            "match_id": 123,
            "game_time_seconds": 900,
            "features": {column: 1.0 for column in feature_columns},
        },
    )
    arguments = [
        "score-replay-state-model",
        "--model-dir",
        str(bundle),
        "--input",
        str(request_path),
        "--output",
        str(response_path),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    first_response = response_path.read_bytes()
    response = json.loads(first_response)
    assert response == {
        "schema_version": 1,
        "model_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "match_id": 123,
        "game_time_seconds": 900,
        "horizon_seconds": 180,
        "prediction_net_worth_diff_change": 123.45,
        "predicted_direction": "radiant_improving",
    }

    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"
    assert response_path.read_bytes() == first_response


def _patch_fake_build(
    monkeypatch: pytest.MonkeyPatch, fake_model: _FakeModel | None = None
) -> _FakeModel:
    loaded_model = fake_model or _FakeModel()

    def fake_train(
        rows: Sequence[object],
        features: Sequence[str],
        parameters: Mapping[str, object],
        path: Path,
    ) -> None:
        path.write_bytes(b"synthetic-catboost-model")

    monkeypatch.setattr(model, "_train_model", fake_train)
    monkeypatch.setattr(model, "_load_catboost_model", lambda path: loaded_model)
    return loaded_model


def _write_standard_fixture(root: Path, *, reverse: bool = False) -> tuple[Path, Path]:
    rows = [
        _training_row(100, 0, target=1),
        _training_row(100, 60, target=2),
        _training_row(200, 0, target=-1),
        _training_row(300, 0, target=99),
    ]
    if reverse:
        rows.reverse()
    training_dir = root / "training"
    trajectory_dir = root / "trajectories"
    training_dir.mkdir()
    trajectory_dir.mkdir()
    _write_training_rows(training_dir, rows)
    for match_id, league_id in ((100, 10), (200, 20), (300, 30)):
        _write_trajectory(trajectory_dir / f"{match_id}.json", match_id, league_id)
    return training_dir, trajectory_dir


def _training_row(
    match_id: int, game_time_seconds: int, *, target: int
) -> dict[str, object]:
    row: dict[str, object] = {}
    for index, column in enumerate(TRAINING_COLUMNS):
        if column == "match_id":
            value: object = match_id
        elif column == "game_time_seconds":
            value = game_time_seconds
        elif column == model.TARGET_COLUMN:
            value = target
        elif column in TARGET_COLUMNS:
            value = target + index
        else:
            value = match_id + game_time_seconds + index
        row[column] = value
    return row


def _write_training_rows(
    directory: Path, rows: Sequence[dict[str, object] | dict[str, str]]
) -> None:
    training_bytes = _csv_bytes(rows)
    (directory / model.TRAINING_FILENAME).write_bytes(training_bytes)
    _write_json(
        directory / model.TRAINING_MANIFEST_FILENAME,
        {
            "schema_version": 1,
            "training_row_count": len(rows),
            "training_file": {
                "filename": model.TRAINING_FILENAME,
                "row_count": len(rows),
                "sha256": hashlib.sha256(training_bytes).hexdigest(),
            },
        },
    )


def _rewrite_training(directory: Path, change: Any) -> None:
    rows = _read_csv(directory / model.TRAINING_FILENAME)
    change(rows)
    _write_training_rows(directory, rows)


def _write_trajectory(path: Path, match_id: int, league_id: int) -> None:
    _write_json(path, {"match_id": match_id, "league_id": league_id})


def _csv_bytes(
    rows: Sequence[dict[str, object] | dict[str, str]],
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=",", lineterminator="\n")
    writer.writerow(TRAINING_COLUMNS)
    for row in rows:
        writer.writerow(row[column] for column in TRAINING_COLUMNS)
    return stream.getvalue().encode("utf-8")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_bytes())
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: object) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    path.write_text(f"{text}\n", encoding="utf-8", newline="\n")
