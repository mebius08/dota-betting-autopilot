from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import socket
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.draft_history.domain import (
    DotaSide,
    DraftWinnerSide,
    HistoricalDotaGame,
    HistoricalDraftAction,
    draft_action_id,
    historical_dota_game_id,
)


OPENDOTA_SOURCE = "opendota"
OPENDOTA_API_BASE_URL = "https://api.opendota.com/api"


class _Response(Protocol):
    def read(self) -> bytes:
        ...

    def __enter__(self) -> _Response:
        ...

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        ...


class _UrlOpen(Protocol):
    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float = 10.0,
    ) -> _Response:
        ...


DEFAULT_URL_OPEN: _UrlOpen = cast(_UrlOpen, urlopen)


class OpenDotaRequestError(RuntimeError):
    pass


class OpenDotaResponseError(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftMappingResult:
    game: HistoricalDotaGame | None
    actions: tuple[HistoricalDraftAction, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DraftCollectionResult:
    games: tuple[tuple[HistoricalDotaGame, tuple[HistoricalDraftAction, ...]], ...]
    fetched_rows: int
    skipped_rows: int
    warnings: tuple[str, ...]


class OpenDotaDraftCollector:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        api_base_url: str = OPENDOTA_API_BASE_URL,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        self.timeout = timeout
        self.api_base_url = api_base_url.rstrip("/")
        self.urlopen_func = urlopen_func

    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        max_pages: int | None,
    ) -> DraftCollectionResult:
        if since is None or until is None:
            raise ValueError("OpenDota draft sync requires --since and --until.")
        _validate_window(since, until, page_size, max_pages)

        pro_rows = fetch_opendota_pro_match_rows(
            since=since,
            until=until,
            page_size=page_size,
            max_pages=max_pages,
            api_base_url=self.api_base_url,
            timeout=self.timeout,
            urlopen_func=self.urlopen_func,
        )
        games: list[tuple[HistoricalDotaGame, tuple[HistoricalDraftAction, ...]]] = []
        warnings: list[str] = []
        skipped = 0
        for row in pro_rows:
            match_id = _int_from_mapping(row, "match_id")
            if match_id is None:
                skipped += 1
                warnings.append("Skipped OpenDota pro row without match_id.")
                continue
            detail = fetch_opendota_match_detail(
                match_id,
                api_base_url=self.api_base_url,
                timeout=self.timeout,
                urlopen_func=self.urlopen_func,
            )
            mapped = map_opendota_match_detail(detail)
            warnings.extend(mapped.warnings)
            if mapped.game is None:
                skipped += 1
                continue
            games.append((mapped.game, mapped.actions))

        return DraftCollectionResult(
            games=tuple(games),
            fetched_rows=len(pro_rows),
            skipped_rows=skipped,
            warnings=tuple(warnings),
        )


def fetch_opendota_pro_match_rows(
    *,
    since: datetime,
    until: datetime,
    page_size: int = 100,
    max_pages: int | None = None,
    api_base_url: str = OPENDOTA_API_BASE_URL,
    timeout: float = 10.0,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> list[Mapping[str, object]]:
    _validate_window(since, until, page_size, max_pages)
    rows: list[Mapping[str, object]] = []
    less_than_match_id: int | None = None
    page = 1
    while max_pages is None or page <= max_pages:
        query: dict[str, str] = {}
        if less_than_match_id is not None:
            query["less_than_match_id"] = str(less_than_match_id)
        url = _url_with_query(f"{api_base_url.rstrip('/')}/proMatches", query)
        page_rows = _fetch_json_list(url, timeout=timeout, urlopen_func=urlopen_func)
        if not page_rows:
            break

        typed_rows = [
            row for row in page_rows if isinstance(row, Mapping)
        ]
        for row in typed_rows:
            start_time = _datetime_from_epoch(row.get("start_time"))
            if start_time is None:
                continue
            if since <= start_time <= until:
                rows.append(row)
        match_ids = [
            match_id
            for row in typed_rows
            if (match_id := _int_from_mapping(row, "match_id")) is not None
        ]
        if not match_ids:
            break
        less_than_match_id = min(match_ids)
        oldest_start = min(
            (
                value
                for row in typed_rows
                if (value := _datetime_from_epoch(row.get("start_time"))) is not None
            ),
            default=None,
        )
        if oldest_start is not None and oldest_start < since:
            break
        if len(typed_rows) < page_size:
            break
        page += 1
    return rows


def fetch_opendota_match_detail(
    match_id: int,
    *,
    api_base_url: str = OPENDOTA_API_BASE_URL,
    timeout: float = 10.0,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> Mapping[str, object]:
    if match_id < 1:
        raise ValueError("match_id must be positive")
    url = f"{api_base_url.rstrip('/')}/matches/{match_id}"
    body = _fetch_json_object(url, timeout=timeout, urlopen_func=urlopen_func)
    return body


def map_opendota_match_detail(
    payload: Mapping[str, object],
) -> DraftMappingResult:
    source_game_id = _text(payload.get("match_id"))
    started_at = _datetime_from_epoch(payload.get("start_time"))
    if source_game_id is None or started_at is None:
        return DraftMappingResult(
            game=None,
            actions=(),
            warnings=("Skipped OpenDota match without match_id/start_time.",),
        )

    duration = _int_from_mapping(payload, "duration")
    ended_at = (
        datetime.fromtimestamp(
            int(started_at.timestamp()) + duration,
            tz=timezone.utc,
        )
        if duration is not None
        else None
    )
    radiant_team_id = _text(payload.get("radiant_team_id"))
    dire_team_id = _text(payload.get("dire_team_id"))
    radiant_name = _display_name(payload.get("radiant_team_name"), "Radiant")
    dire_name = _display_name(payload.get("dire_team_name"), "Dire")
    radiant_win = payload.get("radiant_win")
    winner_side: DraftWinnerSide | None = None
    if isinstance(radiant_win, bool):
        winner_side = "team_a" if radiant_win else "team_b"

    actions, action_warnings = _draft_actions(
        source_game_id=source_game_id,
        game_id=historical_dota_game_id(OPENDOTA_SOURCE, source_game_id),
        payload_actions=payload.get("picks_bans"),
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
    )
    draft_complete = _has_complete_5v5_picks(actions)
    game = HistoricalDotaGame(
        id=historical_dota_game_id(OPENDOTA_SOURCE, source_game_id),
        source=OPENDOTA_SOURCE,
        source_game_id=source_game_id,
        parent_series_source_id=_text(payload.get("series_id")),
        started_at=started_at,
        ended_at=ended_at,
        team_a_name=radiant_name,
        team_b_name=dire_name,
        team_a_source_id=radiant_team_id,
        team_b_source_id=dire_team_id,
        winner_side=winner_side,
        game_number=_int_from_mapping(payload, "game_number"),
        best_of=_series_type_to_best_of(payload.get("series_type")),
        team_a_series_wins_before=None,
        team_b_series_wins_before=None,
        team_a_side="radiant",
        patch=_text(payload.get("patch")),
        draft_complete=draft_complete,
        tournament_name=_text(payload.get("league_name")),
        tournament_source_id=_text(payload.get("leagueid")),
        league_name=_text(payload.get("league_name")),
        league_source_id=_text(payload.get("leagueid")),
    )
    return DraftMappingResult(
        game=game,
        actions=actions,
        warnings=tuple(action_warnings),
    )


def _draft_actions(
    *,
    source_game_id: str,
    game_id: str,
    payload_actions: object,
    radiant_team_id: str | None,
    dire_team_id: str | None,
) -> tuple[tuple[HistoricalDraftAction, ...], list[str]]:
    if not isinstance(payload_actions, list):
        return (), ["OpenDota match has no picks_bans list."]

    actions: list[HistoricalDraftAction] = []
    warnings: list[str] = []
    seen_orders: set[int] = set()
    for fallback_index, item in enumerate(payload_actions, start=1):
        if not isinstance(item, Mapping):
            warnings.append("Skipped malformed picks_bans item.")
            continue
        hero_id = _int_from_mapping(item, "hero_id")
        is_pick = item.get("is_pick")
        if hero_id is None or not isinstance(is_pick, bool):
            warnings.append("Skipped picks_bans item without hero_id/is_pick.")
            continue
        action_order = (
            _int_from_mapping(item, "order")
            or _int_from_mapping(item, "ord")
            or fallback_index
        )
        if action_order in seen_orders:
            warnings.append(f"Skipped duplicate draft order: {action_order}.")
            continue
        seen_orders.add(action_order)
        team_side = _side_from_opendota_team(item.get("team"))
        team_source_id = (
            radiant_team_id
            if team_side == "radiant"
            else dire_team_id
            if team_side == "dire"
            else None
        )
        actions.append(
            HistoricalDraftAction(
                id=draft_action_id(game_id, action_order),
                game_id=game_id,
                source=OPENDOTA_SOURCE,
                source_game_id=source_game_id,
                action_order=action_order,
                action_kind="pick" if is_pick else "ban",
                team_side=team_side,
                team_source_id=team_source_id,
                hero_id=hero_id,
            )
        )
    return tuple(sorted(actions, key=lambda action: action.action_order)), warnings


def _has_complete_5v5_picks(actions: Iterable[HistoricalDraftAction]) -> bool:
    radiant: list[int] = []
    dire: list[int] = []
    for action in actions:
        if action.action_kind != "pick":
            continue
        if action.team_side == "radiant":
            radiant.append(action.hero_id)
        elif action.team_side == "dire":
            dire.append(action.hero_id)
    return (
        len(radiant) == 5
        and len(dire) == 5
        and len(set(radiant)) == 5
        and len(set(dire)) == 5
    )


def _side_from_opendota_team(value: object) -> DotaSide:
    if value in (0, "0", "radiant", "Radiant"):
        return "radiant"
    if value in (1, "1", "dire", "Dire"):
        return "dire"
    return "unknown"


def _series_type_to_best_of(value: object) -> int | None:
    series_type = _int(value)
    if series_type is None or series_type < 1:
        return None
    if series_type % 2 == 1:
        return series_type
    return None


def _validate_window(
    since: datetime,
    until: datetime,
    page_size: int,
    max_pages: int | None,
) -> None:
    if since.tzinfo is None or until.tzinfo is None:
        raise ValueError("OpenDota sync window timestamps must be timezone-aware")
    if since > until:
        raise ValueError("since must be before or equal to until")
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1")


def _fetch_json_list(
    url: str,
    *,
    timeout: float,
    urlopen_func: _UrlOpen,
) -> list[object]:
    value = _fetch_json(url, timeout=timeout, urlopen_func=urlopen_func)
    if not isinstance(value, list):
        raise OpenDotaResponseError("OpenDota returned unexpected JSON shape.")
    return value


def _fetch_json_object(
    url: str,
    *,
    timeout: float,
    urlopen_func: _UrlOpen,
) -> Mapping[str, object]:
    value = _fetch_json(url, timeout=timeout, urlopen_func=urlopen_func)
    if not isinstance(value, Mapping):
        raise OpenDotaResponseError("OpenDota returned unexpected JSON shape.")
    return value


def _fetch_json(
    url: str,
    *,
    timeout: float,
    urlopen_func: _UrlOpen,
) -> object:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen_func(request, timeout=timeout) as response:
            raw_body = response.read()
    except HTTPError as exc:
        raise OpenDotaRequestError(f"OpenDota HTTP {exc.code}.") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise OpenDotaRequestError("OpenDota request timed out.") from exc
    except (URLError, OSError) as exc:
        raise OpenDotaRequestError("OpenDota network request failed.") from exc

    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenDotaResponseError("OpenDota returned invalid JSON.") from exc


def _url_with_query(url: str, query: Mapping[str, str]) -> str:
    if not query:
        return url
    return f"{url}?{urlencode(query)}"


def _datetime_from_epoch(value: object) -> datetime | None:
    integer = _int(value)
    if integer is None:
        return None
    return datetime.fromtimestamp(integer, tz=timezone.utc)


def _display_name(value: object, fallback: str) -> str:
    text = _text(value)
    return text if text is not None and text.strip() else fallback


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_from_mapping(value: Mapping[str, object], key: str) -> int | None:
    return _int(value.get(key))


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None
