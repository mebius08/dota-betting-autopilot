from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app import cli
from app import replay_trajectory_dataset as dataset_module
from app.replay_trajectory_dataset import (
    DECISION_COLUMNS,
    HORIZONS_SECONDS,
    MANIFEST_FILENAME,
    OUTCOME_COLUMNS,
    ReplayTrajectoryDatasetError,
    build_replay_trajectory_dataset,
)


AGGREGATE_FIELDS = (
    "kills",
    "deaths",
    "assists",
    "last_hits",
    "denies",
    "net_worth",
    "current_gold",
    "total_xp",
)
DIFF_STEMS = (
    "kill",
    "death",
    "assist",
    "last_hit",
    "deny",
    "net_worth",
    "current_gold",
    "total_xp",
)
OUTPUT_NAMES = {
    "team_decisions.csv",
    "team_outcomes.csv",
    MANIFEST_FILENAME,
}


def test_valid_deterministic_dataset_build(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_trajectory(input_dir, _trajectory())

    exit_code = cli.main(
        [
            "build-replay-trajectory-dataset",
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out.startswith(
        "BUILT matches=1 snapshots=7 decisions=14 outcomes=22 "
    )
    assert {path.name for path in output_dir.iterdir()} == OUTPUT_NAMES
    for path in output_dir.iterdir():
        content = path.read_bytes()
        assert content.endswith(b"\n")
        assert b"\r\n" not in content


def test_each_snapshot_produces_exactly_two_decision_rows(tmp_path: Path) -> None:
    _, decisions, _ = _build_and_read(tmp_path)

    keys = {
        (row["match_id"], row["game_time_seconds"])
        for row in decisions
    }
    assert len(decisions) == len(keys) * 2
    for key in keys:
        teams = {
            row["team"]
            for row in decisions
            if (row["match_id"], row["game_time_seconds"]) == key
        }
        assert teams == {"RADIANT", "DIRE"}


def test_outcomes_use_only_required_horizons(tmp_path: Path) -> None:
    _, _, outcomes = _build_and_read(tmp_path)

    assert {int(row["horizon_seconds"]) for row in outcomes} == set(
        HORIZONS_SECONDS
    )
    assert sum(row["horizon_seconds"] == "120" for row in outcomes) == 10
    assert sum(row["horizon_seconds"] == "180" for row in outcomes) == 8
    assert sum(row["horizon_seconds"] == "300" for row in outcomes) == 4


def test_no_outcome_without_exact_future_snapshot(tmp_path: Path) -> None:
    _, _, outcomes = _build_and_read(tmp_path)

    assert not [
        row
        for row in outcomes
        if row["game_time_seconds"] in {"300", "360"}
    ]
    for row in outcomes:
        assert int(row["future_game_time_seconds"]) == (
            int(row["game_time_seconds"]) + int(row["horizon_seconds"])
        )


def test_team_and_opponent_orientation_is_correct(tmp_path: Path) -> None:
    _, decisions, _ = _build_and_read(tmp_path)
    radiant = decisions[0]
    dire = decisions[1]

    assert radiant["team"] == "RADIANT"
    assert radiant["team_id"] == "101"
    assert radiant["team_tag"] == "RAD"
    assert radiant["opponent_team"] == "DIRE"
    assert radiant["opponent_team_id"] == "202"
    assert radiant["opponent_team_tag"] == "DIRE"
    assert dire["team"] == "DIRE"
    assert dire["team_kills"] == radiant["opponent_kills"]
    assert dire["opponent_kills"] == radiant["team_kills"]


def test_decision_differences_are_mirrored_negatives(tmp_path: Path) -> None:
    _, decisions, _ = _build_and_read(tmp_path)

    by_time: dict[str, dict[str, dict[str, str]]] = {}
    for row in decisions:
        by_time.setdefault(row["game_time_seconds"], {})[row["team"]] = row
    for rows in by_time.values():
        for stem in DIFF_STEMS:
            column = f"{stem}_diff"
            assert int(rows["RADIANT"][column]) == -int(rows["DIRE"][column])


def test_outcome_changes_equal_future_minus_current(tmp_path: Path) -> None:
    _, decisions, outcomes = _build_and_read(tmp_path)
    decisions_by_key = {
        (row["match_id"], row["game_time_seconds"], row["team"]): row
        for row in decisions
    }

    for outcome in outcomes:
        decision = decisions_by_key[
            (
                outcome["match_id"],
                outcome["game_time_seconds"],
                outcome["team"],
            )
        ]
        for stem in DIFF_STEMS:
            assert int(outcome[f"{stem}_diff_change"]) == (
                int(outcome[f"future_{stem}_diff"])
                - int(decision[f"{stem}_diff"])
            )


def test_team_won_is_outcome_only_and_mirrored(tmp_path: Path) -> None:
    _, _, outcomes = _build_and_read(tmp_path)
    by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in outcomes:
        key = (
            row["match_id"],
            row["game_time_seconds"],
            row["horizon_seconds"],
        )
        by_key.setdefault(key, {})[row["team"]] = row["team_won"]

    assert by_key
    for team_won in by_key.values():
        assert team_won == {"RADIANT": "true", "DIRE": "false"}


def test_decision_schema_contains_no_leakage_columns(tmp_path: Path) -> None:
    output_dir, decisions, _ = _build_and_read(tmp_path)
    header = next(csv.reader((output_dir / "team_decisions.csv").open()))
    forbidden = {
        "winner",
        "team_won",
        "horizon_seconds",
        "game_end_time_seconds",
        "remaining_game_time",
        "last_snapshot_indicator",
    }

    assert tuple(header) == DECISION_COLUMNS
    assert not (set(header) & forbidden)
    assert not [column for column in header if column.startswith("future_")]
    assert all(set(row) == set(DECISION_COLUMNS) for row in decisions)


def test_duplicate_match_id_is_rejected_even_for_identical_content(
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    payload = _trajectory()
    _write_trajectory(input_dir, payload)
    _write_json(input_dir / "copy.json", payload)

    with pytest.raises(
        ReplayTrajectoryDatasetError,
        match="Duplicate match_id 123456",
    ):
        build_replay_trajectory_dataset(input_dir, tmp_path / "output")


def test_filename_must_match_source_match_id(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    _write_json(input_dir / "wrong.json", _trajectory())

    with pytest.raises(
        ReplayTrajectoryDatasetError,
        match="does not match canonical filename 123456.json",
    ):
        build_replay_trajectory_dataset(input_dir, tmp_path / "output")


def test_output_is_independent_of_input_creation_order(tmp_path: Path) -> None:
    first_input = tmp_path / "first-input"
    second_input = tmp_path / "second-input"
    low = _trajectory(match_id=111111, replay_character="a")
    high = _trajectory(match_id=999999, replay_character="b")
    _write_trajectory(first_input, high)
    _write_trajectory(first_input, low)
    _write_trajectory(second_input, low)
    _write_trajectory(second_input, high)

    first_output = tmp_path / "first-output"
    second_output = tmp_path / "second-output"
    build_replay_trajectory_dataset(first_input, first_output)
    build_replay_trajectory_dataset(second_input, second_output)

    for filename in OUTPUT_NAMES:
        assert (first_output / filename).read_bytes() == (
            second_output / filename
        ).read_bytes()


def test_byte_identical_rerun_reports_unchanged(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_trajectory(input_dir, _trajectory())

    first = build_replay_trajectory_dataset(input_dir, output_dir)
    before = {name: (output_dir / name).read_bytes() for name in OUTPUT_NAMES}
    second = build_replay_trajectory_dataset(input_dir, output_dir)

    assert first.status == "BUILT"
    assert second.status == "UNCHANGED"
    assert {name: (output_dir / name).read_bytes() for name in OUTPUT_NAMES} == before


def test_failed_staged_write_preserves_existing_dataset_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_trajectory(input_dir, _trajectory())
    build_replay_trajectory_dataset(input_dir, output_dir)
    before = {name: (output_dir / name).read_bytes() for name in OUTPUT_NAMES}
    _write_trajectory(input_dir, _trajectory(winner=3))
    real_write = dataset_module._write_durable
    write_count = 0

    def fail_on_manifest(path: Path, content: bytes) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 3:
            raise OSError("simulated staged write failure")
        real_write(path, content)

    monkeypatch.setattr(dataset_module, "_write_durable", fail_on_manifest)

    with pytest.raises(
        ReplayTrajectoryDatasetError,
        match="Could not atomically replace",
    ):
        build_replay_trajectory_dataset(input_dir, output_dir)

    assert {name: (output_dir / name).read_bytes() for name in OUTPUT_NAMES} == before
    assert not [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(".output.transaction.")
    ]


def test_invalid_source_is_rejected_by_shared_importer_validator(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_dir = tmp_path / "input"
    payload = _trajectory()
    payload["clock_normalization_status"] = "UNPROVEN"
    _write_trajectory(input_dir, payload)

    exit_code = cli.main(
        [
            "build-replay-trajectory-dataset",
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("REJECTED Invalid source artifact")
    assert "clock_normalization_status must be PROVEN" in captured.err
    assert "Traceback" not in captured.err


def test_manifest_hashes_and_row_counts_match_outputs(tmp_path: Path) -> None:
    output_dir, decisions, outcomes = _build_and_read(tmp_path)
    manifest_bytes = (output_dir / MANIFEST_FILENAME).read_bytes()
    manifest = json.loads(manifest_bytes)
    decision_bytes = (output_dir / "team_decisions.csv").read_bytes()
    outcome_bytes = (output_dir / "team_outcomes.csv").read_bytes()

    assert manifest["schema_version"] == 1
    assert manifest["source_schema_version"] == 1
    assert manifest["horizons_seconds"] == [120, 180, 300]
    assert manifest["match_count"] == 1
    assert manifest["match_ids"] == [123456]
    assert manifest["snapshot_count"] == 7
    assert manifest["decision_row_count"] == len(decisions) == 14
    assert manifest["outcome_row_count"] == len(outcomes) == 22
    assert manifest["decision_file"] == {
        "filename": "team_decisions.csv",
        "row_count": 14,
        "sha256": hashlib.sha256(decision_bytes).hexdigest(),
    }
    assert manifest["outcome_file"] == {
        "filename": "team_outcomes.csv",
        "row_count": 22,
        "sha256": hashlib.sha256(outcome_bytes).hexdigest(),
    }
    assert tuple(next(csv.reader(outcome_bytes.decode().splitlines()))) == (
        OUTCOME_COLUMNS
    )


def _build_and_read(
    tmp_path: Path,
) -> tuple[Path, list[dict[str, str]], list[dict[str, str]]]:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_trajectory(input_dir, _trajectory())
    build_replay_trajectory_dataset(input_dir, output_dir)
    with (output_dir / "team_decisions.csv").open(
        encoding="utf-8", newline=""
    ) as file:
        decisions = list(csv.DictReader(file))
    with (output_dir / "team_outcomes.csv").open(
        encoding="utf-8", newline=""
    ) as file:
        outcomes = list(csv.DictReader(file))
    return output_dir, decisions, outcomes


def _trajectory(
    *,
    match_id: int = 123456,
    replay_character: str = "a",
    winner: int = 2,
) -> dict[str, Any]:
    snapshots = [_snapshot(minute) for minute in range(7)]
    return {
        "schema_version": 1,
        "match_id": match_id,
        "replay_sha256": replay_character * 64,
        "clarity_version": "4.0.1",
        "game_mode": 2,
        "league_id": 19785,
        "winner": winner,
        "radiant_team_id": 101,
        "radiant_team_tag": "RAD",
        "dire_team_id": 202,
        "dire_team_tag": "DIRE",
        "picks_bans": [
            {"is_pick": False, "team": 2, "hero_id": 1},
            {"is_pick": True, "team": 3, "hero_id": 2},
        ],
        "clock_normalization_status": "PROVEN",
        "clock_normalization_method": "GAME_START_TIME_AND_PAUSE_TICKS",
        "zero_replay_tick": 1000,
        "game_end_time_seconds": 405.0,
        "pause_ticks": 0,
        "pause_seconds": 0.0,
        "pause_witnessed": False,
        "snapshots": snapshots,
    }


def _snapshot(minute: int) -> dict[str, Any]:
    players = [_player(slot, minute) for slot in reversed(range(10))]
    radiant = [player for player in players if player["team"] == "RADIANT"]
    dire = [player for player in players if player["team"] == "DIRE"]
    return {
        "game_time_seconds": minute * 60,
        "source_game_time_seconds": float(minute * 60),
        "replay_tick": 1000 + minute * 1800,
        "game_state": 5,
        "teams": [_team("DIRE", dire), _team("RADIANT", radiant)],
        "players": players,
    }


def _player(slot: int, minute: int) -> dict[str, Any]:
    radiant = slot < 5
    return {
        "player_slot": slot,
        "team": "RADIANT" if radiant else "DIRE",
        "hero_id": slot + 1,
        "hero_name": f"hero_{slot + 1}",
        "level": minute + 1,
        "kills": minute * (2 if radiant else 1) + slot % 2,
        "deaths": minute * (1 if radiant else 2) + slot % 2,
        "assists": minute * (3 if radiant else 1) + slot,
        "last_hits": minute * (10 if radiant else 8) + slot,
        "denies": minute * (2 if radiant else 1) + slot % 3,
        "net_worth": 500 + minute * (100 if radiant else 80) + slot,
        "current_gold": 100 + minute * (20 if radiant else 15) + slot,
        "total_xp": 200 + minute * (90 if radiant else 70) + slot,
        "items": [],
    }


def _team(name: str, players: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "team": name,
        **{
            field: sum(player[field] for player in players)
            for field in AGGREGATE_FIELDS
        },
    }


def _write_trajectory(input_dir: Path, payload: dict[str, Any]) -> Path:
    path = input_dir / f"{payload['match_id']}.json"
    _write_json(path, payload)
    return path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
