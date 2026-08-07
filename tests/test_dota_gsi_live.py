from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from app import dota_gsi_live as gsi
from app import live_state_signal as signal
from app import replay_state_history as history
from app.cli import main


def test_real_spectator_mapping_normalizes_exact_oriented_contract() -> None:
    payload = _spectator_payload(clock_time=360)
    payload["map"]["game_time"] = 999

    result = gsi.normalize_dota_gsi_payload(payload)

    assert isinstance(result, gsi.NormalizedDotaGSIPayload)
    assert result.match_id == 123
    assert list(result.snapshot) == [
        "game_time_seconds",
        *history.CURRENT_FEATURE_COLUMNS,
    ]
    assert result.snapshot == {
        "game_time_seconds": 360,
        "kill_diff": -20,
        "death_diff": 5,
        "assist_diff": -15,
        "last_hit_diff": -50,
        "deny_diff": -5,
        "net_worth_diff": -5000,
        "current_gold_diff": -500,
        "total_xp_diff": -1000,
        "total_kills": 50,
        "total_last_hits": 170,
        "total_denies": 35,
        "total_net_worth": 17000,
        "total_current_gold": 1700,
        "total_xp": 7000,
    }
    rendered = json.dumps(result.snapshot, sort_keys=True)
    assert "steamid" not in rendered
    assert "accountid" not in rendered
    assert "hero_name" not in rendered
    assert "xpm" not in rendered


def test_numeric_string_match_id_is_accepted() -> None:
    result = gsi.normalize_dota_gsi_payload(
        _spectator_payload(matchid="000123", clock_time=60)
    )

    assert isinstance(result, gsi.NormalizedDotaGSIPayload)
    assert result.match_id == 123


@pytest.mark.parametrize(
    "matchid",
    [True, False, 0, -1, 123.0, "", "-1", "12.3", "abc", None],
)
def test_malformed_active_match_id_is_rejected(matchid: object) -> None:
    payload = _spectator_payload(matchid=matchid, clock_time=60)
    if matchid is None:
        del payload["map"]["matchid"]

    with pytest.raises(gsi.DotaGSILiveError, match="map.matchid"):
        gsi.normalize_dota_gsi_payload(payload)


@pytest.mark.parametrize(
    "game_state",
    [
        "DOTA_GAMERULES_STATE_PRE_GAME",
        "DOTA_GAMERULES_STATE_HERO_SELECTION",
        "DOTA_GAMERULES_STATE_POST_GAME",
    ],
)
def test_inactive_game_states_are_ignored(game_state: str) -> None:
    result = gsi.normalize_dota_gsi_payload(
        _spectator_payload(game_state=game_state, clock_time=15)
    )

    assert result == gsi.IgnoredDotaGSIPayload(
        reason="game_not_in_progress",
        match_id=123,
        game_time_seconds=15,
    )


def test_confirmed_real_pregame_shape_with_zero_xp_is_ignored() -> None:
    payload = _spectator_payload(
        game_state="DOTA_GAMERULES_STATE_PRE_GAME",
        clock_time=-56,
        matchid=None,
    )
    del payload["map"]["matchid"]
    for team in payload["hero"].values():
        for hero in team.values():
            hero["xp"] = 0

    result = gsi.normalize_dota_gsi_payload(payload)

    assert result == gsi.IgnoredDotaGSIPayload(
        reason="game_not_in_progress",
        match_id=None,
        game_time_seconds=-56,
    )


def test_negative_in_progress_clock_is_ignored_before_match_validation() -> None:
    payload = _spectator_payload(clock_time=-1, matchid=True)

    result = gsi.normalize_dota_gsi_payload(payload)

    assert result == gsi.IgnoredDotaGSIPayload(
        reason="game_not_in_progress",
        match_id=None,
        game_time_seconds=-1,
    )


def test_active_off_boundary_payload_is_ignored_before_team_validation() -> None:
    payload = _spectator_payload(clock_time=361)
    del payload["player"]
    del payload["hero"]

    result = gsi.normalize_dota_gsi_payload(payload)

    assert result == gsi.IgnoredDotaGSIPayload(
        reason="not_sampling_boundary",
        match_id=123,
        game_time_seconds=361,
    )


@pytest.mark.parametrize("section", ["player", "hero"])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_boundary_requires_exactly_five_players_and_heroes(
    section: str,
    change: str,
) -> None:
    payload = _spectator_payload(clock_time=60)
    team = payload[section]["team2"]
    if change == "missing":
        del team["player4"]
    else:
        team["player10"] = dict(next(iter(team.values())))

    with pytest.raises(gsi.DotaGSILiveError, match="exactly five"):
        gsi.normalize_dota_gsi_payload(payload)


@pytest.mark.parametrize("section", ["player", "hero"])
def test_boundary_requires_exact_team_mappings(section: str) -> None:
    payload = _spectator_payload(clock_time=60)
    payload[section]["team4"] = {}

    with pytest.raises(gsi.DotaGSILiveError, match="exactly team2 and team3"):
        gsi.normalize_dota_gsi_payload(payload)


def test_player_and_hero_member_labels_must_match() -> None:
    payload = _spectator_payload(clock_time=60)
    payload["hero"]["team2"]["different_label"] = payload["hero"]["team2"].pop(
        "player4"
    )

    with pytest.raises(gsi.DotaGSILiveError, match="member labels must match"):
        gsi.normalize_dota_gsi_payload(payload)


@pytest.mark.parametrize(
    ("team", "replacement", "expected"),
    [
        ("team2", "dire", "radiant"),
        ("team3", "radiant", "dire"),
        ("team2", "Radiant", "radiant"),
        ("team3", None, "dire"),
    ],
)
def test_team_orientation_is_strict(
    team: str,
    replacement: object,
    expected: str,
) -> None:
    payload = _spectator_payload(clock_time=60)
    payload["player"][team][next(iter(payload["player"][team]))][
        "team_name"
    ] = replacement

    with pytest.raises(gsi.DotaGSILiveError, match=f"must be {expected}"):
        gsi.normalize_dota_gsi_payload(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [("player", field) for field in gsi.PLAYER_METRICS] + [("hero", "xp")],
)
def test_missing_required_metric_is_rejected(section: str, field: str) -> None:
    payload = _spectator_payload(clock_time=60)
    del payload[section]["team2"]["player0"][field]

    with pytest.raises(gsi.DotaGSILiveError, match=field):
        gsi.normalize_dota_gsi_payload(payload)


@pytest.mark.parametrize("value", [True, False, "1"])
def test_boolean_and_string_metrics_are_rejected(value: object) -> None:
    payload = _spectator_payload(clock_time=60)
    payload["player"]["team2"]["player0"]["kills"] = value

    with pytest.raises(gsi.DotaGSILiveError, match="must be numeric"):
        gsi.normalize_dota_gsi_payload(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_metrics_are_rejected(value: float) -> None:
    payload = _spectator_payload(clock_time=60)
    payload["hero"]["team3"]["player5"]["xp"] = value

    with pytest.raises(gsi.DotaGSILiveError, match="must be finite"):
        gsi.normalize_dota_gsi_payload(payload)


@pytest.mark.parametrize(
    ("section", "field"),
    [("player", field) for field in gsi.PLAYER_METRICS] + [("hero", "xp")],
)
def test_negative_cumulative_metrics_are_rejected(
    section: str,
    field: str,
) -> None:
    payload = _spectator_payload(clock_time=60)
    payload[section]["team3"]["player5"][field] = -1

    with pytest.raises(gsi.DotaGSILiveError, match="must be non-negative"):
        gsi.normalize_dota_gsi_payload(payload)


def test_xpm_is_never_used_as_accumulated_xp() -> None:
    payload = _spectator_payload(clock_time=60)
    del payload["hero"]["team2"]["player0"]["xp"]
    payload["hero"]["team2"]["player0"]["xpm"] = 999999

    with pytest.raises(gsi.DotaGSILiveError, match=r"\.xp"):
        gsi.normalize_dota_gsi_payload(payload)


def test_member_mapping_order_does_not_affect_floating_point_output() -> None:
    payload = _spectator_payload(clock_time=60)
    for team in ("team2", "team3"):
        for player in payload["player"][team].values():
            player["gold"] = float(player["gold"]) + 0.25
        payload["player"][team] = dict(
            reversed(list(payload["player"][team].items()))
        )
        payload["hero"][team] = dict(
            reversed(list(payload["hero"][team].items()))
        )
    reversed_result = gsi.normalize_dota_gsi_payload(payload)
    canonical_result = gsi.normalize_dota_gsi_payload(
        _spectator_payload(clock_time=60, fractional_gold=True)
    )

    assert reversed_result == canonical_result


@pytest.mark.parametrize("response_status", ["not_ready", "scored"])
def test_existing_live_ingestion_is_invoked_and_response_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_status: str,
) -> None:
    input_path = tmp_path / "payload.json"
    output_path = tmp_path / "response.json"
    expected_response = {
        "schema_version": 1,
        "status": response_status,
        "sentinel": "existing-live-api-response",
    }
    _write_json(input_path, _spectator_payload(clock_time=60))
    calls: list[dict[str, Any]] = []
    staged_paths: list[Path] = []

    def ingest_spy(
        model_dir: str | Path,
        state_dir: str | Path,
        request_path: str | Path,
        destination: str | Path,
    ) -> signal.LiveStateSignalResult:
        staged_path = Path(request_path)
        staged_paths.append(staged_path)
        calls.append(json.loads(staged_path.read_bytes()))
        signal.persist_live_state_signal_response(destination, expected_response)
        return signal.LiveStateSignalResult(
            status="BUILT",
            response_status=cast(
                Literal["not_ready", "scored"], response_status
            ),
            history_path=Path(state_dir) / "123" / "history.json",
            output_path=Path(destination),
            history_file_sha256="1" * 64,
        )

    monkeypatch.setattr(gsi.signal, "ingest_live_state_snapshot", ingest_spy)

    result = gsi.ingest_dota_gsi_payload(
        tmp_path / "bundle",
        tmp_path / "state",
        input_path,
        output_path,
    )

    assert isinstance(result, signal.LiveStateSignalResult)
    assert result.response_status == response_status
    assert json.loads(output_path.read_bytes()) == expected_response
    assert calls == [
        {
            "schema_version": 1,
            "match_id": 123,
            "snapshot": _expected_snapshot(game_time=60),
        }
    ]
    assert all(not path.exists() for path in staged_paths)


def test_ignored_payload_does_not_invoke_model_or_mutate_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "pregame.json"
    output_path = tmp_path / "response.json"
    history_path = tmp_path / "state" / "123" / "history.json"
    history_path.parent.mkdir(parents=True)
    history_path.write_bytes(b"existing-history\n")
    original_history = history_path.read_bytes()
    _write_json(
        input_path,
        _spectator_payload(
            game_state="DOTA_GAMERULES_STATE_PRE_GAME",
            clock_time=-56,
        ),
    )

    def unexpected_ingest(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("existing live ingestion must not be invoked")

    monkeypatch.setattr(gsi.signal, "ingest_live_state_snapshot", unexpected_ingest)

    result = gsi.ingest_dota_gsi_payload(
        tmp_path / "bundle",
        tmp_path / "state",
        input_path,
        output_path,
    )

    assert result.status == "BUILT"
    assert history_path.read_bytes() == original_history
    assert json.loads(output_path.read_bytes()) == {
        "schema_version": 1,
        "status": "ignored",
        "reason": "game_not_in_progress",
        "match_id": 123,
        "game_time_seconds": -56,
    }


def test_ignored_output_is_atomic_deterministic_and_then_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "off-boundary.json"
    output_path = tmp_path / "response.json"
    _write_json(input_path, _spectator_payload(clock_time=361))
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def replace_spy(source: Path, destination: Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == destination_path.parent
        replacements.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(signal.os, "replace", replace_spy)

    first = gsi.ingest_dota_gsi_payload(
        tmp_path / "bundle",
        tmp_path / "state",
        input_path,
        output_path,
    )
    first_bytes = output_path.read_bytes()
    second = gsi.ingest_dota_gsi_payload(
        tmp_path / "bundle",
        tmp_path / "state",
        input_path,
        output_path,
    )

    assert first.status == "BUILT"
    assert second.status == "UNCHANGED"
    assert output_path.read_bytes() == first_bytes
    assert [destination for _, destination in replacements] == [output_path]
    assert not list(tmp_path.rglob("*.transaction.*"))


def test_cli_prints_built_then_unchanged_for_ignored_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "payload.json"
    output_path = tmp_path / "response.json"
    _write_json(input_path, _spectator_payload(clock_time=359))
    arguments = [
        "ingest-dota-gsi-payload",
        "--model-dir",
        str(tmp_path / "bundle"),
        "--state-dir",
        str(tmp_path / "state"),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]

    assert main(arguments) == 0
    assert capsys.readouterr().out == "BUILT\n"
    assert main(arguments) == 0
    assert capsys.readouterr().out == "UNCHANGED\n"


def test_cli_does_not_accept_an_auth_token_option(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "ingest-dota-gsi-payload",
        "--model-dir",
        str(tmp_path / "bundle"),
        "--state-dir",
        str(tmp_path / "state"),
        "--input",
        str(tmp_path / "input.json"),
        "--output",
        str(tmp_path / "output.json"),
        "--auth-token",
        "secret",
    ]

    assert main(arguments) == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_payload_containing_auth_is_rejected() -> None:
    payload = _spectator_payload(clock_time=60)
    payload["auth"] = {"token": "must-not-be-accepted"}

    with pytest.raises(gsi.DotaGSILiveError, match="must not contain auth"):
        gsi.normalize_dota_gsi_payload(payload)


def test_adapter_does_not_duplicate_model_or_history_logic() -> None:
    source = Path(gsi.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "load_replay_state_model",
        "score_replay_state_history_request",
        "persist_match_history",
        "CatBoost",
        "catboost",
    ):
        assert forbidden not in source


def _spectator_payload(
    *,
    game_state: str = gsi.IN_PROGRESS_STATE,
    clock_time: int | float = 360,
    matchid: object = 123,
    fractional_gold: bool = False,
) -> dict[str, Any]:
    players: dict[str, dict[str, dict[str, Any]]] = {"team2": {}, "team3": {}}
    heroes: dict[str, dict[str, dict[str, Any]]] = {"team2": {}, "team3": {}}
    for index in range(5):
        label = f"player{index}"
        gold: int | float = 100 + index * 10
        if fractional_gold:
            gold = float(gold) + 0.25
        players["team2"][label] = {
            "kills": 1 + index,
            "deaths": 2 + index,
            "assists": 3 + index,
            "last_hits": 10 + index,
            "denies": 1 + index,
            "gold": gold,
            "net_worth": 1000 + index * 100,
            "team_name": "radiant",
            "steamid": f"radiant-steam-{index}",
            "accountid": 1000 + index,
        }
        heroes["team2"][label] = {
            "xp": 500 + index * 50,
            "xpm": 9000 + index,
            "name": f"radiant-hero-{index}",
        }
    for offset in range(5):
        player_index = 5 + offset
        label = f"player{player_index}"
        gold = 200 + offset * 10
        if fractional_gold:
            gold = float(gold) + 0.25
        players["team3"][label] = {
            "kills": 5 + offset,
            "deaths": 1 + offset,
            "assists": 6 + offset,
            "last_hits": 20 + offset,
            "denies": 2 + offset,
            "gold": gold,
            "net_worth": 2000 + offset * 100,
            "team_name": "dire",
            "steamid": f"dire-steam-{offset}",
            "accountid": 2000 + offset,
        }
        heroes["team3"][label] = {
            "xp": 700 + offset * 50,
            "xpm": 8000 + offset,
            "name": f"dire-hero-{offset}",
        }
    return {
        "map": {
            "matchid": matchid,
            "clock_time": clock_time,
            "game_time": 123456,
            "game_state": game_state,
            "radiant_score": 999,
            "dire_score": 999,
            "radiant_win_chance": 0.99,
        },
        "player": players,
        "hero": heroes,
    }


def _expected_snapshot(game_time: int) -> dict[str, int]:
    return {
        "game_time_seconds": game_time,
        "kill_diff": -20,
        "death_diff": 5,
        "assist_diff": -15,
        "last_hit_diff": -50,
        "deny_diff": -5,
        "net_worth_diff": -5000,
        "current_gold_diff": -500,
        "total_xp_diff": -1000,
        "total_kills": 50,
        "total_last_hits": 170,
        "total_denies": 35,
        "total_net_worth": 17000,
        "total_current_gold": 1700,
        "total_xp": 7000,
    }


def _write_json(path: Path, payload: object) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    path.write_text(f"{content}\n", encoding="utf-8")
