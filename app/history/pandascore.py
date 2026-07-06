from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request

from app.collectors.match_collector import normalize_team_name
from app.collectors.pandascore_match_collector import (
    DEFAULT_URL_OPEN,
    PANDASCORE_MISSING_TOKEN_MESSAGE,
    PANDASCORE_TOKEN_ENV,
    PandaScoreConfigurationError,
    PandaScoreRequestError,
    PandaScoreResponseError,
    _UrlOpen,
    _display_text,
    _http_error,
    _int_from_value,
    _text_from_value,
    _url_with_query,
)
from app.history.domain import HistoricalMatch, WinnerSide
from app.tournaments import (
    TournamentStage,
    identify_tournament,
    parse_ewc_2026_stage,
    parse_tournament_stage,
)


PANDASCORE_DOTA_PAST_MATCHES_URL = (
    "https://api.pandascore.co/dota2/matches/past"
)
PANDASCORE_HISTORY_SOURCE = "pandascore"
PANDASCORE_SORT = "begin_at"


@dataclass(frozen=True)
class HistoricalMappingResult:
    matches: list[HistoricalMatch]
    skipped_rows: int
    warnings: list[str]


@dataclass(frozen=True)
class HistoricalCollectionResult:
    matches: list[HistoricalMatch]
    fetched_rows: int
    skipped_rows: int
    warnings: list[str]


@dataclass(frozen=True)
class _ProviderTeam:
    name: str
    source_id: str | None


class PandaScoreHistoricalMatchCollector:
    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 10.0,
        endpoint: str = PANDASCORE_DOTA_PAST_MATCHES_URL,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")

        self.token = (
            token if token is not None else os.environ.get(PANDASCORE_TOKEN_ENV)
        )
        self.timeout = timeout
        self.endpoint = endpoint
        self.urlopen_func = urlopen_func

    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int = 50,
        max_pages: int = 10,
    ) -> HistoricalCollectionResult:
        rows = fetch_pandascore_past_match_rows(
            token=self._required_token(),
            timeout=self.timeout,
            since=since,
            until=until,
            page_size=page_size,
            max_pages=max_pages,
            endpoint=self.endpoint,
            urlopen_func=self.urlopen_func,
        )
        mapped = map_pandascore_historical_matches(rows)
        deduped_matches: list[HistoricalMatch] = []
        seen_ids: set[tuple[str, str]] = set()
        duplicate_rows = 0
        warnings = list(mapped.warnings)

        for match in mapped.matches:
            key = (match.source, match.source_match_id)
            if key in seen_ids:
                duplicate_rows += 1
                warnings.append(
                    f"Skipped duplicate provider match id: {match.source_match_id}"
                )
                continue
            seen_ids.add(key)
            deduped_matches.append(match)

        return HistoricalCollectionResult(
            matches=deduped_matches,
            fetched_rows=len(rows),
            skipped_rows=mapped.skipped_rows + duplicate_rows,
            warnings=warnings,
        )

    def _required_token(self) -> str:
        token = (self.token or "").strip()
        if not token:
            raise PandaScoreConfigurationError(PANDASCORE_MISSING_TOKEN_MESSAGE)
        return token


def fetch_pandascore_past_match_rows(
    *,
    token: str,
    timeout: float = 10.0,
    since: datetime | None = None,
    until: datetime | None = None,
    page_size: int = 50,
    max_pages: int = 10,
    endpoint: str = PANDASCORE_DOTA_PAST_MATCHES_URL,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> list[object]:
    _validate_fetch_options(
        token=token,
        timeout=timeout,
        since=since,
        until=until,
        page_size=page_size,
        max_pages=max_pages,
    )

    rows: list[object] = []
    for page_number in range(1, max_pages + 1):
        page_rows = fetch_pandascore_past_match_page(
            token=token,
            timeout=timeout,
            since=since,
            until=until,
            page_size=page_size,
            page_number=page_number,
            endpoint=endpoint,
            urlopen_func=urlopen_func,
        )
        rows.extend(page_rows)
        if not page_rows or len(page_rows) < page_size:
            break
    return rows


def fetch_pandascore_past_match_page(
    *,
    token: str,
    timeout: float = 10.0,
    since: datetime | None = None,
    until: datetime | None = None,
    page_size: int = 50,
    page_number: int = 1,
    endpoint: str = PANDASCORE_DOTA_PAST_MATCHES_URL,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> list[object]:
    _validate_fetch_options(
        token=token,
        timeout=timeout,
        since=since,
        until=until,
        page_size=page_size,
        max_pages=1,
    )
    if page_number < 1:
        raise ValueError("page_number must be at least 1")

    url = _url_with_query(
        endpoint,
        _history_query_params(
            since=since,
            until=until,
            page_size=page_size,
            page_number=page_number,
        ),
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}",
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

    return _decode_provider_list(raw_body)


def map_pandascore_historical_matches(
    payloads: Iterable[object],
) -> HistoricalMappingResult:
    matches: list[HistoricalMatch] = []
    skipped_rows = 0
    warnings: list[str] = []

    for index, payload in enumerate(payloads, start=1):
        if not isinstance(payload, Mapping):
            skipped_rows += 1
            warnings.append(f"Skipped malformed provider row at index {index}.")
            continue

        match = map_pandascore_historical_match(payload)
        if match is None:
            skipped_rows += 1
            provider_id = _text_from_value(payload.get("id")) or f"index {index}"
            warnings.append(f"Skipped unusable provider row: {provider_id}.")
            continue
        matches.append(match)

    return HistoricalMappingResult(
        matches=matches,
        skipped_rows=skipped_rows,
        warnings=warnings,
    )


def map_pandascore_historical_match(
    payload: Mapping[str, object],
) -> HistoricalMatch | None:
    source_match_id = _text_from_value(payload.get("id"))
    if source_match_id is None:
        return None

    started_at = _datetime_from_provider(
        payload.get("begin_at")
        or payload.get("scheduled_at")
        or payload.get("start_time")
    )
    if started_at is None:
        return None

    teams = _opponents(payload.get("opponents"))
    if len(teams) != 2:
        return None

    team_a, team_b = teams
    if normalize_team_name(team_a.name) == normalize_team_name(team_b.name):
        return None

    status = _normalize_status(_text_from_value(payload.get("status")))
    winner_source_id = _winner_source_id(payload)
    winner_name = _winner_name(payload)
    winner_side = _winner_side(
        team_a=team_a,
        team_b=team_b,
        winner_source_id=winner_source_id,
        winner_name=winner_name,
    )
    raw_stage_label = _stage_label_from_payload(payload)
    stage = _stage_from_payload(payload, raw_stage_label)

    return HistoricalMatch(
        id=f"{PANDASCORE_HISTORY_SOURCE}-{source_match_id}",
        source=PANDASCORE_HISTORY_SOURCE,
        source_match_id=source_match_id,
        started_at=started_at,
        ended_at=_datetime_from_provider(
            payload.get("end_at")
            or payload.get("finished_at")
            or payload.get("completed_at")
        ),
        team_a_name=team_a.name,
        team_b_name=team_b.name,
        team_a_source_id=team_a.source_id,
        team_b_source_id=team_b.source_id,
        winner_name=winner_name,
        winner_source_id=winner_source_id,
        winner_side=winner_side,
        tournament_name=_object_name(payload.get("tournament"))
        or _competition_name(payload),
        tournament_source_id=_object_id(payload.get("tournament")),
        league_name=_object_name(payload.get("league")),
        league_source_id=_object_id(payload.get("league")),
        series_name=_object_name(payload.get("serie")),
        series_source_id=_object_id(payload.get("serie")),
        raw_stage_label=raw_stage_label,
        competitive_stage=stage.competitive_stage,
        normalized_round=stage.round,
        best_of=_int_from_value(
            payload.get("number_of_games") or payload.get("games_count")
        ),
        status=status,
    )


def _validate_fetch_options(
    *,
    token: str,
    timeout: float,
    since: datetime | None,
    until: datetime | None,
    page_size: int,
    max_pages: int,
) -> None:
    if not token.strip():
        raise PandaScoreConfigurationError(PANDASCORE_MISSING_TOKEN_MESSAGE)
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if since is not None and until is not None and since > until:
        raise ValueError("since must be before or equal to until")


def _history_query_params(
    *,
    since: datetime | None,
    until: datetime | None,
    page_size: int,
    page_number: int,
) -> dict[str, str]:
    params = {
        "page[number]": str(page_number),
        "page[size]": str(page_size),
        "sort": PANDASCORE_SORT,
    }
    if since is not None or until is not None:
        lower = _format_provider_datetime(since) if since is not None else ""
        upper = _format_provider_datetime(until) if until is not None else ""
        params["range[begin_at]"] = f"{lower},{upper}"
    return params


def _decode_provider_list(raw_body: bytes) -> list[object]:
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PandaScoreResponseError("PandaScore returned invalid JSON.") from exc

    if not isinstance(decoded, list):
        raise PandaScoreResponseError(
            "PandaScore returned unexpected JSON shape: expected a list."
        )
    return decoded


def _opponents(value: object) -> list[_ProviderTeam]:
    if not isinstance(value, list):
        return []

    teams: list[_ProviderTeam] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue

        opponent = item.get("opponent")
        if isinstance(opponent, Mapping):
            name = _display_text(_text_from_value(opponent.get("name")))
            source_id = _object_id(opponent)
        else:
            name = _display_text(_text_from_value(item.get("name")))
            source_id = _object_id(item)

        if name:
            teams.append(_ProviderTeam(name=name, source_id=source_id))
    return teams


def _winner_source_id(payload: Mapping[str, object]) -> str | None:
    winner = payload.get("winner")
    if isinstance(winner, Mapping):
        return _object_id(winner) or _text_from_value(payload.get("winner_id"))
    return _text_from_value(payload.get("winner_id"))


def _winner_name(payload: Mapping[str, object]) -> str | None:
    winner = payload.get("winner")
    if isinstance(winner, Mapping):
        return _display_text(_text_from_value(winner.get("name"))) or None
    return _display_text(_text_from_value(payload.get("winner_name"))) or None


def _winner_side(
    *,
    team_a: _ProviderTeam,
    team_b: _ProviderTeam,
    winner_source_id: str | None,
    winner_name: str | None,
) -> WinnerSide | None:
    if winner_source_id is not None:
        if team_a.source_id is not None and winner_source_id == team_a.source_id:
            return "team_a"
        if team_b.source_id is not None and winner_source_id == team_b.source_id:
            return "team_b"
        return None

    if winner_name is None:
        return None

    normalized_winner = normalize_team_name(winner_name)
    matches: list[WinnerSide] = []
    if normalized_winner == normalize_team_name(team_a.name):
        matches.append("team_a")
    if normalized_winner == normalize_team_name(team_b.name):
        matches.append("team_b")
    if len(matches) == 1:
        return matches[0]
    return None


def _stage_from_payload(
    payload: Mapping[str, object],
    raw_stage_label: str | None,
) -> TournamentStage:
    tournament_name = _competition_name(payload)
    if identify_tournament(tournament_name) is not None:
        return parse_ewc_2026_stage(raw_stage_label)
    return parse_tournament_stage(raw_stage_label)


def _stage_label_from_payload(payload: Mapping[str, object]) -> str | None:
    for key in ("stage", "stage_name", "round", "bracket", "name"):
        text = _display_text(_text_from_value(payload.get(key)))
        if text:
            return text

    tournament = payload.get("tournament")
    if isinstance(tournament, Mapping):
        tournament_text = _object_name(tournament)
        if tournament_text:
            return tournament_text

    return None


def _competition_name(payload: Mapping[str, object]) -> str | None:
    parts: list[str] = []
    for key in ("league", "serie", "tournament"):
        name = _object_name(payload.get(key))
        if name and name not in parts:
            parts.append(name)
    if not parts:
        return None
    return " / ".join(parts)


def _object_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return (
        _display_text(
            _text_from_value(value.get("full_name"))
            or _text_from_value(value.get("name"))
        )
        or None
    )


def _object_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _text_from_value(value.get("id"))


def _normalize_status(value: str | None) -> str:
    if value is None:
        return "unknown"

    normalized = value.strip().casefold()
    if normalized in ("finished", "completed"):
        return "finished"
    if normalized in ("canceled", "cancelled"):
        return "cancelled"
    if normalized in ("not_started", "not yet started", "scheduled", "upcoming"):
        return "upcoming"
    if normalized in ("running", "live", "in_progress"):
        return "live"
    return normalized or "unknown"


def _datetime_from_provider(value: object) -> datetime | None:
    text = _text_from_value(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_provider_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat().replace("+00:00", "Z")
