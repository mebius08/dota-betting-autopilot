from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from app.history.rosters import RosterSnapshot

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class RosterCollectionResult:
    snapshots: list[RosterSnapshot]
    tournaments_requested: int
    fetched_rows: int
    skipped_records: int
    warnings: list[str]


@dataclass(frozen=True)
class RosterSyncResult:
    tournaments_requested: int
    rosters_fetched: int
    snapshots_inserted: int
    snapshots_updated: int
    snapshots_unchanged: int
    unique_players_seen: int
    unique_organizations_seen: int
    skipped_records: int
    warnings: list[str]


@dataclass(frozen=True)
class RosterHistoryStatus:
    players: int
    organizations: int
    roster_snapshots: int
    player_memberships: int
    coach_memberships: int
    snapshots_with_temporal_validity: int
    snapshots_without_explicit_validity: int
    observed_at_min: datetime | None
    observed_at_max: datetime | None
    unique_player_roster_fingerprints: int


class RosterCollector(Protocol):
    def collect(
        self,
        *,
        tournament_source_ids: list[str],
        max_tournaments: int,
    ) -> RosterCollectionResult:
        ...


def sync_roster_history(
    *,
    repository: SQLiteRepository,
    collector: RosterCollector,
    max_tournaments: int,
) -> RosterSyncResult:
    if max_tournaments < 1:
        raise ValueError("max_tournaments must be at least 1")

    tournament_source_ids = repository.list_historical_tournament_source_ids(
        source="pandascore",
        limit=max_tournaments,
    )
    if not tournament_source_ids:
        return RosterSyncResult(
            tournaments_requested=0,
            rosters_fetched=0,
            snapshots_inserted=0,
            snapshots_updated=0,
            snapshots_unchanged=0,
            unique_players_seen=0,
            unique_organizations_seen=0,
            skipped_records=0,
            warnings=["No historical PandaScore tournament IDs found."],
        )

    collection = collector.collect(
        tournament_source_ids=tournament_source_ids,
        max_tournaments=max_tournaments,
    )

    inserted = 0
    updated = 0
    unchanged = 0
    for snapshot in collection.snapshots:
        result = repository.upsert_roster_snapshot(snapshot)
        if result == "inserted":
            inserted += 1
        elif result == "updated":
            updated += 1
        else:
            unchanged += 1

    player_keys = {
        (player.source, player.source_player_id)
        for snapshot in collection.snapshots
        for player in snapshot.players
    }
    organization_keys = {
        (
            snapshot.organization.source,
            snapshot.organization.source_team_id,
        )
        for snapshot in collection.snapshots
    }

    return RosterSyncResult(
        tournaments_requested=collection.tournaments_requested,
        rosters_fetched=len(collection.snapshots),
        snapshots_inserted=inserted,
        snapshots_updated=updated,
        snapshots_unchanged=unchanged,
        unique_players_seen=len(player_keys),
        unique_organizations_seen=len(organization_keys),
        skipped_records=collection.skipped_records,
        warnings=collection.warnings,
    )


def build_roster_history_status(
    repository: SQLiteRepository,
) -> RosterHistoryStatus:
    observed_at_min, observed_at_max = repository.roster_observed_at_range()
    snapshots = repository.count_roster_snapshots()
    with_validity = repository.count_roster_snapshots_with_explicit_validity()
    return RosterHistoryStatus(
        players=repository.count_players(),
        organizations=repository.count_team_organizations(),
        roster_snapshots=snapshots,
        player_memberships=repository.count_roster_memberships(role="player"),
        coach_memberships=repository.count_roster_memberships(role="coach"),
        snapshots_with_temporal_validity=with_validity,
        snapshots_without_explicit_validity=snapshots - with_validity,
        observed_at_min=observed_at_min,
        observed_at_max=observed_at_max,
        unique_player_roster_fingerprints=(
            repository.count_unique_player_roster_fingerprints()
        ),
    )
