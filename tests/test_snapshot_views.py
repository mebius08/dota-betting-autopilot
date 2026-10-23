from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, localcontext

import pytest

from app.analytics.historical_snapshot import HistoricalObservation, HistoricalSnapshot, MatchIdentity
from app.analytics.snapshot_views import project_snapshot


GLOBAL = "HISTORICAL_GLOBAL"
RADIANT = "HISTORICAL_RADIANT_POV"
DIRE = "HISTORICAL_DIRE_POV"
TIME = Decimal("2280.0002")
OBSERVED = Decimal("2270.0334")
AGE = Decimal("9.9668")
TICK = 100335
REQUEST_TICK = 100634
MATCH = MatchIdentity("replay_clarity", "fixture-match")


def observation(origin, values):
    return HistoricalObservation(origin, "AVAILABLE", OBSERVED, TICK, AGE, values)


@pytest.fixture
def snapshot():
    pov, enemies, heroes, positions = {}, {}, [], []
    for slot in range(10):
        identity = dict(source=MATCH.source, source_match_id=MATCH.source_match_id,
                        player_slot=slot, team="RADIANT" if slot < 5 else "DIRE", hero_id=slot + 1)
        heroes.append(dict(
            **identity, hero_name=f"hero_{slot}", level=12, net_worth=1000, current_gold=None,
            total_xp=500, kills=0, deaths=0, assists=0, last_hits=0, denies=0,
            items=[dict(inventory_slot=0, item_name="item_fixture")],
        ))
        positions.append(dict(**identity, x=Decimal("999.125"), y=Decimal("888.25"),
                              life_state=0, position_status="OBSERVED"))
    for team in ("RADIANT", "DIRE"):
        pov[team] = dict(visible_enemy_count=0 if team == "RADIANT" else 2,
                         visible_enemy_with_position_count=0 if team == "RADIANT" else 1,
                         hidden_enemy_count=5 if team == "RADIANT" else 2,
                         own_team_spread=Decimal("123.456789012345678901234567890123456789"),
                         nearest_visible_enemy_distance=None if team == "RADIANT" else Decimal("456.25"),
                         own_team_spread_status="OK",
                         nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION" if team == "RADIANT" else "OK")
        enemies[team] = [dict(
            source=MATCH.source, source_match_id=MATCH.source_match_id, match_id=MATCH.source_match_id,
            replay_sha256="a" * 64, game_time_seconds=OBSERVED, replay_tick=TICK, pov_team=team,
            enemy_player_slot=slot, enemy_hero_id=slot + 1,
            enemy_team="DIRE" if team == "RADIANT" else "RADIANT",
            visibility_state="HIDDEN", current_position_status="HIDDEN", current_x=None, current_y=None,
            last_seen_status="NEVER_SEEN", last_seen_game_time_seconds=None, last_seen_age_seconds=None,
            last_seen_x=None, last_seen_y=None, last_seen_age_at_requested_time_seconds=None,
        ) for slot in (range(5, 10) if team == "RADIANT" else range(5))]
    history = dict(last_seen_status="LAST_SEEN", last_seen_game_time_seconds=Decimal("2260.0334"),
                   last_seen_age_seconds=Decimal("10.0"), last_seen_x=Decimal("123.125"),
                   last_seen_y=Decimal("456.25"), last_seen_age_at_requested_time_seconds=Decimal("19.9668"))
    enemies["RADIANT"][0].update(history)
    enemies["DIRE"][0].update(
        visibility_state="VISIBLE", current_position_status="OBSERVED",
        current_x=Decimal("999.125"), current_y=Decimal("888.25"),
        last_seen_status="OBSERVED_NOW", last_seen_game_time_seconds=OBSERVED,
        last_seen_age_seconds=Decimal("0.0"), last_seen_x=Decimal("999.125"),
        last_seen_y=Decimal("888.25"), last_seen_age_at_requested_time_seconds=AGE,
    )
    enemies["DIRE"][1].update(history, visibility_state="VISIBLE", current_position_status="VISIBLE_POSITION_UNAVAILABLE")
    enemies["DIRE"][2].update(visibility_state="UNKNOWN", current_position_status="UNKNOWN")
    return HistoricalSnapshot(MATCH, TIME, {
        "economy": observation("HISTORICAL_ORACLE", dict(
            radiant_net_worth=5000, dire_net_worth=5000, radiant_current_gold=None,
            dire_current_gold=100, radiant_total_xp=2500, dire_total_xp=2500,
        )),
        "heroes": observation("HISTORICAL_ORACLE", {"players": heroes}),
        "spatial_global": observation("HISTORICAL_ORACLE", {"players": positions, "replay_sha256": "a" * 64}),
        "pov": observation("HISTORICAL_TEAM_POV", pov),
        "pov_enemy_positions": observation("HISTORICAL_TEAM_POV", enemies),
    })


def test_global_preserves_all_five_channels_and_values(snapshot):
    result = project_snapshot(snapshot, GLOBAL, requested_replay_tick=REQUEST_TICK)
    assert result["view"] == GLOBAL and len(result["observations"]) == 5
    assert result["observations"] == asdict(snapshot)["observations"]
    assert result["snapshot_kind"] == snapshot.snapshot_kind
    assert result["match"] == asdict(MATCH)
    assert result["requested_game_time_seconds"] == TIME and result["requested_replay_tick"] == REQUEST_TICK


@pytest.mark.parametrize(("view", "team"), [(RADIANT, "RADIANT"), (DIRE, "DIRE")])
def test_team_views_only_contain_own_confirmed_channels(snapshot, view, team):
    result = project_snapshot(snapshot, view, requested_replay_tick=REQUEST_TICK)
    assert set(result["observations"]) == {"pov", "pov_enemy_positions"}
    for channel, projected in result["observations"].items():
        original = snapshot.observations[channel]
        assert projected["origin"] == "HISTORICAL_TEAM_POV"
        assert projected["status"] == original.status
        assert projected["actual_game_time_seconds"] == original.actual_game_time_seconds
        assert projected["replay_tick"] == original.replay_tick and projected["age_seconds"] == original.age_seconds
        assert projected["values"] == {team: original.values[team]}
    for enemy in result["observations"]["pov_enemy_positions"]["values"][team]:
        assert enemy["pov_team"] == team and enemy["enemy_team"] != team


def test_hidden_last_seen_visible_without_position_and_unknown_are_preserved(snapshot):
    hidden = project_snapshot(snapshot, RADIANT)["observations"]["pov_enemy_positions"]["values"]["RADIANT"][0]
    assert hidden["current_x"] is hidden["current_y"] is None
    assert hidden["visibility_state"] == "HIDDEN"
    assert hidden["last_seen_x"] == Decimal("123.125")
    assert hidden["last_seen_game_time_seconds"] == Decimal("2260.0334")
    assert hidden["last_seen_age_seconds"] == Decimal("10.0")
    assert hidden["last_seen_age_at_requested_time_seconds"] == Decimal("19.9668")
    assert hidden["last_seen_x"] != snapshot.observations["spatial_global"].values["players"][5]["x"]
    dire = project_snapshot(snapshot, DIRE)["observations"]["pov_enemy_positions"]["values"]["DIRE"]
    assert dire[1]["visibility_state"] == "VISIBLE" and dire[1]["current_position_status"] == "VISIBLE_POSITION_UNAVAILABLE"
    assert dire[1]["current_x"] is dire[1]["current_y"] is None
    assert dire[2]["visibility_state"] == "UNKNOWN" and dire[2]["last_seen_x"] is None


def test_global_and_opposite_team_values_cannot_influence_radiant_view(snapshot):
    expected = project_snapshot(snapshot, RADIANT)
    snapshot.observations["economy"].values["target"] = {"future": 1}
    snapshot.observations["spatial_global"].values["players"][5]["x"] = Decimal("999999")
    snapshot.observations["pov"].values["DIRE"]["own_team_spread"] = {"target": 1}
    snapshot.observations["pov_enemy_positions"].values["DIRE"][0]["current_x"] = Decimal("777777")
    assert project_snapshot(snapshot, RADIANT) == expected


@pytest.mark.parametrize("view", [RADIANT, DIRE])
def test_team_field_allowlist_drops_future_and_unconfirmed_fields(snapshot, view):
    expected = project_snapshot(snapshot, view)
    team = "RADIANT" if view == RADIANT else "DIRE"
    snapshot.observations["pov"].values[team].update(target=1, global_enemy_x=999, logistic_probability=.9)
    snapshot.observations["pov_enemy_positions"].values[team][0].update(own_deaths_next_30s=3, life_state=2)
    assert project_snapshot(snapshot, view) == expected
    with pytest.raises(ValueError, match="allowlist"):
        project_snapshot(snapshot, GLOBAL)


@pytest.mark.parametrize("view", [GLOBAL, RADIANT, DIRE])
def test_missing_observation_preserves_status_nulls_and_original_envelope(snapshot, view):
    snapshot.observations["pov_enemy_positions"] = HistoricalObservation(
        "HISTORICAL_TEAM_POV", "NO_PRECEDING_SNAPSHOT", None, None, None, None,
    )
    projected = project_snapshot(snapshot, view)["observations"]["pov_enemy_positions"]
    assert projected == asdict(snapshot.observations["pov_enemy_positions"])
    del snapshot.observations["pov_enemy_positions"]
    assert "pov_enemy_positions" not in project_snapshot(snapshot, view)["observations"]


@pytest.mark.parametrize("view", [GLOBAL, RADIANT, DIRE])
def test_views_are_detached_and_do_not_mutate_snapshot(snapshot, view):
    before = deepcopy(snapshot)
    result = project_snapshot(snapshot, view)
    assert snapshot == before
    if view == GLOBAL:
        result["observations"]["heroes"]["values"]["players"][0]["items"][0]["item_name"] = "changed"
    else:
        team = "RADIANT" if view == RADIANT else "DIRE"
        result["observations"]["pov_enemy_positions"]["values"][team][0]["last_seen_x"] = 0
        result["observations"]["pov"]["values"][team]["visible_enemy_count"] = 99
    result["match"]["source"] = "changed"
    assert snapshot == before


def test_decimal_values_timestamps_and_unknown_requested_tick_are_exact(snapshot):
    with localcontext() as context:
        context.prec = 6
        result = project_snapshot(snapshot, RADIANT)
    assert result["requested_game_time_seconds"] == TIME and result["requested_replay_tick"] is None
    pov = result["observations"]["pov"]
    assert pov["actual_game_time_seconds"] == OBSERVED and pov["age_seconds"] == AGE
    assert pov["values"]["RADIANT"]["own_team_spread"] == Decimal("123.456789012345678901234567890123456789")
    assert pov["values"]["RADIANT"]["nearest_visible_enemy_distance"] is None


@pytest.mark.parametrize("view", ["LIVE_RADIANT_POV", "UNKNOWN", None, []])
def test_unknown_view_has_no_fallback(snapshot, view):
    with pytest.raises(ValueError, match="Unknown historical snapshot view"):
        project_snapshot(snapshot, view)


@pytest.mark.parametrize("channel", ["economy", "heroes", "spatial_global", "pov", "pov_enemy_positions"])
@pytest.mark.parametrize("view", [GLOBAL, RADIANT, DIRE])
def test_contradictory_provenance_always_fails(snapshot, channel, view):
    snapshot.observations[channel] = replace(snapshot.observations[channel], origin="UNVERIFIED")
    with pytest.raises(ValueError, match="provenance"):
        project_snapshot(snapshot, view)


def test_unknown_channels_and_labels_hidden_in_allowed_fields_are_rejected(snapshot):
    snapshot.observations["predictions"] = observation("HISTORICAL_OUT_OF_FOLD", {"target": 1})
    with pytest.raises(ValueError, match="Unknown observation channel"):
        project_snapshot(snapshot, GLOBAL)
    del snapshot.observations["predictions"]
    snapshot.observations["pov"].values["RADIANT"]["own_team_spread"] = {"target": 1}
    with pytest.raises(ValueError):
        project_snapshot(snapshot, RADIANT)


@pytest.mark.parametrize("problem", ["hidden_current", "foreign_observer", "future_last_seen", "missing_team"])
def test_contradictory_team_positions_are_not_sanitized_into_safe_looking_data(snapshot, problem):
    row = snapshot.observations["pov_enemy_positions"].values["RADIANT"][0]
    if problem == "hidden_current":
        row.update(current_x=999, current_y=888)
    elif problem == "foreign_observer":
        row["pov_team"] = "DIRE"
    elif problem == "future_last_seen":
        row["last_seen_game_time_seconds"] = Decimal("2270.0334000000000000000000000000000000000001")
    else:
        del snapshot.observations["pov_enemy_positions"].values["RADIANT"]
    with pytest.raises(ValueError):
        project_snapshot(snapshot, RADIANT)


@pytest.mark.parametrize("tick", [True, -1, "100634", TICK - 1])
def test_requested_tick_is_not_invented_or_allowed_before_an_observation(snapshot, tick):
    with pytest.raises(ValueError):
        project_snapshot(snapshot, GLOBAL, requested_replay_tick=tick)
