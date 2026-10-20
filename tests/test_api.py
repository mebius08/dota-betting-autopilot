import asyncio
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
import httpx
from pydantic import TypeAdapter
import pytest

from app.api import create_app
from app.history.domain import HistoricalMatch
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


async def _get_matches(application: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get("/api/matches")


def test_matches_returns_stored_historical_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "history.sqlite3"
    unused_path = tmp_path / "unused.sqlite3"
    monkeypatch.setenv("DOTA_ANALYTICS_DB_PATH", str(unused_path))
    repository = SQLiteRepository(db_path)
    earlier = make_historical_match("history-1")
    later = replace(
        make_historical_match(
            "history-2",
            started_at=datetime(2026, 1, 3, 10, tzinfo=timezone.utc),
            winner_side=None,
            status="unknown",
            tournament_name=None,
        ),
        ended_at=None,
    )
    repository.save_historical_match(later)
    repository.save_historical_match(earlier)

    response = asyncio.run(_get_matches(create_app(db_path)))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    assert len(payload) == 2
    assert set(payload[0]) == {field.name for field in fields(HistoricalMatch)}
    assert payload[0]["source_match_id"] == "history-1"
    assert payload[0]["competitive_stage"] == "group"
    assert payload[1]["ended_at"] is None
    assert payload[1]["winner_side"] is None
    assert TypeAdapter(list[HistoricalMatch]).validate_python(payload) == [earlier, later]
    assert not unused_path.exists()


def test_matches_returns_empty_list_for_empty_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "empty.sqlite3"
    monkeypatch.setenv("DOTA_ANALYTICS_DB_PATH", str(db_path))

    response = asyncio.run(_get_matches(create_app()))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == []
    assert SQLiteRepository(db_path).list_historical_matches() == []
