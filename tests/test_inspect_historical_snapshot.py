import csv
from dataclasses import asdict
from decimal import Decimal, localcontext
import json

import pytest

from tools import inspect_historical_snapshot as cli


SHA = "a" * 64
MATCH_SHAS = {"8960991322": SHA, "8960882635": "b" * 64,
              "8958734008": "c" * 64, "8957013760": "d" * 64}
SPREAD = "123.456789012345678901234567890123456789"


def write_sources(root, match_id=cli.MATCH_ID, sha=SHA):
    """Small isolated files; no gitignored data, database or network is needed."""
    compact_path, spatial_path, pov_path, enemy_path = cli.MATCH_SOURCES[match_id]
    compact, spatial, pov, enemies = [], [], [], []
    for index in range(3):
        players, positions = [], []
        for slot in range(10):
            identity = dict(player_slot=slot, team="RADIANT" if slot < 5 else "DIRE", hero_id=slot + 1)
            players.append(dict(
                **identity, hero_name=f"hero_{slot}", level=1,
                net_worth=100, current_gold=10, total_xp=0,
                kills=0, deaths=0, assists=0, last_hits=0, denies=0,
                items=[dict(inventory_slot=0, item_name="item_test")], target=1,
            ))
            positions.append(dict(**identity, x=slot + 1.125, y=slot + 2.25,
                                  position_status="OBSERVED", life_state=0))
        tick = 100 * (index + 1)
        compact.append(dict(
            game_time_seconds=index * 10, source_game_time_seconds=0 if index == 0 else index * 10 + .0002,
            replay_tick=tick, players=players, teams=[
                dict(team=team, net_worth=500, current_gold=50 if team == "RADIANT" else None, total_xp=0)
                for team in cli.TEAMS
            ],
        ))
        time = index * 10 + .0334
        spatial.append(dict(source_game_time_seconds=time, game_time_seconds=time,
                            target_game_time_seconds=index * 10, replay_tick=tick + 1, players=positions))
        for team in cli.TEAMS:
            lineage = dict(match_id=match_id, replay_sha256=sha, game_time_seconds=str(time),
                           replay_tick=str(tick + 1), pov_team=team)
            pov.append(dict(
                **lineage, own_alive_hero_count="5", own_valid_position_count="5",
                own_unknown_life_state_count="0", own_team_spread=SPREAD,
                visible_enemy_count="0", visible_enemy_with_position_count="0",
                hidden_enemy_count="5", unknown_enemy_count="0", nearest_visible_enemy_distance="",
                own_team_spread_status="OK", nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION",
            ))
            for slot in (range(5, 10) if team == "RADIANT" else range(5)):
                enemies.append(dict(
                    **lineage, enemy_player_slot=str(slot), enemy_hero_id=str(slot + 1),
                    enemy_team="DIRE" if team == "RADIANT" else "RADIANT",
                    visibility_state="HIDDEN", current_position_status="HIDDEN", current_x="", current_y="",
                    last_seen_status="NEVER_SEEN", last_seen_game_time_seconds="", last_seen_age_seconds="",
                    last_seen_x="", last_seen_y="",
                ))
    for path, frames, extra in (
        (compact_path, compact, dict(winner="DIRE", own_deaths_next_30s=99)),
        (spatial_path, spatial, dict(artifact_kind="replay_hero_positions")),
    ):
        full_path = root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(json.dumps(dict(
            match_id=int(match_id), replay_sha256=sha, clock_normalization_status="PROVEN",
            snapshots=frames, **extra,
        )), encoding="utf-8")
    for path, columns, rows in (
        (pov_path, cli.TEAM_COLUMNS, pov),
        (enemy_path, cli.ENEMY_OBSERVATION_FIELDS, enemies),
    ):
        full_path = root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        with full_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    write_sources(tmp_path)
    return tmp_path


def arguments(time="10.0002", tick="200", match=cli.MATCH_ID):
    return ["--match-id", match, "--at-seconds", time, "--at-tick", tick]


def nested_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


def test_cli_builds_five_channels_once_and_keeps_nulls_and_information_boundaries(sources, capsys, monkeypatch):
    original = cli.build_historical_snapshot
    calls = []

    def build(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "build_historical_snapshot", build)
    assert cli.main(arguments()) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and len(calls) == 1
    payload = json.loads(captured.out)
    assert payload["match"] == {"source": "replay_clarity", "source_match_id": cli.MATCH_ID}
    assert payload["requested_game_time_seconds"] == "10.0002" and payload["requested_replay_tick"] == 200
    assert payload["replay_sha256"] == SHA
    observations = payload["observations"]
    assert set(observations) == {"economy", "heroes", "spatial_global", "pov", "pov_enemy_positions"}
    for channel, observation in observations.items():
        assert observation["status"] == "AVAILABLE"
        oracle = channel in ("economy", "heroes", "spatial_global")
        assert observation["origin"] == ("HISTORICAL_ORACLE" if oracle else "HISTORICAL_TEAM_POV")
        assert observation["actual_game_time_seconds"] == ("10.0002" if channel in ("economy", "heroes") else "0.0334")
        assert Decimal(observation["actual_game_time_seconds"]) <= Decimal("10.0002")
        assert observation["replay_tick"] <= 200
    assert observations["economy"]["values"]["dire_current_gold"] is None
    assert len(observations["heroes"]["values"]["players"]) == 10
    assert observations["spatial_global"]["values"]["players"][5]["x"] == "6.125"
    for team in cli.TEAMS:
        assert observations["pov"]["values"][team]["nearest_visible_enemy_distance"] is None
        assert observations["pov"]["values"][team]["own_team_spread"] == SPREAD
        enemies = observations["pov_enemy_positions"]["values"][team]
        assert len(enemies) == 5
        assert all(enemy["current_x"] is None and enemy["current_y"] is None for enemy in enemies)
        assert all(enemy["last_seen_status"] == "NEVER_SEEN" for enemy in enemies)
    assert not {"target", "winner", "own_deaths_next_30s", "outcomes", "logistic_probability"} & set(nested_keys(payload))


@pytest.mark.parametrize(("tick", "expected"), [(199, (0, "0.0334")), (201, ("10.0002", "10.0334"))])
def test_all_channels_obey_tick_even_when_newer_frames_have_past_game_time(sources, tick, expected):
    snapshot = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), tick)
    for channel, observation in snapshot["observations"].items():
        expected_time = expected[0] if channel in ("economy", "heroes") else expected[1]
        assert observation["actual_game_time_seconds"] == Decimal(str(expected_time))
        assert observation["replay_tick"] <= tick


def test_json_preserves_precise_requested_time_and_decimal_values(sources, capsys):
    requested = "10.0002000000000000000000000000000000000001"
    with localcontext() as context:
        context.prec = 6
        assert cli.main(arguments(time=requested)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requested_game_time_seconds"] == requested
    assert Decimal(payload["observations"]["economy"]["age_seconds"]) == Decimal("1e-40")
    assert payload["observations"]["pov"]["values"]["DIRE"]["own_team_spread"] == SPREAD


def test_before_observations_json_contains_explicit_absence(sources, capsys):
    assert cli.main(arguments(time="-1", tick="0")) == 0
    for observation in json.loads(capsys.readouterr().out)["observations"].values():
        assert observation["status"] == "NO_PRECEDING_SNAPSHOT"
        assert all(observation[field] is None for field in ("values", "actual_game_time_seconds", "replay_tick", "age_seconds"))


def test_other_match_is_rejected_before_loading_any_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    with pytest.raises(SystemExit) as error:
        cli.main(arguments(match="9999999999"))
    assert error.value.code == 2 and cli.MATCH_ID in capsys.readouterr().err
    with pytest.raises(ValueError, match="Unsupported match ID"):
        cli.inspect_snapshot("9999999999", Decimal("1"), 1)


@pytest.mark.parametrize(("path", "field", "value"), [
    (cli.COMPACT_PATH, "match_id", 1),
    (cli.SPATIAL_PATH, "replay_sha256", "b" * 64),
    (cli.COMPACT_PATH, "clock_normalization_status", "UNPROVEN"),
])
def test_source_identity_sha_and_clock_errors_produce_no_json(sources, capsys, path, field, value):
    full_path = sources / path
    data = json.loads(full_path.read_text(encoding="utf-8"))
    data[field] = value
    full_path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(arguments()) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Historical snapshot error:" in captured.err


def test_observation_csv_rejects_future_label_columns(sources, capsys):
    path = sources / cli.POV_PATH
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0] + ",target", *[line + ",1" for line in lines[1:]]]) + "\n", encoding="utf-8")
    assert cli.main(arguments()) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "observation-only CSV schema" in captured.err


def test_missing_source_fails_without_a_partial_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(arguments()) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Historical snapshot error:" in captured.err


@pytest.mark.parametrize(("time", "tick"), [("NaN", "1"), ("Infinity", "1"), ("invalid", "1"), ("1", "-1")])
def test_invalid_query_is_rejected(time, tick):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments(time=time, tick=tick))
    assert error.value.code == 2


@pytest.mark.parametrize("match_id", tuple(MATCH_SHAS))
def test_each_confirmed_match_selects_only_its_own_sources(sources, capsys, match_id):
    for mid, sha in MATCH_SHAS.items():
        write_sources(sources, mid, sha)
    assert cli.main(arguments(match=match_id)) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["match"]["source_match_id"] == match_id
    assert payload["replay_sha256"] == MATCH_SHAS[match_id]
    observations = payload["observations"]
    assert set(observations) == {"economy", "heroes", "spatial_global", "pov", "pov_enemy_positions"}
    for observation in observations.values():
        assert observation["status"] == "AVAILABLE"
        assert Decimal(observation["actual_game_time_seconds"]) <= Decimal("10.0002")
        assert observation["replay_tick"] <= 200
    players = observations["heroes"]["values"]["players"] + observations["spatial_global"]["values"]["players"]
    enemies = observations["pov_enemy_positions"]["values"]
    for row in [*players, *enemies["RADIANT"], *enemies["DIRE"]]:
        assert row["source_match_id"] == match_id
    for row in [*enemies["RADIANT"], *enemies["DIRE"]]:
        assert row["match_id"] == match_id and row["replay_sha256"] == MATCH_SHAS[match_id]


@pytest.mark.parametrize("component", range(4), ids=("compact", "spatial", "pov", "enemies"))
def test_missing_selected_source_reports_match_and_path_without_fallback(sources, capsys, component):
    match_id = "8960882635"
    write_sources(sources, match_id, MATCH_SHAS[match_id])
    missing = sources / cli.MATCH_SOURCES[match_id][component]
    missing.unlink()
    assert cli.main(arguments(match=match_id)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert match_id in captured.err and str(missing) in captured.err
    assert "required source file is missing" in captured.err


@pytest.mark.parametrize("component", range(4), ids=("compact", "spatial", "pov", "enemies"))
def test_sources_from_another_confirmed_match_cannot_be_mixed(sources, capsys, component):
    match_id = "8960882635"
    write_sources(sources, match_id, MATCH_SHAS[match_id])
    target = sources / cli.MATCH_SOURCES[match_id][component]
    foreign = sources / cli.MATCH_SOURCES[cli.MATCH_ID][component]
    target.write_bytes(foreign.read_bytes())
    assert cli.main(arguments(match=match_id)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert match_id in captured.err and "match" in captured.err


def test_default_and_explicit_global_preserve_legacy_json_and_delegate_tick(sources, capsys, monkeypatch):
    original = cli.project_snapshot
    calls = []

    def project(snapshot, view, **kwargs):
        calls.append((snapshot, view, kwargs))
        return original(snapshot, view, **kwargs)

    monkeypatch.setattr(cli, "project_snapshot", project)
    assert cli.main(arguments()) == 0
    implicit = json.loads(capsys.readouterr().out)
    assert cli.main([*arguments(), "--view", "HISTORICAL_GLOBAL"]) == 0
    explicit = json.loads(capsys.readouterr().out)
    legacy = asdict(calls[0][0]) | {"requested_replay_tick": 200, "replay_sha256": SHA}
    assert implicit == explicit == json.loads(json.dumps(legacy, default=cli._json_decimal))
    assert len(calls) == 2
    assert all(view == "HISTORICAL_GLOBAL" and kwargs == {"requested_replay_tick": 200}
               for _, view, kwargs in calls)


@pytest.mark.parametrize(("view", "team"), [
    ("HISTORICAL_RADIANT_POV", "RADIANT"), ("HISTORICAL_DIRE_POV", "DIRE"),
])
def test_cli_team_views_are_isolated_and_preserve_last_seen_nulls_and_metadata(sources, capsys, monkeypatch, view, team):
    path = sources / cli.ENEMY_PATH
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    slot = "5" if team == "RADIANT" else "0"
    for row in rows:
        if row["game_time_seconds"] == "0.0334" and row["pov_team"] == team and row["enemy_player_slot"] == slot:
            row.update(last_seen_status="LAST_SEEN", last_seen_game_time_seconds="0.0",
                       last_seen_age_seconds="0.0334", last_seen_x=SPREAD, last_seen_y="456.125")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=cli.ENEMY_OBSERVATION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    original = cli.project_snapshot
    calls = []

    def project(snapshot, selected_view, **kwargs):
        calls.append((selected_view, kwargs))
        return original(snapshot, selected_view, **kwargs)

    monkeypatch.setattr(cli, "project_snapshot", project)
    assert cli.main([*arguments(), "--view", view]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and calls == [(view, {"requested_replay_tick": 200})]
    payload = json.loads(captured.out)
    assert payload["view"] == view
    assert payload["match"] == {"source": "replay_clarity", "source_match_id": cli.MATCH_ID}
    assert payload["requested_game_time_seconds"] == "10.0002" and payload["requested_replay_tick"] == 200
    assert payload["replay_sha256"] == SHA
    observations = payload["observations"]
    assert set(observations) == {"pov", "pov_enemy_positions"}
    for observation in observations.values():
        assert observation["origin"] == "HISTORICAL_TEAM_POV" and observation["status"] == "AVAILABLE"
        assert observation["actual_game_time_seconds"] == "0.0334" and observation["replay_tick"] == 101
        assert observation["age_seconds"] == "9.9668" and set(observation["values"]) == {team}
    features = observations["pov"]["values"][team]
    assert features["nearest_visible_enemy_distance"] is None
    assert features["nearest_visible_enemy_status"] == "NO_VISIBLE_ENEMY_POSITION"
    assert features["own_team_spread"] == SPREAD
    enemies = observations["pov_enemy_positions"]["values"][team]
    assert len(enemies) == 5
    assert all(row["pov_team"] == team and row["enemy_team"] != team for row in enemies)
    assert all(row["visibility_state"] == "HIDDEN" and row["current_x"] is row["current_y"] is None for row in enemies)
    remembered = enemies[0]
    assert remembered["last_seen_x"] == SPREAD and remembered["last_seen_y"] == "456.125"
    assert remembered["last_seen_game_time_seconds"] == "0.0"
    assert remembered["last_seen_age_seconds"] == "0.0334"
    assert remembered["last_seen_age_at_requested_time_seconds"] == "10.0002"
    assert not {"economy", "heroes", "spatial_global", "net_worth", "current_gold", "life_state",
                "target", "winner", "own_deaths_next_30s", "logistic_probability"} & set(nested_keys(payload))


@pytest.mark.parametrize("view", ["HISTORICAL_RADIANT_POV", "HISTORICAL_DIRE_POV"])
def test_cli_team_view_keeps_exact_decimal_request_and_missing_source_status(sources, capsys, view):
    requested = "10.0002000000000000000000000000000000000001"
    with localcontext() as context:
        context.prec = 6
        assert cli.main([*arguments(time=requested), "--view", view]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requested_game_time_seconds"] == requested
    team = "RADIANT" if view == "HISTORICAL_RADIANT_POV" else "DIRE"
    assert payload["observations"]["pov"]["values"][team]["own_team_spread"] == SPREAD
    with localcontext() as context:
        context.prec = 80
        assert Decimal(payload["observations"]["pov"]["age_seconds"]) == Decimal(requested) - Decimal("0.0334")
    assert cli.main([*arguments(time="-1", tick="0"), "--view", view]) == 0
    missing = json.loads(capsys.readouterr().out)["observations"]
    assert set(missing) == {"pov", "pov_enemy_positions"}
    assert all(observation["status"] == "NO_PRECEDING_SNAPSHOT" and observation["values"] is None
               for observation in missing.values())


def test_unknown_view_fails_before_loading_sources(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    with pytest.raises(SystemExit) as error:
        cli.main([*arguments(), "--view", "LIVE_RADIANT_POV"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "invalid choice" in captured.err
