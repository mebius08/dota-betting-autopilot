from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from app.collectors.match_collector import normalize_team_name
from app.tournaments import CompetitiveStage, TournamentRound


WinnerSide = Literal["team_a", "team_b"]


@dataclass(frozen=True)
class HistoricalMatch:
    id: str
    source: str
    source_match_id: str
    started_at: datetime
    ended_at: datetime | None
    team_a_name: str
    team_b_name: str
    team_a_source_id: str | None = None
    team_b_source_id: str | None = None
    winner_name: str | None = None
    winner_source_id: str | None = None
    winner_side: WinnerSide | None = None
    tournament_name: str | None = None
    tournament_source_id: str | None = None
    league_name: str | None = None
    league_source_id: str | None = None
    series_name: str | None = None
    series_source_id: str | None = None
    raw_stage_label: str | None = None
    competitive_stage: CompetitiveStage = CompetitiveStage.UNKNOWN
    normalized_round: TournamentRound = TournamentRound.UNKNOWN
    best_of: int | None = None
    status: str = "unknown"
    ingested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def is_completed(self) -> bool:
        return self.status in {"finished", "completed"}

    @property
    def has_distinct_teams(self) -> bool:
        return normalize_team_name(self.team_a_name) != normalize_team_name(
            self.team_b_name
        )

    @property
    def has_resolved_binary_winner(self) -> bool:
        return self.winner_side in ("team_a", "team_b")

    @property
    def result_available_at(self) -> datetime | None:
        if not self.is_completed:
            return None
        return self.ended_at

    @property
    def usable_for_match_winner_training(self) -> bool:
        return (
            self.has_distinct_teams
            and self.is_completed
            and self.ended_at is not None
            and self.has_resolved_binary_winner
        )

    def completed_before(self, cutoff: datetime) -> bool:
        return (
            self.result_available_at is not None
            and self.result_available_at < cutoff
        )
