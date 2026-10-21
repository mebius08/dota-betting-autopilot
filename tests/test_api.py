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


async def _get_matches(
    application: FastAPI,
    path: str = "/api/matches",
    *,
    raise_app_exceptions: bool = True,
) -> httpx.Response:
    transport = httpx.ASGITransport(
        app=application, raise_app_exceptions=raise_app_exceptions
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get(path)


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


@pytest.mark.parametrize("has_matches", [False, True])
def test_homepage_and_matches_api_share_existing_storage(
    tmp_path: Path, has_matches: bool
) -> None:
    db_path = tmp_path / "web.sqlite3"
    repository = SQLiteRepository(db_path)
    match = make_historical_match("stored-match")
    if has_matches:
        repository.save_historical_match(match)
    application = create_app(db_path)

    async def request_page_and_matches() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get("/"), await client.get("/api/matches")

    page, api_response = asyncio.run(request_page_and_matches())

    assert page.status_code == 200
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert '<html lang="ru">' in page.text
    assert "История матчей" in page.text
    assert 'fetch("/api/matches"' in page.text
    assert api_response.status_code == 200
    expected = [match] if has_matches else []
    assert TypeAdapter(list[HistoricalMatch]).validate_python(api_response.json()) == expected
    assert repository.list_historical_matches() == expected


@pytest.mark.parametrize("source", ["replay_clarity", "other_provider"])
def test_match_detail_uses_both_identifiers_and_matches_list_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setenv("DOTA_ANALYTICS_DB_BACKEND", "sqlite")
    application = create_app(tmp_path / "detail.sqlite3")
    repository = application.state.repository
    matches = [
        replace(
            make_historical_match("catalog-other", source_match_id="8960991322"),
            source="other_provider",
        ),
        replace(
            make_historical_match("catalog-second", source_match_id="8960882635"),
            source="replay_clarity",
        ),
        replace(
            make_historical_match("catalog-replay", source_match_id="8960991322"),
            source="replay_clarity",
            ended_at=None,
        ),
    ]
    for match in matches:
        repository.save_historical_match(match)

    response = asyncio.run(
        _get_matches(application, f"/api/matches/{source}/8960991322")
    )
    listing = asyncio.run(_get_matches(application))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    expected = next(
        match for match in matches
        if (match.source, match.source_match_id) == (source, "8960991322")
    )
    assert TypeAdapter(HistoricalMatch).validate_python(response.json()) == expected
    assert response.json() == next(
        row for row in listing.json()
        if (row["source"], row["source_match_id"]) == (source, "8960991322")
    )
    assert repository.list_historical_matches() == sorted(
        matches, key=lambda match: (match.started_at, match.source, match.source_match_id)
    )


@pytest.mark.parametrize(
    ("source", "source_match_id"),
    [
        ("unknown_provider", "8960991322"),
        ("replay_clarity", "missing"),
        ("replay_clarity", "catalog-internal-id"),
    ],
)
def test_missing_match_detail_returns_404(tmp_path, monkeypatch, source, source_match_id):
    monkeypatch.setenv("DOTA_ANALYTICS_DB_BACKEND", "sqlite")
    application = create_app(tmp_path / "detail.sqlite3")
    application.state.repository.save_historical_match(
        replace(
            make_historical_match("catalog-internal-id", source_match_id="8960991322"),
            source="replay_clarity",
        )
    )

    response = asyncio.run(
        _get_matches(application, f"/api/matches/{source}/{source_match_id}")
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Historical match not found."}


def test_sqlite_detail_database_error_is_not_a_404(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTA_ANALYTICS_DB_BACKEND", "sqlite")
    db_path = tmp_path / "broken.sqlite3"
    application = create_app(db_path)
    db_path.write_bytes(b"invalid SQLite file")

    response = asyncio.run(
        _get_matches(
            application, "/api/matches/replay_clarity/8960991322",
            raise_app_exceptions=False,
        )
    )
    assert response.status_code == 500


@pytest.mark.parametrize("has_match", [False, True])
def test_sqlite_economy_is_explicitly_unsupported(tmp_path, monkeypatch, has_match):
    monkeypatch.setenv("DOTA_ANALYTICS_DB_BACKEND", "sqlite")
    application = create_app(tmp_path / "economy.sqlite3")
    match = replace(
        make_historical_match("catalog-id", source_match_id="8960991322"),
        source="replay_clarity",
    )
    if has_match:
        application.state.repository.save_historical_match(match)

    response = asyncio.run(
        _get_matches(application, "/api/matches/replay_clarity/8960991322/economy")
    )

    assert response.status_code == 501
    assert response.json() == {
        "detail": "Historical match economy is not supported by this database backend."
    }
    assert application.state.repository.list_historical_matches() == (
        [match] if has_match else []
    )
