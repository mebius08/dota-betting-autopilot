import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from app.analytics.historical_snapshot import (
    ECONOMY_FIELDS, POV_FIELDS, HistoricalSnapshot, MatchIdentity,
    build_historical_snapshot,
)


MATCH = MatchIdentity("replay_clarity", "8960991322")


def economy(time, tick, **updates):
    return dict(
        actual_game_time_seconds=time, replay_tick=tick,
        radiant_net_worth=90000, dire_net_worth=85000,
        radiant_current_gold=1000, dire_current_gold=None,
        radiant_total_xp=80000, dire_total_xp=81000,
    ) | updates


def pov(team, time, tick, **updates):
    return dict(
        actual_game_time_seconds=time, replay_tick=tick, pov_team=team,
        visible_enemy_count=0, visible_enemy_with_position_count=0,
        hidden_enemy_count=5, own_team_spread=1000,
        nearest_visible_enemy_distance=None, own_team_spread_status="OK",
        nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION",
    ) | updates


def pair(time, tick):
    return [pov(team, time, tick) for team in ("RADIANT", "DIRE")]


def test_selects_independent_past_sources_and_the_latest_complete_pov():
    snapshot = build_historical_snapshot(
        MATCH, 2280.0002,
        [economy(2340.0002, 102434), economy(2220.0002, 98834), economy(2280.0002, 100634)],
        [*pair(2280.0334, 100635), pov("RADIANT", 2275, 100485), *pair(2270.0334, 100335)],
    )
    assert isinstance(snapshot, HistoricalSnapshot)
    assert snapshot.match == MATCH
    assert snapshot.snapshot_kind == "HISTORICAL_OBSERVATIONS"
    assert snapshot.requested_game_time_seconds == Decimal("2280.0002")
    economic, team_pov = snapshot.observations["economy"], snapshot.observations["pov"]
    assert economic.origin == "HISTORICAL_ORACLE" and team_pov.origin == "HISTORICAL_TEAM_POV"
    assert economic.status == team_pov.status == "AVAILABLE"
    assert economic.actual_game_time_seconds == Decimal("2280.0002")
    assert economic.age_seconds == 0 and economic.replay_tick == 100634
    assert team_pov.actual_game_time_seconds == Decimal("2270.0334")
    assert team_pov.age_seconds == Decimal("9.9668") and team_pov.replay_tick == 100335
    assert set(team_pov.values) == {"RADIANT", "DIRE"}


@pytest.mark.parametrize(("t", "expected"), [
    (Decimal("0"), None),
    (Decimal("-1"), None),
    (Decimal("60.0001999999999999999"), Decimal("0.0002")),
    (Decimal("60.0002"), Decimal("60.0002")),
    (Decimal("100"), Decimal("60.0002")),
])
def test_as_of_boundaries_use_exact_game_time(t, expected):
    snapshot = build_historical_snapshot(
        MATCH, t, [economy(Decimal("0.0002"), 1), economy(Decimal("60.0002"), 2)],
        [*pair(Decimal("60.0002"), 102), *pair(Decimal("0.0002"), 101)],
    )
    for observation in snapshot.observations.values():
        assert observation.actual_game_time_seconds == expected
        if expected is None:
            assert observation.status == "NO_PRECEDING_SNAPSHOT"
            assert observation.replay_tick is observation.age_seconds is observation.values is None
        else:
            assert observation.status == "AVAILABLE"
            assert observation.age_seconds == t - expected
            assert observation.actual_game_time_seconds <= t


def test_no_observations_have_explicit_absence_and_no_fabricated_zeros():
    snapshot = build_historical_snapshot(MATCH, 0, [], [])
    assert len(snapshot.observations) == 2
    for observation in snapshot.observations.values():
        assert observation.status == "NO_PRECEDING_SNAPSHOT"
        assert observation.actual_game_time_seconds is None
        assert observation.replay_tick is None and observation.age_seconds is None
        assert observation.values is None


def test_teams_from_different_ticks_never_form_a_pov_frame():
    snapshot = build_historical_snapshot(
        MATCH, 100, [economy(10, 1)], [pov("RADIANT", 10, 100), pov("DIRE", 10, 101)],
    )
    assert snapshot.observations["economy"].status == "AVAILABLE"
    assert snapshot.observations["pov"].status == "NO_PRECEDING_SNAPSHOT"


@pytest.mark.parametrize("problem", ["duplicate_team", "different_time", "wrong_team", "duplicate_economy"])
def test_ambiguous_or_malformed_records_fail_explicitly(problem):
    economics, povs = [economy(10, 1)], pair(10, 100)
    if problem == "duplicate_team":
        povs.append(povs[0].copy())
    elif problem == "different_time":
        povs[1]["actual_game_time_seconds"] = Decimal("10.00000000000000001")
    elif problem == "wrong_team":
        povs[1]["pov_team"] = "GLOBAL"
    else:
        economics.append(economics[0].copy())
    with pytest.raises(ValueError):
        build_historical_snapshot(MATCH, 100, economics, povs)


def test_nulls_raw_flags_statuses_and_information_boundaries_are_preserved():
    economics = [economy(10, 1, target=1, logistic_probability=.99)]
    povs = pair(10, 100)
    povs[1].update(
        visible_enemy_count=3, visible_enemy_with_position_count=0, hidden_enemy_count=2,
        own_team_spread=None, own_team_spread_status="INSUFFICIENT_OWN_POSITIONS",
        target=1, own_deaths_next_30s=4, global_enemy_x=99999,
    )
    before = deepcopy((economics, povs))
    snapshot = build_historical_snapshot(MATCH, 10, economics, povs)
    econ = snapshot.observations["economy"].values
    dire = snapshot.observations["pov"].values["DIRE"]
    assert set(econ) == set(ECONOMY_FIELDS) and econ["dire_current_gold"] is None
    assert set(dire) == set(POV_FIELDS)
    assert dire["visible_enemy_count"] == 3 and dire["visible_enemy_with_position_count"] == 0
    assert dire["nearest_visible_enemy_distance"] is None
    assert dire["nearest_visible_enemy_status"] == "NO_VISIBLE_ENEMY_POSITION"
    assert dire["own_team_spread"] is None
    assert dire["own_team_spread_status"] == "INSUFFICIENT_OWN_POSITIONS"
    assert (economics, povs) == before
    dire["visible_enemy_count"] = 5
    econ["radiant_net_worth"] = 0
    assert (economics, povs) == before  # Returned payloads do not alias input records.
    povs[1]["target"] = 0
    povs[1]["global_enemy_x"] = -99999
    assert build_historical_snapshot(MATCH, 10, economics, povs) == build_historical_snapshot(MATCH, 10, *before)


def test_valid_future_records_do_not_change_an_existing_snapshot():
    economics, povs = [economy(2280.0002, 100634)], pair(2270.0334, 100335)
    original = build_historical_snapshot(MATCH, 2280.0002, economics, povs)
    extended = build_historical_snapshot(
        MATCH, 2280.0002, [economy(2340.0002, 102434), *economics],
        [*pair(2280.0334, 100635), *povs],
    )
    assert extended == original


def test_equal_times_use_the_higher_tick_without_cross_source_tick_alignment():
    snapshot = build_historical_snapshot(
        MATCH, 10, [economy(10, 2), economy(10, 3)], [*pair(10, 200), *pair(10, 100)],
    )
    assert snapshot.observations["economy"].replay_tick == 3
    assert snapshot.observations["pov"].replay_tick == 200


def test_decimal_time_and_age_survive_a_low_precision_calling_context():
    requested = Decimal("1.0000000000000000000000000000000000000002")
    observed = Decimal("1e-40")
    expected_age = Decimal("1.0000000000000000000000000000000000000001")
    with localcontext() as context:
        context.prec = 6
        snapshot = build_historical_snapshot(MATCH, requested, [economy(observed, 1)], pair(observed, 100))
        assert context.prec == 6
    assert snapshot.requested_game_time_seconds == requested
    for observation in snapshot.observations.values():
        assert observation.actual_game_time_seconds == observed
        assert observation.age_seconds == expected_age


@pytest.mark.parametrize("metadata", [
    {"source": "other", "source_match_id": MATCH.source_match_id},
    {"source": MATCH.source, "source_match_id": "another-match"},
    {"source": MATCH.source},
])
def test_identity_metadata_must_match_in_full(metadata):
    with pytest.raises(ValueError, match="identity"):
        build_historical_snapshot(MATCH, 100, [economy(10, 1, **metadata)], [])
    with pytest.raises(ValueError, match="identity"):
        build_historical_snapshot(MATCH, 100, [], [pov("RADIANT", 10, 1, **metadata)])


def test_matching_identity_is_accepted_without_returning_result_metadata():
    @dataclass(frozen=True)
    class WithResult(MatchIdentity):
        winner: str = "future-result"

    identity = WithResult(MATCH.source, MATCH.source_match_id)
    tagged = economy(10, 1, source=MATCH.source, source_match_id=MATCH.source_match_id)
    snapshot = build_historical_snapshot(identity, 10, [tagged], [])
    assert asdict(snapshot)["match"] == {"source": MATCH.source, "source_match_id": MATCH.source_match_id}


@pytest.mark.parametrize("time", [float("nan"), float("inf"), Decimal("NaN"), True, "2280.0002"])
def test_unknown_or_nonfinite_requested_time_is_rejected(time):
    with pytest.raises(ValueError):
        build_historical_snapshot(MATCH, time, [], [])


def test_nominal_time_is_never_used_as_an_actual_time_fallback():
    row = economy(10, 1)
    del row["actual_game_time_seconds"]
    row["game_time_seconds"] = 0
    with pytest.raises(ValueError, match="Actual game time"):
        build_historical_snapshot(MATCH, 100, [row], [])


def heroes(time, tick, **updates):
    return dict(
        source_game_time_seconds=time, replay_tick=tick, game_time_seconds=0,
        players=[dict(
            player_slot=slot, team="RADIANT" if slot < 5 else "DIRE",
            hero_id=slot + 1, hero_name=f"hero_{slot}", level=12,
            net_worth=12000, current_gold=500, total_xp=10000,
            kills=2, deaths=1, assists=3, last_hits=100, denies=5,
            items=[dict(inventory_slot=0, item_name="item_blink")],
        ) for slot in range(10)],
    ) | updates


@pytest.mark.parametrize(("t", "expected", "tick"), [
    (Decimal("-1"), None, None),
    (Decimal("0"), None, None),
    (Decimal("2280.0001999999999999999999999999"), Decimal("2220.0002"), 98834),
    (Decimal("2280.0002"), Decimal("2280.0002"), 100634),
    (Decimal("2280.0333999999999999999999999999"), Decimal("2280.0002"), 100634),
    (Decimal("2280.0334"), Decimal("2280.0334"), 100635),
])
def test_heroes_strict_as_of_uses_actual_callback_time(t, expected, tick):
    observation = build_historical_snapshot(MATCH, t, [], [], hero_records=[
        heroes(Decimal("2280.0334"), 100635),
        heroes(Decimal("2220.0002"), 98834),
        heroes(Decimal("2280.0002"), 100634),
    ]).observations["heroes"]
    assert observation.origin == "HISTORICAL_ORACLE"
    assert observation.actual_game_time_seconds == expected
    assert observation.replay_tick == tick
    if expected is None:
        assert observation.status == "NO_PRECEDING_SNAPSHOT"
        assert observation.age_seconds is observation.values is None
    else:
        assert observation.status == "AVAILABLE"
        with localcontext() as context:
            context.prec = 50
            assert observation.age_seconds == t - expected
        assert observation.actual_game_time_seconds <= t


def test_partial_hero_frames_are_skipped_and_never_merged():
    partial = heroes(20, 200)
    partial["players"] = partial["players"][:5]
    other = heroes(20, 201)
    other["players"] = other["players"][5:]
    snapshot = build_historical_snapshot(
        MATCH, 20, [], [], hero_records=[partial, heroes(10, 100), other],
    )
    observation = snapshot.observations["heroes"]
    assert observation.replay_tick == 100 and observation.age_seconds == 10
    for records in ([], [partial, other]):
        missing = build_historical_snapshot(MATCH, 20, [], [], hero_records=records).observations["heroes"]
        assert missing.status == "NO_PRECEDING_SNAPSHOT" and missing.values is None


def test_hero_payload_preserves_all_ten_players_items_nulls_and_source_identity():
    frame = heroes(10, 100, match_id=8960991322, target=1)
    frame["players"][0].update(current_gold=None, net_worth=None, hero_name=None, target=1, hp=999)
    frame["players"][0]["items"].append(dict(inventory_slot=17, item_name=None, charges=99))
    frame["players"][1]["items"] = None
    before = deepcopy(frame)
    observation = build_historical_snapshot(MATCH, 10, [], [], hero_records=[frame]).observations["heroes"]
    players = observation.values["players"]
    assert set(observation.values) == {"players"}
    assert len(players) == len({p["player_slot"] for p in players}) == 10
    assert sum(p["team"] == "RADIANT" for p in players) == 5
    assert sum(p["team"] == "DIRE" for p in players) == 5
    for player, original in zip(players, frame["players"], strict=True):
        assert player["source"] == MATCH.source and player["source_match_id"] == MATCH.source_match_id
        assert {k: v for k, v in player.items() if k not in ("source", "source_match_id", "items")} == {
            k: v for k, v in original.items() if k not in ("items", "target", "hp")
        }
    assert players[0]["current_gold"] is players[0]["net_worth"] is players[0]["hero_name"] is None
    assert players[0]["items"] == [
        {"inventory_slot": 0, "item_name": "item_blink"},
        {"inventory_slot": 17, "item_name": None},
    ]
    assert players[1]["items"] is None
    players[0]["items"][0]["item_name"] = "changed"
    players[1]["kills"] = 99
    assert frame == before


@pytest.mark.parametrize("problem", [
    "duplicate_slot", "six_radiant", "invalid_team", "bad_tick", "nonfinite_time",
    "nominal_only", "player_tick", "player_time", "contradictory_frame_time",
    "duplicate_item_slot", "missing_metric", "eleven_players",
])
def test_malformed_hero_frames_fail_explicitly_even_with_a_valid_predecessor(problem):
    frame = heroes(20, 200)
    player = frame["players"][0]
    if problem == "duplicate_slot":
        frame["players"][1]["player_slot"] = 0
    elif problem == "six_radiant":
        frame["players"][5]["team"] = "RADIANT"
    elif problem == "invalid_team":
        player["team"] = "GLOBAL"
    elif problem == "bad_tick":
        frame["replay_tick"] = True
    elif problem == "nonfinite_time":
        frame["source_game_time_seconds"] = Decimal("NaN")
    elif problem == "nominal_only":
        del frame["source_game_time_seconds"]
    elif problem == "player_tick":
        player["replay_tick"] = 201
    elif problem == "player_time":
        player["source_game_time_seconds"] = Decimal("20.00000000000000000001")
    elif problem == "contradictory_frame_time":
        frame["actual_game_time_seconds"] = Decimal("20.00000000000000000001")
    elif problem == "duplicate_item_slot":
        player["items"].append(player["items"][0].copy())
    elif problem == "missing_metric":
        del player["current_gold"]
    else:
        frame["players"].append(player.copy())
    with pytest.raises(ValueError):
        build_historical_snapshot(MATCH, 20, [], [], hero_records=[heroes(10, 100), frame])


@pytest.mark.parametrize("metadata", [
    {"match_id": "other"},
    {"source": "other", "source_match_id": MATCH.source_match_id},
    {"source": MATCH.source, "source_match_id": "other"},
    {"source": MATCH.source},
])
@pytest.mark.parametrize("scope", ["frame", "player", "empty_frame"])
def test_hero_identity_metadata_must_match(metadata, scope):
    frame = heroes(10, 100)
    if scope == "player":
        frame["players"][0].update(metadata)
    else:
        frame.update(metadata)
        if scope == "empty_frame":
            frame["players"] = []
    with pytest.raises(ValueError, match="identity"):
        build_historical_snapshot(MATCH, 10, [], [], hero_records=[frame])


def test_hero_duplicate_ticks_fail_and_equal_times_select_the_higher_tick():
    with pytest.raises(ValueError, match="Duplicate hero replay tick"):
        build_historical_snapshot(MATCH, 20, [], [], hero_records=[heroes(10, 100), heroes(20, 100)])
    observation = build_historical_snapshot(
        MATCH, 10, [], [], hero_records=[heroes(10, 101), heroes(10, 100)],
    ).observations["heroes"]
    assert observation.replay_tick == 101


def test_matching_hero_lineage_is_accepted_without_exporting_extra_metadata():
    bare = heroes(10, 100)
    tagged = deepcopy(bare)
    tagged.update(source=MATCH.source, source_match_id=MATCH.source_match_id, match_id=8960991322)
    tagged["players"][0].update(
        source=MATCH.source, source_match_id=MATCH.source_match_id, match_id=8960991322,
        source_game_time_seconds=10, actual_game_time_seconds=Decimal("10"), replay_tick=100,
    )
    assert build_historical_snapshot(MATCH, 10, [], [], hero_records=[tagged]) == build_historical_snapshot(
        MATCH, 10, [], [], hero_records=[bare],
    )


def test_hero_decimal_precision_and_future_independence():
    observed = Decimal("1.0000000000000000000000000000000000000001")
    requested = Decimal("1.0000000000000000000000000000000000000002")
    frame = heroes(observed, 100)
    with localcontext() as context:
        context.prec = 6
        original = build_historical_snapshot(MATCH, requested, [], [], hero_records=[frame])
    observation = original.observations["heroes"]
    assert observation.actual_game_time_seconds == observed
    assert observation.age_seconds == Decimal("1e-40")
    assert build_historical_snapshot(
        MATCH, requested, [], [], hero_records=[frame, heroes(Decimal("1.0000000000000000000000000000000000000003"), 101)],
    ) == original


def test_heroes_are_optional_and_leave_economy_and_team_pov_unchanged():
    args = (MATCH, 2280.0002, [economy(2280.0002, 100634)], pair(2270.0334, 100335))
    original = build_historical_snapshot(*args)
    extended = build_historical_snapshot(*args, hero_records=[heroes(2280.0002, 100634)])
    assert set(original.observations) == {"economy", "pov"}
    assert extended.match == original.match
    for channel, observation in original.observations.items():
        assert extended.observations[channel] == observation
    assert extended.observations["heroes"].origin == "HISTORICAL_ORACLE"
    assert extended.observations["pov"].origin == "HISTORICAL_TEAM_POV"


def test_real_compact_heroes_at_2280_if_local_artifact_exists():
    path = Path(__file__).resolve().parents[1] / "tools/replay_probe/output/8960991322.compact.json"
    if not path.is_file():
        pytest.skip("Optional gitignored compact artifact is not installed.")
    compact = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    assert str(compact["match_id"]) == MATCH.source_match_id
    records = [dict(frame, match_id=compact["match_id"]) for frame in compact["snapshots"]]
    observation = build_historical_snapshot(
        MATCH, Decimal("2280.0002"), [], [], hero_records=records,
    ).observations["heroes"]
    assert observation.status == "AVAILABLE" and observation.origin == "HISTORICAL_ORACLE"
    assert observation.actual_game_time_seconds == Decimal("2280.0002")
    assert observation.replay_tick == 100634 and observation.age_seconds == 0
    selected = next(frame for frame in records if frame["replay_tick"] == 100634)
    assert observation.values["players"] == [
        dict(source=MATCH.source, source_match_id=MATCH.source_match_id, **player)
        for player in selected["players"]
    ]
    assert len(observation.values["players"]) == 10


def spatial(time, tick, **updates):
    return dict(
        match_id=8960991322, replay_sha256="a" * 64,
        source_game_time_seconds=time, replay_tick=tick,
        game_time_seconds=0, target_game_time_seconds=0,
        players=[dict(
            player_slot=slot, team="RADIANT" if slot < 5 else "DIRE", hero_id=slot + 1,
            selected_hero_handle=1000 + slot, hero_entity_handle=1000 + slot,
            hero_entity_class=f"hero_{slot}", life_state=0, position_status="OBSERVED",
            x=Decimal("12889.00000000000000000000000000000001"), y=Decimal("16191.78125"),
        ) for slot in range(10)],
    ) | updates


@pytest.mark.parametrize(("t", "expected"), [
    (Decimal("-1"), None),
    (Decimal("0"), None),
    (Decimal("2270.0333999999999999999999999999999999"), None),
    (Decimal("2270.0334"), Decimal("2270.0334")),
    (Decimal("2280.0002"), Decimal("2270.0334")),
    (Decimal("2280.0333999999999999999999999999999999"), Decimal("2270.0334")),
    (Decimal("2280.0334"), Decimal("2280.0334")),
])
def test_spatial_as_of_never_uses_nominal_grid_or_a_future_frame(t, expected):
    observation = build_historical_snapshot(MATCH, t, [], [], spatial_records=[
        spatial(Decimal("2280.0334"), 100635), spatial(Decimal("2270.0334"), 100335),
    ]).observations["spatial_global"]
    assert observation.origin == "HISTORICAL_ORACLE"
    assert observation.actual_game_time_seconds == expected
    if expected is None:
        assert observation.status == "NO_PRECEDING_SNAPSHOT"
        assert observation.replay_tick is observation.age_seconds is observation.values is None
    else:
        assert observation.status == "AVAILABLE"
        with localcontext() as context:
            context.prec = 60
            assert observation.age_seconds == t - expected


@pytest.mark.parametrize(("t", "requested_tick", "expected_tick"), [
    (Decimal("2280.0002"), 100635, 100335),  # Tick permission cannot admit future game time.
    (Decimal("2280.0334"), 100634, 100335),
    (Decimal("2280.0334"), 100635, 100635),
    (Decimal("2280.0334"), 100000, None),
])
def test_spatial_requires_both_time_and_optional_tick_upper_bounds(t, requested_tick, expected_tick):
    observation = build_historical_snapshot(MATCH, t, [], [], spatial_records=[
        spatial(Decimal("2270.0334"), 100335), spatial(Decimal("2280.0334"), 100635),
    ], requested_replay_tick=requested_tick).observations["spatial_global"]
    assert observation.replay_tick == expected_tick
    if expected_tick is not None:
        assert observation.actual_game_time_seconds <= t
        assert observation.replay_tick <= requested_tick
    else:
        assert observation.status == "NO_PRECEDING_SNAPSHOT"


@pytest.mark.parametrize("tick", [True, -1, 10.5, "100335", Decimal("100335")])
def test_requested_spatial_tick_must_be_a_nonnegative_integer(tick):
    with pytest.raises(ValueError, match="Requested replay tick"):
        build_historical_snapshot(MATCH, 10, [], [], spatial_records=[], requested_replay_tick=tick)


def test_spatial_equal_game_times_and_pauses_do_not_turn_tick_gaps_into_age():
    frames = [spatial(10, 100), spatial(10, 10000)]
    bounded = build_historical_snapshot(
        MATCH, 11, [], [], spatial_records=frames, requested_replay_tick=9999,
    ).observations["spatial_global"]
    unbounded = build_historical_snapshot(MATCH, 11, [], [], spatial_records=frames).observations["spatial_global"]
    assert bounded.replay_tick == 100 and unbounded.replay_tick == 10000
    assert bounded.age_seconds == unbounded.age_seconds == Decimal("1")
    assert build_historical_snapshot(
        MATCH, 0, [], [], spatial_records=[],
    ).observations["spatial_global"].status == "NO_PRECEDING_SNAPSHOT"


def test_spatial_preserves_exact_positions_dead_nulls_and_source_statuses():
    frame = spatial(10, 100, target=1)
    frame["players"][0].update(target=1, visibility="HIDDEN", future_x=99999)
    for slot, life in ((1, 1), (2, 2)):
        frame["players"][slot].update(x=None, y=None, life_state=life, position_status="HERO_NOT_ALIVE")
    frame["players"][3].update(x=None, y=None, life_state=None, position_status="HERO_ENTITY_MISSING")
    before = deepcopy(frame)
    observation = build_historical_snapshot(MATCH, 10, [], [], spatial_records=[frame]).observations["spatial_global"]
    players = observation.values["players"]
    assert len(players) == len({p["player_slot"] for p in players}) == len({p["hero_id"] for p in players}) == 10
    assert [sum(p["team"] == team for p in players) for team in ("RADIANT", "DIRE")] == [5, 5]
    for actual, original in zip(players, frame["players"], strict=True):
        assert actual == dict(
            source=MATCH.source, source_match_id=MATCH.source_match_id,
            **{k: v for k, v in original.items() if k not in ("target", "visibility", "future_x")},
        )
    for slot, life in ((1, 1), (2, 2)):
        assert players[slot]["x"] is players[slot]["y"] is None
        assert players[slot]["life_state"] == life and players[slot]["position_status"] == "HERO_NOT_ALIVE"
    assert players[3]["life_state"] is None and players[3]["position_status"] == "HERO_ENTITY_MISSING"
    assert observation.values["replay_sha256"] == "a" * 64
    players[0]["x"] = Decimal("0")
    assert frame == before


@pytest.mark.parametrize("problem", [
    "partial_roster", "duplicate_slot", "duplicate_hero", "six_radiant", "wrong_team",
    "bad_tick", "nominal_only", "nonfinite_time", "frame_time", "player_tick", "source_tick",
    "player_time", "partial_coordinates", "nonfinite_coordinate", "missing_coordinate",
    "observed_null", "dead_observed", "false_death_status", "bad_life_state", "wrong_selected_hero",
])
def test_invalid_spatial_frames_fail_instead_of_silently_falling_back(problem):
    frame = spatial(20, 200)
    player = frame["players"][0]
    if problem == "partial_roster":
        frame["players"].pop()
    elif problem == "duplicate_slot":
        frame["players"][1]["player_slot"] = player["player_slot"]
    elif problem == "duplicate_hero":
        frame["players"][1]["hero_id"] = player["hero_id"]
    elif problem == "six_radiant":
        frame["players"][5]["team"] = "RADIANT"
    elif problem == "wrong_team":
        player["team"] = "GLOBAL"
    elif problem == "bad_tick":
        frame["replay_tick"] = -1
    elif problem == "nominal_only":
        del frame["source_game_time_seconds"]
    elif problem == "nonfinite_time":
        frame["source_game_time_seconds"] = Decimal("NaN")
    elif problem == "frame_time":
        frame["actual_game_time_seconds"] = Decimal("20.00000000000000000001")
    elif problem == "player_tick":
        player["replay_tick"] = 201
    elif problem == "source_tick":
        frame["source_replay_tick"] = 201
    elif problem == "player_time":
        player["source_game_time_seconds"] = Decimal("20.00000000000000000001")
    elif problem == "partial_coordinates":
        player["x"] = None
    elif problem == "nonfinite_coordinate":
        player["y"] = float("inf")
    elif problem == "missing_coordinate":
        del player["x"]
    elif problem == "observed_null":
        player.update(x=None, y=None)
    elif problem == "dead_observed":
        player["life_state"] = 2
    elif problem == "false_death_status":
        player.update(position_status="HERO_NOT_ALIVE", x=None, y=None)
    elif problem == "bad_life_state":
        player["life_state"] = True
    else:
        player["hero_entity_handle"] = 99999
    with pytest.raises(ValueError):
        build_historical_snapshot(MATCH, 10, [], [], spatial_records=[spatial(0, 1), frame])


@pytest.mark.parametrize("metadata", [
    {"match_id": "another-match"},
    {"source": "other", "source_match_id": MATCH.source_match_id},
    {"source": MATCH.source, "source_match_id": "another-match"},
    {"source": MATCH.source},
    {"replay_sha256": "b" * 64},
    {"replay_sha256": "not-a-sha"},
])
@pytest.mark.parametrize("scope", ["frame", "player"])
def test_spatial_match_and_replay_lineage_must_agree(metadata, scope):
    frame = spatial(10, 100)
    (frame if scope == "frame" else frame["players"][0]).update(metadata)
    with pytest.raises(ValueError):
        build_historical_snapshot(
            MATCH, 10, [], [], spatial_records=[frame], replay_sha256="a" * 64,
        )


def test_spatial_checks_cross_frame_sha_and_duplicate_ticks_without_a_reference_sha():
    with pytest.raises(ValueError, match="SHA-256"):
        build_historical_snapshot(MATCH, 10, [], [], spatial_records=[
            spatial(0, 1), spatial(10, 100, replay_sha256="b" * 64),
        ])
    with pytest.raises(ValueError, match="Duplicate spatial replay tick"):
        build_historical_snapshot(MATCH, 10, [], [], spatial_records=[spatial(0, 1), spatial(10, 1)])
    with pytest.raises(ValueError, match="SHA-256"):
        build_historical_snapshot(MATCH, 10, [], [], spatial_records=[], replay_sha256="bad")
    with pytest.raises(ValueError, match="monotonic"):
        build_historical_snapshot(MATCH, 20, [], [], spatial_records=[spatial(20, 1), spatial(10, 100)])


def test_spatial_global_is_optional_independent_of_pov_and_has_no_future_leakage():
    args = (MATCH, 2280.0002, [economy(2280.0002, 100634)], pair(2270.0334, 100335))
    original = build_historical_snapshot(*args, hero_records=[heroes(2280.0002, 100634)])
    frames = [spatial(2270.0334, 100335)]
    combined = build_historical_snapshot(
        *args, hero_records=[heroes(2280.0002, 100634)], spatial_records=frames,
        requested_replay_tick=100634,
    )
    assert "spatial_global" not in original.observations
    for channel, observation in original.observations.items():
        assert combined.observations[channel] == observation
    assert combined.observations["spatial_global"].origin == "HISTORICAL_ORACLE"
    assert combined.observations["pov"].origin == "HISTORICAL_TEAM_POV"
    assert combined.observations["pov"].values["RADIANT"]["nearest_visible_enemy_distance"] is None
    frames.append(spatial(2280.0334, 100635))
    assert combined == build_historical_snapshot(
        *args, hero_records=[heroes(2280.0002, 100634)], spatial_records=frames,
        requested_replay_tick=100634,
    )
    frames[0]["players"][5]["x"] = Decimal("999999")
    assert build_historical_snapshot(*args, spatial_records=frames).observations["pov"] == original.observations["pov"]


def test_spatial_decimal_time_age_and_coordinates_survive_low_context_precision():
    observed = Decimal("1.0000000000000000000000000000000000000001")
    requested = Decimal("1.0000000000000000000000000000000000000002")
    with localcontext() as context:
        context.prec = 6
        observation = build_historical_snapshot(
            MATCH, requested, [], [], spatial_records=[spatial(observed, 100)],
        ).observations["spatial_global"]
        assert context.prec == 6
    assert observation.actual_game_time_seconds == observed
    assert observation.age_seconds == Decimal("1e-40")
    assert observation.values["players"][0]["x"] == Decimal("12889.00000000000000000000000000000001")


def test_real_spatial_snapshot_at_2280_and_dead_nulls_if_local_artifact_exists():
    path = Path(__file__).resolve().parents[1] / "tools/replay_probe/output/visibility-check/8960991322.compact.spatial.json"
    if not path.is_file():
        pytest.skip("Optional gitignored spatial artifact is not installed.")
    sidecar = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    records = [dict(frame, match_id=sidecar["match_id"], replay_sha256=sidecar["replay_sha256"])
               for frame in sidecar["snapshots"]]
    observation = build_historical_snapshot(
        MATCH, Decimal("2280.0002"), [], [], spatial_records=records,
        requested_replay_tick=100634, replay_sha256=sidecar["replay_sha256"],
    ).observations["spatial_global"]
    assert observation.status == "AVAILABLE" and observation.origin == "HISTORICAL_ORACLE"
    assert observation.actual_game_time_seconds == Decimal("2270.0334")
    assert observation.replay_tick == 100335 and observation.age_seconds == Decimal("9.9668")
    players = observation.values["players"]
    assert len(players) == sum(p["x"] is not None and p["y"] is not None for p in players) == 10
    assert players[0]["hero_entity_class"] == "CDOTA_Unit_Hero_Juggernaut"
    assert players[0]["x"] == Decimal("12889.0") and players[0]["y"] == Decimal("16191.78125")
    dead_frame = build_historical_snapshot(
        MATCH, Decimal("2310.0334"), [], [], spatial_records=records,
    ).observations["spatial_global"]
    missing = [p for p in dead_frame.values["players"] if p["x"] is None]
    assert len(missing) == 3
    assert all(p["y"] is None and p["position_status"] == "HERO_NOT_ALIVE" and p["life_state"] in (1, 2)
               for p in missing)
