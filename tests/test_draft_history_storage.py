from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request

from app.draft_history import (
    OPENDOTA_SOURCE,
    OPENDOTA_USER_AGENT,
    HistoricalDotaGame,
    HistoricalDraftAction,
    draft_action_id,
    fetch_opendota_match_detail,
    fetch_opendota_pro_match_rows,
    historical_dota_game_id,
    map_opendota_match_detail,
)
from app.storage import SQLiteRepository, get_connection


def test_draft_tables_are_created(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path)

    with closing(get_connection(db_path)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

    assert "historical_dota_games" in tables
    assert "historical_draft_actions" in tables
    assert "historical_dota_player_final_stats" in tables
    assert "historical_dota_advantage_points" in tables


def test_opendota_mapping_preserves_order_side_winner_and_patch() -> None:
    mapped = map_opendota_match_detail(_opendota_payload())

    assert mapped.game is not None
    assert mapped.game.source == OPENDOTA_SOURCE
    assert mapped.game.source_game_id == "1001"
    assert mapped.game.team_a_side == "radiant"
    assert mapped.game.winner_side == "team_a"
    assert mapped.game.patch == "7.36"
    assert mapped.game.draft_complete is True
    assert [action.action_order for action in mapped.actions[:4]] == [1, 2, 3, 4]
    assert [action.hero_id for action in mapped.actions if action.action_kind == "pick"] == [
        10,
        20,
        11,
        21,
        12,
        22,
        13,
        23,
        14,
        24,
    ]


def test_opendota_match_detail_request_uses_project_user_agent_and_keyword_timeout() -> None:
    urlopen = _StdlibShapedFakeOpen(b'{"match_id": 1001}')

    detail = fetch_opendota_match_detail(
        1001,
        timeout=2.5,
        urlopen_func=urlopen,
    )

    assert detail == {"match_id": 1001}
    request = _assert_single_opendota_request(urlopen, expected_timeout=2.5)
    assert request.full_url == "https://api.opendota.com/api/matches/1001"


def test_opendota_pro_match_list_request_uses_project_user_agent_and_keyword_timeout() -> None:
    urlopen = _StdlibShapedFakeOpen(
        b'[{"match_id": 1001, "start_time": 1767259200}]'
    )

    rows = fetch_opendota_pro_match_rows(
        since=datetime(2025, 12, 31, tzinfo=timezone.utc),
        until=datetime(2026, 1, 2, tzinfo=timezone.utc),
        max_pages=1,
        timeout=4.5,
        urlopen_func=urlopen,
    )

    assert rows == [{"match_id": 1001, "start_time": 1_767_259_200}]
    request = _assert_single_opendota_request(urlopen, expected_timeout=4.5)
    assert request.full_url == "https://api.opendota.com/api/proMatches"


def test_draft_game_upsert_is_idempotent_and_replaces_actions(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    mapped = map_opendota_match_detail(_opendota_payload())
    assert mapped.game is not None

    first = repository.upsert_historical_dota_game(mapped.game, mapped.actions)
    second = repository.upsert_historical_dota_game(mapped.game, mapped.actions)

    assert first == "inserted"
    assert second == "unchanged"
    assert repository.list_historical_dota_games() == [mapped.game]
    assert len(repository.list_historical_draft_actions(mapped.game.id)) == len(
        mapped.actions
    )


def test_conflicting_draft_winner_is_rejected(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    game = _game("1001", winner_side="team_a")
    actions = _actions(game)
    repository.upsert_historical_dota_game(game, actions)
    conflicting = _game("1001", winner_side="team_b")

    try:
        repository.upsert_historical_dota_game(conflicting, _actions(conflicting))
    except ValueError as exc:
        assert "Conflicting historical Dota game winner" in str(exc)
    else:
        raise AssertionError("expected conflicting draft game to be rejected")


def _opendota_payload() -> dict[str, object]:
    picks_bans: list[dict[str, object]] = []
    for order, hero_id, team in (
        (1, 10, 0),
        (2, 20, 1),
        (3, 11, 0),
        (4, 21, 1),
        (5, 12, 0),
        (6, 22, 1),
        (7, 13, 0),
        (8, 23, 1),
        (9, 14, 0),
        (10, 24, 1),
        (11, 30, 0),
        (12, 31, 1),
    ):
        picks_bans.append(
            {
                "order": order,
                "hero_id": hero_id,
                "team": team,
                "is_pick": order <= 10,
            }
        )
    return {
        "match_id": 1001,
        "start_time": 1_767_259_200,
        "duration": 3600,
        "radiant_win": True,
        "radiant_team_id": 101,
        "dire_team_id": 202,
        "radiant_team_name": "Team A",
        "dire_team_name": "Team B",
        "series_id": 5001,
        "series_type": 3,
        "leagueid": 9001,
        "league_name": "DreamLeague Season 28",
        "patch": "7.36",
        "picks_bans": picks_bans,
    }


class _RawFakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "_RawFakeResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False


class _StdlibShapedFakeOpen:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[Request] = []
        self.data_values: list[object | None] = []
        self.timeouts: list[float | None] = []

    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float | None = None,
    ) -> _RawFakeResponse:
        self.requests.append(request)
        self.data_values.append(data)
        self.timeouts.append(timeout)
        if data is not None:
            raise AssertionError("OpenDota GET requests must not pass request data.")
        return _RawFakeResponse(self.body)


def _assert_single_opendota_request(
    urlopen: _StdlibShapedFakeOpen,
    *,
    expected_timeout: float,
) -> Request:
    assert len(urlopen.requests) == 1
    request = urlopen.requests[0]
    assert isinstance(request, Request)
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("User-agent") == OPENDOTA_USER_AGENT
    assert urlopen.data_values == [None]
    assert urlopen.timeouts == [expected_timeout]
    return request


def _game(source_game_id: str, *, winner_side: str) -> HistoricalDotaGame:
    return HistoricalDotaGame(
        id=historical_dota_game_id(OPENDOTA_SOURCE, source_game_id),
        source=OPENDOTA_SOURCE,
        source_game_id=source_game_id,
        started_at=datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
        team_a_name="Team A",
        team_b_name="Team B",
        team_a_source_id="101",
        team_b_source_id="202",
        winner_side=winner_side,  # type: ignore[arg-type]
        team_a_side="radiant",
        draft_complete=True,
        tournament_name="DreamLeague Season 28",
    )


def _actions(game: HistoricalDotaGame) -> tuple[HistoricalDraftAction, ...]:
    return tuple(
        HistoricalDraftAction(
            id=draft_action_id(game.id, order),
            game_id=game.id,
            source=game.source,
            source_game_id=game.source_game_id,
            action_order=order,
            action_kind="pick",
            team_side="radiant" if order <= 5 else "dire",
            hero_id=order,
        )
        for order in range(1, 11)
    )
