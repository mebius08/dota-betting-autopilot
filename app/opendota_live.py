from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from email.message import Message
import json
import math
import os
from pathlib import Path
import socket
import tempfile
import time
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request

from app import replay_fetcher as _opendota_http


OPENDOTA_LIVE_URL = f"{_opendota_http.OPENDOTA_API_BASE_URL}/live"
OPENDOTA_LIVE_USER_AGENT = "dota-betting-autopilot opendota-live/1.0"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = _opendota_http.MAX_OPENDOTA_ATTEMPTS
RETRYABLE_HTTP_STATUSES = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 521, 522, 523, 524}
)

WriteStatus = Literal["BUILT", "UNCHANGED"]


class OpenDotaLiveError(RuntimeError):
    """Base error for bounded OpenDota live discovery."""


class OpenDotaLiveRequestError(OpenDotaLiveError):
    """The live endpoint could not be fetched successfully."""


class OpenDotaLiveResponseError(OpenDotaLiveError):
    """The live endpoint returned an invalid response."""


class OpenDotaLivePersistenceError(OpenDotaLiveError):
    """The deterministic discovery artifact could not be persisted."""


@dataclass(frozen=True, slots=True)
class OpenDotaLiveGame:
    match_id: int
    server_steam_id: int
    league_id: int
    game_time: int
    radiant_team_name: str | None
    dire_team_name: str | None


@dataclass(frozen=True, slots=True)
class LiveGameSelection:
    matches: tuple[OpenDotaLiveGame, ...]
    skipped_source_record_count: int


def fetch_live_games(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
    urlopen_func: _opendota_http._UrlOpen = _opendota_http.DEFAULT_URL_OPEN,
    sleep_func: Callable[[float], None] = time.sleep,
) -> list[object]:
    """Fetch and decode the public OpenDota ``/api/live`` array."""

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS}")

    request = Request(
        OPENDOTA_LIVE_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": OPENDOTA_LIVE_USER_AGENT,
        },
        method="GET",
    )
    raw_body: bytes | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen_func(request, timeout=timeout) as response:
                status = _response_status(response)
                if status != 200:
                    headers = _response_headers(response)
                    raise HTTPError(
                        OPENDOTA_LIVE_URL,
                        status,
                        "OpenDota live HTTP error",
                        headers,
                        None,
                    )
                raw_body = response.read()
        except HTTPError as exc:
            if exc.code in RETRYABLE_HTTP_STATUSES and attempt < max_attempts:
                sleep_func(
                    _opendota_http._retry_delay_seconds(
                        exc,
                        completed_attempt=attempt,
                    )
                )
                continue
            raise OpenDotaLiveRequestError(
                f"OpenDota live request failed with HTTP {exc.code}."
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            if attempt < max_attempts:
                sleep_func(_opendota_http._exponential_backoff(attempt))
                continue
            raise OpenDotaLiveRequestError(
                f"OpenDota live request timed out after {attempt} attempts."
            ) from exc
        except (URLError, OSError) as exc:
            if attempt < max_attempts:
                sleep_func(_opendota_http._exponential_backoff(attempt))
                continue
            raise OpenDotaLiveRequestError(
                f"OpenDota live connection failed after {attempt} attempts."
            ) from exc
        break

    if not isinstance(raw_body, bytes):
        raise OpenDotaLiveResponseError("OpenDota live response body was not bytes.")

    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenDotaLiveResponseError(
            "OpenDota live response was not valid UTF-8."
        ) from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenDotaLiveResponseError(
            "OpenDota live response was not valid JSON."
        ) from exc
    if not isinstance(payload, list):
        raise OpenDotaLiveResponseError("OpenDota live response must be a JSON array.")
    return list(payload)


def validate_live_game(record: object) -> OpenDotaLiveGame:
    """Validate and normalize one retained OpenDota live-game record."""

    if not isinstance(record, Mapping):
        raise OpenDotaLiveResponseError("A retained live game must be an object.")

    return OpenDotaLiveGame(
        match_id=_required_source_id(
            record,
            "match_id",
            minimum=1,
            allow_decimal_string=True,
        ),
        server_steam_id=_required_source_id(
            record,
            "server_steam_id",
            minimum=1,
            allow_decimal_string=True,
        ),
        league_id=_required_source_id(
            record,
            "league_id",
            minimum=0,
            allow_decimal_string=False,
        ),
        game_time=_required_json_integer(record, "game_time"),
        radiant_team_name=_optional_string(record, "team_name_radiant"),
        dire_team_name=_optional_string(record, "team_name_dire"),
    )


def filter_live_games(
    records: Iterable[object],
    selected_league_ids: Collection[int] | None = None,
) -> LiveGameSelection:
    """Filter by league before fully validating only retained records."""

    selected = _validate_league_filter(selected_league_ids)
    retained: list[OpenDotaLiveGame] = []
    skipped = 0
    for record in records:
        if not isinstance(record, Mapping):
            skipped += 1
            continue
        if selected:
            if "league_id" not in record:
                skipped += 1
                continue
            league_id = _required_source_id(
                record,
                "league_id",
                minimum=0,
                allow_decimal_string=False,
            )
            if league_id not in selected:
                skipped += 1
                continue
        if not all(field in record for field in _ESSENTIAL_SOURCE_FIELDS):
            skipped += 1
            continue
        retained.append(validate_live_game(record))
    return LiveGameSelection(
        matches=tuple(retained),
        skipped_source_record_count=skipped,
    )


def deduplicate_and_sort_live_matches(
    matches: Iterable[OpenDotaLiveGame],
) -> list[OpenDotaLiveGame]:
    """Collapse exact duplicates, reject conflicts, and sort deterministically."""

    by_match_id: dict[int, OpenDotaLiveGame] = {}
    for match in matches:
        existing = by_match_id.get(match.match_id)
        if existing is not None and existing != match:
            raise OpenDotaLiveResponseError(
                f"Conflicting duplicate match_id {match.match_id}."
            )
        by_match_id[match.match_id] = match
    return sorted(
        by_match_id.values(),
        key=lambda match: (match.league_id, match.match_id),
    )


def persist_live_matches(
    output_path: str | Path,
    matches: Iterable[OpenDotaLiveGame],
    *,
    skipped_source_record_count: int = 0,
) -> WriteStatus:
    """Atomically persist the canonical live-match artifact if it changed."""

    if type(skipped_source_record_count) is not int or skipped_source_record_count < 0:
        raise ValueError("skipped_source_record_count must be a non-negative integer")
    ordered_matches = deduplicate_and_sort_live_matches(matches)
    payload = {
        "schema_version": 1,
        "match_count": len(ordered_matches),
        "skipped_source_record_count": skipped_source_record_count,
        "matches": [_match_payload(match) for match in ordered_matches],
    }
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    path = Path(output_path)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() == content:
            return "UNCHANGED"
    except OSError as exc:
        raise OpenDotaLivePersistenceError(
            "Could not inspect the OpenDota live output."
        ) from exc

    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise OpenDotaLivePersistenceError(
            "Could not atomically write the OpenDota live output."
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return "BUILT"


def _response_status(response: object) -> int:
    getcode = getattr(response, "getcode", None)
    status = getcode() if callable(getcode) else getattr(response, "status", None)
    if status is None:
        return 200
    if type(status) is not int:
        raise OpenDotaLiveResponseError(
            "OpenDota live response had an invalid HTTP status."
        )
    return status


def _response_headers(response: object) -> Message:
    response_headers = getattr(response, "headers", None)
    if isinstance(response_headers, Message):
        return response_headers
    headers = Message()
    if isinstance(response_headers, Mapping):
        retry_after = response_headers.get("Retry-After")
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
    return headers


_ESSENTIAL_SOURCE_FIELDS = (
    "match_id",
    "server_steam_id",
    "league_id",
    "game_time",
)


def _required_source_id(
    record: Mapping[object, object],
    field: str,
    *,
    minimum: int,
    allow_decimal_string: bool,
) -> int:
    value = record.get(field)
    if isinstance(value, bool):
        raise OpenDotaLiveResponseError(f"{field} must be an integer.")
    if isinstance(value, int):
        parsed = value
    elif (
        allow_decimal_string
        and isinstance(value, str)
        and value.isascii()
        and value.isdigit()
    ):
        parsed = int(value)
    else:
        raise OpenDotaLiveResponseError(f"{field} must be an integer.")
    if parsed < minimum:
        raise OpenDotaLiveResponseError(f"{field} must be at least {minimum}.")
    return parsed


def _required_json_integer(record: Mapping[object, object], field: str) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpenDotaLiveResponseError(f"{field} must be an integer.")
    return value


def _optional_string(
    record: Mapping[object, object],
    field: str,
) -> str | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpenDotaLiveResponseError(f"{field} must be a string or null.")
    return value


def _validate_league_filter(
    selected_league_ids: Collection[int] | None,
) -> frozenset[int]:
    if selected_league_ids is None:
        return frozenset()
    selected: set[int] = set()
    for league_id in selected_league_ids:
        if type(league_id) is not int or league_id < 0:
            raise ValueError("selected league IDs must be non-negative integers")
        selected.add(league_id)
    return frozenset(selected)


def _match_payload(match: OpenDotaLiveGame) -> dict[str, object]:
    return {
        "match_id": match.match_id,
        "server_steam_id": match.server_steam_id,
        "league_id": match.league_id,
        "game_time": match.game_time,
        "radiant_team_name": match.radiant_team_name,
        "dire_team_name": match.dire_team_name,
    }
