from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from fractions import Fraction
from typing import Protocol

from app.history.rosters import (
    PlayerIdentity,
    RosterCoach,
    RosterSnapshot,
)


class RosterChronologySource(str, Enum):
    EXPLICIT_VALID_FROM = "explicit_valid_from"
    TOURNAMENT_MATCH_CONTEXT = "tournament_match_context"
    OBSERVED_AT_FALLBACK = "observed_at_fallback"


class CoachContinuity(str, Enum):
    SAME_STABLE_ID = "same_stable_id"
    DIFFERENT_STABLE_ID = "different_stable_id"
    UNKNOWN = "unknown"


class RosterContinuityStrength(str, Enum):
    EXACT = "exact"
    STRONG = "strong"
    COACH_SUPPORTED = "coach_supported"
    WEAK = "weak"
    NONE = "none"


class RosterPredecessorResolutionState(str, Enum):
    RESOLVED = "resolved"
    NO_PREDECESSOR = "no_predecessor"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class HistoricalTournamentChronologyContext:
    source: str
    tournament_source_id: str
    earliest_started_at: datetime | None
    latest_ended_at: datetime | None

    @property
    def context_at(self) -> datetime | None:
        return self.earliest_started_at or self.latest_ended_at


class RosterLineageRepository(Protocol):
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
class RosterChronologyPoint:
    snapshot_id: str
    context_at: datetime
    source: RosterChronologySource


@dataclass(frozen=True)
class RosterContinuityPolicy:
    minimum_core_players: int = 5
    strong_min_shared_players: int = 4
    strong_min_overlap_ratio: Fraction = Fraction(4, 5)
    coach_supported_min_shared_players: int = 3
    coach_supported_min_overlap_ratio: Fraction = Fraction(3, 5)
    weak_min_shared_players: int = 3


@dataclass(frozen=True)
class RosterContinuityEvidence:
    previous_snapshot_id: str
    current_snapshot_id: str
    shared_stable_player_ids: tuple[tuple[str, str], ...]
    overlap_count: int
    previous_player_count: int
    current_player_count: int
    overlap_ratio_previous: Fraction
    overlap_ratio_current: Fraction
    overlap_ratio_smaller_roster: Fraction
    exact_player_set_equality: bool
    player_roster_fingerprint_equality: bool
    coach_continuity: CoachContinuity
    same_organization: bool
    previous_chronology: RosterChronologyPoint
    current_chronology: RosterChronologyPoint
    continuity_strength: RosterContinuityStrength
    auto_link_eligible: bool
    reason_code: str


@dataclass(frozen=True)
class RosterPredecessorResolution:
    current_snapshot_id: str
    state: RosterPredecessorResolutionState
    predecessor_snapshot_id: str | None
    evidence: RosterContinuityEvidence | None
    candidate_evidence: tuple[RosterContinuityEvidence, ...]
    tied_evidence: tuple[RosterContinuityEvidence, ...] = ()


@dataclass(frozen=True)
class RosterLineageEdge:
    previous_snapshot_id: str
    current_snapshot_id: str
    evidence: RosterContinuityEvidence


@dataclass(frozen=True)
class RosterLineageGraph:
    as_of: datetime
    snapshots_by_id: Mapping[str, RosterSnapshot]
    chronology_points: Mapping[str, RosterChronologyPoint]
    evidence: tuple[RosterContinuityEvidence, ...]
    predecessor_resolutions: Mapping[str, RosterPredecessorResolution]
    accepted_edges: tuple[RosterLineageEdge, ...]
    ambiguous_resolutions: tuple[RosterPredecessorResolution, ...]
    unlinked_snapshot_ids: tuple[str, ...]

    def get_predecessor_chain(self, snapshot_id: str) -> tuple[RosterSnapshot, ...]:
        edge_by_current = {
            edge.current_snapshot_id: edge for edge in self.accepted_edges
        }
        chain: list[RosterSnapshot] = []
        seen: set[str] = set()
        cursor = snapshot_id
        while cursor in edge_by_current:
            edge = edge_by_current[cursor]
            previous_id = edge.previous_snapshot_id
            if previous_id in seen:
                raise ValueError("Roster lineage graph contains a cycle")
            seen.add(previous_id)
            chain.append(self.snapshots_by_id[previous_id])
            cursor = previous_id

        return tuple(reversed(chain))

    def resolve_roster_history(
        self,
        snapshot_id: str,
    ) -> tuple[RosterSnapshot, ...]:
        return self.get_predecessor_chain(snapshot_id)

    def derived_component_count(self) -> int:
        parent = {snapshot_id: snapshot_id for snapshot_id in self.snapshots_by_id}

        def find(snapshot_id: str) -> str:
            while parent[snapshot_id] != snapshot_id:
                parent[snapshot_id] = parent[parent[snapshot_id]]
                snapshot_id = parent[snapshot_id]
            return snapshot_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for edge in self.accepted_edges:
            union(edge.previous_snapshot_id, edge.current_snapshot_id)

        return len({find(snapshot_id) for snapshot_id in parent})


@dataclass(frozen=True)
class RosterLineageStatus:
    as_of: datetime
    available_roster_snapshots: int
    chronology_source_counts: Mapping[RosterChronologySource, int]
    exact_continuity_links: int
    strong_continuity_links: int
    coach_supported_continuity_links: int
    ambiguous_predecessor_resolutions: int
    unlinked_root_snapshots: int
    derived_lineage_components: int
    cross_organization_accepted_links: int
    same_organization_accepted_links: int
    largest_predecessor_chain_size: int


class RosterContinuityEvaluator:
    def __init__(
        self,
        policy: RosterContinuityPolicy | None = None,
    ) -> None:
        self.policy = policy or RosterContinuityPolicy()

    def evaluate(
        self,
        previous: RosterSnapshot,
        current: RosterSnapshot,
        *,
        previous_chronology: RosterChronologyPoint,
        current_chronology: RosterChronologyPoint,
    ) -> RosterContinuityEvidence:
        previous_player_ids = _stable_player_ids(previous.players)
        current_player_ids = _stable_player_ids(current.players)
        shared_player_ids = tuple(sorted(previous_player_ids & current_player_ids))
        overlap_count = len(shared_player_ids)
        previous_count = len(previous_player_ids)
        current_count = len(current_player_ids)
        smaller_count = min(previous_count, current_count)

        ratio_previous = _ratio(overlap_count, previous_count)
        ratio_current = _ratio(overlap_count, current_count)
        ratio_smaller = _ratio(overlap_count, smaller_count)
        exact_player_set = previous_player_ids == current_player_ids
        fingerprint_equal = (
            previous.player_roster_fingerprint == current.player_roster_fingerprint
        )
        coach_continuity = evaluate_coach_continuity(
            previous.coach,
            current.coach,
        )
        strength, auto_link_eligible, reason_code = self._classify(
            overlap_count=overlap_count,
            ratio_smaller=ratio_smaller,
            exact_player_set=exact_player_set,
            player_count=min(previous_count, current_count),
            coach_continuity=coach_continuity,
        )

        return RosterContinuityEvidence(
            previous_snapshot_id=previous.id,
            current_snapshot_id=current.id,
            shared_stable_player_ids=shared_player_ids,
            overlap_count=overlap_count,
            previous_player_count=previous_count,
            current_player_count=current_count,
            overlap_ratio_previous=ratio_previous,
            overlap_ratio_current=ratio_current,
            overlap_ratio_smaller_roster=ratio_smaller,
            exact_player_set_equality=exact_player_set,
            player_roster_fingerprint_equality=fingerprint_equal,
            coach_continuity=coach_continuity,
            same_organization=_same_organization(previous, current),
            previous_chronology=previous_chronology,
            current_chronology=current_chronology,
            continuity_strength=strength,
            auto_link_eligible=auto_link_eligible,
            reason_code=reason_code,
        )

    def _classify(
        self,
        *,
        overlap_count: int,
        ratio_smaller: Fraction,
        exact_player_set: bool,
        player_count: int,
        coach_continuity: CoachContinuity,
    ) -> tuple[RosterContinuityStrength, bool, str]:
        policy = self.policy
        if exact_player_set and player_count >= policy.minimum_core_players:
            return (
                RosterContinuityStrength.EXACT,
                True,
                "exact_player_set",
            )
        if (
            overlap_count >= policy.strong_min_shared_players
            and ratio_smaller >= policy.strong_min_overlap_ratio
        ):
            return (
                RosterContinuityStrength.STRONG,
                True,
                "strong_player_overlap",
            )
        if (
            overlap_count >= policy.coach_supported_min_shared_players
            and ratio_smaller >= policy.coach_supported_min_overlap_ratio
            and coach_continuity is CoachContinuity.SAME_STABLE_ID
        ):
            return (
                RosterContinuityStrength.COACH_SUPPORTED,
                True,
                "coach_supported_player_overlap",
            )
        if overlap_count >= policy.weak_min_shared_players:
            return (
                RosterContinuityStrength.WEAK,
                False,
                "weak_player_overlap",
            )
        return (
            RosterContinuityStrength.NONE,
            False,
            "insufficient_player_overlap",
        )


class RosterLineageResolver:
    def __init__(
        self,
        repository: RosterLineageRepository,
        policy: RosterContinuityPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.policy = policy or RosterContinuityPolicy()
        self.evaluator = RosterContinuityEvaluator(self.policy)

    def resolve(self, as_of: datetime) -> RosterLineageGraph:
        snapshots = sorted(
            self.repository.list_roster_snapshots_available_before(as_of),
            key=_snapshot_output_key,
        )
        snapshots_by_id = {snapshot.id: snapshot for snapshot in snapshots}
        chronology_points = {
            snapshot.id: self._resolve_chronology_point(snapshot, as_of)
            for snapshot in snapshots
        }

        evidence: list[RosterContinuityEvidence] = []
        resolutions: dict[str, RosterPredecessorResolution] = {}
        accepted_edges: list[RosterLineageEdge] = []

        for current in snapshots:
            current_chronology = chronology_points[current.id]
            candidate_evidence: list[RosterContinuityEvidence] = []
            for previous in snapshots:
                if previous.id == current.id:
                    continue
                previous_chronology = chronology_points[previous.id]
                if previous_chronology.context_at >= current_chronology.context_at:
                    continue

                comparison = self.evaluator.evaluate(
                    previous,
                    current,
                    previous_chronology=previous_chronology,
                    current_chronology=current_chronology,
                )
                evidence.append(comparison)
                if comparison.auto_link_eligible:
                    candidate_evidence.append(comparison)

            resolution = _resolve_predecessor(
                current.id,
                tuple(candidate_evidence),
            )
            resolutions[current.id] = resolution
            if (
                resolution.state is RosterPredecessorResolutionState.RESOLVED
                and resolution.evidence is not None
                and resolution.predecessor_snapshot_id is not None
            ):
                accepted_edges.append(
                    RosterLineageEdge(
                        previous_snapshot_id=resolution.predecessor_snapshot_id,
                        current_snapshot_id=current.id,
                        evidence=resolution.evidence,
                    )
                )

        accepted_edges_tuple = tuple(sorted(accepted_edges, key=_edge_output_key))
        incoming = {edge.current_snapshot_id for edge in accepted_edges_tuple}
        unlinked_snapshot_ids = tuple(
            sorted(snapshot_id for snapshot_id in snapshots_by_id if snapshot_id not in incoming)
        )
        ambiguous_resolutions = tuple(
            sorted(
                (
                    resolution
                    for resolution in resolutions.values()
                    if resolution.state is RosterPredecessorResolutionState.AMBIGUOUS
                ),
                key=lambda resolution: resolution.current_snapshot_id,
            )
        )

        return RosterLineageGraph(
            as_of=as_of,
            snapshots_by_id=snapshots_by_id,
            chronology_points=chronology_points,
            evidence=tuple(sorted(evidence, key=_evidence_output_key)),
            predecessor_resolutions=resolutions,
            accepted_edges=accepted_edges_tuple,
            ambiguous_resolutions=ambiguous_resolutions,
            unlinked_snapshot_ids=unlinked_snapshot_ids,
        )

    def _resolve_chronology_point(
        self,
        snapshot: RosterSnapshot,
        as_of: datetime,
    ) -> RosterChronologyPoint:
        if snapshot.valid_from is not None:
            return RosterChronologyPoint(
                snapshot_id=snapshot.id,
                context_at=snapshot.valid_from,
                source=RosterChronologySource.EXPLICIT_VALID_FROM,
            )

        if snapshot.tournament_source_id is not None:
            tournament_context = (
                self.repository.get_historical_tournament_chronology_context(
                    source=snapshot.source,
                    tournament_source_id=snapshot.tournament_source_id,
                    cutoff_timestamp=as_of,
                )
            )
            if tournament_context is not None and tournament_context.context_at is not None:
                return RosterChronologyPoint(
                    snapshot_id=snapshot.id,
                    context_at=tournament_context.context_at,
                    source=RosterChronologySource.TOURNAMENT_MATCH_CONTEXT,
                )

        return RosterChronologyPoint(
            snapshot_id=snapshot.id,
            context_at=snapshot.observed_at,
            source=RosterChronologySource.OBSERVED_AT_FALLBACK,
        )


def evaluate_coach_continuity(
    previous_coach: RosterCoach | None,
    current_coach: RosterCoach | None,
) -> CoachContinuity:
    previous_key = _stable_coach_id(previous_coach)
    current_key = _stable_coach_id(current_coach)
    if previous_key is None or current_key is None:
        return CoachContinuity.UNKNOWN
    if previous_key == current_key:
        return CoachContinuity.SAME_STABLE_ID
    return CoachContinuity.DIFFERENT_STABLE_ID


def build_roster_lineage_graph(
    repository: RosterLineageRepository,
    *,
    as_of: datetime,
    policy: RosterContinuityPolicy | None = None,
) -> RosterLineageGraph:
    return RosterLineageResolver(repository, policy).resolve(as_of)


def build_roster_lineage_status(
    repository: RosterLineageRepository,
    *,
    as_of: datetime,
    policy: RosterContinuityPolicy | None = None,
) -> RosterLineageStatus:
    graph = build_roster_lineage_graph(
        repository,
        as_of=as_of,
        policy=policy,
    )
    chronology_counts = {
        source: 0
        for source in RosterChronologySource
    }
    for chronology_point in graph.chronology_points.values():
        chronology_counts[chronology_point.source] += 1

    exact_links = _count_edges_with_strength(
        graph.accepted_edges,
        RosterContinuityStrength.EXACT,
    )
    strong_links = _count_edges_with_strength(
        graph.accepted_edges,
        RosterContinuityStrength.STRONG,
    )
    coach_supported_links = _count_edges_with_strength(
        graph.accepted_edges,
        RosterContinuityStrength.COACH_SUPPORTED,
    )
    largest_chain_size = max(
        (
            len(graph.get_predecessor_chain(snapshot_id)) + 1
            for snapshot_id in graph.snapshots_by_id
        ),
        default=0,
    )

    return RosterLineageStatus(
        as_of=as_of,
        available_roster_snapshots=len(graph.snapshots_by_id),
        chronology_source_counts=chronology_counts,
        exact_continuity_links=exact_links,
        strong_continuity_links=strong_links,
        coach_supported_continuity_links=coach_supported_links,
        ambiguous_predecessor_resolutions=len(graph.ambiguous_resolutions),
        unlinked_root_snapshots=len(graph.unlinked_snapshot_ids),
        derived_lineage_components=graph.derived_component_count(),
        cross_organization_accepted_links=sum(
            1 for edge in graph.accepted_edges if not edge.evidence.same_organization
        ),
        same_organization_accepted_links=sum(
            1 for edge in graph.accepted_edges if edge.evidence.same_organization
        ),
        largest_predecessor_chain_size=largest_chain_size,
    )


def _resolve_predecessor(
    current_snapshot_id: str,
    candidate_evidence: tuple[RosterContinuityEvidence, ...],
) -> RosterPredecessorResolution:
    sorted_candidates = tuple(sorted(candidate_evidence, key=_evidence_output_key))
    if not sorted_candidates:
        return RosterPredecessorResolution(
            current_snapshot_id=current_snapshot_id,
            state=RosterPredecessorResolutionState.NO_PREDECESSOR,
            predecessor_snapshot_id=None,
            evidence=None,
            candidate_evidence=(),
        )

    best_rank = max(_predecessor_rank(evidence) for evidence in sorted_candidates)
    tied = tuple(
        evidence
        for evidence in sorted_candidates
        if _predecessor_rank(evidence) == best_rank
    )
    if len(tied) > 1:
        return RosterPredecessorResolution(
            current_snapshot_id=current_snapshot_id,
            state=RosterPredecessorResolutionState.AMBIGUOUS,
            predecessor_snapshot_id=None,
            evidence=None,
            candidate_evidence=sorted_candidates,
            tied_evidence=tuple(sorted(tied, key=_evidence_output_key)),
        )

    selected = tied[0]
    return RosterPredecessorResolution(
        current_snapshot_id=current_snapshot_id,
        state=RosterPredecessorResolutionState.RESOLVED,
        predecessor_snapshot_id=selected.previous_snapshot_id,
        evidence=selected,
        candidate_evidence=sorted_candidates,
    )


def _predecessor_rank(
    evidence: RosterContinuityEvidence,
) -> tuple[datetime, int, int, Fraction]:
    return (
        evidence.previous_chronology.context_at,
        _STRENGTH_RANK[evidence.continuity_strength],
        evidence.overlap_count,
        evidence.overlap_ratio_smaller_roster,
    )


def _count_edges_with_strength(
    edges: tuple[RosterLineageEdge, ...],
    strength: RosterContinuityStrength,
) -> int:
    return sum(1 for edge in edges if edge.evidence.continuity_strength is strength)


def _ratio(numerator: int, denominator: int) -> Fraction:
    if denominator <= 0:
        return Fraction(0, 1)
    return Fraction(numerator, denominator)


def _stable_player_ids(
    players: tuple[PlayerIdentity, ...],
) -> set[tuple[str, str]]:
    return {
        _identity_key(player.source, player.source_player_id)
        for player in players
    }


def _stable_coach_id(coach: RosterCoach | None) -> tuple[str, str] | None:
    if coach is None or coach.source_coach_id is None:
        return None
    return _identity_key(coach.source, coach.source_coach_id)


def _same_organization(
    previous: RosterSnapshot,
    current: RosterSnapshot,
) -> bool:
    return _identity_key(
        previous.organization.source,
        previous.organization.source_team_id,
    ) == _identity_key(
        current.organization.source,
        current.organization.source_team_id,
    )


def _identity_key(source: str, source_id: str) -> tuple[str, str]:
    return (source.strip().casefold(), source_id.strip())


def _snapshot_output_key(snapshot: RosterSnapshot) -> tuple[str, str, str]:
    return (snapshot.source, snapshot.source_snapshot_id, snapshot.id)


def _evidence_output_key(
    evidence: RosterContinuityEvidence,
) -> tuple[datetime, str, str]:
    return (
        evidence.previous_chronology.context_at,
        evidence.previous_snapshot_id,
        evidence.current_snapshot_id,
    )


def _edge_output_key(edge: RosterLineageEdge) -> tuple[datetime, str, str]:
    return (
        edge.evidence.current_chronology.context_at,
        edge.previous_snapshot_id,
        edge.current_snapshot_id,
    )


_STRENGTH_RANK: Mapping[RosterContinuityStrength, int] = {
    RosterContinuityStrength.EXACT: 3,
    RosterContinuityStrength.STRONG: 2,
    RosterContinuityStrength.COACH_SUPPORTED: 1,
    RosterContinuityStrength.WEAK: 0,
    RosterContinuityStrength.NONE: 0,
}
