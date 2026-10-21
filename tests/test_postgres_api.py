import asyncio
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import httpx
from pydantic import TypeAdapter
import psycopg
import pytest

from app import api
from app.history.domain import HistoricalMatch
from app.storage import SQLiteRepository
from app.storage.postgres import CONNECTION_ENV, PostgresMatchRepository
from tests.history_test_helpers import make_historical_match


@pytest.fixture
def postgres(monkeypatch):
    monkeypatch.setenv("DOTA_ANALYTICS_DB_BACKEND", "postgresql")
    settings = dict(
        host="db",
        port="5432",
        dbname="analytics",
        user="analytics",
        password="test-password",
    )
    for key, name in CONNECTION_ENV.items():
        monkeypatch.setenv(name, settings[key])
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchall.return_value = []
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    sqlite = MagicMock(side_effect=AssertionError("SQLite fallback must not run"))
    monkeypatch.setattr(api, "SQLiteRepository", sqlite)
    return connection, connect, sqlite, settings


def get_matches(application, path="/api/matches"):
    async def request():
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path)

    return asyncio.run(request())


@pytest.mark.parametrize("backend", [None, "sqlite"])
def test_sqlite_remains_default_and_can_be_selected_explicitly(
    tmp_path, monkeypatch, backend
):
    if backend is None:
        monkeypatch.delenv("DOTA_ANALYTICS_DB_BACKEND", raising=False)
    else:
        monkeypatch.setenv("DOTA_ANALYTICS_DB_BACKEND", backend)
    monkeypatch.setenv("DOTA_ANALYTICS_DB_PATH", str(tmp_path / "local.sqlite3"))
    monkeypatch.setenv("POSTGRES_HOST", "unused-host")
    connect = MagicMock(side_effect=AssertionError("PostgreSQL must not be contacted"))
    monkeypatch.setattr(psycopg, "connect", connect)

    application = api.create_app()
    assert isinstance(application.state.repository, SQLiteRepository)
    response = get_matches(application)
    assert response.status_code == 200
    assert response.json() == []
    connect.assert_not_called()


def test_empty_postgres_uses_selected_connection_and_returns_empty_array(
    postgres, monkeypatch, tmp_path
):
    _, connect, sqlite, settings = postgres
    unused_path = tmp_path / "unused.sqlite3"
    monkeypatch.setenv("DOTA_ANALYTICS_DB_PATH", str(unused_path))

    application = api.create_app()
    assert isinstance(application.state.repository, PostgresMatchRepository)
    response = get_matches(application)
    assert response.status_code == 200
    assert response.json() == []
    assert connect.call_count == 2
    for call in connect.call_args_list:
        assert all(call.kwargs[key] == value for key, value in settings.items())
        assert call.kwargs["connect_timeout"] == 5
    sqlite.assert_not_called()
    assert not unused_path.exists()


def test_postgres_preserves_historical_match_json_contract(postgres):
    connection, _, sqlite, _ = postgres
    match = replace(
        make_historical_match("stored-match", winner_side=None), ended_at=None
    )
    row = asdict(match)
    row["competitive_stage"] = match.competitive_stage.value
    row["normalized_round"] = match.normalized_round.value
    connection.execute.return_value.fetchall.return_value = [row]

    response = get_matches(api.create_app())
    assert response.status_code == 200
    assert TypeAdapter(list[HistoricalMatch]).validate_python(response.json()) == [
        match
    ]
    assert set(response.json()[0]) == set(row)
    sqlite.assert_not_called()


def test_postgres_initial_connection_failure_prevents_startup(postgres):
    _, connect, sqlite, _ = postgres
    connect.side_effect = psycopg.OperationalError("database unavailable")
    with pytest.raises(psycopg.OperationalError, match="database unavailable"):
        api.create_app()
    sqlite.assert_not_called()


def test_postgres_query_connection_failure_returns_500_without_sqlite_fallback(
    postgres,
):
    _, connect, sqlite, _ = postgres
    application = api.create_app()
    connect.side_effect = psycopg.OperationalError("database unavailable")
    response = get_matches(application)
    assert response.status_code == 500
    assert response.text != "[]"
    sqlite.assert_not_called()


@pytest.mark.parametrize("missing", list(CONNECTION_ENV.values()))
def test_missing_postgres_settings_fail_before_connecting(
    postgres, monkeypatch, missing
):
    _, connect, sqlite, _ = postgres
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        api.create_app()
    connect.assert_not_called()
    sqlite.assert_not_called()


def test_explicit_sqlite_path_conflicts_with_postgres_backend(postgres, tmp_path: Path):
    _, connect, sqlite, _ = postgres
    with pytest.raises(ValueError, match="db_path"):
        api.create_app(tmp_path / "unused.sqlite3")
    connect.assert_not_called()
    sqlite.assert_not_called()


def test_unknown_backend_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("DOTA_ANALYTICS_DB_BACKEND", "unsupported")
    with pytest.raises(ValueError, match="DOTA_ANALYTICS_DB_BACKEND"):
        api.create_app(tmp_path / "unused.sqlite3")


def test_postgres_detail_preserves_list_contract_and_binds_both_identifiers(postgres):
    connection, _, sqlite, _ = postgres
    match = replace(
        make_historical_match("catalog-id", source_match_id="8960991322"),
        source="replay_clarity",
        ended_at=None,
    )
    row = asdict(match)
    row["competitive_stage"] = match.competitive_stage.value
    row["normalized_round"] = match.normalized_round.value
    connection.execute.return_value.fetchall.return_value = [row]
    connection.execute.return_value.fetchone.return_value = row
    application = api.create_app()
    listing = get_matches(application)

    response = get_matches(application, "/api/matches/replay_clarity/8960991322")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == listing.json()[0]
    assert TypeAdapter(HistoricalMatch).validate_python(response.json()) == match
    query, parameters = connection.execute.call_args.args
    assert "WHERE source = %s AND source_match_id = %s" in query
    assert parameters == ("replay_clarity", "8960991322")
    sqlite.assert_not_called()


@pytest.mark.parametrize(
    ("source", "source_match_id"),
    [
        ("unknown_provider", "8960991322"),
        ("replay_clarity", "missing' OR '1'='1"),
    ],
)
def test_postgres_missing_detail_is_404_with_bound_parameters(
    postgres, source, source_match_id
):
    connection, _, sqlite, _ = postgres
    connection.execute.return_value.fetchone.return_value = None
    application = api.create_app()

    response = get_matches(application, f"/api/matches/{source}/{source_match_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Historical match not found."}
    query, parameters = connection.execute.call_args.args
    assert "WHERE source = %s AND source_match_id = %s" in query
    assert parameters == (source, source_match_id)
    sqlite.assert_not_called()


@pytest.mark.parametrize("failure_at", ["connect", "query"])
def test_postgres_detail_database_error_is_not_a_404(postgres, failure_at):
    connection, connect, sqlite, _ = postgres
    application = api.create_app()
    if failure_at == "connect":
        connect.side_effect = psycopg.OperationalError("database unavailable")
    else:
        connection.execute.side_effect = psycopg.ProgrammingError("query failed")

    response = get_matches(application, "/api/matches/replay_clarity/8960991322")
    assert response.status_code == 500
    sqlite.assert_not_called()


def economy_snapshot(time=60.0002, tick=34034):
    return {
        "actual_game_time_seconds": time,
        "replay_tick": tick,
        "radiant_net_worth": 15001,
        "dire_net_worth": 14800,
        "radiant_current_gold": 1301,
        "dire_current_gold": 0,
        "radiant_total_xp": 12001,
        "dire_total_xp": 12500,
    }


@pytest.mark.parametrize("has_snapshots", [False, True])
def test_postgres_economy_preserves_actual_time_and_exact_contract(
    postgres, has_snapshots
):
    connection, _, sqlite, _ = postgres
    connection.execute.return_value.fetchone.return_value = asdict(
        make_historical_match("catalog-id", source_match_id="8960991322")
    )
    snapshots = [economy_snapshot()] if has_snapshots else []
    connection.execute.return_value.fetchall.return_value = snapshots
    application = api.create_app()
    connection.execute.reset_mock()

    response = get_matches(
        application, "/api/matches/replay_clarity/8960991322/economy"
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == snapshots
    assert connection.execute.call_count == 2
    query, parameters = connection.execute.call_args.args
    assert "FROM historical_match_economy_snapshots" in query
    assert "ORDER BY actual_game_time_seconds, replay_tick" in query
    assert parameters == ("replay_clarity", "8960991322")
    sqlite.assert_not_called()


@pytest.mark.parametrize("source", ["replay_clarity", "unknown_provider"])
def test_postgres_economy_missing_match_is_404_without_reading_snapshots(
    postgres, source
):
    connection, _, sqlite, _ = postgres
    connection.execute.return_value.fetchone.return_value = None
    application = api.create_app()
    connection.execute.reset_mock()

    response = get_matches(application, f"/api/matches/{source}/missing/economy")

    assert response.status_code == 404
    assert response.json() == {"detail": "Historical match not found."}
    connection.execute.assert_called_once_with(
        "SELECT * FROM historical_matches WHERE source = %s AND source_match_id = %s",
        (source, "missing"),
    )
    sqlite.assert_not_called()


@pytest.mark.parametrize("failure_at", ["connect", "catalog", "economy"])
def test_postgres_economy_database_errors_are_not_empty_history_or_404(
    postgres, failure_at
):
    connection, connect, sqlite, _ = postgres
    application = api.create_app()
    error = psycopg.OperationalError("database unavailable")
    if failure_at == "connect":
        connect.side_effect = error
    elif failure_at == "catalog":
        connection.execute.side_effect = error
    else:
        catalog_result = MagicMock()
        catalog_result.fetchone.return_value = asdict(make_historical_match("stored"))
        connection.execute.side_effect = [catalog_result, error]

    response = get_matches(
        application, "/api/matches/replay_clarity/8960991322/economy"
    )

    assert response.status_code == 500
    assert response.text != "[]"
    sqlite.assert_not_called()


def test_economy_repository_filters_exact_identity_and_orders_in_sql(postgres):
    """Execute the read SQL against a temporary SQL double, without PostgreSQL I/O."""
    connection, _, _, _ = postgres
    repository = PostgresMatchRepository({})
    later = economy_snapshot()
    earlier = economy_snapshot(time=0.0002, tick=32234)
    schema = Path("app/storage/postgres_economy_schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = lambda cursor, values: dict(
            zip((column[0] for column in cursor.description), values)
        )
        database.execute(schema)
        # Insert unsorted frames plus rows from a different source and match.
        for source, match_id, frame in [
            ("replay_clarity", "8960991322", later),
            ("other_provider", "8960991322", earlier),
            ("replay_clarity", "other_match", earlier),
            ("replay_clarity", "8960991322", earlier),
        ]:
            row = dict(source=source, source_match_id=match_id, replay_sha256="a" * 64)
            row.update(frame)
            database.execute(
                f"INSERT INTO historical_match_economy_snapshots ({', '.join(row)}) "
                f"VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )

        def execute_read(query, parameters):
            assert query.startswith("SELECT ")
            assert "ORDER BY actual_game_time_seconds, replay_tick" in query
            return database.execute(query.replace("%s", "?"), parameters)

        connection.execute.side_effect = execute_read
        assert repository.list_historical_match_economy(
            "replay_clarity", "8960991322"
        ) == [earlier, later]
        assert repository.list_historical_match_economy(
            "replay_clarity", "missing' OR '1'='1"
        ) == []


def pov_snapshot(team, time=2310.0334, tick=101535):
    return {
        "actual_game_time_seconds": time,
        "replay_tick": tick,
        "pov_team": team,
        "visible_enemy_count": 3,
        "visible_enemy_with_position_count": 0 if team == "DIRE" else 3,
        "hidden_enemy_count": 2,
        "own_team_spread": None if team == "DIRE" else 5879.364732537277,
        "nearest_visible_enemy_distance": None if team == "DIRE" else 5397.91892634111,
        "own_team_spread_status": "INSUFFICIENT_OWN_POSITIONS" if team == "DIRE" else "OK",
        "nearest_visible_enemy_status": "NO_VISIBLE_ENEMY_POSITION" if team == "DIRE" else "OK",
    }


@pytest.mark.parametrize("has_snapshots", [False, True])
def test_postgres_pov_preserves_team_pair_exact_times_nulls_and_statuses(
    postgres, has_snapshots
):
    connection, _, sqlite, _ = postgres
    connection.execute.return_value.fetchone.return_value = asdict(
        replace(
            make_historical_match("catalog-id", source_match_id="8960991322"),
            source="replay_clarity",
        )
    )
    snapshots = [pov_snapshot(team) for team in ("DIRE", "RADIANT")] if has_snapshots else []
    connection.execute.return_value.fetchall.return_value = snapshots
    application = api.create_app()
    connection.execute.reset_mock()

    response = get_matches(application, "/api/matches/replay_clarity/8960991322/pov")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == snapshots
    if has_snapshots:
        assert [row["pov_team"] for row in response.json()] == ["DIRE", "RADIANT"]
        assert set(response.json()[0]) == set(pov_snapshot("DIRE"))
        assert response.json()[0]["visible_enemy_count"] == 3
        assert response.json()[0]["visible_enemy_with_position_count"] == 0
        assert response.json()[0]["nearest_visible_enemy_distance"] is None
        assert response.json()[0]["own_team_spread"] is None
    assert connection.execute.call_count == 2
    query, parameters = connection.execute.call_args.args
    assert "FROM historical_match_pov_snapshots" in query
    assert "ORDER BY actual_game_time_seconds, replay_tick, pov_team" in query
    assert parameters == ("replay_clarity", "8960991322")
    sqlite.assert_not_called()


@pytest.mark.parametrize("source", ["replay_clarity", "unknown_provider"])
def test_postgres_missing_pov_match_is_404_without_reading_history(postgres, source):
    connection, _, sqlite, _ = postgres
    connection.execute.return_value.fetchone.return_value = None
    application = api.create_app()
    connection.execute.reset_mock()

    response = get_matches(application, f"/api/matches/{source}/missing/pov")

    assert response.status_code == 404
    assert response.json() == {"detail": "Historical match not found."}
    connection.execute.assert_called_once_with(
        "SELECT * FROM historical_matches WHERE source = %s AND source_match_id = %s",
        (source, "missing"),
    )
    sqlite.assert_not_called()


@pytest.mark.parametrize("failure_at", ["connect", "catalog", "pov"])
def test_postgres_pov_database_error_is_not_empty_history_or_404(postgres, failure_at):
    connection, connect, sqlite, _ = postgres
    application = api.create_app()
    if failure_at == "connect":
        connect.side_effect = psycopg.OperationalError("database unavailable")
    elif failure_at == "catalog":
        connection.execute.side_effect = psycopg.ProgrammingError("catalog query failed")
    else:
        catalog_result = MagicMock()
        catalog_result.fetchone.return_value = asdict(make_historical_match("stored"))
        connection.execute.side_effect = [
            catalog_result, psycopg.ProgrammingError("POV table unavailable")
        ]

    response = get_matches(application, "/api/matches/replay_clarity/8960991322/pov")

    assert response.status_code == 500
    assert response.text != "[]"
    sqlite.assert_not_called()


def test_pov_repository_filters_identity_and_sorts_actual_frames_and_teams(postgres):
    """Run the SELECT against temporary SQL data, not a live PostgreSQL database."""
    connection, _, _, _ = postgres
    repository = PostgresMatchRepository({})
    earlier = [pov_snapshot(team) for team in ("DIRE", "RADIANT")]
    later = [pov_snapshot(team, 2320.0001, 101834) for team in ("DIRE", "RADIANT")]
    schema = Path("app/storage/postgres_pov_schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = lambda cursor, values: dict(
            zip((column[0] for column in cursor.description), values)
        )
        database.execute(schema)
        for source, match_id, frame in [
            ("replay_clarity", "8960991322", later[1]),
            ("other_provider", "8960991322", earlier[0]),
            ("replay_clarity", "other_match", earlier[0]),
            ("replay_clarity", "8960991322", earlier[1]),
            ("replay_clarity", "8960991322", later[0]),
            ("replay_clarity", "8960991322", earlier[0]),
        ]:
            row = dict(source=source, source_match_id=match_id, replay_sha256="a" * 64)
            row.update(frame)
            database.execute(
                f"INSERT INTO historical_match_pov_snapshots ({', '.join(row)}) "
                f"VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )

        def execute_read(query, parameters):
            assert query.startswith("SELECT ")
            assert "ORDER BY actual_game_time_seconds, replay_tick, pov_team" in query
            return database.execute(query.replace("%s", "?"), parameters)

        connection.execute.side_effect = execute_read
        assert repository.list_historical_match_pov(
            "replay_clarity", "8960991322"
        ) == earlier + later
        assert repository.list_historical_match_pov(
            "replay_clarity", "missing' OR '1'='1"
        ) == []
