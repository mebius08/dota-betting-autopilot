from copy import deepcopy
import csv
from decimal import Decimal, localcontext
import json

import pytest

from app.analytics.historical_snapshot import HistoricalObservation, HistoricalSnapshot, MatchIdentity
from tools import extract_snapshot_features as cli


SHA = "a" * 64
SPREAD = Decimal("123.456789012345678901234567890123456789")


def snapshot(mid="10", sha=SHA, requested="20.0002", frame="10.0002", tick=200):
    time, observed = Decimal(requested), Decimal(frame)
    players, positions = [], []
    for slot in range(10):
        team = "RADIANT" if slot < 5 else "DIRE"
        identity = dict(source="replay_clarity", source_match_id=mid, player_slot=slot, team=team, hero_id=slot + 1)
        players.append(dict(
            **identity, hero_name=f"hero_{slot}", level=slot + 1, net_worth=100, current_gold=10, total_xp=0,
            kills=0, deaths=0, assists=0, last_hits=slot * 2, denies=0,
            items=[dict(inventory_slot=index, item_name="item_branch") for index in range(2 if slot < 5 else 3)],
        ))
        positions.append(dict(**identity, x=Decimal("100.125"), y=Decimal("200.25"), position_status="OBSERVED", life_state=0))
    for slot in (4, 9):
        positions[slot].update(x=None, y=None, position_status="HERO_NOT_ALIVE", life_state=2)
    pov = {team: dict(own_team_spread=SPREAD if team == "RADIANT" else None,
                     visible_enemy_count=2 if team == "RADIANT" else 1,
                     visible_enemy_with_position_count=1, hidden_enemy_count=3 if team == "RADIANT" else 4,
                     nearest_visible_enemy_distance=Decimal("101.125") if team == "RADIANT" else None,
                     own_team_spread_status="OK" if team == "RADIANT" else "INSUFFICIENT_OWN_POSITIONS",
                     nearest_visible_enemy_status="OK" if team == "RADIANT" else "NO_OWN_POSITION") for team in cli.TEAMS}
    enemies = {}
    for team in cli.TEAMS:
        records = []
        for index, slot in enumerate(range(5, 10) if team == "RADIANT" else range(5)):
            seen = (Decimal(5) if team == "RADIANT" else Decimal(0)) if index == 0 else None
            if team == "RADIANT" and index == 1:
                seen = observed
            visible = team == "RADIANT" and index == 1
            records.append(dict(
                source="replay_clarity", source_match_id=mid, match_id=mid, replay_sha256=sha,
                game_time_seconds=observed, replay_tick=100, pov_team=team, enemy_player_slot=slot,
                enemy_hero_id=slot + 1, enemy_team="DIRE" if team == "RADIANT" else "RADIANT",
                visibility_state="VISIBLE" if visible else "HIDDEN", current_position_status="OBSERVED" if visible else "HIDDEN",
                current_x=Decimal("1.125") if visible else None, current_y=Decimal("2.25") if visible else None,
                last_seen_status="OBSERVED_NOW" if visible else "LAST_SEEN" if seen is not None else "NEVER_SEEN",
                last_seen_game_time_seconds=seen,
                last_seen_age_seconds=cli._elapsed_seconds(observed, seen) if seen is not None else None,
                last_seen_x=Decimal("1.125") if seen is not None else None,
                last_seen_y=Decimal("2.25") if seen is not None else None,
                last_seen_age_at_requested_time_seconds=cli._elapsed_seconds(time, seen) if seen is not None else None,
            ))
        enemies[team] = records
    values = dict(
        economy=dict(radiant_net_worth=1000, dire_net_worth=1200, radiant_total_xp=500, dire_total_xp=400,
                     radiant_current_gold=50, dire_current_gold=None),
        heroes=dict(players=players), spatial_global=dict(players=positions, replay_sha256=sha),
        pov=pov, pov_enemy_positions=enemies,
    )
    observations = {channel: HistoricalObservation(origin, "AVAILABLE", observed, 100,
                                                    cli._elapsed_seconds(time, observed), values[channel])
                    for channel, origin in cli.CHANNEL_ORIGINS.items()}
    state = HistoricalSnapshot(MatchIdentity("replay_clarity", mid), time, observations)
    return {view: json.loads(json.dumps(cli.project_snapshot(state, view, requested_replay_tick=tick) |
                                       {"replay_sha256": sha}, default=str)) for view in cli.VIEWS}


def save(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def test_exact_global_features_orientation_item_entries_and_coordinate_coverage():
    raw = snapshot()["HISTORICAL_GLOBAL"]
    original = deepcopy(raw)
    row = cli.extract_record(raw)
    assert raw == original
    expected = dict(
        radiant_net_worth=1000, dire_net_worth=1200, net_worth_lead_radiant_minus_dire=-200,
        radiant_total_xp=500, dire_total_xp=400, xp_lead_radiant_minus_dire=100,
        radiant_level_sum=15, dire_level_sum=40, radiant_last_hits_sum=20, dire_last_hits_sum=70,
        radiant_item_entry_count=10, dire_item_entry_count=15,
        radiant_observed_global_position_count=4, dire_observed_global_position_count=4,
    )
    assert {field: row[field] for field in cli.GLOBAL_FEATURES} == expected
    assert all(row[field] is None and row[f"feature_status_{field}"] == "NOT_IN_VIEW" for field in cli.POV_FEATURES)
    assert row["provenance"] == "HISTORICAL_ORACLE"
    assert row["heroes_age_seconds"] == Decimal(10)


@pytest.mark.parametrize(("view", "team", "count", "maximum"), [
    ("HISTORICAL_RADIANT_POV", "RADIANT", 2, Decimal("15.0002")),
    ("HISTORICAL_DIRE_POV", "DIRE", 1, Decimal("20.0002")),
])
def test_own_team_features_and_last_seen_stay_separate_from_current_coordinates(view, team, count, maximum):
    raw = snapshot()[view]
    row = cli.extract_record(raw)
    expected = raw["observations"]["pov"]["values"][team]
    for field in cli.POV_FEATURES[:5]:
        assert row[field] == (Decimal(expected[field]) if expected[field] is not None else None)
    assert row["enemy_last_seen_position_count"] == count
    assert row["max_enemy_last_seen_age_seconds"] == maximum
    assert row["provenance"] == "HISTORICAL_TEAM_POV"
    assert all(row[field] is None and row[f"feature_status_{field}"] == "NOT_IN_VIEW" for field in cli.GLOBAL_FEATURES)
    assert row["economy_origin"] is None and row["economy_status"] == "CHANNEL_ABSENT"
    assert not {"x", "y", "current_x", "last_seen_x", "target"} & set(row)


def test_null_is_not_zero_and_original_statuses_are_retained():
    raw = snapshot()["HISTORICAL_DIRE_POV"]
    row = cli.extract_record(raw)
    assert row["own_team_spread"] is row["nearest_visible_enemy_distance"] is None
    assert json.loads(row["source_statuses"])["pov"] == {
        "own_team_spread_status": "INSUFFICIENT_OWN_POSITIONS", "nearest_visible_enemy_status": "NO_OWN_POSITION",
    }
    raw["observations"]["pov"]["values"]["DIRE"].update(own_team_spread="0", own_team_spread_status="OK")
    assert cli.extract_record(raw)["own_team_spread"] == 0


@pytest.mark.parametrize("channel", ["economy", "heroes", "spatial_global", "pov", "pov_enemy_positions"])
def test_absent_channel_leaves_null_features_with_explicit_status(channel):
    view = "HISTORICAL_GLOBAL" if channel in ("economy", "heroes", "spatial_global") else "HISTORICAL_RADIANT_POV"
    raw = snapshot()[view]
    del raw["observations"][channel]
    row = cli.extract_record(raw)
    selected = {"economy": cli.GLOBAL_FEATURES[:6], "heroes": cli.GLOBAL_FEATURES[6:12],
                "spatial_global": cli.GLOBAL_FEATURES[12:], "pov": cli.POV_FEATURES[:5],
                "pov_enemy_positions": cli.POV_FEATURES[5:]}[channel]
    assert all(row[f] is None and row[f"feature_status_{f}"] == "CHANNEL_ABSENT" for f in selected)
    raw["observations"][channel] = dict(origin=cli.CHANNEL_ORIGINS[channel], status="NO_PRECEDING_SNAPSHOT",
                                         actual_game_time_seconds=None, replay_tick=None, age_seconds=None, values=None)
    row = cli.extract_record(raw)
    assert all(row[f] is None and row[f"feature_status_{f}"] == "NO_PRECEDING_SNAPSHOT" for f in selected)


def test_partial_hero_values_do_not_create_partial_sums_or_fake_inventory_zeros():
    raw = snapshot()["HISTORICAL_GLOBAL"]
    heroes = raw["observations"]["heroes"]["values"]["players"]
    heroes[0].update(level=None, last_hits=None, items=None)
    row = cli.extract_record(raw)
    for field in ("radiant_level_sum", "radiant_last_hits_sum", "radiant_item_entry_count"):
        assert row[field] is None and row[f"feature_status_{field}"] == "MISSING_COMPONENT"
    for player in heroes[:5]:
        player["items"] = []
    assert cli.extract_record(raw)["radiant_item_entry_count"] == 0


def test_no_confirmed_last_seen_means_zero_count_but_null_maximum_age():
    raw = snapshot()["HISTORICAL_DIRE_POV"]
    for enemy in raw["observations"]["pov_enemy_positions"]["values"]["DIRE"]:
        enemy.update(last_seen_status="NEVER_SEEN", last_seen_game_time_seconds=None, last_seen_age_seconds=None,
                     last_seen_x=None, last_seen_y=None, last_seen_age_at_requested_time_seconds=None)
    row = cli.extract_record(raw)
    assert row["enemy_last_seen_position_count"] == 0
    assert row["max_enemy_last_seen_age_seconds"] is None
    assert row["feature_status_max_enemy_last_seen_age_seconds"] == "NO_CONFIRMED_LAST_SEEN"


@pytest.mark.parametrize("problem", ["oracle", "opposite_team", "provenance", "hidden_coordinate", "sha", "future_seen"])
def test_team_information_scope_and_causality_cannot_be_bypassed(problem):
    records = snapshot()
    raw = records["HISTORICAL_RADIANT_POV"]
    if problem == "oracle":
        raw["observations"]["economy"] = records["HISTORICAL_GLOBAL"]["observations"]["economy"]
    elif problem == "opposite_team":
        raw["observations"]["pov"]["values"]["DIRE"] = records["HISTORICAL_DIRE_POV"]["observations"]["pov"]["values"]["DIRE"]
    elif problem == "provenance":
        raw["observations"]["pov"]["origin"] = "HISTORICAL_ORACLE"
    else:
        enemy = raw["observations"]["pov_enemy_positions"]["values"]["RADIANT"][0]
        if problem == "hidden_coordinate":
            enemy.update(current_x="100", current_y="200")
        elif problem == "sha":
            enemy["replay_sha256"] = "b" * 64
        else:
            enemy["last_seen_game_time_seconds"] = "20.0003"
    with pytest.raises(ValueError):
        cli.extract_record(raw)


@pytest.mark.parametrize("problem", ["incomplete", "duplicate", "team", "match", "inventory", "position"])
def test_corrupt_global_inputs_are_not_used_for_aggregates(problem):
    raw = snapshot()["HISTORICAL_GLOBAL"]
    players = raw["observations"]["heroes"]["values"]["players"]
    if problem == "incomplete":
        players.pop()
    elif problem == "duplicate":
        players[0]["player_slot"] = players[1]["player_slot"]
    elif problem == "team":
        players[0]["team"] = "DIRE"
    elif problem == "match":
        players[0]["source_match_id"] = "20"
    elif problem == "inventory":
        players[0]["items"].append(deepcopy(players[0]["items"][0]))
    else:
        raw["observations"]["spatial_global"]["values"]["players"][0]["x"] = None
    with pytest.raises(ValueError):
        cli.extract_record(raw)


@pytest.mark.parametrize("location", ["top", "channel", "feature"])
def test_future_fields_are_rejected_and_never_expand_feature_allowlists(location):
    raw = snapshot()["HISTORICAL_RADIANT_POV"]
    if location == "top":
        raw["future_target"] = 1
    elif location == "channel":
        raw["observations"]["predictions"] = {}
    else:
        raw["observations"]["pov"]["values"]["RADIANT"]["own_deaths_next_30s"] = 99
    with pytest.raises(ValueError):
        cli.extract_record(raw)
    assert not set(cli.METADATA) & set(cli.FEATURES)
    assert not any(name.startswith("future_") or "_next_" in name for name in cli.COLUMNS)
    assert set(cli.FEATURES_BY_VIEW["HISTORICAL_GLOBAL"]).isdisjoint(cli.FEATURES_BY_VIEW["HISTORICAL_DIRE_POV"])


def test_decimal_features_timestamps_ages_and_derived_values_are_not_rounded(tmp_path):
    frame = "10.0002000000000000000000000000000000000001"
    requested = "20.0002000000000000000000000000000000000001"
    records = snapshot(requested=requested, frame=frame)
    economy = records["HISTORICAL_GLOBAL"]["observations"]["economy"]["values"]
    economy.update(radiant_net_worth="1000.0000000000000000000000000000000000000001", dire_net_worth="1000")
    source = save(tmp_path / "timeline.jsonl", records.values())
    out = tmp_path / "features.csv"
    with localcontext() as context:
        context.prec = 6
        cli.extract_features(source, out)
    with out.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["requested_game_time_seconds"] == requested
    assert rows[0]["economy_actual_game_time_seconds"] == frame
    assert rows[0]["net_worth_lead_radiant_minus_dire"] == "1E-40"
    assert rows[1]["own_team_spread"] == str(SPREAD)
    assert rows[1]["pov_age_seconds"] == "10.0000000000000000000000000000000000000000"
    assert rows[2]["own_team_spread"] == ""  # Unknown remains CSV NULL.


def test_stable_order_repeated_exports_unique_rows_and_coverage(tmp_path):
    records = [*snapshot().values(), *snapshot(mid="20", sha="b" * 64, requested="30.0002", tick=300).values()]
    source = save(tmp_path / "timeline.jsonl", list(reversed(records)))
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    report = cli.extract_features(source, first)
    source_bytes = source.read_bytes()
    cli.extract_features(source, second)
    assert source.read_bytes() == source_bytes and first.read_bytes() == second.read_bytes()
    with first.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    keys = [(r["source_match_id"], Decimal(r["requested_game_time_seconds"]), int(r["requested_replay_tick"]),
             tuple(cli.VIEWS).index(r["view"])) for r in rows]
    assert keys == sorted(keys) and len(keys) == len(set(keys)) == report["rows"] == 6
    assert len(report["matches"]) == 2
    assert report["coverage"]["HISTORICAL_GLOBAL"]["features"]["radiant_item_entry_count"] == dict(filled=2, null=0)
    assert report["coverage"]["HISTORICAL_DIRE_POV"]["features"]["radiant_item_entry_count"] == dict(filled=0, null=2)


def test_duplicate_identity_fails_without_overwriting_existing_output(tmp_path):
    record = snapshot()["HISTORICAL_GLOBAL"]
    source = save(tmp_path / "timeline.jsonl", [record, record])
    out = tmp_path / "out.csv"
    out.write_text("previous complete output", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        cli.extract_features(source, out)
    assert out.read_text(encoding="utf-8") == "previous complete output"


def test_cli_dry_run_creates_no_file_and_reports_all_views(tmp_path, capsys):
    source = save(tmp_path / "timeline.jsonl", snapshot().values())
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    assert cli.main(["--timeline", str(source)]) == 0
    output = capsys.readouterr().out
    assert "DRY_RUN: rows=3; matches=1" in output and "filled/NULL" in output
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert cli.main(["--timeline", str(source), "--out", str(source)]) == 1
    assert "must not overwrite" in capsys.readouterr().err
