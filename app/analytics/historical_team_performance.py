"""Read-only historical team results from supplied opendota_pro_matches rows.

Completion is the catalog-derived start_time + duration_seconds boundary, not
an independently observed wall-clock end timestamp or result-publication time.
Only strictly completed prior matches contribute. Decay age is measured from
that completion boundary, unlike the start-based observed-experience counts.
These are sample statistics, not opponent-adjusted strength or win probability.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
import math

from app.analytics.historical_experience import HALF_LIFE_DAYS, HALF_LIFE_SECONDS


SOURCE = "opendota/proMatches"
FIELDS = ("source", "start_time", "duration_seconds", "radiant_team_id", "dire_team_id", "radiant_win")
EXCLUSIONS = (
    "target_match", "unknown_match_id", "unknown_source", "unknown_start_time",
    "unknown_duration", "not_started_before_target", "not_completed_before_target",
    "unknown_team_id", "unknown_result",
)


def _positive(value, name):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer or NULL.")
    return value or None  # Source zero IDs/times and zero duration cannot prove completion.


def _features(target, contributions, coverage):
    teams = {}
    for side in ("RADIANT", "DIRE"):
        team_id = target[f"{side.lower()}_team_id"]
        status = ("UNKNOWN_TARGET_START_TIME" if target["start_time"] is None else
                  "UNKNOWN_TEAM_ID" if team_id is None else None)
        if status is not None:
            stats = dict(prior_match_count=None, wins=None, losses=None,
                         weighted_wins=None, weighted_matches=None, weighted_win_fraction=None)
        else:
            observations = contributions.get(team_id, ())
            wins = sum(won for _, won in observations)
            weighted = math.fsum(weight for weight, _ in observations)
            weighted_wins = math.fsum(weight for weight, won in observations if won)
            stats = dict(prior_match_count=len(observations), wins=wins,
                         losses=len(observations)-wins, weighted_wins=weighted_wins,
                         weighted_matches=weighted,
                         weighted_win_fraction=weighted_wins/weighted if weighted else None)
            status = "NO_OBSERVED_HISTORY" if not observations else "AVAILABLE" if weighted else "NO_EFFECTIVE_WEIGHT"
        teams[side] = dict(team_id=team_id, status=status, **stats)
    return dict(match_id=target["match_id"], start_time=target["start_time"],
                provenance="HISTORICAL_OPENDOTA_TEAM_PERFORMANCE", source=SOURCE,
                half_life_days=HALF_LIFE_DAYS, decay_reference="CATALOG_COMPLETION_TIME",
                completion_time_basis="start_time + duration_seconds", coverage=coverage, teams=teams)


def build_historical_team_performance(target: Mapping, history: Iterable[Mapping]) -> dict:
    """Summarize supplied catalog rows for both target teams, without any I/O.

    target needs only match_id/start_time/radiant_team_id/dire_team_id; its
    result and duration are never read. History uses the SQL column names and
    confirmed source='opendota/proMatches'. Missing values are excluded with
    reasons, malformed values/conflicting duplicates raise. A team ID follows
    the catalog identity across sides, never asserts an unchanged player roster.
    Coverage describes the supplied corpus, including exclusions; it is metadata,
    not a numeric predictor derived from future catalog coverage.
    """
    if not isinstance(target, Mapping):
        raise ValueError("Target must be match metadata.")
    current = {field: _positive(target.get(field), field) for field in (
        "match_id", "start_time", "radiant_team_id", "dire_team_id",
    )}
    if current["match_id"] is None:
        raise ValueError("A confirmed target match_id is required.")
    if current["radiant_team_id"] is not None and current["radiant_team_id"] == current["dire_team_id"]:
        raise ValueError("Opposing target team IDs must differ.")
    coverage = dict(scope="SUPPLIED_PROFESSIONAL_CATALOG_SAMPLE", complete_career_history=False,
        team_id_is_fixed_roster=False, history_evaluated=current["start_time"] is not None,
        limitation="Available professional catalog only; neither full career nor LIVE publication coverage is established.",
        input_rows=None, unique_history_matches=None, duplicate_rows=None,
        eligible_completed_matches=None, eligible_relevant_matches=None,
        excluded=dict.fromkeys(EXCLUSIONS))
    contributions = defaultdict(list)
    if current["start_time"] is None:
        return _features(current, contributions, coverage)

    excluded, unique, duplicates, input_rows = Counter(), {}, 0, 0
    for row in history:
        input_rows += 1
        if not isinstance(row, Mapping):
            raise ValueError("History must contain catalog row mappings.")
        mid = _positive(row.get("match_id"), "match_id")
        if mid is None:
            excluded["unknown_match_id"] += 1
            continue
        if mid == current["match_id"]:
            excluded["target_match"] += 1
            continue  # Never inspect the target's outcome, even in the history iterable.
        selected = {field: row.get(field) for field in FIELDS}
        if mid in unique:
            if any(type(unique[mid][field]) is not type(selected[field])
                   or unique[mid][field] != selected[field] for field in FIELDS):
                raise ValueError(f"Conflicting historical match_id: {mid}.")
            duplicates += 1
        else:
            unique[mid] = selected
    completed = relevant = 0
    for mid, row in sorted(unique.items()):
        if row["source"] is None:
            excluded["unknown_source"] += 1
            continue
        if row["source"] != SOURCE:
            raise ValueError(f"Unsupported historical source for match {mid}.")
        start = _positive(row["start_time"], "start_time")
        duration = _positive(row["duration_seconds"], "duration_seconds")
        if start is None:
            excluded["unknown_start_time"] += 1
            continue
        if duration is None:
            excluded["unknown_duration"] += 1
            continue
        end = start + duration
        if start >= current["start_time"]:
            excluded["not_started_before_target"] += 1
            continue
        if end >= current["start_time"]:
            excluded["not_completed_before_target"] += 1
            continue
        radiant, dire = (_positive(row[f"{side}_team_id"], f"{side}_team_id")
                         for side in ("radiant", "dire"))
        if radiant is None or dire is None:
            excluded["unknown_team_id"] += 1
            continue
        if radiant == dire:
            raise ValueError(f"Identical opposing team IDs in match {mid}.")
        won = row["radiant_win"]
        if won is None:
            excluded["unknown_result"] += 1
            continue
        if type(won) is not bool:
            raise ValueError(f"radiant_win must be boolean or NULL in match {mid}.")
        completed += 1
        weight = math.exp2(-(current["start_time"] - end) / HALF_LIFE_SECONDS)
        used = False
        for team_id, winner in ((radiant, won), (dire, not won)):
            if team_id in (current["radiant_team_id"], current["dire_team_id"]):
                contributions[team_id].append((weight, winner))
                used = True
        relevant += used
    coverage.update(input_rows=input_rows, unique_history_matches=len(unique), duplicate_rows=duplicates,
                    eligible_completed_matches=completed, eligible_relevant_matches=relevant,
                    excluded={reason: excluded[reason] for reason in EXCLUSIONS})
    return _features(current, contributions, coverage)
