from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


DraftProvider = Literal["opendota", "pandascore"]
DraftActionKind = Literal["pick", "ban"]
DotaSide = Literal["radiant", "dire", "unknown"]
DraftWinnerSide = Literal["team_a", "team_b"]


@dataclass(frozen=True)
class HistoricalDraftAction:
    id: str
    game_id: str
    source: str
    source_game_id: str
    action_order: int
    action_kind: DraftActionKind
    team_side: DotaSide
    hero_id: int
    team_source_id: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("draft action id must not be empty")
        if not self.game_id.strip():
            raise ValueError("draft action game_id must not be empty")
        if not self.source.strip():
            raise ValueError("draft action source must not be empty")
        if not self.source_game_id.strip():
            raise ValueError("draft action source_game_id must not be empty")
        if self.action_order < 1:
            raise ValueError("draft action order must be at least 1")
        if self.hero_id < 1:
            raise ValueError("draft action hero_id must be positive")


@dataclass(frozen=True)
class HistoricalDotaGame:
    id: str
    source: str
    source_game_id: str
    started_at: datetime
    ended_at: datetime | None
    team_a_name: str
    team_b_name: str
    team_a_source_id: str | None
    team_b_source_id: str | None
    parent_series_source_id: str | None = None
    linked_historical_match_id: str | None = None
    winner_side: DraftWinnerSide | None = None
    game_number: int | None = None
    best_of: int | None = None
    team_a_series_wins_before: int | None = None
    team_b_series_wins_before: int | None = None
    team_a_side: DotaSide = "unknown"
    patch: str | None = None
    draft_complete: bool = False
    tournament_name: str | None = None
    tournament_source_id: str | None = None
    league_name: str | None = None
    league_source_id: str | None = None
    raw_stage_label: str | None = None
    ingested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("historical Dota game id must not be empty")
        if not self.source.strip():
            raise ValueError("historical Dota game source must not be empty")
        if not self.source_game_id.strip():
            raise ValueError("historical Dota game source_game_id must not be empty")
        if not self.team_a_name.strip() or not self.team_b_name.strip():
            raise ValueError("historical Dota game teams must not be empty")
        if self.started_at.tzinfo is None:
            raise ValueError("historical Dota game started_at must be timezone-aware")
        if self.ended_at is not None and self.ended_at.tzinfo is None:
            raise ValueError("historical Dota game ended_at must be timezone-aware")
        if self.game_number is not None and self.game_number < 1:
            raise ValueError("game_number must be at least 1")
        if self.best_of is not None and self.best_of < 1:
            raise ValueError("best_of must be at least 1")

    @property
    def has_resolved_binary_winner(self) -> bool:
        return self.winner_side in ("team_a", "team_b")

    @property
    def usable_for_draft_training(self) -> bool:
        return (
            self.ended_at is not None
            and self.has_resolved_binary_winner
            and self.draft_complete
        )

    def completed_before(self, cutoff: datetime) -> bool:
        return self.ended_at is not None and self.ended_at < cutoff


def draft_action_id(game_id: str, action_order: int) -> str:
    return f"{game_id}:draft:{action_order}"


def historical_dota_game_id(source: str, source_game_id: str) -> str:
    return f"{source.strip().casefold()}-{source_game_id.strip()}"
