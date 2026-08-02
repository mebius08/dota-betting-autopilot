from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, cast

import pytest

from app import cli
from app.replay_state_training import (
    MANIFEST_FILENAME,
    REQUIRED_LAGS_SECONDS,
    TRAINING_COLUMNS,
    TRAINING_FILENAME,
    ReplayStateTrainingError,
    build_replay_state_training_set,
)
from app.replay_trajectory_dataset import DECISION_COLUMNS, OUTCOME_COLUMNS


def test_exact_features_targets_radiant_only_totals_and_no_leakage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset_dir = _write_dataset(tmp_path / "dataset", {200: range(0, 361, 60)})
    output_dir = tmp_path / "output"

    exit_code = cli.main(
        [
            "build-replay-state-training-set",
            "--dataset-dir",
            str(dataset_dir),
            "--output-dir",
            str(output_dir),
        ]
    )
    captured = capsys.readouterr()
    rows = _read_training(output_dir)

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out.startswith("BUILT matches=1 snapshots=7 decisions=14 ")
    assert len(rows) == 1
    row = rows[0]
    assert (int(row["match_id"]), int(row["game_time_seconds"])) == (200, 180)

    current = _radiant_state(200, 180)
    for lag in REQUIRED_LAGS_SECONDS:
        previous = _radiant_state(200, 180 - lag)
        for column in (
            "net_worth_diff",
            "total_xp_diff",
            "kill_diff",
            "last_hit_diff",
        ):
            assert int(row[f"{column}_change_prev_{lag}"]) == (
                current[column] - previous[column]
            )

    future = _radiant_state(200, 360)
    assert int(row["target_net_worth_diff_change_180"]) == (
        future["net_worth_diff"] - current["net_worth_diff"]
    )
    assert int(row["target_total_xp_diff_change_180"]) == (
        future["total_xp_diff"] - current["total_xp_diff"]
    )
    assert int(row["target_kill_diff_change_180"]) == (
        future["kill_diff"] - current["kill_diff"]
    )
    for total_column, stem in (
        ("total_kills", "kills"),
        ("total_last_hits", "last_hits"),
        ("total_denies", "denies"),
        ("total_net_worth", "net_worth"),
        ("total_current_gold", "current_gold"),
        ("total_xp", "total_xp"),
    ):
        assert int(row[total_column]) == (
            current[f"team_{stem}"] + current[f"opponent_{stem}"]
        )

    header = tuple(rows[0])
    assert header == TRAINING_COLUMNS
    forbidden = {
        "team_won",
        "team_id",
        "team_tag",
        "opponent_team_id",
        "opponent_team_tag",
        "replay_sha256",
        "league_id",
        "game_mode",
        "team",
    }
    assert not (set(header) & forbidden)
    assert not [column for column in header if column.startswith("future_")]
    assert [column for column in header if column.startswith("target_")] == [
        "target_net_worth_diff_change_180",
        "target_total_xp_diff_change_180",
        "target_kill_diff_change_180",
    ]


@pytest.mark.parametrize(
    "times",
    [
        [0, 61, 120, 180, 360],
        [0, 60, 180, 360],
    ],
    ids=["no-interpolation", "missing-lag"],
)
def test_missing_exact_lags_are_excluded(
    tmp_path: Path,
    times: list[int],
) -> None:
    dataset_dir = _write_dataset(tmp_path / "dataset", {200: times})

    result = build_replay_state_training_set(dataset_dir, tmp_path / "output")

    assert result.training_row_count == 0
    assert _read_training(tmp_path / "output") == []


def test_lag_lookup_never_crosses_match_boundaries(tmp_path: Path) -> None:
    dataset_dir = _write_dataset(
        tmp_path / "dataset",
        {
            100: [180, 360],
            200: [0, 60, 120],
        },
    )

    result = build_replay_state_training_set(dataset_dir, tmp_path / "output")

    assert result.training_row_count == 0


@pytest.mark.parametrize(
    "filename",
    ["team_decisions.csv", "team_outcomes.csv"],
)
def test_duplicate_source_keys_are_rejected(
    tmp_path: Path,
    filename: str,
) -> None:
    dataset_dir = _write_dataset(tmp_path / "dataset", {200: range(0, 361, 60)})
    _rewrite_csv(dataset_dir, filename, lambda rows: [*rows, rows[0]])

    with pytest.raises(ReplayStateTrainingError, match="Duplicate .* key"):
        build_replay_state_training_set(dataset_dir, tmp_path / "output")


def test_mismatched_outcome_decision_key_is_rejected(tmp_path: Path) -> None:
    dataset_dir = _write_dataset(tmp_path / "dataset", {200: range(0, 361, 60)})

    def change_match(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        rows[0]["match_id"] = "999"
        return rows

    _rewrite_csv(dataset_dir, "team_outcomes.csv", change_match)

    with pytest.raises(
        ReplayStateTrainingError,
        match="no matching decision key",
    ):
        build_replay_state_training_set(dataset_dir, tmp_path / "output")


@pytest.mark.parametrize("kind", ["hash", "count"])
def test_source_manifest_hash_and_count_mismatches_are_rejected(
    tmp_path: Path,
    kind: str,
) -> None:
    dataset_dir = _write_dataset(tmp_path / "dataset", {200: range(0, 361, 60)})
    manifest_path = dataset_dir / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if kind == "hash":
        manifest["decision_file"]["sha256"] = "0" * 64
    else:
        manifest["decision_row_count"] += 1
        manifest["decision_file"]["row_count"] += 1
    _write_json(manifest_path, manifest)

    with pytest.raises(ReplayStateTrainingError, match=f"Source .*{kind}"):
        build_replay_state_training_set(dataset_dir, tmp_path / "output")


def test_training_rows_are_deterministically_ordered(tmp_path: Path) -> None:
    dataset_dir = _write_dataset(
        tmp_path / "dataset",
        {
            900: range(0, 421, 60),
            100: range(0, 421, 60),
        },
        reverse_rows=True,
    )

    build_replay_state_training_set(dataset_dir, tmp_path / "output")
    keys = [
        (int(row["match_id"]), int(row["game_time_seconds"]))
        for row in _read_training(tmp_path / "output")
    ]

    assert keys == [(100, 180), (100, 240), (900, 180), (900, 240)]


def test_byte_identical_rerun_reports_unchanged(tmp_path: Path) -> None:
    dataset_dir = _write_dataset(tmp_path / "dataset", {200: range(0, 361, 60)})
    output_dir = tmp_path / "output"

    first = build_replay_state_training_set(dataset_dir, output_dir)
    before = {
        filename: (output_dir / filename).read_bytes()
        for filename in (TRAINING_FILENAME, MANIFEST_FILENAME)
    }
    second = build_replay_state_training_set(dataset_dir, output_dir)

    assert first.status == "BUILT"
    assert second.status == "UNCHANGED"
    assert {
        filename: (output_dir / filename).read_bytes()
        for filename in (TRAINING_FILENAME, MANIFEST_FILENAME)
    } == before


def test_output_manifest_hashes_counts_and_line_endings(tmp_path: Path) -> None:
    dataset_dir = _write_dataset(tmp_path / "dataset", {200: range(0, 361, 60)})
    source_manifest_bytes = (dataset_dir / MANIFEST_FILENAME).read_bytes()
    output_dir = tmp_path / "output"

    result = build_replay_state_training_set(dataset_dir, output_dir)
    training_bytes = (output_dir / TRAINING_FILENAME).read_bytes()
    manifest = json.loads((output_dir / MANIFEST_FILENAME).read_bytes())

    assert result.training_row_count == 1
    assert manifest == {
        "schema_version": 1,
        "source_manifest_sha256": hashlib.sha256(
            source_manifest_bytes
        ).hexdigest(),
        "source_match_count": 1,
        "source_snapshot_count": 7,
        "source_decision_row_count": 14,
        "source_outcome_row_count": 8,
        "required_horizon_seconds": 180,
        "required_lags_seconds": [60, 120, 180],
        "training_row_count": 1,
        "training_file": {
            "filename": TRAINING_FILENAME,
            "row_count": 1,
            "sha256": hashlib.sha256(training_bytes).hexdigest(),
        },
    }
    assert training_bytes.endswith(b"\n")
    assert b"\r\n" not in training_bytes
    assert "timestamp" not in manifest
    assert not _contains_absolute_path(manifest)


def _write_dataset(
    dataset_dir: Path,
    matches: dict[int, range | list[int]],
    *,
    reverse_rows: bool = False,
) -> Path:
    decisions: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    decisions_by_key: dict[tuple[int, int, str], dict[str, object]] = {}
    for match_id, times_value in matches.items():
        times = list(times_value)
        for game_time in times:
            for team in ("RADIANT", "DIRE"):
                row = _decision_row(match_id, game_time, team)
                decisions.append(row)
                decisions_by_key[(match_id, game_time, team)] = row
        for game_time in times:
            future_time = game_time + 180
            if future_time not in times:
                continue
            for team in ("RADIANT", "DIRE"):
                outcomes.append(
                    _outcome_row(
                        decisions_by_key[(match_id, game_time, team)],
                        decisions_by_key[(match_id, future_time, team)],
                    )
                )
    if reverse_rows:
        decisions.reverse()
        outcomes.reverse()

    dataset_dir.mkdir(parents=True)
    decision_bytes = _csv_bytes(DECISION_COLUMNS, decisions)
    outcome_bytes = _csv_bytes(OUTCOME_COLUMNS, outcomes)
    (dataset_dir / "team_decisions.csv").write_bytes(decision_bytes)
    (dataset_dir / "team_outcomes.csv").write_bytes(outcome_bytes)
    manifest = {
        "schema_version": 1,
        "horizons_seconds": [180],
        "match_count": len(matches),
        "snapshot_count": sum(len(times) for times in matches.values()),
        "decision_row_count": len(decisions),
        "outcome_row_count": len(outcomes),
        "decision_file": {
            "filename": "team_decisions.csv",
            "row_count": len(decisions),
            "sha256": hashlib.sha256(decision_bytes).hexdigest(),
        },
        "outcome_file": {
            "filename": "team_outcomes.csv",
            "row_count": len(outcomes),
            "sha256": hashlib.sha256(outcome_bytes).hexdigest(),
        },
    }
    _write_json(dataset_dir / MANIFEST_FILENAME, manifest)
    return dataset_dir


def _decision_row(match_id: int, game_time: int, team: str) -> dict[str, object]:
    radiant = _team_values(match_id, game_time, "RADIANT")
    dire = _team_values(match_id, game_time, "DIRE")
    team_values, opponent_values = (radiant, dire) if team == "RADIANT" else (dire, radiant)
    row: dict[str, object] = {
        "match_id": match_id,
        "replay_sha256": "a" * 64,
        "game_mode": 2,
        "league_id": 19785,
        "game_time_seconds": game_time,
        "team": team,
        "team_id": 1 if team == "RADIANT" else 2,
        "team_tag": "RAD" if team == "RADIANT" else "DIRE",
        "opponent_team": "DIRE" if team == "RADIANT" else "RADIANT",
        "opponent_team_id": 2 if team == "RADIANT" else 1,
        "opponent_team_tag": "DIRE" if team == "RADIANT" else "RAD",
    }
    diff_names = {
        "kills": "kill_diff",
        "deaths": "death_diff",
        "assists": "assist_diff",
        "last_hits": "last_hit_diff",
        "denies": "deny_diff",
        "net_worth": "net_worth_diff",
        "current_gold": "current_gold_diff",
        "total_xp": "total_xp_diff",
    }
    for stem, diff_column in diff_names.items():
        row[f"team_{stem}"] = team_values[stem]
        row[f"opponent_{stem}"] = opponent_values[stem]
        row[diff_column] = team_values[stem] - opponent_values[stem]
    return row


def _team_values(match_id: int, game_time: int, team: str) -> dict[str, int]:
    minute = game_time // 60
    match_bias = match_id % 17
    if team == "RADIANT":
        return {
            "kills": 3 + minute * minute + match_bias,
            "deaths": 2 + minute,
            "assists": 4 + minute * 3,
            "last_hits": 20 + minute * minute * 4 + match_bias,
            "denies": 2 + minute * 2,
            "net_worth": 3000 + minute * minute * 150 + match_bias,
            "current_gold": 300 + minute * 23,
            "total_xp": 1000 + minute * minute * 110 + match_bias,
        }
    return {
        "kills": 2 + minute * 2,
        "deaths": 3 + minute * 2,
        "assists": 3 + minute * 2,
        "last_hits": 18 + minute * 11,
        "denies": 3 + minute,
        "net_worth": 2900 + minute * 125,
        "current_gold": 250 + minute * 19,
        "total_xp": 900 + minute * 105,
    }


def _outcome_row(
    current: dict[str, object],
    future: dict[str, object],
) -> dict[str, object]:
    row: dict[str, object] = {
        "match_id": current["match_id"],
        "game_time_seconds": current["game_time_seconds"],
        "team": current["team"],
        "horizon_seconds": 180,
        "future_game_time_seconds": future["game_time_seconds"],
    }
    diff_names = {
        "kills": "kill_diff",
        "deaths": "death_diff",
        "assists": "assist_diff",
        "last_hits": "last_hit_diff",
        "denies": "deny_diff",
        "net_worth": "net_worth_diff",
        "current_gold": "current_gold_diff",
        "total_xp": "total_xp_diff",
    }
    for stem, diff_column in diff_names.items():
        row[f"future_team_{stem}"] = future[f"team_{stem}"]
        row[f"future_opponent_{stem}"] = future[f"opponent_{stem}"]
        row[f"future_{diff_column}"] = future[diff_column]
        row[f"{diff_column}_change"] = cast(int, future[diff_column]) - cast(
            int, current[diff_column]
        )
    row["team_won"] = "true" if current["team"] == "RADIANT" else "false"
    return row


def _radiant_state(match_id: int, game_time: int) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in _decision_row(match_id, game_time, "RADIANT").items()
        if isinstance(value, int)
    }


def _csv_bytes(
    columns: tuple[str, ...],
    rows: Sequence[Mapping[str, object]],
) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=",", lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row[column] for column in columns)
    return stream.getvalue().encode("utf-8")


def _rewrite_csv(
    dataset_dir: Path,
    filename: str,
    change: Callable[[list[dict[str, str]]], list[dict[str, str]]],
) -> None:
    path = dataset_dir / filename
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        columns = tuple(reader.fieldnames or ())
        rows = list(reader)
    changed = change(rows)
    content = _csv_bytes(columns, changed)
    path.write_bytes(content)

    manifest_path = dataset_dir / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prefix = "decision" if filename == "team_decisions.csv" else "outcome"
    manifest[f"{prefix}_row_count"] = len(changed)
    manifest[f"{prefix}_file"]["row_count"] = len(changed)
    manifest[f"{prefix}_file"]["sha256"] = hashlib.sha256(content).hexdigest()
    _write_json(manifest_path, manifest)


def _read_training(output_dir: Path) -> list[dict[str, str]]:
    with (output_dir / TRAINING_FILENAME).open(
        encoding="utf-8", newline=""
    ) as file:
        return list(csv.DictReader(file))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _contains_absolute_path(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    if isinstance(value, str):
        return ":\\" in value or value.startswith("/")
    return False
