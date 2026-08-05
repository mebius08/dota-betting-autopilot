from __future__ import annotations

from collections.abc import Callable, Sequence
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

import pytest

from app.cli import main
from app import replay_state_tournament_holdout as holdout
from app.replay_state_training import TARGET_COLUMNS, TRAINING_COLUMNS


def test_lineage_is_deterministic_and_rejects_conflicts(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_trajectory(first / "z.json", 200, 20)
    _write_trajectory(first / "a.json", 100, 10)
    _write_trajectory(second / "one.json", 100, 10)
    _write_trajectory(second / "two.json", 200, 20)

    expected = {100: 10, 200: 20}
    assert holdout.build_match_to_league_lineage(first) == expected
    assert holdout.build_match_to_league_lineage(second) == expected
    assert holdout._lineage_sha256(tuple(expected.items())) == holdout._lineage_sha256(
        tuple(reversed(expected.items()))
    )

    _write_trajectory(first / "conflict.json", 100, 99)
    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="Conflicting lineage"
    ):
        holdout.build_match_to_league_lineage(first)


def test_invalid_lineage_fields_are_rejected(tmp_path: Path) -> None:
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    (trajectory_dir / "bad.json").write_text(
        '{"match_id": 100, "league_id": "10"}\n', encoding="utf-8"
    )

    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="invalid league_id"
    ):
        holdout.build_match_to_league_lineage(trajectory_dir)


def test_explicit_selection_exclusion_median_features_and_fit_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    captured: dict[str, object] = {}

    def fake_catboost(
        train_rows: Sequence[object],
        test_rows: Sequence[object],
        feature_columns: Sequence[str],
    ) -> list[float]:
        captured["train_match_ids"] = {
            getattr(row, "match_id") for row in train_rows
        }
        captured["test_match_ids"] = {getattr(row, "match_id") for row in test_rows}
        captured["features"] = tuple(feature_columns)
        return [getattr(row, "values")[holdout.TARGET_COLUMN] + 1.0 for row in test_rows]

    monkeypatch.setattr(holdout, "_predict_catboost", fake_catboost)
    output_dir = tmp_path / "output"
    result = holdout.evaluate_replay_state_tournament_holdout(
        training_dir,
        trajectory_dir,
        [20, 10],
        [30],
        output_dir,
    )

    assert result.status == "BUILT"
    assert captured["train_match_ids"] == {100, 200}
    assert captured["test_match_ids"] == {300}
    features = captured["features"]
    assert isinstance(features, tuple)
    assert "match_id" not in features
    assert "league_id" not in features
    assert "game_time_seconds" not in features
    assert set(TARGET_COLUMNS).isdisjoint(features)

    evaluation = _read_json(output_dir / holdout.EVALUATION_FILENAME)
    assert evaluation["train_league_ids"] == [10, 20]
    assert evaluation["test_league_ids"] == [30]
    assert evaluation["train_match_count"] == 2
    assert evaluation["train_row_count"] == 4
    assert evaluation["test_match_count"] == 1
    assert evaluation["test_row_count"] == 6
    assert evaluation["excluded_match_count"] == 1
    assert evaluation["excluded_row_count"] == 1
    assert evaluation["train_target_median"] == 4.0
    assert [entry["league_id"] for entry in evaluation["league_counts"]] == [
        10,
        20,
        30,
        40,
    ]
    assert [entry["selection"] for entry in evaluation["league_counts"]] == [
        "train",
        "train",
        "test",
        "excluded",
    ]
    assert evaluation["catboost_parameters"] == holdout.baseline.catboost_parameters()


def test_aggregate_metrics_relative_improvements_and_time_buckets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    monkeypatch.setattr(
        holdout,
        "_predict_catboost",
        lambda train, test, features: [
            row.values[holdout.TARGET_COLUMN] + 1.0 for row in test
        ],
    )
    output_dir = tmp_path / "output"

    holdout.evaluate_replay_state_tournament_holdout(
        training_dir, trajectory_dir, [10, 20], [30], output_dir
    )
    evaluation = _read_json(output_dir / holdout.EVALUATION_FILENAME)
    aggregate = evaluation["aggregate_metrics"]

    assert aggregate["catboost"] == {
        "row_count": 6,
        "match_count": 1,
        "MAE": 1.0,
        "RMSE": 1.0,
        "median_absolute_error": 1.0,
        "sign_accuracy": 5 / 6,
    }
    assert aggregate["zero"]["MAE"] == 3.0
    assert aggregate["zero"]["RMSE"] == pytest.approx(math.sqrt(76 / 6))
    assert aggregate["momentum_180"]["MAE"] == 1.5
    improvements = evaluation["relative_improvements"]
    assert improvements["catboost_vs_zero"]["MAE"] == pytest.approx(2 / 3)
    assert improvements["catboost_vs_momentum_180"]["MAE"] == pytest.approx(
        1 / 3
    )

    buckets = evaluation["time_bucket_metrics"]
    assert list(buckets) == list(holdout.TIME_BUCKETS)
    assert buckets["early"]["zero"]["row_count"] == 1
    assert buckets["mid"]["zero"]["row_count"] == 2
    assert buckets["late"]["zero"]["row_count"] == 2
    assert buckets["very_late"]["zero"]["row_count"] == 1


@pytest.mark.parametrize(
    ("game_time_seconds", "bucket"),
    [
        (599, "early"),
        (600, "mid"),
        (1199, "mid"),
        (1200, "late"),
        (1799, "late"),
        (1800, "very_late"),
    ],
)
def test_fixed_time_bucket_boundaries(
    game_time_seconds: int, bucket: str
) -> None:
    assert holdout._time_bucket_name(game_time_seconds) == bucket


def test_empty_time_buckets_are_deterministic() -> None:
    metrics = holdout._time_bucket_metrics(
        [
            holdout._PredictionRow(
                match_id=1,
                league_id=10,
                game_time_seconds=599,
                actual=1.0,
                zero_prediction=0.0,
                train_median_prediction=0.0,
                momentum_180_prediction=0.0,
                catboost_prediction=0.0,
            )
        ]
    )

    for bucket in ("mid", "late", "very_late"):
        for predictor in holdout.PREDICTOR_NAMES:
            assert metrics[bucket][predictor] == {
                "row_count": 0,
                "match_count": 0,
                "MAE": None,
                "RMSE": None,
                "median_absolute_error": None,
                "sign_accuracy": None,
            }


def test_missing_lineage_is_rejected(tmp_path: Path) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    (trajectory_dir / "400.json").unlink()

    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="Missing canonical lineage"
    ):
        holdout.evaluate_replay_state_tournament_holdout(
            training_dir, trajectory_dir, [10, 20], [30], tmp_path / "output"
        )


@pytest.mark.parametrize("manifest_failure", ["filename", "count", "hash"])
def test_source_manifest_validation_rejects_invalid_training_source(
    tmp_path: Path, manifest_failure: str
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    manifest = _read_json(training_dir / holdout.MANIFEST_FILENAME)
    if manifest_failure == "filename":
        manifest["training_file"]["filename"] = "other.csv"
    elif manifest_failure == "count":
        manifest["training_row_count"] += 1
        manifest["training_file"]["row_count"] += 1
    else:
        manifest["training_file"]["sha256"] = "0" * 64
    _write_json(training_dir / holdout.MANIFEST_FILENAME, manifest)

    with pytest.raises(holdout.ReplayStateTournamentHoldoutError):
        holdout.evaluate_replay_state_tournament_holdout(
            training_dir, trajectory_dir, [10, 20], [30], tmp_path / "output"
        )


def test_duplicate_training_key_and_malformed_number_are_rejected(
    tmp_path: Path,
) -> None:
    duplicate_root = tmp_path / "duplicate"
    duplicate_root.mkdir()
    training_dir, trajectory_dir = _write_standard_fixture(duplicate_root)
    _rewrite_training(
        training_dir,
        lambda rows: rows.append(dict(rows[0])),
    )
    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="Duplicate"
    ):
        holdout.evaluate_replay_state_tournament_holdout(
            training_dir, trajectory_dir, [10, 20], [30], tmp_path / "output-1"
        )

    malformed_root = tmp_path / "malformed"
    malformed_root.mkdir()
    training_dir, trajectory_dir = _write_standard_fixture(malformed_root)

    def make_non_finite(rows: list[dict[str, str]]) -> None:
        rows[0]["kill_diff"] = "nan"

    _rewrite_training(training_dir, make_non_finite)
    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="Malformed numeric"
    ):
        holdout.evaluate_replay_state_tournament_holdout(
            training_dir, trajectory_dir, [10, 20], [30], tmp_path / "output-2"
        )


def test_nonempty_disjoint_league_sets_are_required(tmp_path: Path) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="non-empty"
    ):
        holdout.evaluate_replay_state_tournament_holdout(
            training_dir, trajectory_dir, [], [30], tmp_path / "empty"
        )
    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="overlap"
    ):
        holdout.evaluate_replay_state_tournament_holdout(
            training_dir, trajectory_dir, [10, 30], [30], tmp_path / "overlap"
        )


def test_selected_leagues_must_have_matches_and_rows(tmp_path: Path) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path)
    with pytest.raises(
        holdout.ReplayStateTournamentHoldoutError, match="no training matches"
    ):
        holdout.evaluate_replay_state_tournament_holdout(
            training_dir, trajectory_dir, [999], [30], tmp_path / "output"
        )


def test_cli_rerun_prediction_order_hash_and_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    training_dir, trajectory_dir = _write_standard_fixture(tmp_path, reverse=True)
    output_dir = tmp_path / "output"
    monkeypatch.setattr(
        holdout,
        "_predict_catboost",
        lambda train, test, features: [0.25 for _ in test],
    )
    arguments = [
        "evaluate-replay-state-tournament-holdout",
        "--training-dir",
        str(training_dir),
        "--trajectory-dir",
        str(trajectory_dir),
        "--train-league-id",
        "20",
        "--train-league-id",
        "10",
        "--test-league-id",
        "30",
        "--output-dir",
        str(output_dir),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    prediction_bytes = (output_dir / holdout.PREDICTIONS_FILENAME).read_bytes()
    evaluation_bytes = (output_dir / holdout.EVALUATION_FILENAME).read_bytes()

    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"
    assert (output_dir / holdout.PREDICTIONS_FILENAME).read_bytes() == prediction_bytes
    assert (output_dir / holdout.EVALUATION_FILENAME).read_bytes() == evaluation_bytes

    prediction_rows = _read_csv(output_dir / holdout.PREDICTIONS_FILENAME)
    keys = [
        (int(row["match_id"]), int(row["game_time_seconds"]))
        for row in prediction_rows
    ]
    assert keys == sorted(keys)
    assert list(prediction_rows[0]) == list(holdout.PREDICTION_COLUMNS)
    assert {int(row["league_id"]) for row in prediction_rows} == {30}
    evaluation = json.loads(evaluation_bytes)
    assert evaluation["prediction_file"] == {
        "filename": holdout.PREDICTIONS_FILENAME,
        "row_count": len(prediction_rows),
        "sha256": hashlib.sha256(prediction_bytes).hexdigest(),
    }
    assert evaluation["lineage_sha256"] == holdout._lineage_sha256(
        ((100, 10), (200, 20), (300, 30))
    )


def _write_standard_fixture(
    root: Path, *, reverse: bool = False
) -> tuple[Path, Path]:
    rows = [
        _training_row(100, 599, target=1, momentum=0),
        _training_row(100, 600, target=3, momentum=1),
        _training_row(200, 1199, target=5, momentum=2),
        _training_row(200, 1200, target=7, momentum=3),
        _training_row(300, 599, target=2, momentum=1),
        _training_row(300, 600, target=-2, momentum=-1),
        _training_row(300, 1199, target=0, momentum=0),
        _training_row(300, 1200, target=4, momentum=2),
        _training_row(300, 1799, target=-4, momentum=-2),
        _training_row(300, 1800, target=6, momentum=3),
        _training_row(400, 1800, target=99, momentum=99),
    ]
    if reverse:
        rows.reverse()
    training_dir = root / "training"
    trajectory_dir = root / "trajectories"
    training_dir.mkdir()
    trajectory_dir.mkdir()
    _write_training_rows(training_dir, rows)
    for match_id, league_id in ((100, 10), (200, 20), (300, 30), (400, 40)):
        _write_trajectory(trajectory_dir / f"{match_id}.json", match_id, league_id)
    return training_dir, trajectory_dir


def _training_row(
    match_id: int, game_time_seconds: int, *, target: int, momentum: int
) -> dict[str, object]:
    row: dict[str, object] = {}
    for index, column in enumerate(TRAINING_COLUMNS):
        if column == "match_id":
            value: object = match_id
        elif column == "game_time_seconds":
            value = game_time_seconds
        elif column == holdout.TARGET_COLUMN:
            value = target
        elif column == holdout.MOMENTUM_COLUMN:
            value = momentum
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
    (directory / holdout.TRAINING_FILENAME).write_bytes(training_bytes)
    manifest = {
        "schema_version": 1,
        "training_row_count": len(rows),
        "training_file": {
            "filename": holdout.TRAINING_FILENAME,
            "row_count": len(rows),
            "sha256": hashlib.sha256(training_bytes).hexdigest(),
        },
    }
    _write_json(directory / holdout.MANIFEST_FILENAME, manifest)


def _rewrite_training(
    directory: Path, change: Callable[[list[dict[str, str]]], None]
) -> None:
    rows = _read_csv(directory / holdout.TRAINING_FILENAME)
    change(rows)
    _write_training_rows(directory, rows)


def _write_trajectory(
    path: Path, match_id: int, league_id: int
) -> None:
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
