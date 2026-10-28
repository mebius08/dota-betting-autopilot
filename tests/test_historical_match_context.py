from copy import deepcopy
import hashlib
import json

import pytest

from app.analytics.historical_match_context import (
    CONTEXT_FIELDS, DRAFT_FIELDS, PLAYER_FIELDS, PROVENANCE, SOURCE_FIELDS,
    adapt_match_context, load_historical_match_context,
)


def seal(value):
    value["content_sha256"] = hashlib.sha256(json.dumps(
        {k: v for k, v in value.items() if k != "content_sha256"},
        sort_keys=True, allow_nan=False,
    ).encode()).hexdigest()
    return value


@pytest.fixture
def cache():
    return seal(dict(
        schema_version=1, match_id=12345678901, replay_sha256="a" * 64,
        start_time=1787486918, league_id=19719, radiant_team_id=101, dire_team_id=202,
        patch=60,
        players=[dict(player_slot=i, opendota_player_slot=i if i < 5 else 128+i-5,
                      team="RADIANT" if i < 5 else "DIRE", account_id=2**53+i+1,
                      hero_id=i+1) for i in range(10)],
        # Deliberately not sorted by order: preserve both list and explicit order.
        picks_bans=[dict(is_pick=True, team="DIRE", hero_id=6, order=1),
                    dict(is_pick=False, team="RADIANT", hero_id=20, order=0)],
        provenance=dict(kind="HISTORICAL_MATCH_CONTEXT", metadata_source="LOCAL_JSON",
                        metadata_path="saved/opendota.json", metadata_payload_sha256="b" * 64,
                        compact_path="saved/12345678901.compact.json", compact_sha256="c" * 64,
                        endpoint="https://api.opendota.com/api/matches/12345678901",
                        retrieved_at_utc=None,
                        availability="HISTORICAL; LIVE/PREDRAFT availability not established"),
    ))


def test_exact_identity_sides_draft_patch_and_provenance(cache):
    original = deepcopy(cache)
    result = adapt_match_context(cache, match_id=str(cache["match_id"]))
    assert set(result) == set(CONTEXT_FIELDS) | {"provenance", "source_provenance"}
    assert result["match_id"] == 12345678901 and result["replay_sha256"] == "a" * 64
    assert (result["radiant_team_id"], result["dire_team_id"]) == (101, 202)
    assert result["players"] == cache["players"]
    assert result["players"][5] == dict(player_slot=5, opendota_player_slot=128,
                                        team="DIRE", account_id=2**53+6, hero_id=6)
    assert [row["order"] for row in result["picks_bans"]] == [1, 0]
    assert result["patch"] == 60 and result["provenance"] == PROVENANCE
    assert result["source_provenance"] == cache["provenance"]
    assert "LIVE/PREDRAFT availability not established" in result["source_provenance"]["availability"]
    assert cache == original
    result["players"][0]["hero_id"] = 999
    result["picks_bans"][0]["order"] = 999
    result["source_provenance"]["kind"] = "changed"
    assert cache == original


def test_nulls_and_missing_draft_are_never_inferred(cache):
    for field in ("start_time", "league_id", "radiant_team_id", "dire_team_id", "patch", "picks_bans"):
        cache[field] = None
    for player in cache["players"]:
        player.update(account_id=None, hero_id=None, opendota_player_slot=None)
    result = adapt_match_context(seal(cache), match_id=cache["match_id"])
    assert result["picks_bans"] is result["patch"] is result["radiant_team_id"] is None
    assert result["start_time"] is result["league_id"] is result["dire_team_id"] is None
    assert all(row["account_id"] is row["hero_id"] is row["opendota_player_slot"] is None
               for row in result["players"])


def test_null_order_is_not_reconstructed_and_patch_zero_is_preserved(cache):
    cache["patch"] = 0
    cache["picks_bans"][0]["order"] = None
    result = adapt_match_context(seal(cache), match_id=cache["match_id"])
    assert result["patch"] == 0
    assert result["picks_bans"][0]["order"] is None


def test_whitelists_exclude_outcomes_statistics_and_nested_arbitrary_data(cache):
    cache.update(radiant_win=True, future_targets=[999], duration=9999)
    cache["players"][0].update(kills=50, total_gold=99999, rank=999)
    cache["picks_bans"][0]["post_event_label"] = 999
    cache["provenance"]["arbitrary_statistics"] = {"winner": "DIRE"}
    result = adapt_match_context(seal(cache), match_id=cache["match_id"])
    assert set(result) == set(CONTEXT_FIELDS) | {"provenance", "source_provenance"}
    assert all(set(row) == set(PLAYER_FIELDS) for row in result["players"])
    assert all(set(row) == set(DRAFT_FIELDS) for row in result["picks_bans"])
    assert set(result["source_provenance"]) == set(SOURCE_FIELDS)


def test_loader_reads_only_selected_file_and_leaves_everything_unchanged(cache, tmp_path):
    selected = tmp_path / f"{cache['match_id']}.context.json"
    selected.write_text(json.dumps(cache))
    other = tmp_path / "2.context.json"
    other.write_text("unrelated invalid JSON")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = load_historical_match_context(cache["match_id"], cache_dir=tmp_path)
    assert result["players"] == cache["players"]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    with pytest.raises(FileNotFoundError, match="3.context.json"):
        load_historical_match_context(3, cache_dir=tmp_path)


@pytest.mark.parametrize("mid", [None, True, 0, -1, 1.0, "../1", "1.0", "1e3"])
def test_invalid_requested_identity_is_rejected_before_file_access(mid, tmp_path):
    with pytest.raises(ValueError, match="match_id"):
        load_historical_match_context(mid, cache_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_match_mismatch_and_checksum_damage_fail_explicitly(cache):
    with pytest.raises(ValueError, match="match_id mismatch"):
        adapt_match_context(cache, match_id=2)
    cache["patch"] += 1
    with pytest.raises(ValueError, match="checksum mismatch"):
        adapt_match_context(cache, match_id=cache["match_id"])


@pytest.mark.parametrize("damage", [
    "schema", "replay_sha", "source_sha", "source_kind", "source_type", "missing_field",
    "missing_player", "duplicate_slot", "side", "native_slot", "duplicate_hero",
    "account_float", "anonymous_account", "draft_shape", "duplicate_order", "draft_side",
    "duplicate_action", "order_float", "missing_player_field",
])
def test_malformed_cache_is_not_silently_repaired(cache, damage):
    if damage == "schema":
        cache["schema_version"] = True
    elif damage == "replay_sha":
        cache["replay_sha256"] = "invalid"
    elif damage == "source_sha":
        cache["provenance"]["compact_sha256"] = "invalid"
    elif damage == "source_kind":
        cache["provenance"]["kind"] = "LIVE"
    elif damage == "source_type":
        cache["provenance"]["metadata_source"] = "UNCONFIRMED"
    elif damage == "missing_field":
        del cache["patch"]
    elif damage == "missing_player":
        cache["players"].pop()
    elif damage == "duplicate_slot":
        cache["players"][1]["player_slot"] = 0
    elif damage == "side":
        cache["players"][0]["team"] = "DIRE"
    elif damage == "native_slot":
        cache["players"][5]["opendota_player_slot"] = 5
    elif damage == "duplicate_hero":
        cache["players"][1]["hero_id"] = 1
    elif damage == "account_float":
        cache["players"][0]["account_id"] = 12.0
    elif damage == "anonymous_account":
        cache["players"][0]["account_id"] = 4294967295
    elif damage == "draft_shape":
        cache["picks_bans"] = {}
    elif damage == "duplicate_order":
        cache["picks_bans"][1]["order"] = 1
    elif damage == "draft_side":
        cache["picks_bans"][0]["team"] = 1
    elif damage == "duplicate_action":
        cache["picks_bans"][1] = dict(cache["picks_bans"][0], order=0)
    elif damage == "order_float":
        cache["picks_bans"][0]["order"] = 1.0
    else:
        del cache["players"][0]["account_id"]
    with pytest.raises(ValueError):
        adapt_match_context(seal(cache), match_id=cache["match_id"])
