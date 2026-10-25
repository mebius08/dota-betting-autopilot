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


def rewrite_csv(root, path, columns, transform):
    with (root / path).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    updated = transform(rows)
    with (root / path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows if updated is None else updated)


@pytest.mark.parametrize(("current_nw", "expected_nw"), [((145, 190), ("45", "-10", "55")), ((80, 250), ("-20", "50", "-70"))])
def test_lookback_global_deltas_and_gap_use_exact_confirmed_compact_baseline(sources, capsys, current_nw, expected_nw):
    path = sources / cli.COMPACT_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    for frame, nw, xp in zip(data["snapshots"], ((99999, 99999), (100, 200), current_nw), ((99999, 99999), (1000, 900), (900, 1100)), strict=True):
        for index, team in enumerate(frame["teams"]):
            team.update(net_worth=nw[index], total_xp=xp[index])
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main([*arguments(time="20.0002", tick="300"), "--lookback-seconds", "10"]) == 0
    payload = json.loads(capsys.readouterr().out)
    changes = payload["changes"]
    group = changes["groups"]["economy"]
    assert changes["status"] == group["status"] == "AVAILABLE"
    assert changes["requested_baseline_time_seconds"] == "10.0002"
    assert changes["baseline_tick_boundary"] == {"actual_game_time_seconds": "10.0002", "replay_tick": 200, "age_seconds": "0.0000"}
    assert group["current_observation"]["actual_game_time_seconds"] == "20.0002"
    assert group["current_observation"]["replay_tick"] == 300
    assert group["baseline_observation"]["actual_game_time_seconds"] == "10.0002"
    assert group["baseline_observation"]["replay_tick"] == 200
    assert group["elapsed_game_time_seconds"] == "10.0000"
    assert group["deltas"] == dict(
        radiant_net_worth=expected_nw[0], dire_net_worth=expected_nw[1],
        radiant_minus_dire_net_worth=expected_nw[2], radiant_total_xp="-100", dire_total_xp="200",
    )
    assert all(status == "AVAILABLE" for status in group["field_statuses"].values())
    assert not {"target", "winner", "outcomes", "logistic_probability"} & set(nested_keys(payload))


@pytest.mark.parametrize(("view", "team", "expected"), [
    ("HISTORICAL_RADIANT_POV", "RADIANT", (Decimal(2), Decimal("-20.0249"), Decimal("-50.023"))),
    ("HISTORICAL_DIRE_POV", "DIRE", (Decimal(-3), Decimal("5.125"), Decimal("20.25"))),
])
def test_lookback_team_changes_use_only_own_projected_features(sources, view, team, expected):
    def edit(rows):
        for row in rows:
            current = row["game_time_seconds"] == "10.0334"
            if row["pov_team"] == team:
                values = ((3, "80.1001", "250.1") if current else (1, "100.125", "300.123")) if team == "RADIANT" else (
                    (1, "105.125", "220.25") if current else (4, "100", "200")
                )
            else:
                values = (5 if current else 0, "999999999", "999999999")
            row.update(visible_enemy_count=str(values[0]), own_team_spread=values[1],
                       nearest_visible_enemy_distance=values[2], nearest_visible_enemy_status="OK")
    rewrite_csv(sources, cli.POV_PATH, cli.TEAM_COLUMNS, edit)
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), 300, view=view, lookback_seconds=Decimal(10))
    group = result["changes"]["groups"]["pov"]
    assert set(result["changes"]["groups"]) == {"pov"}
    assert group["pov_team"] == team and group["status"] == "AVAILABLE"
    assert tuple(group["deltas"].values()) == expected
    assert all(isinstance(value, Decimal) for value in group["deltas"].values())
    assert group["elapsed_game_time_seconds"] == Decimal(10)
    assert group["current_observation"]["replay_tick"] == 201
    assert group["baseline_observation"]["replay_tick"] == 101
    assert not {"economy", "radiant_net_worth", "dire_net_worth", "heroes", "spatial_global", "target"} & set(nested_keys(result))


@pytest.mark.parametrize(("side", "expected_status"), [
    ("both", "MISSING_BOTH_VALUES"), ("current", "MISSING_CURRENT_VALUE"), ("baseline", "MISSING_BASELINE_VALUE"),
])
def test_lookback_preserves_null_distances_and_explains_missing_values(sources, side, expected_status):
    def edit(rows):
        for row in rows:
            current = row["game_time_seconds"] == "10.0334"
            known = side != "both" and ((side == "baseline" and current) or (side == "current" and not current))
            if known:
                row.update(nearest_visible_enemy_distance="123.125", nearest_visible_enemy_status="OK")
    rewrite_csv(sources, cli.POV_PATH, cli.TEAM_COLUMNS, edit)
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), 300,
                                  view="HISTORICAL_RADIANT_POV", lookback_seconds=Decimal(10))
    group = result["changes"]["groups"]["pov"]
    assert group["status"] == "PARTIAL"
    assert group["deltas"]["nearest_visible_enemy_distance"] is None
    assert group["field_statuses"]["nearest_visible_enemy_distance"] == expected_status
    assert "nearest_visible_enemy_status" in group["current_feature_statuses"]


def test_lookback_null_economy_does_not_create_zero_or_a_partial_gap(sources):
    path = sources / cli.COMPACT_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    data["snapshots"][2]["teams"][0]["net_worth"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), 300, lookback_seconds=Decimal(10))
    group = result["changes"]["groups"]["economy"]
    assert group["status"] == "PARTIAL"
    assert group["deltas"]["radiant_net_worth"] is group["deltas"]["radiant_minus_dire_net_worth"] is None
    assert group["field_statuses"]["radiant_net_worth"] == "MISSING_CURRENT_VALUE"


@pytest.mark.parametrize("view", ["HISTORICAL_GLOBAL", "HISTORICAL_RADIANT_POV"])
def test_lookback_same_source_frame_is_not_reported_as_zero_dynamics(sources, view):
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("9.9"), 199, view=view, lookback_seconds=Decimal(1))
    group = next(iter(result["changes"]["groups"].values()))
    assert group["status"] == result["changes"]["status"] == "NO_NEW_OBSERVATION"
    assert group["elapsed_game_time_seconds"] == 0
    assert group["current_observation"]["replay_tick"] == group["baseline_observation"]["replay_tick"]
    assert all(value is None for value in group["deltas"].values())


@pytest.mark.parametrize("reason", ["BASELINE_BEFORE_GAME_START", "NO_PRECEDING_BASELINE_FRAME"])
def test_lookback_without_a_confirmed_baseline_has_explicit_status(sources, reason):
    if reason == "NO_PRECEDING_BASELINE_FRAME":
        # No source has a witness before the requested baseline, not just POV.
        for relative in (cli.COMPACT_PATH, cli.SPATIAL_PATH):
            path = sources / relative
            data = json.loads(path.read_text(encoding="utf-8"))
            data["snapshots"] = data["snapshots"][1:]
            path.write_text(json.dumps(data), encoding="utf-8")
        for path, columns in ((cli.POV_PATH, cli.TEAM_COLUMNS), (cli.ENEMY_PATH, cli.ENEMY_OBSERVATION_FIELDS)):
            rewrite_csv(sources, path, columns, lambda rows: [row for row in rows if row["game_time_seconds"] != "0.0334"])
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), 300,
                                  lookback_seconds=Decimal(60) if reason == "BASELINE_BEFORE_GAME_START" else Decimal("10.0001"))
    changes = result["changes"]
    group = changes["groups"]["economy"]
    assert changes["status"] == group["status"] == reason
    assert changes["baseline_tick_boundary"] is group["baseline_observation"] is None
    assert group["elapsed_game_time_seconds"] is None
    assert all(value is None for value in group["deltas"].values())


def test_lookback_tick_boundary_uses_actual_compact_evidence_despite_pause_sized_gaps(sources, monkeypatch):
    ticks = {100: 100, 101: 137, 200: 500000, 201: 500123, 300: 9000000, 301: 9000033}
    for relative in (cli.COMPACT_PATH, cli.SPATIAL_PATH):
        path = sources / relative
        data = json.loads(path.read_text(encoding="utf-8"))
        for frame in data["snapshots"]:
            frame["replay_tick"] = ticks[frame["replay_tick"]]
        path.write_text(json.dumps(data), encoding="utf-8")
    def edit(rows):
        for row in rows:
            row["replay_tick"] = str(ticks[int(row["replay_tick"])])
    rewrite_csv(sources, cli.POV_PATH, cli.TEAM_COLUMNS, edit)
    rewrite_csv(sources, cli.ENEMY_PATH, cli.ENEMY_OBSERVATION_FIELDS, edit)
    original = cli.project_snapshot
    calls = []
    def project(snapshot, view, **kwargs):
        calls.append((snapshot.requested_game_time_seconds, kwargs["requested_replay_tick"]))
        return original(snapshot, view, **kwargs)
    monkeypatch.setattr(cli, "project_snapshot", project)
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), 9000000, lookback_seconds=Decimal(10))
    assert calls == [(Decimal("20.0002"), 9000000), (Decimal("10.0002"), 500000)]
    assert result["changes"]["baseline_tick_boundary"]["replay_tick"] == 500000
    assert result["changes"]["groups"]["economy"]["baseline_observation"]["replay_tick"] == 500000


def test_lookback_decimal_math_does_not_round_large_values_or_timestamps(sources, capsys):
    path = sources / cli.COMPACT_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    big = 10 ** 60
    for index, team in enumerate(data["snapshots"][1]["teams"]):
        team["net_worth"] = big
    for index, team in enumerate(data["snapshots"][2]["teams"]):
        team["net_worth"] = big + (3 if index == 0 else 1)
    path.write_text(json.dumps(data), encoding="utf-8")
    requested = "20.0002000000000000000000000000000000000001"
    with localcontext() as context:
        context.prec = 6
        assert cli.main([*arguments(time=requested, tick="300"), "--lookback-seconds", "10"]) == 0
    result = json.loads(capsys.readouterr().out)
    changes = result["changes"]
    assert changes["requested_baseline_time_seconds"] == "10.0002000000000000000000000000000000000001"
    assert changes["groups"]["economy"]["deltas"]["radiant_net_worth"] == "3"
    assert changes["groups"]["economy"]["deltas"]["dire_net_worth"] == "1"
    assert changes["groups"]["economy"]["deltas"]["radiant_minus_dire_net_worth"] == "2"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "invalid"])
def test_invalid_lookback_is_rejected_before_reading_sources(tmp_path, monkeypatch, capsys, value):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    with pytest.raises(SystemExit) as error:
        cli.main([*arguments(), "--lookback-seconds", value])
    assert error.value.code == 2 and capsys.readouterr().out == ""


@pytest.mark.parametrize("view", ["HISTORICAL_GLOBAL", "HISTORICAL_RADIANT_POV", "HISTORICAL_DIRE_POV"])
def test_exact_compact_boundary_preserves_all_views_and_older_pov_frames(sources, view):
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), 300, view=view, lookback_seconds=Decimal(10))
    changes = result["changes"]
    assert changes["baseline_tick_boundary"] == {
        "actual_game_time_seconds": Decimal("10.0002"), "replay_tick": 200, "age_seconds": Decimal("0.0000"),
    }
    if view == "HISTORICAL_GLOBAL":
        assert set(result["observations"]) == {"economy", "heroes", "spatial_global", "pov", "pov_enemy_positions"}
        group = changes["groups"]["economy"]
        assert group["baseline_observation"]["actual_game_time_seconds"] == Decimal("10.0002")
        assert group["baseline_observation"]["replay_tick"] == 200
    else:
        team = "RADIANT" if view == "HISTORICAL_RADIANT_POV" else "DIRE"
        assert set(result["observations"]) == {"pov", "pov_enemy_positions"}
        assert all(set(observation["values"]) == {team} for observation in result["observations"].values())
        group = changes["groups"]["pov"]
        assert group["baseline_observation"]["actual_game_time_seconds"] == Decimal("0.0334")
        assert group["baseline_observation"]["replay_tick"] == 101
        assert group["current_observation"]["actual_game_time_seconds"] == Decimal("10.0334")
    assert group["elapsed_game_time_seconds"] == Decimal(10)
    assert not {"target", "winner", "outcomes"} & set(nested_keys(result))


@pytest.mark.parametrize("view", ["HISTORICAL_GLOBAL", "HISTORICAL_RADIANT_POV", "HISTORICAL_DIRE_POV"])
@pytest.mark.parametrize(("baseline_time", "expected_time", "expected_tick"), [
    (Decimal("10.0003"), Decimal("10.0002"), 200),
    (Decimal("11.0002"), Decimal("10.0334"), 201),
])
def test_no_exact_witness_uses_latest_confirmed_past_pair_without_future_frames(sources, view, baseline_time, expected_time, expected_tick):
    current_time = Decimal("20.0002")
    result = cli.inspect_snapshot(cli.MATCH_ID, current_time, 300, view=view,
                                  lookback_seconds=cli._elapsed_seconds(current_time, baseline_time))
    boundary = result["changes"]["baseline_tick_boundary"]
    assert boundary["actual_game_time_seconds"] == expected_time <= baseline_time
    assert boundary["replay_tick"] == expected_tick
    assert boundary["age_seconds"] == cli._elapsed_seconds(baseline_time, expected_time)
    for group in result["changes"]["groups"].values():
        assert group["baseline_observation"]["actual_game_time_seconds"] <= baseline_time
        assert group["baseline_observation"]["replay_tick"] <= expected_tick
        assert group["elapsed_game_time_seconds"] == cli._elapsed_seconds(
            group["current_observation"]["actual_game_time_seconds"],
            group["baseline_observation"]["actual_game_time_seconds"],
        )


@pytest.mark.parametrize("conflict", ["same_tick", "same_time", "clock_order"])
def test_conflicting_past_clock_witnesses_are_diagnostic_errors(sources, capsys, conflict):
    if conflict == "clock_order":
        path = sources / cli.COMPACT_PATH
        data = json.loads(path.read_text(encoding="utf-8"))
        data["snapshots"][1]["source_game_time_seconds"] = 0.02
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        def edit(rows):
            for row in rows:
                if row["game_time_seconds"] == "0.0334":
                    row["game_time_seconds"] = "0.0335" if conflict == "same_tick" else "10.0002"
                    if conflict == "same_time":
                        row["replay_tick"] = "150"
        rewrite_csv(sources, cli.POV_PATH, cli.TEAM_COLUMNS, edit)
    assert cli.main([*arguments(time="20.0002", tick="300"), "--lookback-seconds", "10"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Conflicting baseline clock" in captured.err
    assert cli.MATCH_ID in captured.err
    assert ("ordering" in captured.err if conflict == "clock_order" else
            "101" in captured.err if conflict == "same_tick" else "200" in captured.err and "150" in captured.err)


def test_conflicting_clock_normalization_metadata_prevents_a_baseline(sources, capsys):
    for relative, method in ((cli.COMPACT_PATH, "CLOCK_A"), (cli.SPATIAL_PATH, "CLOCK_B")):
        path = sources / relative
        data = json.loads(path.read_text(encoding="utf-8"))
        data["clock_normalization_method"] = method
        path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main([*arguments(time="20.0002", tick="300"), "--lookback-seconds", "10"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "clock_normalization_method" in captured.err
    assert "CLOCK_A" in captured.err and "CLOCK_B" in captured.err


def test_baseline_witness_must_also_be_no_later_than_the_requested_current_tick(sources):
    result = cli.inspect_snapshot(cli.MATCH_ID, Decimal("20.0002"), 199, lookback_seconds=Decimal(10))
    changes = result["changes"]
    assert changes["baseline_tick_boundary"]["replay_tick"] == 101  # Exact compact tick 200 is not yet allowed.
    assert changes["status"] == "NO_NEW_OBSERVATION"
    assert changes["groups"]["economy"]["baseline_observation"]["replay_tick"] == 100
