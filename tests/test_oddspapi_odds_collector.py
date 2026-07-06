from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from app.collectors.oddspapi_odds_collector import (
    ODDSPAPI_MISSING_API_KEY_MESSAGE,
    OddsPapiConfigurationError,
    OddsPapiFixture,
    OddsPapiOddsCollector,
    OddsPapiRequestError,
    OddsPapiResponseError,
    fetch_oddspapi_fixtures,
    find_matching_fixture,
    map_oddspapi_odds,
    match_from_fixture,
    parse_oddspapi_fixture,
    parse_oddspapi_fixtures,
)
from app.domain import Match


UNIT_TEST_API_KEY = "unit-test-placeholder"


def test_parse_oddspapi_fixtures_filters_to_dota2_fixtures_with_odds() -> None:
    fixtures = parse_oddspapi_fixtures(
        [
            _fixture_payload(),
            _fixture_payload(fixture_id="wrong-sport", sport_id=1),
            _fixture_payload(fixture_id="no-odds", has_odds=False),
            _fixture_payload(fixture_id=None),
            "not-a-dict",
        ]
    )

    assert [fixture.id for fixture in fixtures] == ["fixture-1"]
    assert fixtures[0].team_a == "Team Spirit"
    assert fixtures[0].team_b == "PARIVISION"
    assert fixtures[0].tournament_name == "DreamLeague"


def test_parse_oddspapi_fixture_skips_duplicate_or_invalid_teams() -> None:
    assert (
        parse_oddspapi_fixture(
            _fixture_payload(team_a=" TEAM   SPIRIT ", team_b="Team\tSpirit")
        )
        is None
    )
    assert parse_oddspapi_fixture(_fixture_payload(participants=[])) is None
    assert parse_oddspapi_fixture(_fixture_payload(start_time="not-a-date")) is None


def test_find_matching_fixture_uses_normalized_teams_and_reversed_order() -> None:
    start_time = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    match = _match(start_time=start_time)
    fixture = OddsPapiFixture(
        id="fixture-1",
        team_a=" parivision ",
        team_b="TEAM   SPIRIT",
        start_time=start_time + timedelta(minutes=90),
        has_odds=True,
    )

    assert find_matching_fixture(match, [fixture]) == fixture


def test_find_matching_fixture_skips_outside_tolerance_and_ambiguous() -> None:
    start_time = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    match = _match(start_time=start_time)
    outside_tolerance = OddsPapiFixture(
        id="late",
        team_a="Team Spirit",
        team_b="PARIVISION",
        start_time=start_time + timedelta(hours=3),
        has_odds=True,
    )
    first = OddsPapiFixture(
        id="first",
        team_a="Team Spirit",
        team_b="PARIVISION",
        start_time=start_time,
        has_odds=True,
    )
    second = OddsPapiFixture(
        id="second",
        team_a="PARIVISION",
        team_b="Team Spirit",
        start_time=start_time + timedelta(minutes=15),
        has_odds=True,
    )

    assert find_matching_fixture(match, [outside_tolerance]) is None
    assert find_matching_fixture(match, [first, second]) is None


def test_map_oddspapi_odds_maps_match_winner_prices() -> None:
    fixture = _fixture()
    match = match_from_fixture(fixture)

    snapshots = map_oddspapi_odds(
        {
            "updatedAt": "2026-07-06T13:00:00Z",
            "bookmakers": [
                {
                    "name": "pinnacle",
                    "markets": [
                        {
                            "id": "market-1",
                            "name": "Match Winner",
                            "outcomes": [
                                {"name": "Team Spirit", "price": 1.82},
                                {"name": "PARIVISION", "odds": "2.04"},
                            ],
                        }
                    ],
                }
            ],
        },
        fixture=fixture,
        match=match,
    )

    assert len(snapshots) == 2
    assert {snapshot.market for snapshot in snapshots} == {"map_winner"}
    assert {snapshot.selection for snapshot in snapshots} == {
        "Team Spirit",
        "PARIVISION",
    }
    assert [snapshot.odds for snapshot in snapshots] == [1.82, 2.04]
    assert {snapshot.bookmaker for snapshot in snapshots} == {"pinnacle"}
    assert snapshots[0].created_at == datetime(
        2026,
        7,
        6,
        13,
        0,
        tzinfo=timezone.utc,
    )


def test_map_oddspapi_odds_skips_unsupported_or_malformed_outcomes() -> None:
    fixture = _fixture()
    match = match_from_fixture(fixture)

    snapshots = map_oddspapi_odds(
        {
            "bookmakers": [
                {
                    "name": "inactive-book",
                    "active": False,
                    "markets": [
                        {
                            "name": "Match Winner",
                            "outcomes": [{"name": "Team Spirit", "price": 1.8}],
                        }
                    ],
                },
                {
                    "name": "bet365",
                    "markets": [
                        {
                            "name": "Map Winner",
                            "outcomes": [{"name": "Team Spirit", "price": 1.8}],
                        },
                        {
                            "name": "Match Winner",
                            "outcomes": [
                                {"name": "Team Spirit", "price": 1.8},
                                {"name": "PARIVISION", "price": True},
                                {"name": "PARIVISION", "price": 1.0},
                                {"name": "PARIVISION", "price": "nan"},
                                {"name": "PARIVISION", "price": "inf"},
                                {"name": "Draw", "price": 3.2},
                                {
                                    "name": "PARIVISION",
                                    "price": 2.0,
                                    "active": False,
                                },
                            ],
                        },
                    ],
                },
                {
                    "markets": [
                        {
                            "name": "Match Winner",
                            "outcomes": [{"name": "Team Spirit", "price": 1.8}],
                        }
                    ],
                },
            ]
        },
        fixture=fixture,
        match=match,
    )

    assert len(snapshots) == 1
    assert snapshots[0].bookmaker == "bet365"
    assert snapshots[0].selection == "Team Spirit"


def test_collect_fetches_fixtures_and_odds() -> None:
    seen_urls: list[str] = []

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        seen_urls.append(request.full_url)
        assert request.get_header("Accept") == "application/json"
        assert timeout == 2.5
        if "/fixtures" in request.full_url:
            return _FakeResponse(
                b'[{"id": "fixture-1", "sportId": 16, "hasOdds": true, '
                b'"startTime": "2026-07-06T12:00:00Z", '
                b'"participants": [{"name": "Team Spirit"}, '
                b'{"name": "PARIVISION"}]}]'
            )
        return _FakeResponse(
            b'{"bookmakers": [{"name": "pinnacle", "markets": ['
            b'{"name": "Match Winner", "outcomes": ['
            b'{"name": "Team Spirit", "price": 1.82}]}]}]}'
        )

    collector = OddsPapiOddsCollector(
        api_key=UNIT_TEST_API_KEY,
        timeout=2.5,
        limit=1,
        bookmakers=["pinnacle"],
        urlopen_func=fake_urlopen,
    )

    results = collector.collect()

    assert len(results) == 1
    assert results[0].fixture.id == "fixture-1"
    assert results[0].snapshots[0].selection == "Team Spirit"
    queries = [_query_from_url(url) for url in seen_urls]
    assert any(query.get("apiKey") == [UNIT_TEST_API_KEY] for query in queries)
    assert any(query.get("sportId") == ["16"] for query in queries)
    assert any(query.get("bookmakers") == ["pinnacle"] for query in queries)


def test_collector_missing_key_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODDSPAPI_API_KEY", raising=False)
    collector = OddsPapiOddsCollector()

    with pytest.raises(OddsPapiConfigurationError) as exc_info:
        collector.collect()

    assert str(exc_info.value) == ODDSPAPI_MISSING_API_KEY_MESSAGE


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        ("not-json", OddsPapiResponseError),
        ('{"id": 1}', OddsPapiResponseError),
    ],
)
def test_fetch_fixtures_handles_invalid_response(
    body: str,
    expected_error: type[Exception],
) -> None:
    with pytest.raises(expected_error):
        fetch_oddspapi_fixtures(
            api_key=UNIT_TEST_API_KEY,
            urlopen_func=_fake_urlopen(body),
        )


@pytest.mark.parametrize(
    ("status_code", "expected_text"),
    [
        (401, "HTTP 401"),
        (403, "HTTP 403"),
        (429, "HTTP 429"),
        (500, "HTTP 500"),
    ],
)
def test_fetch_handles_http_errors_without_leaking_key(
    status_code: int,
    expected_text: str,
) -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise HTTPError(request.full_url, status_code, "error", hdrs=None, fp=None)

    with pytest.raises(OddsPapiRequestError) as exc_info:
        fetch_oddspapi_fixtures(
            api_key=UNIT_TEST_API_KEY,
            urlopen_func=fake_urlopen,
        )

    message = str(exc_info.value)
    assert expected_text in message
    assert UNIT_TEST_API_KEY not in message


def test_fetch_handles_timeout_without_leaking_key() -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise TimeoutError

    with pytest.raises(OddsPapiRequestError) as exc_info:
        fetch_oddspapi_fixtures(
            api_key=UNIT_TEST_API_KEY,
            urlopen_func=fake_urlopen,
        )

    assert "timed out" in str(exc_info.value)
    assert UNIT_TEST_API_KEY not in str(exc_info.value)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False


def _fake_urlopen(body: str):
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        return _FakeResponse(body.encode("utf-8"))

    return fake_urlopen


def _query_from_url(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def _fixture() -> OddsPapiFixture:
    parsed = parse_oddspapi_fixture(_fixture_payload())
    assert parsed is not None
    return parsed


def _match(*, start_time: datetime | None) -> Match:
    return Match(
        id="pandascore-1",
        session_id="pandascore",
        tournament_name="DreamLeague",
        team_a="Team Spirit",
        team_b="PARIVISION",
        format="bo3",
        status="upcoming",
        start_time=start_time,
        external_id="1",
    )


def _fixture_payload(
    *,
    fixture_id: str | None = "fixture-1",
    sport_id: int = 16,
    has_odds: bool = True,
    team_a: str = "Team Spirit",
    team_b: str = "PARIVISION",
    participants: list[dict[str, object]] | None = None,
    start_time: object = "2026-07-06T12:00:00Z",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "sportId": sport_id,
        "hasOdds": has_odds,
        "startTime": start_time,
        "tournament": {"name": "DreamLeague"},
        "participants": (
            [{"name": team_a}, {"name": team_b}]
            if participants is None
            else participants
        ),
    }
    if fixture_id is not None:
        payload["id"] = fixture_id
    return payload
