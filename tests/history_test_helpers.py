from datetime import datetime, timezone
from typing import TypeVar, cast

from app.history import HistoricalMatch
from app.history.domain import WinnerSide
from app.tournaments import CompetitiveStage, TournamentRound


T = TypeVar("T")


def make_historical_match(
    match_id: str = "match-1",
    *,
    source_match_id: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    team_a_name: str = "Team Spirit",
    team_b_name: str = "PARIVISION",
    team_a_source_id: str | None = "10",
    team_b_source_id: str | None = "20",
    winner_side: str | None = "team_a",
    status: str = "finished",
    tournament_name: str | None = "DreamLeague",
    tournament_source_id: str | None = "100",
    league_name: str | None = "DreamLeague",
    league_source_id: str | None = "200",
    series_name: str | None = "Season 25",
    series_source_id: str | None = "300",
    competitive_stage: CompetitiveStage = CompetitiveStage.GROUP,
    normalized_round: TournamentRound = TournamentRound.GROUP,
    raw_stage_label: str | None = "Group Stage",
    ingested_at: datetime | None = None,
) -> HistoricalMatch:
    return HistoricalMatch(
        id=match_id,
        source="pandascore",
        source_match_id=source_match_id or match_id,
        started_at=started_at
        or datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        ended_at=ended_at
        if ended_at is not None
        else datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        team_a_name=team_a_name,
        team_b_name=team_b_name,
        team_a_source_id=team_a_source_id,
        team_b_source_id=team_b_source_id,
        winner_name=_winner_value(winner_side, team_a_name, team_b_name),
        winner_source_id=_winner_value(
            winner_side,
            team_a_source_id,
            team_b_source_id,
        ),
        winner_side=cast(WinnerSide | None, winner_side),
        tournament_name=tournament_name,
        tournament_source_id=tournament_source_id,
        league_name=league_name,
        league_source_id=league_source_id,
        series_name=series_name,
        series_source_id=series_source_id,
        raw_stage_label=raw_stage_label,
        competitive_stage=competitive_stage,
        normalized_round=normalized_round,
        best_of=3,
        status=status,
        ingested_at=ingested_at
        or datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc),
    )


def _winner_value(
    winner_side: str | None,
    team_a_value: T,
    team_b_value: T,
) -> T | None:
    if winner_side == "team_a":
        return team_a_value
    if winner_side == "team_b":
        return team_b_value
    return None
