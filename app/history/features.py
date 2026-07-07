from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Literal, Protocol

from app.history.domain import HistoricalMatch
from app.history.roster_lineage import (
    HistoricalTournamentChronologyContext,
    RosterLineageGraph,
    build_roster_lineage_graph,
)
from app.history.rosters import RosterSnapshot
from app.tournaments import CompetitiveStage


HistoryBridgeMode = Literal[
    "lineage_chronology_windows",
    "direct_organization_history",
]

NumericFeatureValue = int | float

HISTORICAL_NUMERIC_FEATURE_COLUMNS = [
    "team_a_history_matches",
    "team_a_history_wins",
    "team_a_history_losses",
    "team_a_raw_win_rate",
    "team_a_recency_weighted_matches",
    "team_a_recency_weighted_wins",
    "team_a_recency_weighted_win_rate",
    "team_a_opponent_adjusted_strength",
    "team_b_history_matches",
    "team_b_history_wins",
    "team_b_history_losses",
    "team_b_raw_win_rate",
    "team_b_recency_weighted_matches",
    "team_b_recency_weighted_wins",
    "team_b_recency_weighted_win_rate",
    "team_b_opponent_adjusted_strength",
    "raw_win_rate_diff",
    "recency_weighted_win_rate_diff",
    "opponent_adjusted_strength_diff",
    "history_matches_diff",
]


class HistoricalFeatureRepository(Protocol):
    def list_historical_matches(self) -> list[HistoricalMatch]:
        ...

    def list_historical_matches_before(
        self,
        cutoff_timestamp: datetime,
    ) -> list[HistoricalMatch]:
        ...

    def get_latest_roster_snapshot_for_organization_as_of(
        self,
        source: str,
        source_team_id: str,
        cutoff_timestamp: datetime,
    ) -> RosterSnapshot | None:
        ...

    def list_roster_snapshots_available_before(
        self,
        cutoff_timestamp: datetime,
    ) -> list[RosterSnapshot]:
        ...

    def get_historical_tournament_chronology_context(
        self,
        *,
        source: str,
        tournament_source_id: str,
        cutoff_timestamp: datetime,
    ) -> HistoricalTournamentChronologyContext | None:
        ...


@dataclass(frozen=True)
class RecencyWeightingPolicy:
    decay_days: float = 90.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.decay_days) or self.decay_days <= 0:
            raise ValueError("decay_days must be greater than 0")


@dataclass(frozen=True)
class HistoricalFeaturePolicy:
    recency: RecencyWeightingPolicy = field(
        default_factory=RecencyWeightingPolicy
    )
    neutral_win_rate: float = 0.5
    neutral_strength: float = 0.0
    low_sample_shrinkage_matches: float = 5.0
    opponent_strength_factor: float = 0.5
    opponent_iterations: int = 4

    def __post_init__(self) -> None:
        if not 0 <= self.neutral_win_rate <= 1:
            raise ValueError("neutral_win_rate must be between 0 and 1")
        if not math.isfinite(self.neutral_strength):
            raise ValueError("neutral_strength must be finite")
        if (
            not math.isfinite(self.low_sample_shrinkage_matches)
            or self.low_sample_shrinkage_matches < 0
        ):
            raise ValueError("low_sample_shrinkage_matches must be non-negative")
        if (
            not math.isfinite(self.opponent_strength_factor)
            or self.opponent_strength_factor < 0
        ):
            raise ValueError("opponent_strength_factor must be non-negative")
        if self.opponent_iterations < 1:
            raise ValueError("opponent_iterations must be at least 1")


@dataclass(frozen=True)
class HistoricalPredictionContext:
    source: str
    source_match_id: str
    prediction_timestamp: datetime
    team_a_source_id: str
    team_b_source_id: str
    target_match_id: str | None = None
    tournament_source_id: str | None = None
    tournament_name: str | None = None
    competitive_stage: CompetitiveStage = CompetitiveStage.UNKNOWN

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if not self.source_match_id.strip():
            raise ValueError("source_match_id must not be empty")
        if not self.team_a_source_id.strip():
            raise ValueError("team_a_source_id must not be empty")
        if not self.team_b_source_id.strip():
            raise ValueError("team_b_source_id must not be empty")
        if self.team_a_source_id == self.team_b_source_id:
            raise ValueError("team source IDs must be distinct")


@dataclass(frozen=True)
class TeamHistorySummary:
    history_matches: int
    history_wins: int
    history_losses: int
    raw_win_rate: float
    recency_weighted_matches: float
    recency_weighted_wins: float
    recency_weighted_win_rate: float


@dataclass(frozen=True)
class HistoricalFeatureRow:
    source: str
    source_match_id: str
    prediction_timestamp: datetime
    team_a_source_id: str
    team_b_source_id: str
    target_match_id: str | None
    tournament_source_id: str | None
    tournament_name: str | None
    competitive_stage: CompetitiveStage
    team_a_history_bridge: HistoryBridgeMode
    team_b_history_bridge: HistoryBridgeMode
    team_a_history_matches: int
    team_a_history_wins: int
    team_a_history_losses: int
    team_a_raw_win_rate: float
    team_a_recency_weighted_matches: float
    team_a_recency_weighted_wins: float
    team_a_recency_weighted_win_rate: float
    team_a_opponent_adjusted_strength: float
    team_b_history_matches: int
    team_b_history_wins: int
    team_b_history_losses: int
    team_b_raw_win_rate: float
    team_b_recency_weighted_matches: float
    team_b_recency_weighted_wins: float
    team_b_recency_weighted_win_rate: float
    team_b_opponent_adjusted_strength: float
    raw_win_rate_diff: float
    recency_weighted_win_rate_diff: float
    opponent_adjusted_strength_diff: float
    history_matches_diff: int

    def numeric_features(self) -> dict[str, NumericFeatureValue]:
        return {
            column: getattr(self, column)
            for column in HISTORICAL_NUMERIC_FEATURE_COLUMNS
        }


@dataclass(frozen=True)
class LabeledHistoricalFeatureRow:
    feature_row: HistoricalFeatureRow
    target: int

    def numeric_features(self) -> dict[str, NumericFeatureValue]:
        return self.feature_row.numeric_features()


@dataclass(frozen=True)
class PointInTimeStrengthState:
    prediction_timestamp: datetime
    eligible_matches: tuple[HistoricalMatch, ...]
    team_summaries: Mapping[str, TeamHistorySummary]
    opponent_adjusted_strengths: Mapping[str, float]


@dataclass(frozen=True)
class HistoricalFeatureStatus:
    as_of: datetime
    decay_days: float
    historical_matches_available: int
    usable_match_result_records: int
    stable_teams_in_strength_state: int
    teams_with_no_history: int
    average_raw_history_matches_per_team: float
    average_recency_weighted_history_mass: float
    min_opponent_adjusted_strength: float | None
    max_opponent_adjusted_strength: float | None
    neutral_raw_win_rate: float
    neutral_recency_weighted_win_rate: float
    neutral_opponent_adjusted_strength: float


@dataclass(frozen=True)
class _TeamHistoryWindow:
    source: str
    source_team_id: str
    window_start: datetime | None
    window_end: datetime
    roster_snapshot_id: str | None

    @property
    def team_key(self) -> str:
        return _team_key(self.source, self.source_team_id)


@dataclass(frozen=True)
class _ResolvedCompetitiveHistory:
    bridge_mode: HistoryBridgeMode
    windows: tuple[_TeamHistoryWindow, ...]


@dataclass(frozen=True)
class _TeamResult:
    match: HistoricalMatch
    team_key: str
    opponent_key: str
    won: bool
    weight: float


def calculate_recency_weight(
    ended_at: datetime,
    prediction_timestamp: datetime,
    policy: RecencyWeightingPolicy | None = None,
) -> float:
    recency_policy = policy or RecencyWeightingPolicy()
    if ended_at >= prediction_timestamp:
        raise ValueError("historical match must end before prediction timestamp")

    age_days = (prediction_timestamp - ended_at).total_seconds() / 86_400
    if age_days < 0:
        raise ValueError("age_days must not be negative")
    return math.exp(-age_days / recency_policy.decay_days)


def build_prediction_context_from_match(
    match: HistoricalMatch,
) -> HistoricalPredictionContext:
    if match.team_a_source_id is None or match.team_b_source_id is None:
        raise ValueError("target match must have stable provider team IDs")

    return HistoricalPredictionContext(
        source=match.source,
        source_match_id=match.source_match_id,
        prediction_timestamp=match.started_at,
        team_a_source_id=match.team_a_source_id,
        team_b_source_id=match.team_b_source_id,
        target_match_id=match.id,
        tournament_source_id=match.tournament_source_id,
        tournament_name=match.tournament_name,
        competitive_stage=match.competitive_stage,
    )


def build_historical_match_features(
    repository: HistoricalFeatureRepository,
    context: HistoricalPredictionContext,
    *,
    policy: HistoricalFeaturePolicy | None = None,
    historical_matches: Iterable[HistoricalMatch] | None = None,
) -> HistoricalFeatureRow:
    feature_policy = policy or HistoricalFeaturePolicy()
    eligible_matches = _eligible_matches(
        repository=repository,
        context=context,
        historical_matches=historical_matches,
    )
    strength_state = build_point_in_time_strength_state(
        context.prediction_timestamp,
        eligible_matches,
        policy=feature_policy,
    )
    lineage_graph = build_roster_lineage_graph(
        repository,
        as_of=context.prediction_timestamp,
    )
    team_a_history = resolve_competitive_history(
        repository,
        source=context.source,
        source_team_id=context.team_a_source_id,
        prediction_timestamp=context.prediction_timestamp,
        lineage_graph=lineage_graph,
    )
    team_b_history = resolve_competitive_history(
        repository,
        source=context.source,
        source_team_id=context.team_b_source_id,
        prediction_timestamp=context.prediction_timestamp,
        lineage_graph=lineage_graph,
    )

    team_a_results = _collect_competitive_history_results(
        eligible_matches,
        team_a_history,
        context.prediction_timestamp,
        feature_policy,
    )
    team_b_results = _collect_competitive_history_results(
        eligible_matches,
        team_b_history,
        context.prediction_timestamp,
        feature_policy,
    )
    team_a_summary = _summarize_team_results(team_a_results, feature_policy)
    team_b_summary = _summarize_team_results(team_b_results, feature_policy)
    team_a_strength = _opponent_adjusted_strength_for_results(
        team_a_results,
        strength_state,
        feature_policy,
    )
    team_b_strength = _opponent_adjusted_strength_for_results(
        team_b_results,
        strength_state,
        feature_policy,
    )

    return HistoricalFeatureRow(
        source=context.source,
        source_match_id=context.source_match_id,
        prediction_timestamp=context.prediction_timestamp,
        team_a_source_id=context.team_a_source_id,
        team_b_source_id=context.team_b_source_id,
        target_match_id=context.target_match_id,
        tournament_source_id=context.tournament_source_id,
        tournament_name=context.tournament_name,
        competitive_stage=context.competitive_stage,
        team_a_history_bridge=team_a_history.bridge_mode,
        team_b_history_bridge=team_b_history.bridge_mode,
        team_a_history_matches=team_a_summary.history_matches,
        team_a_history_wins=team_a_summary.history_wins,
        team_a_history_losses=team_a_summary.history_losses,
        team_a_raw_win_rate=team_a_summary.raw_win_rate,
        team_a_recency_weighted_matches=team_a_summary.recency_weighted_matches,
        team_a_recency_weighted_wins=team_a_summary.recency_weighted_wins,
        team_a_recency_weighted_win_rate=(
            team_a_summary.recency_weighted_win_rate
        ),
        team_a_opponent_adjusted_strength=team_a_strength,
        team_b_history_matches=team_b_summary.history_matches,
        team_b_history_wins=team_b_summary.history_wins,
        team_b_history_losses=team_b_summary.history_losses,
        team_b_raw_win_rate=team_b_summary.raw_win_rate,
        team_b_recency_weighted_matches=team_b_summary.recency_weighted_matches,
        team_b_recency_weighted_wins=team_b_summary.recency_weighted_wins,
        team_b_recency_weighted_win_rate=(
            team_b_summary.recency_weighted_win_rate
        ),
        team_b_opponent_adjusted_strength=team_b_strength,
        raw_win_rate_diff=(
            team_a_summary.raw_win_rate - team_b_summary.raw_win_rate
        ),
        recency_weighted_win_rate_diff=(
            team_a_summary.recency_weighted_win_rate
            - team_b_summary.recency_weighted_win_rate
        ),
        opponent_adjusted_strength_diff=team_a_strength - team_b_strength,
        history_matches_diff=(
            team_a_summary.history_matches - team_b_summary.history_matches
        ),
    )


def build_labeled_historical_feature_row(
    match: HistoricalMatch,
    repository: HistoricalFeatureRepository,
    *,
    policy: HistoricalFeaturePolicy | None = None,
    historical_matches: Iterable[HistoricalMatch] | None = None,
) -> LabeledHistoricalFeatureRow | None:
    if match.winner_side not in ("team_a", "team_b"):
        return None
    if match.team_a_source_id is None or match.team_b_source_id is None:
        return None

    context = build_prediction_context_from_match(match)
    feature_row = build_historical_match_features(
        repository,
        context,
        policy=policy,
        historical_matches=historical_matches,
    )
    return LabeledHistoricalFeatureRow(
        feature_row=feature_row,
        target=1 if match.winner_side == "team_a" else 0,
    )


def build_historical_feature_dataset(
    repository: HistoricalFeatureRepository,
    *,
    policy: HistoricalFeaturePolicy | None = None,
) -> list[LabeledHistoricalFeatureRow]:
    matches = tuple(repository.list_historical_matches())
    rows: list[LabeledHistoricalFeatureRow] = []
    for match in sorted(matches, key=_match_target_order_key):
        if not match.usable_for_match_winner_training:
            continue
        row = build_labeled_historical_feature_row(
            match,
            repository,
            policy=policy,
            historical_matches=matches,
        )
        if row is not None:
            rows.append(row)
    return rows


def build_point_in_time_strength_state(
    prediction_timestamp: datetime,
    historical_matches: Iterable[HistoricalMatch],
    *,
    policy: HistoricalFeaturePolicy | None = None,
) -> PointInTimeStrengthState:
    feature_policy = policy or HistoricalFeaturePolicy()
    eligible_matches = tuple(
        sorted(
            (
                match
                for match in historical_matches
                if match.completed_before(prediction_timestamp)
            ),
            key=_match_history_order_key,
        )
    )
    team_results = _results_by_team(
        eligible_matches,
        prediction_timestamp,
        feature_policy,
    )
    team_summaries = {
        team_key: _summarize_team_results(results, feature_policy)
        for team_key, results in sorted(team_results.items())
    }
    strengths = _calculate_opponent_adjusted_strengths(
        team_results,
        team_summaries,
        feature_policy,
    )
    return PointInTimeStrengthState(
        prediction_timestamp=prediction_timestamp,
        eligible_matches=eligible_matches,
        team_summaries=team_summaries,
        opponent_adjusted_strengths=strengths,
    )


def build_historical_feature_status(
    repository: HistoricalFeatureRepository,
    *,
    as_of: datetime,
    policy: HistoricalFeaturePolicy | None = None,
) -> HistoricalFeatureStatus:
    feature_policy = policy or HistoricalFeaturePolicy()
    available_matches = tuple(repository.list_historical_matches_before(as_of))
    state = build_point_in_time_strength_state(
        as_of,
        available_matches,
        policy=feature_policy,
    )
    summaries = tuple(state.team_summaries.values())
    strengths = tuple(state.opponent_adjusted_strengths.values())
    stable_team_count = len(summaries)

    return HistoricalFeatureStatus(
        as_of=as_of,
        decay_days=feature_policy.recency.decay_days,
        historical_matches_available=len(available_matches),
        usable_match_result_records=sum(
            1
            for match in available_matches
            if _match_to_result_pair(match, as_of, feature_policy) is not None
        ),
        stable_teams_in_strength_state=stable_team_count,
        teams_with_no_history=0,
        average_raw_history_matches_per_team=(
            sum(summary.history_matches for summary in summaries) / stable_team_count
            if stable_team_count
            else 0.0
        ),
        average_recency_weighted_history_mass=(
            sum(summary.recency_weighted_matches for summary in summaries)
            / stable_team_count
            if stable_team_count
            else 0.0
        ),
        min_opponent_adjusted_strength=min(strengths) if strengths else None,
        max_opponent_adjusted_strength=max(strengths) if strengths else None,
        neutral_raw_win_rate=feature_policy.neutral_win_rate,
        neutral_recency_weighted_win_rate=feature_policy.neutral_win_rate,
        neutral_opponent_adjusted_strength=feature_policy.neutral_strength,
    )


def resolve_competitive_history(
    repository: HistoricalFeatureRepository,
    *,
    source: str,
    source_team_id: str,
    prediction_timestamp: datetime,
    lineage_graph: RosterLineageGraph | None = None,
) -> _ResolvedCompetitiveHistory:
    snapshot = repository.get_latest_roster_snapshot_for_organization_as_of(
        source,
        source_team_id,
        prediction_timestamp,
    )
    if snapshot is None:
        return _direct_organization_history(
            source=source,
            source_team_id=source_team_id,
            prediction_timestamp=prediction_timestamp,
        )

    graph = lineage_graph or build_roster_lineage_graph(
        repository,
        as_of=prediction_timestamp,
    )
    if snapshot.id not in graph.snapshots_by_id:
        return _direct_organization_history(
            source=source,
            source_team_id=source_team_id,
            prediction_timestamp=prediction_timestamp,
        )

    lineage_snapshots = (*graph.get_predecessor_chain(snapshot.id), snapshot)
    windows: list[_TeamHistoryWindow] = []
    for index, lineage_snapshot in enumerate(lineage_snapshots):
        chronology_point = graph.chronology_points[lineage_snapshot.id]
        window_start = chronology_point.context_at
        next_start = (
            graph.chronology_points[lineage_snapshots[index + 1].id].context_at
            if index + 1 < len(lineage_snapshots)
            else prediction_timestamp
        )
        window_end = min(next_start, prediction_timestamp)
        if window_start >= prediction_timestamp or window_start >= window_end:
            continue
        windows.append(
            _TeamHistoryWindow(
                source=lineage_snapshot.organization.source,
                source_team_id=lineage_snapshot.organization.source_team_id,
                window_start=window_start,
                window_end=window_end,
                roster_snapshot_id=lineage_snapshot.id,
            )
        )

    if not windows:
        return _direct_organization_history(
            source=source,
            source_team_id=source_team_id,
            prediction_timestamp=prediction_timestamp,
        )

    return _ResolvedCompetitiveHistory(
        bridge_mode="lineage_chronology_windows",
        windows=tuple(windows),
    )


def _direct_organization_history(
    *,
    source: str,
    source_team_id: str,
    prediction_timestamp: datetime,
) -> _ResolvedCompetitiveHistory:
    return _ResolvedCompetitiveHistory(
        bridge_mode="direct_organization_history",
        windows=(
            _TeamHistoryWindow(
                source=source,
                source_team_id=source_team_id,
                window_start=None,
                window_end=prediction_timestamp,
                roster_snapshot_id=None,
            ),
        ),
    )


def _eligible_matches(
    *,
    repository: HistoricalFeatureRepository,
    context: HistoricalPredictionContext,
    historical_matches: Iterable[HistoricalMatch] | None,
) -> tuple[HistoricalMatch, ...]:
    candidates: Iterable[HistoricalMatch]
    if historical_matches is None:
        candidates = repository.list_historical_matches_before(
            context.prediction_timestamp
        )
    else:
        candidates = historical_matches

    return tuple(
        sorted(
            (
                match
                for match in candidates
                if match.completed_before(context.prediction_timestamp)
                and not _same_source_match(match, context)
            ),
            key=_match_history_order_key,
        )
    )


def _same_source_match(
    match: HistoricalMatch,
    context: HistoricalPredictionContext,
) -> bool:
    return (
        match.source == context.source
        and match.source_match_id == context.source_match_id
    )


def _collect_competitive_history_results(
    matches: Iterable[HistoricalMatch],
    competitive_history: _ResolvedCompetitiveHistory,
    prediction_timestamp: datetime,
    policy: HistoricalFeaturePolicy,
) -> tuple[_TeamResult, ...]:
    results: list[_TeamResult] = []
    seen: set[tuple[str, str, str]] = set()
    for match in sorted(matches, key=_match_history_order_key):
        for window in competitive_history.windows:
            if not _match_in_history_window(match, window):
                continue
            result = _result_for_team_window(
                match,
                window,
                prediction_timestamp,
                policy,
            )
            if result is None:
                continue
            result_key = (match.source, match.source_match_id, result.team_key)
            if result_key in seen:
                continue
            seen.add(result_key)
            results.append(result)

    return tuple(results)


def _match_in_history_window(
    match: HistoricalMatch,
    window: _TeamHistoryWindow,
) -> bool:
    if match.source != window.source:
        return False
    if not _match_has_source_team(match, window.source_team_id):
        return False
    if window.window_start is not None and match.started_at < window.window_start:
        return False
    return match.started_at < window.window_end


def _match_has_source_team(match: HistoricalMatch, source_team_id: str) -> bool:
    return (
        match.team_a_source_id == source_team_id
        or match.team_b_source_id == source_team_id
    )


def _result_for_team_window(
    match: HistoricalMatch,
    window: _TeamHistoryWindow,
    prediction_timestamp: datetime,
    policy: HistoricalFeaturePolicy,
) -> _TeamResult | None:
    team_key = window.team_key
    pair = _match_to_result_pair(match, prediction_timestamp, policy)
    if pair is None:
        return None
    left, right = pair
    if left.team_key == team_key:
        return left
    if right.team_key == team_key:
        return right
    return None


def _results_by_team(
    matches: Iterable[HistoricalMatch],
    prediction_timestamp: datetime,
    policy: HistoricalFeaturePolicy,
) -> dict[str, tuple[_TeamResult, ...]]:
    result_lists: dict[str, list[_TeamResult]] = {}
    for match in sorted(matches, key=_match_history_order_key):
        pair = _match_to_result_pair(match, prediction_timestamp, policy)
        if pair is None:
            continue
        for result in pair:
            result_lists.setdefault(result.team_key, []).append(result)

    return {
        team_key: tuple(results)
        for team_key, results in sorted(result_lists.items())
    }


def _match_to_result_pair(
    match: HistoricalMatch,
    prediction_timestamp: datetime,
    policy: HistoricalFeaturePolicy,
) -> tuple[_TeamResult, _TeamResult] | None:
    if match.winner_side not in ("team_a", "team_b"):
        return None
    if match.team_a_source_id is None or match.team_b_source_id is None:
        return None
    if match.team_a_source_id == match.team_b_source_id:
        return None
    if match.ended_at is None or not match.completed_before(prediction_timestamp):
        return None

    team_a_key = _team_key(match.source, match.team_a_source_id)
    team_b_key = _team_key(match.source, match.team_b_source_id)
    weight = calculate_recency_weight(
        match.ended_at,
        prediction_timestamp,
        policy.recency,
    )
    team_a_won = match.winner_side == "team_a"
    return (
        _TeamResult(
            match=match,
            team_key=team_a_key,
            opponent_key=team_b_key,
            won=team_a_won,
            weight=weight,
        ),
        _TeamResult(
            match=match,
            team_key=team_b_key,
            opponent_key=team_a_key,
            won=not team_a_won,
            weight=weight,
        ),
    )


def _summarize_team_results(
    results: Iterable[_TeamResult],
    policy: HistoricalFeaturePolicy,
) -> TeamHistorySummary:
    result_tuple = tuple(results)
    history_matches = len(result_tuple)
    history_wins = sum(1 for result in result_tuple if result.won)
    history_losses = history_matches - history_wins
    weighted_matches = sum(result.weight for result in result_tuple)
    weighted_wins = sum(result.weight for result in result_tuple if result.won)

    return TeamHistorySummary(
        history_matches=history_matches,
        history_wins=history_wins,
        history_losses=history_losses,
        raw_win_rate=(
            history_wins / history_matches
            if history_matches
            else policy.neutral_win_rate
        ),
        recency_weighted_matches=weighted_matches,
        recency_weighted_wins=weighted_wins,
        recency_weighted_win_rate=(
            weighted_wins / weighted_matches
            if weighted_matches > 0
            else policy.neutral_win_rate
        ),
    )


def _calculate_opponent_adjusted_strengths(
    team_results: Mapping[str, tuple[_TeamResult, ...]],
    team_summaries: Mapping[str, TeamHistorySummary],
    policy: HistoricalFeaturePolicy,
) -> dict[str, float]:
    previous_strengths = {
        team_key: _base_strength(summary, policy)
        for team_key, summary in team_summaries.items()
    }

    for _ in range(policy.opponent_iterations):
        next_strengths: dict[str, float] = {}
        for team_key in sorted(team_results):
            next_strengths[team_key] = _opponent_adjusted_strength_for_result_set(
                team_results[team_key],
                previous_strengths,
                policy,
            )
        previous_strengths = next_strengths

    return previous_strengths


def _opponent_adjusted_strength_for_results(
    results: tuple[_TeamResult, ...],
    strength_state: PointInTimeStrengthState,
    policy: HistoricalFeaturePolicy,
) -> float:
    return _opponent_adjusted_strength_for_result_set(
        results,
        strength_state.opponent_adjusted_strengths,
        policy,
    )


def _opponent_adjusted_strength_for_result_set(
    results: Iterable[_TeamResult],
    opponent_strengths: Mapping[str, float],
    policy: HistoricalFeaturePolicy,
) -> float:
    weighted_matches = 0.0
    weighted_score = 0.0
    for result in results:
        result_score = 0.5 if result.won else -0.5
        opponent_strength = opponent_strengths.get(
            result.opponent_key,
            policy.neutral_strength,
        )
        weighted_score += result.weight * (
            result_score + policy.opponent_strength_factor * opponent_strength
        )
        weighted_matches += result.weight

    if weighted_matches <= 0:
        return policy.neutral_strength
    raw_strength = weighted_score / weighted_matches
    return _regularize_strength(raw_strength, weighted_matches, policy)


def _base_strength(
    summary: TeamHistorySummary,
    policy: HistoricalFeaturePolicy,
) -> float:
    raw_strength = 2 * (summary.recency_weighted_win_rate - policy.neutral_win_rate)
    return _regularize_strength(
        raw_strength,
        summary.recency_weighted_matches,
        policy,
    )


def _regularize_strength(
    raw_strength: float,
    weighted_matches: float,
    policy: HistoricalFeaturePolicy,
) -> float:
    if weighted_matches <= 0:
        return policy.neutral_strength
    shrinkage_denominator = weighted_matches + policy.low_sample_shrinkage_matches
    shrinkage = (
        1.0
        if shrinkage_denominator <= 0
        else weighted_matches / shrinkage_denominator
    )
    value = (
        policy.neutral_strength * (1 - shrinkage)
        + raw_strength * shrinkage
    )
    return max(-1.0, min(1.0, value))


def _team_key(source: str, source_team_id: str) -> str:
    return f"{source.strip().casefold()}:{source_team_id.strip()}"


def _match_history_order_key(match: HistoricalMatch) -> tuple[datetime, datetime, str, str]:
    ended_at = match.ended_at or datetime.max.replace(tzinfo=match.started_at.tzinfo)
    return (ended_at, match.started_at, match.source, match.source_match_id)


def _match_target_order_key(match: HistoricalMatch) -> tuple[datetime, str, str]:
    return (match.started_at, match.source, match.source_match_id)
