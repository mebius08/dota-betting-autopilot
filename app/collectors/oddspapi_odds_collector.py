from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
import socket
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.match_collector import normalize_team_name
from app.domain import Match, OddsSnapshot


ODDSPAPI_BASE_URL = "https://api.oddspapi.io"
ODDSPAPI_FIXTURES_URL = f"{ODDSPAPI_BASE_URL}/v4/fixtures"
ODDSPAPI_ODDS_URL = f"{ODDSPAPI_BASE_URL}/v4/odds"
ODDSPAPI_API_KEY_ENV = "ODDSPAPI_API_KEY"
ODDSPAPI_DOTA2_SPORT_ID = 16
ODDSPAPI_MISSING_API_KEY_MESSAGE = (
    "OddsPapi API key is not configured.\n"
    "Set ODDSPAPI_API_KEY before using the oddspapi provider."
)
MATCH_START_TOLERANCE = timedelta(hours=2)
INTERNAL_MATCH_WINNER_MARKET = "map_winner"


class OddsPapiError(RuntimeError):
    pass


class OddsPapiConfigurationError(OddsPapiError):
    pass


class OddsPapiRequestError(OddsPapiError):
    pass


class OddsPapiResponseError(OddsPapiError):
    pass


class _HTTPResponse(Protocol):
    def read(self) -> bytes:
        ...

    def __enter__(self) -> "_HTTPResponse":
        ...

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        ...


class _UrlOpen(Protocol):
    def __call__(self, request: Request, timeout: float) -> _HTTPResponse:
        ...


DEFAULT_URL_OPEN = cast(_UrlOpen, urlopen)


@dataclass(frozen=True)
class OddsPapiFixture:
    id: str
    team_a: str
    team_b: str
    start_time: datetime | None
    has_odds: bool
    tournament_name: str = "OddsPapi Dota 2"


@dataclass(frozen=True)
class OddsPapiFixtureOdds:
    fixture: OddsPapiFixture
    snapshots: list[OddsSnapshot]


class OddsPapiOddsCollector:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 10.0,
        limit: int = 20,
        bookmakers: Sequence[str] | None = None,
        fixtures_endpoint: str = ODDSPAPI_FIXTURES_URL,
        odds_endpoint: str = ODDSPAPI_ODDS_URL,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        self.api_key = (
            api_key if api_key is not None else os.environ.get(ODDSPAPI_API_KEY_ENV)
        )
        self.timeout = timeout
        self.limit = limit
        self.bookmakers = list(bookmakers or [])
        self.fixtures_endpoint = fixtures_endpoint
        self.odds_endpoint = odds_endpoint
        self.urlopen_func = urlopen_func

    def collect(self) -> list[OddsPapiFixtureOdds]:
        api_key = self._required_api_key()
        fixtures = parse_oddspapi_fixtures(
            fetch_oddspapi_fixtures(
                api_key=api_key,
                timeout=self.timeout,
                limit=self.limit,
                endpoint=self.fixtures_endpoint,
                urlopen_func=self.urlopen_func,
            ),
            require_has_odds=True,
            limit=self.limit,
        )

        results: list[OddsPapiFixtureOdds] = []
        for fixture in fixtures:
            odds_payload = fetch_oddspapi_odds(
                api_key=api_key,
                fixture_id=fixture.id,
                timeout=self.timeout,
                endpoint=self.odds_endpoint,
                bookmakers=self.bookmakers,
                urlopen_func=self.urlopen_func,
            )
            snapshots = map_oddspapi_odds(
                odds_payload,
                fixture=fixture,
                match=match_from_fixture(fixture),
            )
            results.append(OddsPapiFixtureOdds(fixture=fixture, snapshots=snapshots))
        return results

    def _required_api_key(self) -> str:
        api_key = (self.api_key or "").strip()
        if not api_key:
            raise OddsPapiConfigurationError(ODDSPAPI_MISSING_API_KEY_MESSAGE)
        return api_key


def fetch_oddspapi_fixtures(
    *,
    api_key: str,
    timeout: float = 10.0,
    limit: int = 20,
    endpoint: str = ODDSPAPI_FIXTURES_URL,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> list[object]:
    payload = fetch_oddspapi_json(
        endpoint=endpoint,
        api_key=api_key,
        timeout=timeout,
        query={
            "sportId": str(ODDSPAPI_DOTA2_SPORT_ID),
            "limit": str(limit),
        },
        urlopen_func=urlopen_func,
    )
    if not isinstance(payload, list):
        raise OddsPapiResponseError(
            "OddsPapi returned unexpected fixtures JSON shape: expected a list."
        )
    return payload


def fetch_oddspapi_odds(
    *,
    api_key: str,
    fixture_id: str,
    timeout: float = 10.0,
    endpoint: str = ODDSPAPI_ODDS_URL,
    bookmakers: Sequence[str] | None = None,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> object:
    query = {
        "sportId": str(ODDSPAPI_DOTA2_SPORT_ID),
        "fixtureId": fixture_id,
    }
    if bookmakers:
        query["bookmakers"] = ",".join(bookmakers)
    payload = fetch_oddspapi_json(
        endpoint=endpoint,
        api_key=api_key,
        timeout=timeout,
        query=query,
        urlopen_func=urlopen_func,
    )
    if not isinstance(payload, (Mapping, list)):
        raise OddsPapiResponseError(
            "OddsPapi returned unexpected odds JSON shape: expected an object or list."
        )
    return payload


def fetch_oddspapi_json(
    *,
    endpoint: str,
    api_key: str,
    timeout: float,
    query: Mapping[str, str],
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> object:
    api_key = api_key.strip()
    if not api_key:
        raise OddsPapiConfigurationError(ODDSPAPI_MISSING_API_KEY_MESSAGE)

    request = Request(
        _url_with_query(endpoint, {"apiKey": api_key, **query}),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen_func(request, timeout=timeout) as response:
            raw_body = response.read()
    except HTTPError as exc:
        raise _http_error(exc) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise OddsPapiRequestError("OddsPapi request timed out.") from exc
    except (URLError, OSError) as exc:
        raise OddsPapiRequestError("OddsPapi network request failed.") from exc

    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OddsPapiResponseError("OddsPapi returned invalid JSON.") from exc


def parse_oddspapi_fixtures(
    payloads: Iterable[object],
    *,
    require_has_odds: bool = True,
    limit: int | None = None,
) -> list[OddsPapiFixture]:
    fixtures: list[OddsPapiFixture] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        fixture = parse_oddspapi_fixture(payload)
        if fixture is None:
            continue
        if require_has_odds and not fixture.has_odds:
            continue
        fixtures.append(fixture)
        if limit is not None and len(fixtures) >= limit:
            break
    return fixtures


def parse_oddspapi_fixture(
    payload: Mapping[str, object],
) -> OddsPapiFixture | None:
    sport_id = _int_from_value(
        payload.get("sportId")
        or payload.get("sport_id")
        or _mapping_value(payload.get("sport"), "id")
    )
    if sport_id is not None and sport_id != ODDSPAPI_DOTA2_SPORT_ID:
        return None

    fixture_id = _text_from_value(
        payload.get("id") or payload.get("fixtureId") or payload.get("fixture_id")
    )
    if fixture_id is None:
        return None

    teams = _fixture_team_names(payload)
    if len(teams) != 2:
        return None

    team_a, team_b = teams
    if normalize_team_name(team_a) == normalize_team_name(team_b):
        return None

    start_time = _fixture_start_time(payload)
    if start_time is _INVALID_DATETIME:
        return None

    return OddsPapiFixture(
        id=fixture_id,
        team_a=team_a,
        team_b=team_b,
        start_time=cast(datetime | None, start_time),
        has_odds=_bool_from_value(
            payload.get("hasOdds")
            if "hasOdds" in payload
            else payload.get("has_odds")
        ),
        tournament_name=_fixture_tournament_name(payload),
    )


def match_from_fixture(fixture: OddsPapiFixture) -> Match:
    return Match(
        id=f"oddspapi-{fixture.id}",
        session_id="oddspapi",
        tournament_name=fixture.tournament_name,
        team_a=fixture.team_a,
        team_b=fixture.team_b,
        format="unknown",
        status="upcoming",
        start_time=fixture.start_time,
        external_id=fixture.id,
    )


def find_matching_fixture(
    match: Match,
    fixtures: Iterable[OddsPapiFixture],
    *,
    start_tolerance: timedelta = MATCH_START_TOLERANCE,
) -> OddsPapiFixture | None:
    candidates = [
        fixture
        for fixture in fixtures
        if fixture_matches_match(
            match,
            fixture,
            start_tolerance=start_tolerance,
        )
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def fixture_matches_match(
    match: Match,
    fixture: OddsPapiFixture,
    *,
    start_tolerance: timedelta = MATCH_START_TOLERANCE,
) -> bool:
    if not _team_pair_matches(match, fixture):
        return False
    return _start_times_match(match.start_time, fixture.start_time, start_tolerance)


def map_oddspapi_odds(
    payload: object,
    *,
    fixture: OddsPapiFixture,
    match: Match,
) -> list[OddsSnapshot]:
    snapshots: list[OddsSnapshot] = []
    created_at = _provider_updated_at(payload) or datetime.now(timezone.utc)

    for bookmaker in _bookmaker_payloads(payload):
        bookmaker_name = _bookmaker_name(bookmaker)
        if bookmaker_name is None or not _is_active(bookmaker):
            continue

        for market in _market_payloads(bookmaker):
            if not _is_active(market):
                continue
            if not _is_match_winner_market(market):
                continue

            external_market_id = _text_from_value(market.get("id"))
            for outcome in _outcome_payloads(market):
                if not _is_active(outcome):
                    continue

                price = _decimal_odds(outcome.get("price") or outcome.get("odds"))
                if price is None:
                    continue

                selection = _selection_for_outcome(outcome, fixture, match)
                if selection is None:
                    continue

                snapshots.append(
                    OddsSnapshot(
                        id=_snapshot_id(
                            fixture_id=fixture.id,
                            bookmaker=bookmaker_name,
                            selection=selection,
                            index=len(snapshots) + 1,
                        ),
                        session_id=match.session_id,
                        match_id=match.id,
                        external_market_id=external_market_id
                        or f"oddspapi-{fixture.id}-{bookmaker_name}-match-winner",
                        market=INTERNAL_MATCH_WINNER_MARKET,
                        selection=selection,
                        line=None,
                        odds=price,
                        phase="pre_match",
                        is_live=False,
                        is_suspended=False,
                        bookmaker=bookmaker_name,
                        created_at=created_at,
                    )
                )

    return snapshots


def _http_error(exc: HTTPError) -> OddsPapiRequestError:
    if exc.code == 401:
        return OddsPapiRequestError(
            "OddsPapi request failed: HTTP 401 unauthorized. "
            "Check ODDSPAPI_API_KEY."
        )
    if exc.code == 403:
        return OddsPapiRequestError(
            "OddsPapi request failed: HTTP 403 forbidden. "
            "Check OddsPapi API permissions."
        )
    if exc.code == 429:
        return OddsPapiRequestError(
            "OddsPapi request failed: HTTP 429 rate limited."
        )
    return OddsPapiRequestError(f"OddsPapi request failed: HTTP {exc.code}.")


def _url_with_query(endpoint: str, query: Mapping[str, str]) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(query)}"


def _fixture_team_names(payload: Mapping[str, object]) -> list[str]:
    direct = _team_names_from_list(
        payload.get("participants")
        or payload.get("competitors")
        or payload.get("teams")
    )
    if len(direct) == 2:
        return direct

    paired: list[str] = []
    for key in ("homeTeam", "awayTeam"):
        name = _team_name_from_value(payload.get(key))
        if name is not None:
            paired.append(name)
    if len(paired) == 2:
        return paired

    for first_key, second_key in (
        ("home", "away"),
        ("team1", "team2"),
        ("teamA", "teamB"),
    ):
        first = _team_name_from_value(payload.get(first_key))
        second = _team_name_from_value(payload.get(second_key))
        if first is not None and second is not None:
            return [first, second]

    return []


def _team_names_from_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    names: list[str] = []
    for item in value:
        name = _team_name_from_value(item)
        if name is not None:
            names.append(name)
    return names


def _team_name_from_value(value: object) -> str | None:
    if isinstance(value, Mapping):
        name = (
            _text_from_value(value.get("name"))
            or _text_from_value(value.get("teamName"))
            or _text_from_value(value.get("displayName"))
        )
    else:
        name = _text_from_value(value)
    return _display_text(name)


def _fixture_start_time(payload: Mapping[str, object]) -> datetime | None | object:
    value = (
        _text_from_value(payload.get("startTime"))
        or _text_from_value(payload.get("start_time"))
        or _text_from_value(payload.get("startsAt"))
        or _text_from_value(payload.get("commenceTime"))
        or _text_from_value(payload.get("startDate"))
    )
    if value is None:
        return None
    return _datetime_from_text(value)


def _fixture_tournament_name(payload: Mapping[str, object]) -> str:
    for key in ("tournament", "league", "competition"):
        name = _team_name_from_value(payload.get(key))
        if name:
            return name
    return "OddsPapi Dota 2"


def _team_pair_matches(match: Match, fixture: OddsPapiFixture) -> bool:
    match_a = normalize_team_name(match.team_a)
    match_b = normalize_team_name(match.team_b)
    fixture_a = normalize_team_name(fixture.team_a)
    fixture_b = normalize_team_name(fixture.team_b)
    return (
        match_a == fixture_a
        and match_b == fixture_b
        or match_a == fixture_b
        and match_b == fixture_a
    )


def _start_times_match(
    match_start: datetime | None,
    fixture_start: datetime | None,
    tolerance: timedelta,
) -> bool:
    if match_start is None or fixture_start is None:
        return True

    match_start = _as_utc(match_start)
    fixture_start = _as_utc(fixture_start)
    return abs(match_start - fixture_start) <= tolerance


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bookmaker_payloads(payload: object) -> list[Mapping[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []

    for key in ("bookmakers", "sportsbooks"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]

    value = payload.get("data")
    if isinstance(value, Mapping):
        return _bookmaker_payloads(value)

    if "markets" in payload or "odds" in payload:
        return [payload]

    return []


def _market_payloads(bookmaker: Mapping[str, object]) -> list[Mapping[str, object]]:
    for key in ("markets", "odds"):
        value = bookmaker.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _outcome_payloads(market: Mapping[str, object]) -> list[Mapping[str, object]]:
    for key in ("outcomes", "selections", "prices"):
        value = market.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _bookmaker_name(bookmaker: Mapping[str, object]) -> str | None:
    for key in ("name", "bookmaker", "key", "id"):
        value = _display_text(_text_from_value(bookmaker.get(key)))
        if value:
            return value
    return None


def _is_match_winner_market(market: Mapping[str, object]) -> bool:
    name = (
        _text_from_value(market.get("name"))
        or _text_from_value(market.get("market"))
        or _text_from_value(market.get("key"))
    )
    if name is None:
        return False
    normalized = normalize_team_name(name.replace("_", " ").replace("-", " "))
    return normalized in ("match winner", "moneyline", "winner")


def _selection_for_outcome(
    outcome: Mapping[str, object],
    fixture: OddsPapiFixture,
    match: Match,
) -> str | None:
    name = (
        _text_from_value(outcome.get("name"))
        or _text_from_value(outcome.get("selection"))
        or _text_from_value(outcome.get("team"))
    )
    if name is None:
        return None

    normalized = normalize_team_name(name)
    if normalized == normalize_team_name(fixture.team_a):
        return match.team_a
    if normalized == normalize_team_name(fixture.team_b):
        return match.team_b
    return None


def _provider_updated_at(payload: object) -> datetime | None:
    if not isinstance(payload, Mapping):
        return None
    value = (
        _text_from_value(payload.get("updatedAt"))
        or _text_from_value(payload.get("updated_at"))
        or _text_from_value(payload.get("lastUpdate"))
        or _text_from_value(payload.get("last_update"))
    )
    if value is None:
        return None
    parsed = _datetime_from_text(value)
    if parsed is _INVALID_DATETIME:
        return None
    return cast(datetime, parsed)


def _is_active(payload: Mapping[str, object]) -> bool:
    active = payload.get("active")
    if active is None:
        active = payload.get("isActive")
    if isinstance(active, bool):
        return active

    status = _text_from_value(payload.get("status"))
    if status is None:
        return True
    return status.casefold() not in ("inactive", "suspended", "closed")


def _decimal_odds(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value))
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 1:
        return None
    return parsed


def _snapshot_id(
    *,
    fixture_id: str,
    bookmaker: str,
    selection: str,
    index: int,
) -> str:
    parts = [
        "oddspapi",
        _id_part(fixture_id),
        _id_part(bookmaker),
        _id_part(selection),
        str(index),
    ]
    return "-".join(part for part in parts if part)


def _id_part(value: str) -> str:
    normalized = normalize_team_name(value)
    return "".join(char if char.isalnum() else "-" for char in normalized).strip("-")


_INVALID_DATETIME = object()


def _datetime_from_text(value: str) -> datetime | object:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _INVALID_DATETIME


def _text_from_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _display_text(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.strip().split())


def _int_from_value(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _bool_from_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes")
    return bool(value)


def _mapping_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return None
