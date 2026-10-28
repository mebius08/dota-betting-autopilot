"""Observed local experience counts, never win rates, form or career totals.

Only explicit history IDs are read through the validated context adapter. No
directory scan, network, result, rating or post-game statistic is used. Strict
integer start_time comparison establishes ordering; IDs/file times never do.
Earlier start alone does not prove completion or LIVE availability of metadata.
Current hero identity inherits HISTORICAL_POST_DRAFT_CONTEXT availability.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable
import math
from pathlib import Path

from app.analytics.historical_match_context import (
    DEFAULT_CACHE_DIR, _match_id, load_historical_match_context,
)


HALF_LIFE_DAYS = 45
HALF_LIFE_SECONDS = HALF_LIFE_DAYS * 86400


def _check_identities(context):
    accounts = [p["account_id"] for p in context["players"] if p["account_id"] is not None]
    if len(accounts) != len(set(accounts)):
        raise ValueError(f"Duplicate account_id in match {context['match_id']}.")
    radiant, dire = context["radiant_team_id"], context["dire_team_id"]
    if radiant is not None and radiant == dire:
        raise ValueError(f"Identical opposing team IDs in match {context['match_id']}.")


def _counts(weights, key, start_time):
    if start_time is None:
        return None, None, "UNKNOWN_TARGET_START_TIME"
    if key is None:
        return None, None, "UNKNOWN_ID"
    values = weights.get(key, ())
    return len(values), math.fsum(values), "OBSERVED" if values else "NO_OBSERVED_HISTORY"


def build_historical_experience(
    match_id: int | str, history_match_ids: Iterable[int | str], *,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> dict:
    """Count distinct strictly earlier matches in the supplied local sample.

    Missing history caches/times are excluded and reported. Corrupt caches fail
    explicitly. Unknown target time/IDs yield NULL features. A known ID with no
    confirmed history has zero *observed* matches, not zero career experience.
    Team experience follows team_id across sides, never assumes a fixed roster.
    Decayed counts sum 2**(-age_seconds / (45*86400)), using math.fsum.
    Coverage, identities and source metadata are not default numeric predictors.
    """
    target = load_historical_match_context(match_id, cache_dir=cache_dir)
    _check_identities(target)
    mid, start = target["match_id"], target["start_time"]
    supplied = [_match_id(value) for value in history_match_ids]
    unique = set(supplied)
    ids = sorted(unique - {mid})
    excluded = Counter(target_match=int(mid in unique))
    missing, prior = [], []
    if start is not None:
        for previous_id in ids:
            try:
                previous = load_historical_match_context(previous_id, cache_dir=cache_dir)
            except FileNotFoundError:
                missing.append(previous_id)
                excluded["missing_context"] += 1
                continue
            time = previous["start_time"]
            if time is None:
                excluded["unknown_start_time"] += 1
            elif time == start:
                excluded["same_start_time"] += 1
            elif time > start:
                excluded["future_start_time"] += 1
            else:
                _check_identities(previous)
                prior.append(previous)
    prior.sort(key=lambda row: (row["start_time"], row["match_id"]))
    team_weights, player_weights, hero_weights = (defaultdict(list) for _ in range(3))
    unknown = Counter(team_id=0, account_id=0, hero_id=0)
    for previous in prior:
        weight = math.exp2(-(start - previous["start_time"]) / HALF_LIFE_SECONDS)
        for side in ("radiant", "dire"):
            team_id = previous[f"{side}_team_id"]
            if team_id is None:
                unknown["team_id"] += 1
            else:
                team_weights[team_id].append(weight)
        for player in previous["players"]:
            account, hero = player["account_id"], player["hero_id"]
            unknown["account_id"] += account is None
            unknown["hero_id"] += hero is None
            if account is not None:
                player_weights[account].append(weight)
                if hero is not None:
                    hero_weights[account, hero].append(weight)
    teams = {}
    for side in ("RADIANT", "DIRE"):
        team_id = target[f"{side.lower()}_team_id"]
        count, decayed, status = _counts(team_weights, team_id, start)
        teams[side] = dict(team_id=team_id, prior_match_count=count,
                           decayed_prior_match_count=decayed, status=status)
    players = []
    for player in target["players"]:
        account, hero = player["account_id"], player["hero_id"]
        count, decayed, status = _counts(player_weights, account, start)
        pair = (account, hero) if account is not None and hero is not None else None
        hero_count, hero_decayed, hero_status = _counts(hero_weights, pair, start)
        players.append(dict(
            player_slot=player["player_slot"], team=player["team"], account_id=account, hero_id=hero,
            prior_match_count=count, decayed_prior_match_count=decayed, status=status,
            prior_hero_match_count=hero_count, decayed_prior_hero_match_count=hero_decayed,
            hero_status=hero_status,
        ))
    coverage = dict(
        scope="EXPLICIT_LOCAL_CONTEXT_SAMPLE", complete_career_history=False,
        limitation="Missing local matches/identities are not evidence of no real career history.",
        history_evaluated=start is not None, requested_unique_history_matches=len(ids),
        duplicate_input_ids=len(supplied)-len(unique),
        eligible_prior_matches=len(prior) if start is not None else None,
        prior_match_ids=[row["match_id"] for row in prior] if start is not None else None,
        excluded={reason: excluded[reason] if start is not None else None for reason in (
            "missing_context", "unknown_start_time", "same_start_time", "future_start_time",
        )} | {"target_match": excluded["target_match"]},
        missing_context_match_ids=missing if start is not None else None,
        unknown_prior_identity_entries=dict(unknown) if start is not None else None,
    )
    return dict(match_id=mid, start_time=start, replay_sha256=target["replay_sha256"],
                provenance="HISTORICAL_OBSERVED_EXPERIENCE",
                context_provenance=target["provenance"], half_life_days=HALF_LIFE_DAYS,
                status="AVAILABLE" if start is not None else "UNKNOWN_TARGET_START_TIME",
                coverage=coverage, teams=teams, players=players)
