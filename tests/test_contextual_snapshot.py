from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json

import pytest

from app.analytics.contextual_snapshot import compose_historical_snapshot
from app.analytics.historical_snapshot import (
    ECONOMY_FIELDS, HERO_METRICS, HistoricalObservation, MatchIdentity,
    build_historical_snapshot,
)


@pytest.fixture
def inputs(tmp_path):
    mid = 8960991322
    players = [dict(player_slot=i, opendota_player_slot=i if i < 5 else 128+i-5,
                    team="RADIANT" if i < 5 else "DIRE", hero_id=i+1, account_id=2**53+i+1)
               for i in range(10)]
    cache = dict(schema_version=1, match_id=mid, replay_sha256="a" * 64,
        start_time=1787486918, league_id=19719, radiant_team_id=101, dire_team_id=202,
        players=players, picks_bans=[dict(is_pick=True, team="DIRE", hero_id=6, order=1),
                                   dict(is_pick=False, team="RADIANT", hero_id=20, order=0)],
        patch=60, provenance=dict(kind="HISTORICAL_MATCH_CONTEXT", metadata_source="LOCAL_JSON",
            metadata_path="saved/opendota.json", metadata_payload_sha256="b" * 64,
            compact_path="saved/match.compact.json", compact_sha256="c" * 64,
            endpoint=f"https://api.opendota.com/api/matches/{mid}", retrieved_at_utc=None,
            availability="HISTORICAL; LIVE/PREDRAFT availability not established"))
    def save():
        cache["content_sha256"] = hashlib.sha256(json.dumps(
            {k: v for k, v in cache.items() if k != "content_sha256"},
            sort_keys=True, allow_nan=False,
        ).encode()).hexdigest()
        (tmp_path / f"{mid}.context.json").write_text(json.dumps(cache))
    save()
    time = Decimal("2280.000200000000000000000000001")
    observed = Decimal("2270.033400000000000000000000001")
    heroes = [dict(player_slot=p["player_slot"], team=p["team"], hero_id=p["hero_id"],
                   hero_name=f"hero_{p['hero_id']}", items=None,
                   **{name: None for name in HERO_METRICS}) for p in players]
    spatial = [dict(player_slot=p["player_slot"], team=p["team"], hero_id=p["hero_id"],
                    x=None, y=None, position_status="HERO_NOT_ALIVE", life_state=2)
               for p in players]
    economy = dict(actual_game_time_seconds=observed, replay_tick=100335,
                   **{name: None for name in ECONOMY_FIELDS})
    snapshot = build_historical_snapshot(
        MatchIdentity("replay_clarity", str(mid)), time, [economy], [],
        hero_records=[dict(source_game_time_seconds=observed, replay_tick=100335, players=heroes)],
        spatial_records=[dict(source_game_time_seconds=observed, replay_tick=100335,
                              replay_sha256="a" * 64, players=spatial)],
        enemy_records=[], requested_replay_tick=100634, replay_sha256="a" * 64,
    )
    return mid, snapshot, cache, save, tmp_path


def compose(inputs, **kwargs):
    mid, snapshot, _, _, folder = inputs
    return compose_historical_snapshot(mid, snapshot, phase="POST_DRAFT", cache_dir=folder, **kwargs)


def test_composition_preserves_all_channels_ids_provenance_and_exact_times(inputs):
    mid, snapshot, cache, _, _ = inputs
    result = compose(inputs)
    assert result.snapshot == snapshot and result.snapshot is not snapshot
    assert result.phase == "POST_DRAFT"
    context = result.match_context
    assert context["match_id"] == mid
    assert context["players"] == cache["players"]
    assert context["players"][5]["account_id"] == 2**53+6
    assert context["picks_bans"] == cache["picks_bans"]
    assert context["patch"] == 60
    assert context["provenance"] == "HISTORICAL_POST_DRAFT_CONTEXT"
    assert result.snapshot.observations["heroes"].origin == "HISTORICAL_ORACLE"
    assert result.snapshot.observations["pov"].origin == "HISTORICAL_TEAM_POV"
    for name, observation in snapshot.observations.items():
        copied = result.snapshot.observations[name]
        assert copied.actual_game_time_seconds == observation.actual_game_time_seconds
        assert copied.replay_tick == observation.replay_tick
        assert copied.age_seconds == observation.age_seconds
    assert result.snapshot.requested_game_time_seconds == Decimal("2280.000200000000000000000000001")
    assert result.snapshot.observations["heroes"].age_seconds == Decimal("9.9668")


def test_explicit_nulls_missing_draft_and_missing_channels_stay_missing(inputs):
    _, snapshot, cache, save, _ = inputs
    cache.update(picks_bans=None, patch=None, radiant_team_id=None)
    cache["players"][0].update(account_id=None, hero_id=None, opendota_player_slot=None)
    save()
    result = compose(inputs)
    assert result.match_context["picks_bans"] is None
    assert result.match_context["players"][0]["hero_id"] is None  # Never filled from game state.
    assert result.match_context["players"][0]["account_id"] is None
    assert result.snapshot.observations["heroes"].values["players"][0]["hero_id"] == 1
    for name in ("pov", "pov_enemy_positions"):
        assert result.snapshot.observations[name].status == "NO_PRECEDING_SNAPSHOT"
        assert result.snapshot.observations[name].values is None
    assert result.snapshot == snapshot


@pytest.mark.parametrize("phase", [None, False, True, "PRE_DRAFT", "DRAFT", "LIVE", ""])
def test_non_post_draft_phase_fails_before_reading_cache(inputs, phase):
    mid, snapshot, _, _, folder = inputs
    with pytest.raises(ValueError, match="POST_DRAFT"):
        compose_historical_snapshot(mid, snapshot, phase=phase, cache_dir=folder / "absent")


def test_phase_must_be_explicit(inputs):
    mid, snapshot, _, _, folder = inputs
    with pytest.raises(TypeError, match="phase"):
        compose_historical_snapshot(mid, snapshot, cache_dir=folder)


@pytest.mark.parametrize("match", [MatchIdentity("replay_clarity", "1"),
                                   MatchIdentity("live", "8960991322")])
def test_cross_match_and_source_are_rejected(inputs, match):
    mid, snapshot, _, _, folder = inputs
    with pytest.raises(ValueError, match="match_id or source mismatch"):
        compose_historical_snapshot(mid, replace(snapshot, match=match),
                                    phase="POST_DRAFT", cache_dir=folder)


def test_unknown_or_live_channel_is_not_promoted_to_historical(inputs):
    _, snapshot, _, _, _ = inputs
    snapshot.observations["outcome"] = HistoricalObservation("POST_GAME", "AVAILABLE", None, None, None, {})
    with pytest.raises(ValueError, match="channel or provenance"):
        compose(inputs)


@pytest.mark.parametrize("channel, field, value", [
    ("heroes", "hero_id", 999), ("heroes", "team", "DIRE"),
    ("heroes", "player_slot", 99), ("heroes", "account_id", 999),
    ("heroes", "source_match_id", "1"), ("spatial_global", "hero_id", 999),
])
def test_actor_links_must_agree_without_fallback(inputs, channel, field, value):
    _, snapshot, _, _, _ = inputs
    snapshot.observations[channel].values["players"][0][field] = value
    with pytest.raises(ValueError, match="mismatch|not present"):
        compose(inputs)


def test_available_replay_sha_must_agree(inputs):
    _, snapshot, _, _, _ = inputs
    snapshot.observations["spatial_global"].values["replay_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="SHA mismatch"):
        compose(inputs)


def test_enemy_identity_and_observer_sides_are_checked_without_adding_accounts(inputs):
    _, snapshot, _, _, _ = inputs
    row = dict(source="replay_clarity", source_match_id=str(inputs[0]), replay_sha256="a" * 64,
               pov_team="RADIANT", enemy_player_slot=5, enemy_hero_id=6, enemy_team="DIRE",
               current_x=None, current_y=None, last_seen_x=Decimal("1.01"),
               last_seen_y=Decimal("2.02"), last_seen_game_time_seconds=Decimal("2260.0001"))
    observation = HistoricalObservation("HISTORICAL_TEAM_POV", "AVAILABLE", Decimal("2270.0334"),
                                        100335, Decimal("9.9668"), {"RADIANT": [row], "DIRE": []})
    snapshot.observations["pov_enemy_positions"] = observation
    result = compose(inputs)
    assert result.snapshot.observations["pov_enemy_positions"] == observation
    assert "account_id" not in result.snapshot.observations["pov_enemy_positions"].values["RADIANT"][0]
    row["enemy_hero_id"] = 999
    with pytest.raises(ValueError, match="hero_id mismatch"):
        compose(inputs)
    row["enemy_hero_id"], row["pov_team"] = 6, "DIRE"
    with pytest.raises(ValueError, match="observer side mismatch"):
        compose(inputs)


def test_inputs_and_cache_are_read_only_and_context_does_not_enter_observations(inputs):
    _, snapshot, _, _, folder = inputs
    before = deepcopy(snapshot)
    files = {p.name: p.read_bytes() for p in folder.iterdir()}
    result = compose(inputs)
    assert snapshot == before
    assert set(result.snapshot.observations) == set(before.observations)
    assert result.snapshot.observations["heroes"].values["players"][0]["items"] is None
    assert all("account_id" not in row for row in result.snapshot.observations["heroes"].values["players"])
    result.snapshot.observations["heroes"].values["players"][0]["hero_id"] = 999
    result.match_context["players"][0]["account_id"] = 999
    assert snapshot == before
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == files
