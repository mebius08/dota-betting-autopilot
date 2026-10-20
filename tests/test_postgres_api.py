import asyncio
from dataclasses import asdict, replace
from pathlib import Path
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
