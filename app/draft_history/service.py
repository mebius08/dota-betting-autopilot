from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from app.draft_history.domain import HistoricalDotaGame, HistoricalDraftAction
from app.draft_history.opendota import DraftCollectionResult
from app.history import (
    DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    HistoricalCompetitionFamily,
    classify_historical_competition_family,
    is_historical_match_scope_eligible,
)
from app.history.domain import HistoricalMatch
from app.tournaments import CompetitiveStage, TournamentRound

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class DraftSyncResult:
    fetched_rows: int
    mapped_games: int
    inserted: int
    updated: int
    unchanged: int
    skipped: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DraftHistoryStatus:
    provider: str | None
    historical_games: int
    games_with_usable_winner: int
    games_with_complete_5v5_picks: int
    games_with_bans: int
    games_with_ordered_draft_actions: int
    games_with_patch_provenance: int
    unique_heroes: int
    linked_games: int
    scope_eligible_post_draft_target_games: int
    started_at_min: datetime | None
    started_at_max: datetime | None
    completed_at_min: datetime | None
    completed_at_max: datetime | None

    @property
    def source_link_coverage(self) -> float:
        if self.historical_games == 0:
            return 0.0
        return self.linked_games / self.historical_games


class DraftCollector(Protocol):
    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        max_pages: int | None,
    ) -> DraftCollectionResult:
        ...


def sync_draft_history(
    *,
    repository: "SQLiteRepository",
    collector: DraftCollector,
    since: datetime,
    until: datetime,
    page_size: int,
    max_pages: int | None,
) -> DraftSyncResult:
    collection = collector.collect(
        since=since,
        until=until,
        page_size=page_size,
        max_pages=max_pages,
    )
    inserted = 0
    updated = 0
    unchanged = 0
    for game, actions in collection.games:
        result = repository.upsert_historical_dota_game(game, actions)
        if result == "inserted":
            inserted += 1
        elif result == "updated":
            updated += 1
        else:
            unchanged += 1
    return DraftSyncResult(
        fetched_rows=collection.fetched_rows,
        mapped_games=len(collection.games),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        skipped=collection.skipped_rows,
        warnings=collection.warnings,
    )


def build_draft_history_status(
    repository: "SQLiteRepository",
    *,
    provider: str | None = None,
) -> DraftHistoryStatus:
    games = tuple(
        game
        for game in repository.list_historical_dota_games()
        if provider is None or game.source == provider
    )
    actions_by_game = {
        game.id: repository.list_historical_draft_actions(game.id)
        for game in games
    }
    started = [game.started_at for game in games]
    completed = [game.ended_at for game in games if game.ended_at is not None]
    heroes = {
        action.hero_id
        for actions in actions_by_game.values()
        for action in actions
    }
    return DraftHistoryStatus(
        provider=provider,
        historical_games=len(games),
        games_with_usable_winner=sum(
            1 for game in games if game.has_resolved_binary_winner
        ),
        games_with_complete_5v5_picks=sum(
            1
            for game in games
            if has_complete_5v5_picks(game, actions_by_game[game.id])
        ),
        games_with_bans=sum(
            1
            for actions in actions_by_game.values()
            if any(action.action_kind == "ban" for action in actions)
        ),
        games_with_ordered_draft_actions=sum(
            1
            for actions in actions_by_game.values()
            if _has_ordered_actions(actions)
        ),
        games_with_patch_provenance=sum(1 for game in games if game.patch),
        unique_heroes=len(heroes),
        linked_games=sum(1 for game in games if game.linked_historical_match_id),
        scope_eligible_post_draft_target_games=sum(
            1
            for game in games
            if is_draft_game_scope_eligible(game) and game.usable_for_draft_training
        ),
        started_at_min=min(started) if started else None,
        started_at_max=max(started) if started else None,
        completed_at_min=min(completed) if completed else None,
        completed_at_max=max(completed) if completed else None,
    )


def has_complete_5v5_picks(
    game: HistoricalDotaGame,
    actions: Iterable[HistoricalDraftAction],
) -> bool:
    team_a_side = game.team_a_side
    if team_a_side not in ("radiant", "dire"):
        return False
    team_b_side = "dire" if team_a_side == "radiant" else "radiant"
    team_a_picks: list[int] = []
    team_b_picks: list[int] = []
    for action in sorted(actions, key=lambda action: action.action_order):
        if action.action_kind != "pick":
            continue
        if action.team_side == team_a_side:
            team_a_picks.append(action.hero_id)
        elif action.team_side == team_b_side:
            team_b_picks.append(action.hero_id)
    return (
        len(team_a_picks) == 5
        and len(team_b_picks) == 5
        and len(set(team_a_picks)) == 5
        and len(set(team_b_picks)) == 5
    )


def is_draft_game_scope_eligible(game: HistoricalDotaGame) -> bool:
    return is_historical_match_scope_eligible(
        _historical_match_from_game(game),
        DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    )


def draft_game_competition_family(
    game: HistoricalDotaGame,
) -> HistoricalCompetitionFamily:
    return classify_historical_competition_family(_historical_match_from_game(game))


def _has_ordered_actions(actions: Iterable[HistoricalDraftAction]) -> bool:
    ordered = sorted(actions, key=lambda action: action.action_order)
    if not ordered:
        return False
    return [action.action_order for action in ordered] == sorted(
        {action.action_order for action in ordered}
    )


def _historical_match_from_game(game: HistoricalDotaGame) -> HistoricalMatch:
    return HistoricalMatch(
        id=game.id,
        source=game.source,
        source_match_id=game.source_game_id,
        started_at=game.started_at,
        ended_at=game.ended_at,
        team_a_name=game.team_a_name,
        team_b_name=game.team_b_name,
        team_a_source_id=game.team_a_source_id,
        team_b_source_id=game.team_b_source_id,
        winner_side=game.winner_side,
        tournament_name=game.tournament_name,
        tournament_source_id=game.tournament_source_id,
        league_name=game.league_name,
        league_source_id=game.league_source_id,
        series_source_id=game.parent_series_source_id,
        raw_stage_label=game.raw_stage_label,
        competitive_stage=CompetitiveStage.UNKNOWN,
        normalized_round=TournamentRound.UNKNOWN,
        best_of=game.best_of,
        status="finished" if game.ended_at is not None else "unknown",
        ingested_at=game.ingested_at,
    )
