from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.draft_history import (
    HistoricalDotaGame,
    HistoricalDraftAction,
    draft_action_id,
    historical_dota_game_id,
)
from app.draft_ml import build_draft_map_dataset
from app.storage import SQLiteRepository


def test_post_draft_row_contains_target_picks_but_not_winner(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    target = _game("target", day=2, winner_side="team_a")
    repository.upsert_historical_dota_game(
        target,
        _actions(target, team_a_heroes=(100, 10, 30, 40, 50)),
    )

    dataset = build_draft_map_dataset(repository)
    row = dataset.x.iloc[0].to_dict()

    assert dataset.y.tolist() == [1]
    assert row["team_a_pick_1"] == "opendota:hero:100"
    assert row["team_a_pick_2"] == "opendota:hero:10"
    assert row["team_b_pick_1"] == "opendota:hero:201"
    assert "winner_side" not in row
    assert row["team_a_pick_1"] > row["team_a_pick_2"]


def test_future_game_draft_and_exact_boundary_history_do_not_leak(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    prior = _game("prior", day=1, winner_side="team_a")
    exact_boundary = _game(
        "exact-boundary",
        day=2,
        started_hour=8,
        duration_hours=2,
        winner_side="team_a",
    )
    target = _game("target", day=2, started_hour=10, winner_side="team_a")
    future = _game("future", day=3, winner_side="team_b")
    for game, heroes in (
        (prior, (1, 2, 3, 4, 5)),
        (exact_boundary, (6, 7, 8, 9, 10)),
        (target, (1, 2, 3, 4, 5)),
        (future, (1, 2, 3, 4, 5)),
    ):
        repository.upsert_historical_dota_game(
            game,
            _actions(game, team_a_heroes=heroes),
        )

    dataset = build_draft_map_dataset(repository)
    target_row = dataset.x[
        [metadata.source_game_id == "target" for metadata in dataset.metadata]
    ].iloc[0]

    assert target_row["team_a_unseen_hero_count"] == 0
    assert target_row["hero_history_sample_total"] == 10


def test_matchup_orientation_is_team_a_perspective(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    prior = _game("prior", day=1, winner_side="team_b")
    target = _game("target", day=2, winner_side="team_a")
    repository.upsert_historical_dota_game(
        prior,
        _actions(
            prior,
            team_a_heroes=(20, 21, 22, 23, 24),
            team_b_heroes=(10, 11, 12, 13, 14),
        ),
    )
    repository.upsert_historical_dota_game(
        target,
        _actions(
            target,
            team_a_heroes=(10, 11, 12, 13, 14),
            team_b_heroes=(20, 21, 22, 23, 24),
        ),
    )

    dataset = build_draft_map_dataset(repository)
    target_row = dataset.x[
        [metadata.source_game_id == "target" for metadata in dataset.metadata]
    ].iloc[0]

    assert target_row["cross_team_matchup_sample_total"] == 25
    assert target_row["unseen_cross_team_matchup_count"] == 0
    assert target_row["cross_team_matchup_mean_win_rate"] == 1.0


def _game(
    source_game_id: str,
    *,
    day: int,
    winner_side: str,
    started_hour: int = 10,
    duration_hours: int = 1,
) -> HistoricalDotaGame:
    started_at = datetime(2026, 1, day, started_hour, tzinfo=timezone.utc)
    return HistoricalDotaGame(
        id=historical_dota_game_id("opendota", source_game_id),
        source="opendota",
        source_game_id=source_game_id,
        started_at=started_at,
        ended_at=started_at + timedelta(hours=duration_hours),
        team_a_name="Team A",
        team_b_name="Team B",
        team_a_source_id="101",
        team_b_source_id="202",
        winner_side=winner_side,  # type: ignore[arg-type]
        team_a_side="radiant",
        draft_complete=True,
        tournament_name="DreamLeague Season 28",
    )


def _actions(
    game: HistoricalDotaGame,
    *,
    team_a_heroes: tuple[int, int, int, int, int] = (101, 102, 103, 104, 105),
    team_b_heroes: tuple[int, int, int, int, int] = (201, 202, 203, 204, 205),
) -> tuple[HistoricalDraftAction, ...]:
    actions: list[HistoricalDraftAction] = []
    order = 1
    for hero_id in team_a_heroes:
        actions.append(_action(game, order, "radiant", hero_id))
        order += 1
    for hero_id in team_b_heroes:
        actions.append(_action(game, order, "dire", hero_id))
        order += 1
    return tuple(actions)


def _action(
    game: HistoricalDotaGame,
    order: int,
    side: str,
    hero_id: int,
) -> HistoricalDraftAction:
    return HistoricalDraftAction(
        id=draft_action_id(game.id, order),
        game_id=game.id,
        source=game.source,
        source_game_id=game.source_game_id,
        action_order=order,
        action_kind="pick",
        team_side=side,  # type: ignore[arg-type]
        hero_id=hero_id,
    )
