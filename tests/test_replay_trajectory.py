from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from app import cli
from app.replay_trajectory import ReplayTrajectoryError, import_replay_trajectory


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


def test_valid_compact_trajectory_import(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = _write_input(tmp_path, _trajectory())
    destination_dir = tmp_path / "accepted"

    exit_code = cli.main(
        [
            "import-replay-trajectory",
            "--input",
            str(input_path),
            "--local-data-dir",
            str(destination_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "IMPORTED match_id=123456 snapshots=2" in captured.out
    stored = json.loads((destination_dir / "123456.json").read_text("utf-8"))
    assert stored["dire_team_id"] is None
    assert stored["dire_team_tag"] is None


def test_second_import_is_idempotent(tmp_path: Path) -> None:
    input_path = _write_input(tmp_path, _trajectory())
    destination_dir = tmp_path / "accepted"

    first = import_replay_trajectory(input_path, destination_dir)
    second = import_replay_trajectory(input_path, destination_dir)

    assert first.status == "IMPORTED"
    assert second.status == "UNCHANGED"
    assert first.artifact_size_bytes == second.artifact_size_bytes


def test_conflicting_replay_sha256_is_rejected(tmp_path: Path) -> None:
    original = _trajectory()
    conflict = deepcopy(original)
    conflict["replay_sha256"] = "b" * 64
    destination_dir = tmp_path / "accepted"
    first_path = _write_input(tmp_path, original, "first.json")
    conflict_path = _write_input(tmp_path, conflict, "conflict.json")
    import_replay_trajectory(first_path, destination_dir)
    original_bytes = (destination_dir / "123456.json").read_bytes()

    with pytest.raises(ReplayTrajectoryError, match="different replay_sha256"):
        import_replay_trajectory(conflict_path, destination_dir)

    assert (destination_dir / "123456.json").read_bytes() == original_bytes


def test_same_sha256_with_conflicting_content_is_rejected(tmp_path: Path) -> None:
    original = _trajectory()
    conflict = deepcopy(original)
    conflict["clarity_version"] = "different"
    destination_dir = tmp_path / "accepted"
    first_path = _write_input(tmp_path, original, "first.json")
    conflict_path = _write_input(tmp_path, conflict, "conflict.json")
    import_replay_trajectory(first_path, destination_dir)
    original_bytes = (destination_dir / "123456.json").read_bytes()

    with pytest.raises(
        ReplayTrajectoryError,
        match="same replay_sha256 but different canonical content",
    ):
        import_replay_trajectory(conflict_path, destination_dir)

    assert (destination_dir / "123456.json").read_bytes() == original_bytes


def test_unproven_clock_normalization_is_rejected_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _trajectory()
    payload["clock_normalization_status"] = "UNPROVEN"
    input_path = _write_input(tmp_path, payload)

    exit_code = cli.main(
        [
            "import-replay-trajectory",
            "--input",
            str(input_path),
            "--local-data-dir",
            str(tmp_path / "accepted"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("REJECTED ")
    assert "clock_normalization_status must be PROVEN" in captured.err
    assert "Traceback" not in captured.err


def test_invalid_minute_grid_is_rejected(tmp_path: Path) -> None:
    payload = _trajectory()
    payload["snapshots"][1]["game_time_seconds"] = 61
    payload["snapshots"][1]["source_game_time_seconds"] = 61.0

    with pytest.raises(ReplayTrajectoryError, match="must be 60"):
        import_replay_trajectory(
            _write_input(tmp_path, payload), tmp_path / "accepted"
        )


def test_excessive_clock_deviation_is_rejected(tmp_path: Path) -> None:
    payload = _trajectory()
    payload["snapshots"][1]["source_game_time_seconds"] = 60.02

    with pytest.raises(ReplayTrajectoryError, match="clock deviation exceeds"):
        import_replay_trajectory(
            _write_input(tmp_path, payload), tmp_path / "accepted"
        )


def test_duplicate_player_slot_is_rejected(tmp_path: Path) -> None:
    payload = _trajectory()
    players = payload["snapshots"][0]["players"]
    players[1]["player_slot"] = players[0]["player_slot"]

    with pytest.raises(ReplayTrajectoryError, match="duplicate player_slot"):
        import_replay_trajectory(
            _write_input(tmp_path, payload), tmp_path / "accepted"
        )


def test_incorrect_five_versus_five_composition_is_rejected(
    tmp_path: Path,
) -> None:
    payload = _trajectory()
    dire_player = next(
        player
        for player in payload["snapshots"][0]["players"]
        if player["team"] == "DIRE"
    )
    dire_player["team"] = "RADIANT"

    with pytest.raises(ReplayTrajectoryError, match="five RADIANT and five DIRE"):
        import_replay_trajectory(
            _write_input(tmp_path, payload), tmp_path / "accepted"
        )


def test_team_aggregate_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = _trajectory()
    payload["snapshots"][0]["teams"][0]["kills"] += 1

    with pytest.raises(ReplayTrajectoryError, match="kills must equal the player sum"):
        import_replay_trajectory(
            _write_input(tmp_path, payload), tmp_path / "accepted"
        )


def test_duplicate_item_slot_is_rejected(tmp_path: Path) -> None:
    payload = _trajectory()
    items = payload["snapshots"][0]["players"][0]["items"]
    items[1]["inventory_slot"] = items[0]["inventory_slot"]

    with pytest.raises(ReplayTrajectoryError, match="duplicate inventory_slot"):
        import_replay_trajectory(
            _write_input(tmp_path, payload), tmp_path / "accepted"
        )


def test_non_finite_numeric_input_is_rejected(tmp_path: Path) -> None:
    payload = _trajectory()
    payload["snapshots"][0]["players"][0]["net_worth"] = float("nan")

    with pytest.raises(ReplayTrajectoryError, match="Non-finite JSON number"):
        import_replay_trajectory(
            _write_input(tmp_path, payload), tmp_path / "accepted"
        )


def test_canonical_output_is_deterministic(tmp_path: Path) -> None:
    first_payload = _trajectory()
    second_payload = deepcopy(first_payload)
    second_payload = dict(reversed(list(second_payload.items())))
    for snapshot in second_payload["snapshots"]:
        snapshot["teams"].sort(key=lambda team: team["team"])
        snapshot["players"].sort(key=lambda player: player["player_slot"])
        for player in snapshot["players"]:
            player["items"].sort(key=lambda item: item["inventory_slot"])

    first_path = _write_input(tmp_path, first_payload, "first.json")
    second_path = _write_input(tmp_path, second_payload, "second.json")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    import_replay_trajectory(first_path, first_dir)
    import_replay_trajectory(second_path, second_dir)

    first_bytes = (first_dir / "123456.json").read_bytes()
    second_bytes = (second_dir / "123456.json").read_bytes()
    assert first_bytes == second_bytes
    assert first_bytes.endswith(b"\n")

    stored = json.loads(first_bytes)
    assert [team["team"] for team in stored["snapshots"][0]["teams"]] == [
        "RADIANT",
        "DIRE",
    ]
    assert [
        player["player_slot"] for player in stored["snapshots"][0]["players"]
    ] == list(range(10))
    assert [
        item["inventory_slot"]
        for item in stored["snapshots"][0]["players"][0]["items"]
    ] == [0, 1]


def _trajectory() -> dict[str, Any]:
    snapshots = [_snapshot(0), _snapshot(1)]
    return {
        "schema_version": 1,
        "match_id": 123456,
        "replay_sha256": "a" * 64,
        "clarity_version": "4.0.1",
        "game_mode": 2,
        "league_id": 19785,
        "winner": 2,
        "radiant_team_id": 101,
        "radiant_team_tag": "RAD",
        "dire_team_id": None,
        "dire_team_tag": None,
        "picks_bans": [
            {"is_pick": False, "team": 2, "hero_id": 1},
            {"is_pick": True, "team": 3, "hero_id": 2},
        ],
        "clock_normalization_status": "PROVEN",
        "clock_normalization_method": "GAME_START_TIME_AND_PAUSE_TICKS",
        "zero_replay_tick": 1000,
        "game_end_time_seconds": 65.0,
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
    return {
        "player_slot": slot,
        "team": "RADIANT" if slot < 5 else "DIRE",
        "hero_id": slot + 1,
        "hero_name": f"hero_{slot + 1}",
        "level": minute + 1,
        "kills": slot % 3 + minute,
        "deaths": slot % 2,
        "assists": slot + minute,
        "last_hits": slot * 2 + minute,
        "denies": slot % 4,
        "net_worth": 500 + slot * 10 + minute,
        "current_gold": 100 + slot + minute,
        "total_xp": 200 + slot * 5 + minute,
        "items": [
            {"inventory_slot": 1, "item_name": f"item_b_{slot}"},
            {"inventory_slot": 0, "item_name": f"item_a_{slot}"},
        ],
    }


def _team(name: str, players: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "team": name,
        **{
            field: sum(player[field] for player in players)
            for field in AGGREGATE_FIELDS
        },
    }


def _write_input(
    tmp_path: Path,
    payload: dict[str, Any],
    name: str = "input.json",
) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
