from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


DraftProvider = Literal["opendota", "pandascore"]
DraftActionKind = Literal["pick", "ban"]
DotaSide = Literal["radiant", "dire", "unknown"]
DraftWinnerSide = Literal["team_a", "team_b"]
AdvantageMetric = Literal["gold", "xp"]
TimeSemanticsStatus = Literal["normalized_seconds", "source_index_unstable"]


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
class HistoricalDotaPlayerFinalStats:
    id: str
    game_id: str
    source: str
    source_game_id: str
    account_id: str
    team_side: DotaSide
    hero_id: int
    player_slot: int | None = None
    team_source_id: str | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    net_worth: int | None = None
    last_hits: int | None = None
    denies: int | None = None
    gpm: int | None = None
    xpm: int | None = None
    level: int | None = None
    hero_damage: int | None = None
    tower_damage: int | None = None
    hero_healing: int | None = None
    final_item_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("player final stats id must not be empty")
        if not self.game_id.strip():
            raise ValueError("player final stats game_id must not be empty")
        if not self.source.strip():
            raise ValueError("player final stats source must not be empty")
        if not self.source_game_id.strip():
            raise ValueError("player final stats source_game_id must not be empty")
        if not self.account_id.strip():
            raise ValueError("player final stats account_id must not be empty")
        if self.team_side not in ("radiant", "dire"):
            raise ValueError("player final stats team_side must be radiant or dire")
        if self.hero_id < 1:
            raise ValueError("player final stats hero_id must be positive")
        if any(item_id < 1 for item_id in self.final_item_ids):
            raise ValueError("final item ids must be positive")


@dataclass(frozen=True)
class HistoricalDotaAdvantagePoint:
    id: str
    game_id: str
    source: str
    source_game_id: str
    metric: AdvantageMetric
    source_index: int
    value: float
    source_time_value: str | None = None
    normalized_time_seconds: int | None = None
    time_semantics_status: TimeSemanticsStatus = "source_index_unstable"

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("advantage point id must not be empty")
        if not self.game_id.strip():
            raise ValueError("advantage point game_id must not be empty")
        if not self.source.strip():
            raise ValueError("advantage point source must not be empty")
        if not self.source_game_id.strip():
            raise ValueError("advantage point source_game_id must not be empty")
        if self.metric not in ("gold", "xp"):
            raise ValueError("advantage point metric must be gold or xp")
        if self.source_index < 0:
            raise ValueError("advantage point source_index must not be negative")
        if self.normalized_time_seconds is not None and self.normalized_time_seconds < 0:
            raise ValueError(
                "advantage point normalized_time_seconds must not be negative"
            )
        if (
            self.time_semantics_status == "normalized_seconds"
            and self.normalized_time_seconds is None
        ):
            raise ValueError(
                "normalized_seconds status requires normalized_time_seconds"
            )
        if (
            self.time_semantics_status == "source_index_unstable"
            and self.normalized_time_seconds is not None
        ):
            raise ValueError(
                "source_index_unstable status must not set normalized seconds"
            )


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


def historical_dota_player_final_stats_id(
    game_id: str,
    account_id: str,
) -> str:
    return f"{game_id}:player:{account_id.strip()}"


def historical_dota_advantage_point_id(
    game_id: str,
    metric: AdvantageMetric,
    source_index: int,
) -> str:
    return f"{game_id}:advantage:{metric}:{source_index}"
