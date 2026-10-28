from copy import deepcopy
import hashlib
import json
import math

import pytest

from app.analytics.historical_experience import (
    HALF_LIFE_SECONDS, build_historical_experience,
)


NOW = 1800000000
TARGET = 1000


@pytest.fixture
def contexts(tmp_path):
    def make(mid, time=NOW, *, teams=(10, 20), accounts=None, heroes=None, **extra):
        accounts = list(range(100, 110)) if accounts is None else accounts
        heroes = list(range(1, 11)) if heroes is None else heroes
        value = dict(match_id=mid, replay_sha256="a" * 64, start_time=time, league_id=1,
            radiant_team_id=teams[0], dire_team_id=teams[1], patch=60, picks_bans=None,
            players=[dict(player_slot=i, opendota_player_slot=i if i < 5 else i-5+128,
                          team="RADIANT" if i < 5 else "DIRE", account_id=accounts[i], hero_id=heroes[i])
                     for i in range(10)],
            schema_version=1, provenance=dict(kind="HISTORICAL_MATCH_CONTEXT", metadata_source="LOCAL_JSON",
                metadata_path=None, metadata_payload_sha256="b" * 64,
                compact_path="saved/match.compact.json", compact_sha256="c" * 64,
                endpoint=f"https://api.opendota.com/api/matches/{mid}", retrieved_at_utc=None,
                availability="HISTORICAL; LIVE/PREDRAFT availability not established"), **extra)
        value["content_sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
        (tmp_path / f"{mid}.context.json").write_text(json.dumps(value))
        return value
    make(TARGET)
    return make, tmp_path


def features(contexts, ids):
    return build_historical_experience(TARGET, ids, cache_dir=contexts[1])


def test_prior_included_future_equal_and_target_excluded(contexts):
    make, _ = contexts
    make(1, NOW - HALF_LIFE_SECONDS)
    make(2, NOW + 1)
    make(3, NOW)
    result = features(contexts, [2, TARGET, 3, 1])
    for team in result["teams"].values():
        assert team["prior_match_count"] == 1
        assert team["decayed_prior_match_count"] == 0.5
    for player in result["players"]:
        assert player["prior_match_count"] == player["prior_hero_match_count"] == 1
        assert player["decayed_prior_match_count"] == player["decayed_prior_hero_match_count"] == 0.5
    assert result["coverage"]["prior_match_ids"] == [1]
    assert result["coverage"]["excluded"]["future_start_time"] == 1
    assert result["coverage"]["excluded"]["same_start_time"] == 1
    assert result["coverage"]["excluded"]["target_match"] == 1


def test_experience_uses_team_and_account_ids_not_current_side_or_fixed_roster(contexts):
    make, _ = contexts
    accounts = list(range(100, 110))
    accounts[0], accounts[6] = accounts[6], accounts[0]
    make(1, NOW-100, teams=(30, 10), accounts=accounts)
    result = features(contexts, [1])
    assert result["teams"]["RADIANT"]["prior_match_count"] == 1
    assert result["teams"]["DIRE"]["prior_match_count"] == 0
    assert result["players"][0]["prior_match_count"] == 1
    assert result["players"][0]["prior_hero_match_count"] == 0  # Previously hero 7, not hero 1.
    assert result["players"][6]["prior_hero_match_count"] == 0


def test_different_accounts_on_same_hero_never_share_experience(contexts):
    make, _ = contexts
    make(1, NOW-1, accounts=list(range(200, 210)))
    result = features(contexts, [1])
    assert all(p["prior_match_count"] == p["prior_hero_match_count"] == 0 for p in result["players"])
    assert result["teams"]["RADIANT"]["prior_match_count"] == 1


def test_decay_45_days_exact_half_and_90_days_quarter(contexts):
    make, _ = contexts
    make(1, NOW-HALF_LIFE_SECONDS)
    make(2, NOW-2*HALF_LIFE_SECONDS)
    result = features(contexts, [1, 2])
    assert result["half_life_days"] == 45
    assert result["teams"]["RADIANT"]["prior_match_count"] == 2
    assert result["teams"]["RADIANT"]["decayed_prior_match_count"] == 0.75
    assert result["players"][0]["decayed_prior_hero_match_count"] == 0.75


def test_null_target_ids_do_not_get_zero_or_reconstructed_links(contexts):
    make, _ = contexts
    accounts, heroes = list(range(100, 110)), list(range(1, 11))
    accounts[0], heroes[1] = None, None
    make(TARGET, teams=(None, 20), accounts=accounts, heroes=heroes)
    make(1, NOW-1)
    result = features(contexts, [1])
    assert result["teams"]["RADIANT"]["prior_match_count"] is None
    assert result["teams"]["RADIANT"]["decayed_prior_match_count"] is None
    assert result["teams"]["RADIANT"]["status"] == "UNKNOWN_ID"
    assert result["players"][0]["prior_match_count"] is None
    assert result["players"][0]["prior_hero_match_count"] is None
    assert result["players"][1]["prior_match_count"] == 1
    assert result["players"][1]["prior_hero_match_count"] is None
    assert result["players"][1]["hero_status"] == "UNKNOWN_ID"


def test_unknown_target_time_yields_nulls_and_does_not_read_history(contexts):
    make, folder = contexts
    make(TARGET, None)
    (folder / "1.context.json").write_text("not JSON; must not be read")
    result = features(contexts, [1, 2])
    assert result["status"] == "UNKNOWN_TARGET_START_TIME"
    assert result["coverage"]["history_evaluated"] is False
    assert result["coverage"]["eligible_prior_matches"] is None
    assert all(t["prior_match_count"] is t["decayed_prior_match_count"] is None
               for t in result["teams"].values())
    assert all(p["prior_match_count"] is p["prior_hero_match_count"] is None for p in result["players"])


def test_unknown_history_time_missing_cache_and_partial_ids_are_reported(contexts):
    make, _ = contexts
    make(1, None)
    accounts, heroes = list(range(100, 110)), list(range(1, 11))
    accounts[0], heroes[1] = None, None
    make(2, NOW-1, teams=(None, 20), accounts=accounts, heroes=heroes)
    result = features(contexts, [1, 2, 3])
    coverage = result["coverage"]
    assert coverage["complete_career_history"] is False
    assert coverage["excluded"]["unknown_start_time"] == 1
    assert coverage["missing_context_match_ids"] == [3]
    assert coverage["unknown_prior_identity_entries"] == dict(team_id=1, account_id=1, hero_id=1)
    assert result["players"][0]["prior_match_count"] == 0
    assert result["players"][1]["prior_match_count"] == 1
    assert result["players"][1]["prior_hero_match_count"] == 0


def test_empty_observed_history_is_not_complete_career_history(contexts):
    result = features(contexts, [])
    assert result["coverage"]["eligible_prior_matches"] == 0
    assert result["coverage"]["complete_career_history"] is False
    assert result["teams"]["RADIANT"]["prior_match_count"] == 0
    assert result["teams"]["RADIANT"]["status"] == "NO_OBSERVED_HISTORY"
    assert all(p["prior_hero_match_count"] == 0 for p in result["players"])


def test_deduplication_determinism_finite_weights_and_read_only(contexts):
    make, folder = contexts
    make(1, NOW-1)
    make(2, NOW-HALF_LIFE_SECONDS)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    ids = [1, "1", 2, TARGET]
    original = deepcopy(ids)
    result = features(contexts, ids)
    assert result == features(contexts, list(reversed(ids)))
    assert ids == original
    assert result["coverage"]["duplicate_input_ids"] == 1
    assert result["teams"]["RADIANT"]["prior_match_count"] == 2
    assert math.isfinite(result["teams"]["RADIANT"]["decayed_prior_match_count"])
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_future_outcomes_and_arbitrary_fields_never_enter_features(contexts):
    make, _ = contexts
    make(1, NOW-1, radiant_win=True, final_rating=9999, future_target=9999)
    baseline = features(contexts, [1])
    make(1, NOW-1, radiant_win=False, final_rating=-9999, future_target=-9999)
    make(2, NOW+1)
    changed = features(contexts, [1, 2])
    assert changed["teams"] == baseline["teams"] and changed["players"] == baseline["players"]
    assert "future_target" not in changed and "final_rating" not in changed


@pytest.mark.parametrize("time", [0, -1, True, 1.2, "1800000000"])
def test_untrusted_time_never_falls_back_to_match_id_or_mtime(contexts, time):
    make, _ = contexts
    make(1, time)
    with pytest.raises(ValueError):
        features(contexts, [1])


@pytest.mark.parametrize("damage", ["duplicate_account", "identical_teams", "checksum"])
def test_ambiguous_or_corrupt_history_fails_explicitly(contexts, damage):
    make, folder = contexts
    if damage == "duplicate_account":
        make(1, NOW-1, accounts=[100]*10)
    elif damage == "identical_teams":
        make(1, NOW-1, teams=(10, 10))
    else:
        make(1, NOW-1)
        path = folder / "1.context.json"
        value = json.loads(path.read_text())
        value["start_time"] -= 1
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        features(contexts, [1])
