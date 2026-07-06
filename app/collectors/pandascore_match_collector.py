from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
import json
import os
import socket
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.match_collector import (
    MatchCollector,
    normalize_team_name,
)
from app.domain import Match, MatchStatus, Session


PANDASCORE_MATCHES_URL = "https://api.pandascore.co/dota2/matches"
PANDASCORE_TOKEN_ENV = "PANDASCORE_TOKEN"
PANDASCORE_MISSING_TOKEN_MESSAGE = (
    "PandaScore token is not configured.\n"
    "Set PANDASCORE_TOKEN before using the pandascore provider."
)

MatchStatusFilter = Literal["upcoming", "live", "all"]


class PandaScoreError(RuntimeError):
    pass


class PandaScoreConfigurationError(PandaScoreError):
    pass


class PandaScoreRequestError(PandaScoreError):
    pass


class PandaScoreResponseError(PandaScoreError):
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


class PandaScoreMatchCollector(MatchCollector):
    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 10.0,
        limit: int = 20,
        status_filter: MatchStatusFilter = "all",
        endpoint: str = PANDASCORE_MATCHES_URL,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if status_filter not in ("upcoming", "live", "all"):
            raise ValueError("status_filter must be one of: upcoming, live, all")

        self.token = token if token is not None else os.environ.get(PANDASCORE_TOKEN_ENV)
        self.timeout = timeout
        self.limit = limit
        self.status_filter = status_filter
        self.endpoint = endpoint
        self.urlopen_func = urlopen_func

    def fetch_matches(self, session: Session) -> list[Match]:
        return self.collect(session_id=session.id)

    def collect(self, session_id: str = "pandascore") -> list[Match]:
        payloads = fetch_pandascore_matches(
            token=self._required_token(),
            timeout=self.timeout,
            limit=self.limit,
            endpoint=self.endpoint,
            urlopen_func=self.urlopen_func,
        )
        return map_pandascore_matches(
            payloads,
            session_id=session_id,
            status_filter=self.status_filter,
        )

    def _required_token(self) -> str:
        token = (self.token or "").strip()
        if not token:
            raise PandaScoreConfigurationError(PANDASCORE_MISSING_TOKEN_MESSAGE)
        return token


def fetch_pandascore_matches(
    *,
    token: str,
    timeout: float = 10.0,
    limit: int = 20,
    endpoint: str = PANDASCORE_MATCHES_URL,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> list[object]:
    token = token.strip()
    if not token:
        raise PandaScoreConfigurationError(PANDASCORE_MISSING_TOKEN_MESSAGE)

    url = _url_with_query(endpoint, {"per_page": str(limit)})
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )

    try:
        with urlopen_func(request, timeout=timeout) as response:
            raw_body = response.read()
    except HTTPError as exc:
        raise _http_error(exc) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise PandaScoreRequestError("PandaScore request timed out.") from exc
    except (URLError, OSError) as exc:
        raise PandaScoreRequestError("PandaScore network request failed.") from exc

    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PandaScoreResponseError("PandaScore returned invalid JSON.") from exc

    if not isinstance(decoded, list):
        raise PandaScoreResponseError(
            "PandaScore returned unexpected JSON shape: expected a list."
        )
    return decoded


def map_pandascore_matches(
    payloads: Iterable[object],
    *,
    session_id: str = "pandascore",
    status_filter: MatchStatusFilter = "all",
) -> list[Match]:
    matches: list[Match] = []
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue

        match = map_pandascore_match(payload, session_id=session_id)
        if match is None:
            continue
        if status_filter != "all" and match.status != status_filter:
            continue
        matches.append(match)

    return matches


def map_pandascore_match(
    payload: Mapping[str, object],
    *,
    session_id: str = "pandascore",
) -> Match | None:
    provider_id = _text_from_value(payload.get("id"))
    if provider_id is None:
        return None

    status = map_pandascore_status(_text_from_value(payload.get("status")))
    if status is None:
        return None

    teams = _opponent_names(payload.get("opponents"))
    if len(teams) != 2:
        return None

    team_a, team_b = teams
    if normalize_team_name(team_a) == normalize_team_name(team_b):
        return None

    start_time = _start_time_from_payload(payload)
    if start_time is _INVALID_DATETIME:
        return None

    return Match(
        id=f"pandascore-{provider_id}",
        session_id=session_id,
        tournament_name=_competition_name(payload),
        team_a=team_a,
        team_b=team_b,
        format=_match_format(payload),
        status=status,
        start_time=cast(datetime | None, start_time),
        external_id=provider_id,
    )


def map_pandascore_status(value: str | None) -> MatchStatus | None:
    if value is None:
        return None

    normalized = value.strip().casefold()
    if normalized in ("not_started", "not yet started", "scheduled", "upcoming"):
        return "upcoming"
    if normalized in ("running", "live", "in_progress"):
        return "live"
    if normalized in ("finished", "completed"):
        return "finished"
    if normalized in ("canceled", "cancelled"):
        return "cancelled"
    return None


def _http_error(exc: HTTPError) -> PandaScoreRequestError:
    if exc.code == 401:
        return PandaScoreRequestError(
            "PandaScore request failed: HTTP 401 unauthorized. "
            "Check PANDASCORE_TOKEN."
        )
    if exc.code == 403:
        return PandaScoreRequestError(
            "PandaScore request failed: HTTP 403 forbidden. "
            "Check PandaScore API permissions."
        )
    return PandaScoreRequestError(f"PandaScore request failed: HTTP {exc.code}.")


def _url_with_query(endpoint: str, query: Mapping[str, str]) -> str:
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(query)}"


def _opponent_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    names: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue

        opponent = item.get("opponent")
        name: str | None = None
        if isinstance(opponent, Mapping):
            name = _text_from_value(opponent.get("name"))
        if name is None:
            name = _text_from_value(item.get("name"))

        display_name = _display_text(name)
        if display_name:
            names.append(display_name)

    return names


def _competition_name(payload: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in ("league", "serie", "tournament"):
        value = payload.get(key)
        if not isinstance(value, Mapping):
            continue

        name = _display_text(
            _text_from_value(value.get("full_name"))
            or _text_from_value(value.get("name"))
        )
        if name and name not in parts:
            parts.append(name)

    if parts:
        return " / ".join(parts)
    return "PandaScore Dota 2"


def _match_format(payload: Mapping[str, object]) -> str:
    games_count = _int_from_value(
        payload.get("number_of_games") or payload.get("games_count")
    )
    if games_count is not None and games_count > 0:
        return f"bo{games_count}"

    match_type = _display_text(_text_from_value(payload.get("match_type")))
    if match_type:
        return match_type
    return "unknown"


_INVALID_DATETIME = object()


def _start_time_from_payload(
    payload: Mapping[str, object],
) -> datetime | None | object:
    value = (
        _text_from_value(payload.get("begin_at"))
        or _text_from_value(payload.get("scheduled_at"))
        or _text_from_value(payload.get("start_time"))
    )
    if value is None:
        return None

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
