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
