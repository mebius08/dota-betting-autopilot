from __future__ import annotations

import bz2
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import json
import math
import os
from pathlib import Path
import socket
import tempfile
import time
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import zstandard


OPENDOTA_API_BASE_URL = "https://api.opendota.com/api"
OPENDOTA_API_KEY_ENV = "OPENDOTA_API_KEY"
OPENDOTA_USER_AGENT = "dota-betting-autopilot replay-fetcher/1.0"
DEFAULT_REQUEST_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_REPLAY_RESPONSE_TIMEOUT_SECONDS = 7.5
MAX_OPENDOTA_ATTEMPTS = 5
MAX_RETRY_DELAY_SECONDS = 30.0
_COPY_CHUNK_SIZE = 1024 * 1024
_BZIP2_MAGIC = b"BZh"
_ZSTANDARD_MAGIC = b"\x28\xb5\x2f\xfd"
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

ReplayFetchStatus = Literal["DOWNLOADED", "UNCHANGED", "SKIPPED", "FAILED"]
ReplayFailureStage = Literal[
    "league_discovery",
    "match_detail",
    "replay_download",
    "replay_decompression",
]


class _Response(Protocol):
    def read(self, size: int = -1) -> bytes:
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


class ReplayFetchError(RuntimeError):
    pass


class OpenDotaRequestError(ReplayFetchError):
    def __init__(
        self,
        message: str,
        *,
        reason: str,
        attempts: int,
        http_status: int | None = None,
    ) -> None:
        self.reason = reason
        self.attempts = attempts
        self.http_status = http_status
        super().__init__(message)


class OpenDotaResponseError(ReplayFetchError):
    def __init__(
        self,
        message: str,
        *,
        reason: str = "invalid_response",
        attempts: int = 1,
    ) -> None:
        self.reason = reason
        self.attempts = attempts
        self.http_status: int | None = None
        super().__init__(message)


class OpenDotaRateLimitError(OpenDotaRequestError):
    def __init__(self, retry_after: str | None, *, attempts: int) -> None:
        self.retry_after = retry_after
        super().__init__(
            _rate_limit_reason(retry_after),
            reason="http_error",
            attempts=attempts,
            http_status=429,
        )


class ReplayDownloadError(ReplayFetchError):
    def __init__(
        self,
        message: str,
        *,
        stage: ReplayFailureStage = "replay_download",
        reason: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.stage = stage
        self.reason = reason if reason is not None else message
        self.http_status = http_status
        self.attempts = 1
        super().__init__(message)


@dataclass(frozen=True)
class ReplayFetchFailure:
    league_id: int
    stage: ReplayFailureStage
    reason: str
    attempts: int
    match_id: int | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class ReplayFetchResult:
    match_id: int
    status: ReplayFetchStatus
    destination: Path | None = None
    reason: str | None = None
    league_id: int | None = None
    stage: ReplayFailureStage | None = None
    http_status: int | None = None
    attempts: int | None = None


@dataclass(frozen=True)
class ReplayFetchSummary:
    results: tuple[ReplayFetchResult, ...]
    details_requested: int
    league_mode: bool = False
    league_id: int | None = None
    discovered: int = 0
    discovery_failure: ReplayFetchFailure | None = None

    def status_count(self, status: ReplayFetchStatus) -> int:
        return sum(result.status == status for result in self.results)

    @property
    def downloaded(self) -> int:
        return self.status_count("DOWNLOADED")

    @property
    def unchanged(self) -> int:
        return self.status_count("UNCHANGED")

    @property
    def skipped(self) -> int:
        return self.status_count("SKIPPED")

    @property
    def failed(self) -> int:
        return self.status_count("FAILED") + self.discovery_failed

    @property
    def detail_failed(self) -> int:
        return sum(
            result.status == "FAILED" and result.stage == "match_detail"
            for result in self.results
        )

    @property
    def discovery_failed(self) -> int:
        return int(self.discovery_failure is not None)


@dataclass(frozen=True)
class _OpenDotaJsonResponse:
    payload: object
    attempts: int


class ReplayFetcher:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        replay_response_timeout: float = DEFAULT_REPLAY_RESPONSE_TIMEOUT_SECONDS,
        request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
        api_base_url: str = OPENDOTA_API_BASE_URL,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if replay_response_timeout <= 0:
            raise ValueError("replay_response_timeout must be greater than 0")
        if request_delay_seconds < 0:
            raise ValueError("request_delay_seconds must not be negative")

        environment_key = os.environ.get(OPENDOTA_API_KEY_ENV)
        self._api_key = _non_empty(api_key if api_key is not None else environment_key)
        self._timeout = timeout
        self._replay_response_timeout = replay_response_timeout
        self._request_delay_seconds = request_delay_seconds
        self._api_base_url = api_base_url.rstrip("/")
        self._urlopen = urlopen_func
        self._sleep = sleep_func
        self._opendota_request_count = 0

    def fetch(
        self,
        *,
        output_dir: str | Path,
        count: int = 10,
        max_details: int = 25,
        league_id: int | None = None,
        all_league_matches: bool = False,
    ) -> ReplayFetchSummary:
        if not 1 <= count <= 25:
            raise ValueError("count must be between 1 and 25")
        if max_details < 1:
            raise ValueError("max_details must be at least 1")
        if all_league_matches and league_id is None:
            raise ValueError("all_league_matches requires league_id")
        required_league_id = cast(int, league_id)

        destination_dir = Path(output_dir)
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReplayFetchError("output_directory_unavailable") from exc

        endpoint = (
            f"leagues/{league_id}/matches" if all_league_matches else "proMatches"
        )
        max_attempts = MAX_OPENDOTA_ATTEMPTS if all_league_matches else 1
        try:
            discovery_response = self._fetch_opendota_json(
                endpoint,
                max_attempts=max_attempts,
            )
        except (OpenDotaRequestError, OpenDotaResponseError) as exc:
            if not all_league_matches:
                raise
            return ReplayFetchSummary(
                results=(),
                details_requested=0,
                league_mode=True,
                league_id=league_id,
                discovery_failure=_opendota_failure(
                    league_id=required_league_id,
                    stage="league_discovery",
                    exc=exc,
                ),
            )

        payload = discovery_response.payload
        if not isinstance(payload, list):
            if all_league_matches:
                return ReplayFetchSummary(
                    results=(),
                    details_requested=0,
                    league_mode=True,
                    league_id=league_id,
                    discovery_failure=ReplayFetchFailure(
                        league_id=required_league_id,
                        stage="league_discovery",
                        reason="invalid_response",
                        attempts=discovery_response.attempts,
                    ),
                )
            raise OpenDotaResponseError(
                "opendota_invalid_pro_matches_response"
            )

        matches = (
            _league_match_rows(payload)
            if all_league_matches
            else _recent_match_rows(payload, league_id=league_id)
        )
        results: list[ReplayFetchResult] = []
        details_requested = 0
        downloaded = 0

        for match_id, _row in matches:
            if (
                not all_league_matches and downloaded >= count
            ) or details_requested >= max_details:
                break

            destination = destination_dir / f"{match_id}.dem"
            try:
                if destination.is_file() and destination.stat().st_size > 0:
                    results.append(
                        ReplayFetchResult(
                            match_id=match_id,
                            status="UNCHANGED",
                            destination=destination,
                            league_id=league_id if all_league_matches else None,
                        )
                    )
                    continue
            except OSError:
                results.append(
                    _failed_result(
                        match_id=match_id,
                        destination=destination,
                        reason="destination_check_failed",
                        league_id=league_id if all_league_matches else None,
                        stage="replay_download" if all_league_matches else None,
                        attempts=1 if all_league_matches else None,
                    )
                )
                continue

            details_requested += 1
            try:
                detail_response = self._fetch_opendota_json(
                    f"matches/{match_id}",
                    max_attempts=max_attempts,
                )
            except OpenDotaRateLimitError as exc:
                if all_league_matches:
                    results.append(
                        _opendota_failed_result(
                            league_id=required_league_id,
                            match_id=match_id,
                            destination=destination,
                            stage="match_detail",
                            exc=exc,
                        )
                    )
                else:
                    results.append(
                        ReplayFetchResult(
                            match_id=match_id,
                            status="FAILED",
                            destination=destination,
                            reason=_rate_limit_reason(exc.retry_after),
                        )
                    )
                break
            except (OpenDotaRequestError, OpenDotaResponseError) as exc:
                if all_league_matches:
                    results.append(
                        _opendota_failed_result(
                            league_id=required_league_id,
                            match_id=match_id,
                            destination=destination,
                            stage="match_detail",
                            exc=exc,
                        )
                    )
                else:
                    results.append(
                        ReplayFetchResult(
                            match_id=match_id,
                            status="FAILED",
                            destination=destination,
                            reason=str(exc),
                        )
                    )
                continue

            detail = detail_response.payload
            if not isinstance(detail, Mapping):
                if all_league_matches:
                    results.append(
                        _failed_result(
                            match_id=match_id,
                            destination=destination,
                            reason="invalid_response",
                            league_id=league_id,
                            stage="match_detail",
                            attempts=detail_response.attempts,
                        )
                    )
                else:
                    results.append(
                        ReplayFetchResult(
                            match_id=match_id,
                            status="FAILED",
                            destination=destination,
                            reason="opendota_invalid_match_detail_response",
                        )
                    )
                continue

            replay_url = _non_empty(detail.get("replay_url"))
            if replay_url is None:
                results.append(
                    ReplayFetchResult(
                        match_id=match_id,
                        status="SKIPPED",
                        destination=destination,
                        reason="no_replay_url",
                        league_id=league_id if all_league_matches else None,
                    )
                )
                continue
            if not _is_http_url(replay_url):
                results.append(
                    ReplayFetchResult(
                        match_id=match_id,
                        status="SKIPPED",
                        destination=destination,
                        reason="invalid_replay_url",
                        league_id=league_id if all_league_matches else None,
                    )
                )
                continue

            try:
                self._download_replay(
                    replay_url=replay_url,
                    match_id=match_id,
                    destination=destination,
                )
            except ReplayDownloadError as exc:
                if all_league_matches:
                    results.append(
                        _failed_result(
                            match_id=match_id,
                            destination=destination,
                            reason=exc.reason,
                            league_id=league_id,
                            stage=exc.stage,
                            attempts=exc.attempts,
                            http_status=exc.http_status,
                        )
                    )
                else:
                    results.append(
                        ReplayFetchResult(
                            match_id=match_id,
                            status="FAILED",
                            destination=destination,
                            reason=str(exc),
                        )
                    )
                continue

            results.append(
                ReplayFetchResult(
                    match_id=match_id,
                    status="DOWNLOADED",
                    destination=destination,
                    league_id=league_id if all_league_matches else None,
                )
            )
            downloaded += 1

        return ReplayFetchSummary(
            results=tuple(results),
            details_requested=details_requested,
            league_mode=all_league_matches,
            league_id=league_id if all_league_matches else None,
            discovered=len(matches) if all_league_matches else 0,
        )

    def _fetch_opendota_json(
        self,
        endpoint: str,
        *,
        max_attempts: int,
    ) -> _OpenDotaJsonResponse:
        self._delay_opendota_request()
        url = f"{self._api_base_url}/{endpoint.lstrip('/')}"
        if self._api_key is not None:
            url = f"{url}?{urlencode({'api_key': self._api_key})}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": OPENDOTA_USER_AGENT,
            },
            method="GET",
        )

        raw_body: bytes | None = None
        completed_attempt = 0
        for attempt in range(1, max_attempts + 1):
            completed_attempt = attempt
            try:
                with self._urlopen(request, timeout=self._timeout) as response:
                    raw_body = response.read()
            except IncompleteRead as exc:
                if attempt < max_attempts:
                    self._sleep(_exponential_backoff(attempt))
                    continue
                raise OpenDotaRequestError(
                    "opendota_response_incomplete",
                    reason="opendota_response_incomplete",
                    attempts=attempt,
                ) from exc
            except HTTPError as exc:
                if (
                    exc.code in _RETRYABLE_HTTP_STATUSES
                    and attempt < max_attempts
                ):
                    self._sleep(
                        _retry_delay_seconds(exc, completed_attempt=attempt)
                    )
                    continue
                if exc.code == 429:
                    raise OpenDotaRateLimitError(
                        _safe_retry_after(exc, api_key=self._api_key),
                        attempts=attempt,
                    ) from exc
                raise OpenDotaRequestError(
                    f"opendota_http_{exc.code}",
                    reason="http_error",
                    attempts=attempt,
                    http_status=exc.code,
                ) from exc
            except (TimeoutError, socket.timeout) as exc:
                if attempt < max_attempts:
                    self._sleep(_exponential_backoff(attempt))
                    continue
                raise OpenDotaRequestError(
                    "opendota_request_timed_out",
                    reason="request_timeout",
                    attempts=attempt,
                ) from exc
            except (URLError, OSError) as exc:
                if attempt < max_attempts:
                    self._sleep(_exponential_backoff(attempt))
                    continue
                raise OpenDotaRequestError(
                    "opendota_request_failed",
                    reason=_connection_failure_reason(exc),
                    attempts=attempt,
                ) from exc
            break

        if raw_body is None:
            raise OpenDotaRequestError(
                "opendota_request_failed",
                reason="connection_failure",
                attempts=completed_attempt,
            )

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenDotaResponseError(
                "opendota_invalid_json",
                reason="invalid_json",
                attempts=completed_attempt,
            ) from exc
        return _OpenDotaJsonResponse(
            payload=payload,
            attempts=completed_attempt,
        )

    def _delay_opendota_request(self) -> None:
        if self._opendota_request_count > 0 and self._request_delay_seconds > 0:
            self._sleep(self._request_delay_seconds)
        self._opendota_request_count += 1

    def _download_replay(
        self,
        *,
        replay_url: str,
        match_id: int,
        destination: Path,
    ) -> None:
        compressed: Path | None = None
        decompressed: Path | None = None
        try:
            compressed = _temporary_path(
                destination.parent,
                prefix=f".{match_id}.",
                suffix=".dem.bz2.part",
            )
            self._download_compressed(replay_url, compressed)

            decompressed = _temporary_path(
                destination.parent,
                prefix=f".{match_id}.",
                suffix=".dem.part",
            )
            compression = _detect_replay_compression(compressed)
            if compression == "bzip2":
                decompressed_size = _decompress_bzip2(compressed, decompressed)
            else:
                decompressed_size = _decompress_zstandard(
                    compressed,
                    decompressed,
                )
            if decompressed_size < 1:
                raise ReplayDownloadError(
                    "empty_decompressed_replay",
                    stage="replay_decompression",
                )

            try:
                os.replace(decompressed, destination)
            except OSError as exc:
                raise ReplayDownloadError("atomic_replay_write_failed") from exc
        finally:
            _remove_temporary(compressed)
            _remove_temporary(decompressed)

    def _download_compressed(self, replay_url: str, destination: Path) -> None:
        request = Request(
            replay_url,
            headers={
                "Accept": "application/x-bzip2, application/octet-stream",
                "User-Agent": OPENDOTA_USER_AGENT,
            },
            method="GET",
        )
        try:
            with destination.open("wb") as file:
                with self._open_replay_response(request) as response:
                    _set_response_socket_timeout(response, self._timeout)
                    while True:
                        chunk = response.read(_COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
        except (TimeoutError, socket.timeout) as exc:
            raise ReplayDownloadError(
                "replay_download_timed_out",
                reason="request_timeout",
            ) from exc
        except (URLError, OSError) as exc:
            raise ReplayDownloadError("replay_download_failed") from exc

    def _open_replay_response(self, request: Request) -> _Response:
        try:
            return self._urlopen(
                request,
                timeout=self._replay_response_timeout,
            )
        except HTTPError as exc:
            raise ReplayDownloadError(
                f"replay_http_{exc.code}",
                reason="http_error",
                http_status=exc.code,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ReplayDownloadError(
                "replay_first_byte_timed_out",
                reason="replay_first_byte_timed_out",
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ReplayDownloadError(
                    "replay_first_byte_timed_out",
                    reason="replay_first_byte_timed_out",
                ) from exc
            raise ReplayDownloadError("replay_download_failed") from exc
        except OSError as exc:
            raise ReplayDownloadError("replay_download_failed") from exc


def fetch_pro_replays(
    *,
    output_dir: str | Path,
    count: int = 10,
    max_details: int = 25,
    league_id: int | None = None,
    all_league_matches: bool = False,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    replay_response_timeout: float = DEFAULT_REPLAY_RESPONSE_TIMEOUT_SECONDS,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
    api_base_url: str = OPENDOTA_API_BASE_URL,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    sleep_func: Callable[[float], None] = time.sleep,
) -> ReplayFetchSummary:
    fetcher = ReplayFetcher(
        api_key=api_key,
        timeout=timeout,
        replay_response_timeout=replay_response_timeout,
        request_delay_seconds=request_delay_seconds,
        api_base_url=api_base_url,
        urlopen_func=urlopen_func,
        sleep_func=sleep_func,
    )
    return fetcher.fetch(
        output_dir=output_dir,
        count=count,
        max_details=max_details,
        league_id=league_id,
        all_league_matches=all_league_matches,
    )


def format_replay_fetch_result(result: ReplayFetchResult) -> str:
    fields: list[str] = [result.status]
    if result.status == "FAILED" and result.league_id is not None:
        fields.append(f"league_id={result.league_id}")
    fields.append(f"match_id={result.match_id}")
    if result.stage is not None:
        fields.append(f"stage={result.stage}")
    if result.reason is not None:
        fields.append(f"reason={result.reason}")
    if result.http_status is not None:
        fields.append(f"status={result.http_status}")
    if result.attempts is not None:
        fields.append(f"attempts={result.attempts}")
    if result.destination is not None and result.status in ("DOWNLOADED", "UNCHANGED"):
        fields.append(f"path={result.destination.as_posix()}")
    return " ".join(fields)


def format_replay_fetch_failure(failure: ReplayFetchFailure) -> str:
    fields = ["FAILED", f"league_id={failure.league_id}"]
    if failure.match_id is not None:
        fields.append(f"match_id={failure.match_id}")
    fields.extend(
        (
            f"stage={failure.stage}",
            f"reason={failure.reason}",
        )
    )
    if failure.http_status is not None:
        fields.append(f"status={failure.http_status}")
    fields.append(f"attempts={failure.attempts}")
    return " ".join(fields)


def format_replay_fetch_summary(summary: ReplayFetchSummary) -> str:
    if summary.league_mode:
        return (
            f"SUMMARY DISCOVERED={summary.discovered} "
            f"DOWNLOADED={summary.downloaded} "
            f"UNCHANGED={summary.unchanged} "
            f"SKIPPED={summary.skipped} "
            f"DETAIL_FAILED={summary.detail_failed} "
            f"DISCOVERY_FAILED={summary.discovery_failed}"
        )
    return (
        f"SUMMARY DOWNLOADED={summary.downloaded} "
        f"UNCHANGED={summary.unchanged} "
        f"SKIPPED={summary.skipped} "
        f"FAILED={summary.failed}"
    )


def _opendota_failure(
    *,
    league_id: int,
    stage: ReplayFailureStage,
    exc: OpenDotaRequestError | OpenDotaResponseError,
    match_id: int | None = None,
) -> ReplayFetchFailure:
    return ReplayFetchFailure(
        league_id=league_id,
        match_id=match_id,
        stage=stage,
        reason=exc.reason,
        http_status=exc.http_status,
        attempts=exc.attempts,
    )


def _opendota_failed_result(
    *,
    league_id: int,
    match_id: int,
    destination: Path,
    stage: ReplayFailureStage,
    exc: OpenDotaRequestError | OpenDotaResponseError,
) -> ReplayFetchResult:
    failure = _opendota_failure(
        league_id=league_id,
        match_id=match_id,
        stage=stage,
        exc=exc,
    )
    return _failed_result(
        match_id=match_id,
        destination=destination,
        reason=failure.reason,
        league_id=league_id,
        stage=stage,
        attempts=failure.attempts,
        http_status=failure.http_status,
    )


def _failed_result(
    *,
    match_id: int,
    destination: Path,
    reason: str,
    league_id: int | None = None,
    stage: ReplayFailureStage | None = None,
    attempts: int | None = None,
    http_status: int | None = None,
) -> ReplayFetchResult:
    return ReplayFetchResult(
        match_id=match_id,
        status="FAILED",
        destination=destination,
        reason=reason,
        league_id=league_id,
        stage=stage,
        attempts=attempts,
        http_status=http_status,
    )


def _recent_match_rows(
    payload: Sequence[object],
    *,
    league_id: int | None,
) -> list[tuple[int, Mapping[str, object]]]:
    rows: list[tuple[int, Mapping[str, object]]] = []
    seen_match_ids: set[int] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        match_id = _positive_integer(item.get("match_id"))
        if match_id is None or match_id in seen_match_ids:
            continue
        if league_id is not None and _integer(item.get("leagueid")) != league_id:
            continue
        seen_match_ids.add(match_id)
        rows.append((match_id, item))
    rows.sort(
        key=lambda match: (
            _integer(match[1].get("start_time")) or -1,
            match[0],
        ),
        reverse=True,
    )
    return rows


def _league_match_rows(
    payload: Sequence[object],
) -> list[tuple[int, Mapping[str, object]]]:
    rows: list[tuple[int, Mapping[str, object]]] = []
    seen_match_ids: set[int] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        match_id = _positive_integer(item.get("match_id"))
        if match_id is None or match_id in seen_match_ids:
            continue
        seen_match_ids.add(match_id)
        rows.append((match_id, item))
    rows.sort(key=lambda match: match[0], reverse=True)
    return rows


def _temporary_path(directory: Path, *, prefix: str, suffix: str) -> Path:
    try:
        descriptor, name = tempfile.mkstemp(
            dir=directory,
            prefix=prefix,
            suffix=suffix,
        )
        os.close(descriptor)
    except OSError as exc:
        raise ReplayDownloadError("temporary_replay_file_failed") from exc
    return Path(name)


def _detect_replay_compression(source: Path) -> Literal["bzip2", "zstandard"]:
    try:
        with source.open("rb") as compressed_file:
            magic = compressed_file.read(len(_ZSTANDARD_MAGIC))
    except OSError as exc:
        raise ReplayDownloadError(
            "replay_decompression_failed",
            stage="replay_decompression",
        ) from exc

    if magic.startswith(_BZIP2_MAGIC):
        return "bzip2"
    if magic.startswith(_ZSTANDARD_MAGIC):
        return "zstandard"
    raise ReplayDownloadError(
        "unsupported_replay_compression",
        stage="replay_decompression",
    )


def _decompress_bzip2(source: Path, destination: Path) -> int:
    decompressed_size = 0
    try:
        with bz2.open(source, "rb") as compressed_file:
            with destination.open("wb") as decompressed_file:
                while True:
                    chunk = compressed_file.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    decompressed_file.write(chunk)
                    decompressed_size += len(chunk)
                decompressed_file.flush()
                os.fsync(decompressed_file.fileno())
    except (EOFError, OSError) as exc:
        raise ReplayDownloadError(
            "corrupt_bzip2_replay",
            stage="replay_decompression",
        ) from exc
    return decompressed_size


def _decompress_zstandard(source: Path, destination: Path) -> int:
    decompressed_size = 0
    try:
        decompressor = zstandard.ZstdDecompressor().decompressobj()
        with source.open("rb") as compressed_file:
            with destination.open("wb") as decompressed_file:
                while True:
                    compressed_chunk = compressed_file.read(_COPY_CHUNK_SIZE)
                    if not compressed_chunk:
                        break
                    chunk = decompressor.decompress(compressed_chunk)
                    if chunk:
                        decompressed_file.write(chunk)
                        decompressed_size += len(chunk)
                chunk = decompressor.flush()
                if chunk:
                    decompressed_file.write(chunk)
                    decompressed_size += len(chunk)
                if not decompressor.eof:
                    raise ReplayDownloadError(
                        "corrupt_zstd_replay",
                        stage="replay_decompression",
                    )
                decompressed_file.flush()
                os.fsync(decompressed_file.fileno())
    except (zstandard.ZstdError, EOFError, OSError) as exc:
        raise ReplayDownloadError(
            "corrupt_zstd_replay",
            stage="replay_decompression",
        ) from exc
    return decompressed_size


def _remove_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _safe_retry_after(exc: HTTPError, *, api_key: str | None) -> str | None:
    headers = exc.headers
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if api_key is not None and api_key in text:
        return "server_provided"
    if text.isdecimal():
        return text
    return "server_provided"


def _retry_delay_seconds(exc: HTTPError, *, completed_attempt: int) -> float:
    retry_after = _parse_retry_after_seconds(exc)
    if retry_after is not None:
        return retry_after
    return _exponential_backoff(completed_attempt)


def _parse_retry_after_seconds(exc: HTTPError) -> float | None:
    headers = exc.headers
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        seconds = float(text)
    except ValueError:
        seconds = math.nan
    if math.isfinite(seconds) and seconds >= 0:
        return min(seconds, MAX_RETRY_DELAY_SECONDS)

    try:
        retry_at = parsedate_to_datetime(text)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = retry_at.timestamp() - time.time()
    except (OverflowError, TypeError, ValueError, OSError):
        return None
    return min(max(delay, 0.0), MAX_RETRY_DELAY_SECONDS)


def _exponential_backoff(completed_attempt: int) -> float:
    return min(
        float(2 ** (completed_attempt - 1)),
        MAX_RETRY_DELAY_SECONDS,
    )


def _set_response_socket_timeout(response: _Response, timeout: float) -> None:
    response_file = getattr(response, "fp", None)
    raw_socket_file = getattr(response_file, "raw", None)
    response_socket = getattr(raw_socket_file, "_sock", None)
    settimeout = getattr(response_socket, "settimeout", None)
    if callable(settimeout):
        settimeout(timeout)


def _connection_failure_reason(exc: URLError | OSError) -> str:
    if isinstance(exc, URLError) and isinstance(
        exc.reason,
        (TimeoutError, socket.timeout),
    ):
        return "request_timeout"
    return "connection_failure"


def _rate_limit_reason(retry_after: str | None) -> str:
    if retry_after is None:
        return "opendota_rate_limited"
    return f"opendota_rate_limited retry_after={retry_after}"


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _non_empty(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_integer(value: object) -> int | None:
    parsed = _integer(value)
    if parsed is None or parsed < 1:
        return None
    return parsed


def _integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None
