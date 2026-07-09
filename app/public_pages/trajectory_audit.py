from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import ceil
from statistics import median
from typing import TYPE_CHECKING

from app.draft_history import (
    HistoricalDotaAdvantagePoint,
    HistoricalDotaGame,
    HistoricalDotaPlayerFinalStats,
    draft_game_competition_family,
)
from app.public_pages.ingestion import STRATZ_PUBLIC_SOURCE

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


class TrajectoryTimeSemanticsConclusion(str, Enum):
    CONFIRMED = "TRAJECTORY_TIME_SEMANTICS_CONFIRMED"
    PARTIALLY_CONFIRMED = "TRAJECTORY_TIME_SEMANTICS_PARTIALLY_CONFIRMED"
    UNRESOLVED = "TRAJECTORY_TIME_SEMANTICS_UNRESOLVED"


class TrajectoryCorpusReadinessDecision(str, Enum):
    READY_FOR_WINDOW_DESIGN = "STRATZ_TRAJECTORY_CORPUS_READY_FOR_WINDOW_DESIGN"
    NEEDS_SOURCE_SEMANTICS_WORK = (
        "STRATZ_TRAJECTORY_CORPUS_NEEDS_SOURCE_SEMANTICS_WORK"
    )


@dataclass(frozen=True)
class IntDistributionSummary:
    count: int
    min: int | None
    median: float | None
    p90: int | None
    max: int | None


@dataclass(frozen=True)
class TrajectoryMetricAudit:
    metric: str
    games_with_curve: int
    games_without_curve: int
    point_count_distribution: IntDistributionSummary
    zero_length_curves: int
    malformed_source_index_points: int
    games_with_duplicate_source_indices: int
    games_with_non_monotonic_source_indices: int
    repeated_source_index_conflicts: int


@dataclass(frozen=True)
class TrajectoryPairingAudit:
    games_with_both_curves: int
    gold_only_games: int
    xp_only_games: int
    games_with_neither_curve: int
    equal_point_count_games: int
    unequal_point_count_games: int
    point_count_delta_distribution: IntDistributionSummary


@dataclass(frozen=True)
class TemporalSemanticsAudit:
    point_status_counts: dict[str, int]
    game_status_counts: dict[str, int]
    explicit_source_time_point_count: int
    normalized_time_point_count: int
    games_with_explicit_source_time: int
    games_with_normalized_time_seconds: int
    all_points_number_only_unstable: bool


@dataclass(frozen=True)
class TrajectoryVariantSummary:
    key: str
    game_count: int
    gold_point_count_median: float | None
    xp_point_count_median: float | None
    equal_length_games: int
    equal_length_rate: float
    missing_curve_games: int
    missing_curve_rate: float


@dataclass(frozen=True)
class StratzPublicTrajectoryCorpusAudit:
    source: str
    game_count: int
    unique_source_game_ids: int
    duplicate_source_game_ids: int
    started_at_min: datetime | None
    started_at_max: datetime | None
    ended_at_min: datetime | None
    ended_at_max: datetime | None
    patch_distribution: dict[str, int]
    family_distribution: dict[str, int]
    unknown_family_games: int
    games_with_10_players: int
    games_without_10_players: int
    complete_5v5_compositions: int
    incomplete_or_ambiguous_compositions: int
    complete_team_identity_games: int
    partial_team_identity_games: int
    missing_team_identity_games: int
    missing_or_ambiguous_winner_games: int
    missing_duration_games: int
    start_time_source_distinction_available: bool
    start_time_direct_games: int
    start_time_derived_games: int
    start_time_unknown_games: int
    gold: TrajectoryMetricAudit
    xp: TrajectoryMetricAudit
    pairing: TrajectoryPairingAudit
    temporal: TemporalSemanticsAudit
    patch_summaries: tuple[TrajectoryVariantSummary, ...]
    family_summaries: tuple[TrajectoryVariantSummary, ...]
    time_semantics_conclusion: TrajectoryTimeSemanticsConclusion
    readiness_decision: TrajectoryCorpusReadinessDecision
    readiness_blockers: tuple[str, ...]


def build_stratz_public_trajectory_corpus_audit(
    repository: "SQLiteRepository",
) -> StratzPublicTrajectoryCorpusAudit:
    games = tuple(
        game
        for game in repository.list_historical_dota_games()
        if game.source == STRATZ_PUBLIC_SOURCE
    )
    players_by_game = {
        game.id: tuple(repository.list_historical_dota_player_final_stats(game.id))
        for game in games
    }
    points_by_game = {
        game.id: tuple(repository.list_historical_dota_advantage_points(game.id))
        for game in games
    }
    return build_stratz_public_trajectory_corpus_audit_from_records(
        games=games,
        players_by_game=players_by_game,
        points_by_game=points_by_game,
    )


def build_stratz_public_trajectory_corpus_audit_from_records(
    *,
    games: Sequence[HistoricalDotaGame],
    players_by_game: Mapping[str, Sequence[HistoricalDotaPlayerFinalStats]],
    points_by_game: Mapping[str, Sequence[HistoricalDotaAdvantagePoint]],
) -> StratzPublicTrajectoryCorpusAudit:
    stratz_games = tuple(game for game in games if game.source == STRATZ_PUBLIC_SOURCE)
    source_id_counts = Counter(game.source_game_id for game in stratz_games)
    all_points = tuple(
        point for game in stratz_games for point in points_by_game.get(game.id, ())
    )
    gold_counts = _metric_counts(stratz_games, points_by_game, "gold")
    xp_counts = _metric_counts(stratz_games, points_by_game, "xp")
    time_conclusion = classify_trajectory_time_semantics(all_points)
    readiness_decision, readiness_blockers = _trajectory_readiness_decision(
        game_count=len(stratz_games),
        complete_5v5_compositions=sum(
            1
            for game in stratz_games
            if _has_complete_5v5_composition(players_by_game.get(game.id, ()))
        ),
        games_with_both_curves=sum(
            1
            for game in stratz_games
            if gold_counts[game.id] > 0 and xp_counts[game.id] > 0
        ),
        time_conclusion=time_conclusion,
    )

    started_values = [game.started_at for game in stratz_games]
    ended_values = [game.ended_at for game in stratz_games if game.ended_at is not None]
    family_distribution = Counter(
        draft_game_competition_family(game).value for game in stratz_games
    )
    patch_distribution = Counter(game.patch or "unknown" for game in stratz_games)

    return StratzPublicTrajectoryCorpusAudit(
        source=STRATZ_PUBLIC_SOURCE,
        game_count=len(stratz_games),
        unique_source_game_ids=len(source_id_counts),
        duplicate_source_game_ids=sum(
            count - 1 for count in source_id_counts.values() if count > 1
        ),
        started_at_min=min(started_values) if started_values else None,
        started_at_max=max(started_values) if started_values else None,
        ended_at_min=min(ended_values) if ended_values else None,
        ended_at_max=max(ended_values) if ended_values else None,
        patch_distribution=dict(sorted(patch_distribution.items())),
        family_distribution=dict(sorted(family_distribution.items())),
        unknown_family_games=family_distribution.get("unknown", 0),
        games_with_10_players=sum(
            1
            for game in stratz_games
            if len(players_by_game.get(game.id, ())) == 10
        ),
        games_without_10_players=sum(
            1
            for game in stratz_games
            if len(players_by_game.get(game.id, ())) != 10
        ),
        complete_5v5_compositions=sum(
            1
            for game in stratz_games
            if _has_complete_5v5_composition(players_by_game.get(game.id, ()))
        ),
        incomplete_or_ambiguous_compositions=sum(
            1
            for game in stratz_games
            if not _has_complete_5v5_composition(players_by_game.get(game.id, ()))
        ),
        complete_team_identity_games=sum(
            1 for game in stratz_games if game.team_a_source_id and game.team_b_source_id
        ),
        partial_team_identity_games=sum(
            1
            for game in stratz_games
            if bool(game.team_a_source_id) ^ bool(game.team_b_source_id)
        ),
        missing_team_identity_games=sum(
            1
            for game in stratz_games
            if not game.team_a_source_id and not game.team_b_source_id
        ),
        missing_or_ambiguous_winner_games=sum(
            1 for game in stratz_games if game.winner_side not in ("team_a", "team_b")
        ),
        missing_duration_games=sum(1 for game in stratz_games if game.ended_at is None),
        start_time_source_distinction_available=False,
        start_time_direct_games=0,
        start_time_derived_games=0,
        start_time_unknown_games=len(stratz_games),
        gold=_metric_audit(stratz_games, points_by_game, "gold"),
        xp=_metric_audit(stratz_games, points_by_game, "xp"),
        pairing=_pairing_audit(stratz_games, gold_counts, xp_counts),
        temporal=_temporal_semantics_audit(stratz_games, points_by_game),
        patch_summaries=_variant_summaries(
            stratz_games,
            gold_counts,
            xp_counts,
            key_func=lambda game: game.patch or "unknown",
        ),
        family_summaries=_variant_summaries(
            stratz_games,
            gold_counts,
            xp_counts,
            key_func=lambda game: draft_game_competition_family(game).value,
        ),
        time_semantics_conclusion=time_conclusion,
        readiness_decision=readiness_decision,
        readiness_blockers=readiness_blockers,
    )


def classify_trajectory_time_semantics(
    points: Sequence[HistoricalDotaAdvantagePoint],
) -> TrajectoryTimeSemanticsConclusion:
    if not points:
        return TrajectoryTimeSemanticsConclusion.UNRESOLVED

    normalized_points = [
        point
        for point in points
        if point.time_semantics_status == "normalized_seconds"
        and point.normalized_time_seconds is not None
    ]
    unresolved_points = [
        point
        for point in points
        if point.time_semantics_status != "normalized_seconds"
        or point.normalized_time_seconds is None
    ]
    if normalized_points and not unresolved_points:
        return TrajectoryTimeSemanticsConclusion.CONFIRMED
    if normalized_points and unresolved_points:
        return TrajectoryTimeSemanticsConclusion.PARTIALLY_CONFIRMED
    return TrajectoryTimeSemanticsConclusion.UNRESOLVED


def render_stratz_public_trajectory_corpus_audit(
    audit: StratzPublicTrajectoryCorpusAudit,
) -> str:
    lines: list[str] = [
        "STRATZ public trajectory corpus audit",
        f"Source: {audit.source}",
        "",
        "Corpus identity",
        f"Games: {audit.game_count}",
        f"Unique source game IDs: {audit.unique_source_game_ids}",
        f"Duplicate source game IDs: {audit.duplicate_source_game_ids}",
        f"Started range: {_format_datetime(audit.started_at_min)} to "
        f"{_format_datetime(audit.started_at_max)}",
        f"Completed range: {_format_datetime(audit.ended_at_min)} to "
        f"{_format_datetime(audit.ended_at_max)}",
        f"Patch distribution: {_format_counts(audit.patch_distribution)}",
        f"Family distribution: {_format_counts(audit.family_distribution)}",
        f"Unknown family games: {audit.unknown_family_games}",
        "",
        "Match normalization",
        f"Games with 10 players: {audit.games_with_10_players}",
        f"Games without 10 players: {audit.games_without_10_players}",
        f"Complete 5v5 compositions: {audit.complete_5v5_compositions}",
        "Incomplete/ambiguous compositions: "
        f"{audit.incomplete_or_ambiguous_compositions}",
        f"Complete team identity: {audit.complete_team_identity_games}",
        f"Partial team identity: {audit.partial_team_identity_games}",
        f"Missing team identity: {audit.missing_team_identity_games}",
        "Missing/ambiguous winner/result: "
        f"{audit.missing_or_ambiguous_winner_games}",
        f"Missing duration: {audit.missing_duration_games}",
        "Start-time direct/derived distinction: not persisted",
        "",
        "Gold trajectories",
        *_render_metric_lines(audit.gold),
        "",
        "XP trajectories",
        *_render_metric_lines(audit.xp),
        "",
        "Gold/XP pairing",
        f"Games with both curves: {audit.pairing.games_with_both_curves}",
        f"Gold only: {audit.pairing.gold_only_games}",
        f"XP only: {audit.pairing.xp_only_games}",
        f"Neither: {audit.pairing.games_with_neither_curve}",
        f"Equal point counts: {audit.pairing.equal_point_count_games}",
        f"Unequal point counts: {audit.pairing.unequal_point_count_games}",
        "Point-count delta distribution: "
        f"{_format_distribution(audit.pairing.point_count_delta_distribution)}",
        "",
        "Temporal semantics",
        f"Point status counts: {_format_counts(audit.temporal.point_status_counts)}",
        f"Game status counts: {_format_counts(audit.temporal.game_status_counts)}",
        "Explicit source-time points: "
        f"{audit.temporal.explicit_source_time_point_count}",
        f"Normalized-time points: {audit.temporal.normalized_time_point_count}",
        "Games with explicit source time: "
        f"{audit.temporal.games_with_explicit_source_time}",
        "Games with normalized seconds: "
        f"{audit.temporal.games_with_normalized_time_seconds}",
        "All points number-only unstable: "
        f"{_format_bool(audit.temporal.all_points_number_only_unstable)}",
        "",
        "Patch variance",
        *_render_variant_lines(audit.patch_summaries),
        "",
        "Family variance",
        *_render_variant_lines(audit.family_summaries),
        "",
        "Temporal coordinate conclusion",
        audit.time_semantics_conclusion.value,
        "",
        "Trajectory corpus architecture decision",
        audit.readiness_decision.value,
    ]
    for blocker in audit.readiness_blockers:
        lines.append(f"Blocker: {blocker}")
    return "\n".join(lines)


def _metric_counts(
    games: Sequence[HistoricalDotaGame],
    points_by_game: Mapping[str, Sequence[HistoricalDotaAdvantagePoint]],
    metric: str,
) -> dict[str, int]:
    return {
        game.id: sum(
            1 for point in points_by_game.get(game.id, ()) if point.metric == metric
        )
        for game in games
    }


def _metric_audit(
    games: Sequence[HistoricalDotaGame],
    points_by_game: Mapping[str, Sequence[HistoricalDotaAdvantagePoint]],
    metric: str,
) -> TrajectoryMetricAudit:
    point_counts: list[int] = []
    games_with_duplicate_indices = 0
    games_with_non_monotonic_indices = 0
    conflict_groups = 0
    malformed_points = 0

    for game in games:
        points = [
            point for point in points_by_game.get(game.id, ()) if point.metric == metric
        ]
        count = len(points)
        if count > 0:
            point_counts.append(count)
        malformed_points += sum(1 for point in points if point.source_index < 0)
        if _has_duplicate_source_indices(points):
            games_with_duplicate_indices += 1
        if _has_non_monotonic_source_indices(points):
            games_with_non_monotonic_indices += 1
        conflict_groups += _repeated_source_index_conflicts(points)

    games_with_curve = len(point_counts)
    return TrajectoryMetricAudit(
        metric=metric,
        games_with_curve=games_with_curve,
        games_without_curve=len(games) - games_with_curve,
        point_count_distribution=_int_distribution(point_counts),
        zero_length_curves=0,
        malformed_source_index_points=malformed_points,
        games_with_duplicate_source_indices=games_with_duplicate_indices,
        games_with_non_monotonic_source_indices=games_with_non_monotonic_indices,
        repeated_source_index_conflicts=conflict_groups,
    )


def _pairing_audit(
    games: Sequence[HistoricalDotaGame],
    gold_counts: dict[str, int],
    xp_counts: dict[str, int],
) -> TrajectoryPairingAudit:
    deltas: list[int] = []
    games_with_both = 0
    gold_only = 0
    xp_only = 0
    neither = 0
    equal = 0
    unequal = 0
    for game in games:
        gold_count = gold_counts[game.id]
        xp_count = xp_counts[game.id]
        if gold_count > 0 or xp_count > 0:
            deltas.append(abs(gold_count - xp_count))
        if gold_count > 0 and xp_count > 0:
            games_with_both += 1
        elif gold_count > 0:
            gold_only += 1
        elif xp_count > 0:
            xp_only += 1
        else:
            neither += 1
        if gold_count == xp_count:
            equal += 1
        else:
            unequal += 1
    return TrajectoryPairingAudit(
        games_with_both_curves=games_with_both,
        gold_only_games=gold_only,
        xp_only_games=xp_only,
        games_with_neither_curve=neither,
        equal_point_count_games=equal,
        unequal_point_count_games=unequal,
        point_count_delta_distribution=_int_distribution(deltas),
    )


def _temporal_semantics_audit(
    games: Sequence[HistoricalDotaGame],
    points_by_game: Mapping[str, Sequence[HistoricalDotaAdvantagePoint]],
) -> TemporalSemanticsAudit:
    point_status_counts: Counter[str] = Counter()
    game_status_counts: Counter[str] = Counter()
    explicit_source_time_points = 0
    normalized_time_points = 0
    games_with_source_time = 0
    games_with_normalized_seconds = 0

    for game in games:
        points = tuple(points_by_game.get(game.id, ()))
        point_status_counts.update(point.time_semantics_status for point in points)
        statuses = {point.time_semantics_status for point in points}
        if not points:
            game_status = "none"
        elif len(statuses) == 1:
            game_status = next(iter(statuses))
        else:
            game_status = "mixed"
        game_status_counts[game_status] += 1
        if any(point.source_time_value is not None for point in points):
            games_with_source_time += 1
        if any(point.normalized_time_seconds is not None for point in points):
            games_with_normalized_seconds += 1
        explicit_source_time_points += sum(
            1 for point in points if point.source_time_value is not None
        )
        normalized_time_points += sum(
            1 for point in points if point.normalized_time_seconds is not None
        )

    total_points = sum(point_status_counts.values())
    return TemporalSemanticsAudit(
        point_status_counts=dict(sorted(point_status_counts.items())),
        game_status_counts=dict(sorted(game_status_counts.items())),
        explicit_source_time_point_count=explicit_source_time_points,
        normalized_time_point_count=normalized_time_points,
        games_with_explicit_source_time=games_with_source_time,
        games_with_normalized_time_seconds=games_with_normalized_seconds,
        all_points_number_only_unstable=(
            total_points > 0
            and point_status_counts.get("source_index_unstable", 0) == total_points
            and explicit_source_time_points == 0
            and normalized_time_points == 0
        ),
    )


def _variant_summaries(
    games: Sequence[HistoricalDotaGame],
    gold_counts: dict[str, int],
    xp_counts: dict[str, int],
    *,
    key_func: Callable[[HistoricalDotaGame], str],
) -> tuple[TrajectoryVariantSummary, ...]:
    groups: dict[str, list[HistoricalDotaGame]] = defaultdict(list)
    for game in games:
        groups[key_func(game)].append(game)

    summaries: list[TrajectoryVariantSummary] = []
    for key in sorted(groups):
        grouped_games = groups[key]
        gold_values = [gold_counts[game.id] for game in grouped_games if gold_counts[game.id] > 0]
        xp_values = [xp_counts[game.id] for game in grouped_games if xp_counts[game.id] > 0]
        equal_length_games = sum(
            1 for game in grouped_games if gold_counts[game.id] == xp_counts[game.id]
        )
        missing_curve_games = sum(
            1
            for game in grouped_games
            if gold_counts[game.id] == 0 or xp_counts[game.id] == 0
        )
        game_count = len(grouped_games)
        summaries.append(
            TrajectoryVariantSummary(
                key=key,
                game_count=game_count,
                gold_point_count_median=_median(gold_values),
                xp_point_count_median=_median(xp_values),
                equal_length_games=equal_length_games,
                equal_length_rate=equal_length_games / game_count
                if game_count
                else 0.0,
                missing_curve_games=missing_curve_games,
                missing_curve_rate=missing_curve_games / game_count
                if game_count
                else 0.0,
            )
        )
    return tuple(summaries)


def _trajectory_readiness_decision(
    *,
    game_count: int,
    complete_5v5_compositions: int,
    games_with_both_curves: int,
    time_conclusion: TrajectoryTimeSemanticsConclusion,
) -> tuple[TrajectoryCorpusReadinessDecision, tuple[str, ...]]:
    blockers: list[str] = []
    if game_count == 0:
        blockers.append("no persisted STRATZ public games")
    if complete_5v5_compositions == 0:
        blockers.append("no complete 5v5 player-composition games")
    if games_with_both_curves == 0:
        blockers.append("no games with both gold and XP trajectories")
    if time_conclusion is not TrajectoryTimeSemanticsConclusion.CONFIRMED:
        blockers.append(
            "trajectory point coordinates are not confirmed as elapsed match time"
        )
    if blockers:
        return (
            TrajectoryCorpusReadinessDecision.NEEDS_SOURCE_SEMANTICS_WORK,
            tuple(blockers),
        )
    return TrajectoryCorpusReadinessDecision.READY_FOR_WINDOW_DESIGN, ()


def _has_complete_5v5_composition(
    players: Sequence[HistoricalDotaPlayerFinalStats],
) -> bool:
    return (
        len(players) == 10
        and sum(1 for player in players if player.team_side == "radiant") == 5
        and sum(1 for player in players if player.team_side == "dire") == 5
    )


def _has_duplicate_source_indices(
    points: Sequence[HistoricalDotaAdvantagePoint],
) -> bool:
    counts = Counter(point.source_index for point in points)
    return any(count > 1 for count in counts.values())


def _has_non_monotonic_source_indices(
    points: Sequence[HistoricalDotaAdvantagePoint],
) -> bool:
    indices = [point.source_index for point in points]
    return any(current < previous for previous, current in zip(indices, indices[1:]))


def _repeated_source_index_conflicts(
    points: Sequence[HistoricalDotaAdvantagePoint],
) -> int:
    values_by_index: dict[int, set[float]] = defaultdict(set)
    for point in points:
        values_by_index[point.source_index].add(point.value)
    return sum(1 for values in values_by_index.values() if len(values) > 1)


def _int_distribution(values: Sequence[int]) -> IntDistributionSummary:
    if not values:
        return IntDistributionSummary(
            count=0,
            min=None,
            median=None,
            p90=None,
            max=None,
        )
    ordered = sorted(values)
    p90_index = max(0, ceil(len(ordered) * 0.9) - 1)
    return IntDistributionSummary(
        count=len(ordered),
        min=ordered[0],
        median=float(median(ordered)),
        p90=ordered[p90_index],
        max=ordered[-1],
    )


def _median(values: Sequence[int]) -> float | None:
    if not values:
        return None
    return float(median(values))


def _render_metric_lines(audit: TrajectoryMetricAudit) -> list[str]:
    return [
        f"Games with {audit.metric} trajectory: {audit.games_with_curve}",
        f"Games without {audit.metric} trajectory: {audit.games_without_curve}",
        "Point-count distribution: "
        f"{_format_distribution(audit.point_count_distribution)}",
        f"Zero-length curves: {audit.zero_length_curves}",
        f"Malformed source-index points: {audit.malformed_source_index_points}",
        "Games with duplicate source indices: "
        f"{audit.games_with_duplicate_source_indices}",
        "Games with non-monotonic source indices: "
        f"{audit.games_with_non_monotonic_source_indices}",
        "Repeated source-index conflicts: "
        f"{audit.repeated_source_index_conflicts}",
    ]


def _render_variant_lines(
    summaries: Sequence[TrajectoryVariantSummary],
) -> list[str]:
    if not summaries:
        return ["-"]
    return [
        (
            f"{summary.key}: games={summary.game_count}, "
            f"gold_median={_format_optional_float(summary.gold_point_count_median)}, "
            f"xp_median={_format_optional_float(summary.xp_point_count_median)}, "
            f"equal_length={summary.equal_length_games}/"
            f"{summary.game_count} ({summary.equal_length_rate:.1%}), "
            f"missing_curve={summary.missing_curve_games}/"
            f"{summary.game_count} ({summary.missing_curve_rate:.1%})"
        )
        for summary in summaries
    ]


def _format_distribution(summary: IntDistributionSummary) -> str:
    if summary.count == 0:
        return "count=0, min=-, median=-, p90=-, max=-"
    return (
        f"count={summary.count}, min={summary.min}, "
        f"median={_format_optional_float(summary.median)}, "
        f"p90={summary.p90}, max={summary.max}"
    )


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _format_datetime(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "-"


def _format_optional_float(value: float | None) -> str:
    if value is None:
        return "-"
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _format_bool(value: bool) -> str:
    return "yes" if value else "no"
