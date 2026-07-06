from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from app.collectors.match_collector import normalize_team_name
from app.history.domain import HistoricalMatch
from app.history.pandascore import HistoricalCollectionResult
from app.tournaments import CompetitiveStage

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class HistoricalSyncResult:
    fetched_rows: int
    mapped_matches: int
    usable_matches: int
    inserted: int
    updated: int
    unchanged: int
    skipped: int
    warnings: list[str]


@dataclass(frozen=True)
class HistoricalStatus:
    total_matches: int
    usable_winner_records: int
    point_in_time_ready_matches: int
    started_at_min: datetime | None
    started_at_max: datetime | None
    completed_at_min: datetime | None
    completed_at_max: datetime | None
    unique_teams: int
    unique_tournaments: int
    stage_counts: dict[CompetitiveStage, int]


class HistoricalCollector(Protocol):
    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        max_pages: int,
    ) -> HistoricalCollectionResult:
        ...


def sync_historical_matches(
    *,
    repository: SQLiteRepository,
    collector: HistoricalCollector,
    since: datetime | None,
    until: datetime | None,
    page_size: int,
    max_pages: int,
) -> HistoricalSyncResult:
    collection = collector.collect(
        since=since,
        until=until,
        page_size=page_size,
        max_pages=max_pages,
    )

    inserted = 0
    updated = 0
    unchanged = 0
    for match in collection.matches:
        result = repository.upsert_historical_match(match)
        if result == "inserted":
            inserted += 1
        elif result == "updated":
            updated += 1
        else:
            unchanged += 1

    return HistoricalSyncResult(
        fetched_rows=collection.fetched_rows,
        mapped_matches=len(collection.matches),
        usable_matches=sum(
            1 for match in collection.matches if match.usable_for_match_winner_training
        ),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        skipped=collection.skipped_rows,
        warnings=collection.warnings,
    )


def list_training_matches_before(
    repository: SQLiteRepository,
    cutoff: datetime,
) -> list[HistoricalMatch]:
    return [
        match
        for match in repository.list_historical_matches_before(cutoff)
        if match.usable_for_match_winner_training
    ]


def get_team_history_before(
    repository: SQLiteRepository,
    team_id_or_name: str,
    cutoff: datetime,
    *,
    limit: int | None = None,
) -> list[HistoricalMatch]:
    identity = team_id_or_name.strip()
    normalized_identity = normalize_team_name(identity)
    matches = [
        match
        for match in repository.list_historical_matches_before(cutoff)
        if _match_has_team(match, identity, normalized_identity)
    ]

    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        matches = matches[-limit:]
    return matches


def build_historical_status(repository: SQLiteRepository) -> HistoricalStatus:
    matches = repository.list_historical_matches()
    started_values = [match.started_at for match in matches]
    completed_values = [
        match.ended_at
        for match in matches
        if match.ended_at is not None and match.is_completed
    ]
    stage_counts = {stage: 0 for stage in CompetitiveStage}
    team_keys: set[str] = set()
    tournament_keys: set[str] = set()

    for match in matches:
        stage_counts[match.competitive_stage] += 1
        team_keys.add(
            _team_key(match.source, match.team_a_source_id, match.team_a_name)
        )
        team_keys.add(
            _team_key(match.source, match.team_b_source_id, match.team_b_name)
        )
        if match.tournament_source_id is not None:
            tournament_keys.add(f"{match.source}:id:{match.tournament_source_id}")
        elif match.tournament_name is not None:
            tournament_keys.add(normalize_team_name(match.tournament_name))

    return HistoricalStatus(
        total_matches=len(matches),
        usable_winner_records=sum(
            1 for match in matches if match.usable_for_match_winner_training
        ),
        point_in_time_ready_matches=sum(
            1
            for match in matches
            if match.is_completed and match.ended_at is not None
        ),
        started_at_min=min(started_values) if started_values else None,
        started_at_max=max(started_values) if started_values else None,
        completed_at_min=min(completed_values) if completed_values else None,
        completed_at_max=max(completed_values) if completed_values else None,
        unique_teams=len(team_keys),
        unique_tournaments=len(tournament_keys),
        stage_counts=stage_counts,
    )


def _match_has_team(
    match: HistoricalMatch,
    identity: str,
    normalized_identity: str,
) -> bool:
    source_ids = {
        source_id
        for source_id in (match.team_a_source_id, match.team_b_source_id)
        if source_id is not None
    }
    if identity in source_ids:
        return True

    return normalized_identity in {
        normalize_team_name(match.team_a_name),
        normalize_team_name(match.team_b_name),
    }


def _team_key(source: str, source_id: str | None, name: str) -> str:
    if source_id is not None:
        return f"{source}:id:{source_id}"
    return normalize_team_name(name)
