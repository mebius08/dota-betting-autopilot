from __future__ import annotations

import bz2
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from typing import Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


OPENDOTA_API_BASE_URL = "https://api.opendota.com/api"
OPENDOTA_API_KEY_ENV = "OPENDOTA_API_KEY"
OPENDOTA_USER_AGENT = "dota-betting-autopilot replay-fetcher/1.0"
DEFAULT_REQUEST_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 30.0
_COPY_CHUNK_SIZE = 1024 * 1024

ReplayFetchStatus = Literal["DOWNLOADED", "UNCHANGED", "SKIPPED", "FAILED"]


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
    pass


class OpenDotaResponseError(ReplayFetchError):
    pass


class OpenDotaRateLimitError(OpenDotaRequestError):
    def __init__(self, retry_after: str | None) -> None:
        self.retry_after = retry_after
        super().__init__(_rate_limit_reason(retry_after))


class ReplayDownloadError(ReplayFetchError):
    pass


@dataclass(frozen=True)
class ReplayFetchResult:
    match_id: int
    status: ReplayFetchStatus
    destination: Path | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReplayFetchSummary:
    results: tuple[ReplayFetchResult, ...]
    details_requested: int

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
        return self.status_count("FAILED")


class ReplayFetcher:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
        api_base_url: str = OPENDOTA_API_BASE_URL,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if request_delay_seconds < 0:
            raise ValueError("request_delay_seconds must not be negative")

        environment_key = os.environ.get(OPENDOTA_API_KEY_ENV)
        self._api_key = _non_empty(api_key if api_key is not None else environment_key)
        self._timeout = timeout
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
    ) -> ReplayFetchSummary:
        if not 1 <= count <= 25:
            raise ValueError("count must be between 1 and 25")
        if max_details < 1:
            raise ValueError("max_details must be at least 1")

        destination_dir = Path(output_dir)
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReplayFetchError("output_directory_unavailable") from exc

        payload = self._fetch_opendota_json("proMatches")
        if not isinstance(payload, list):
            raise OpenDotaResponseError("opendota_invalid_pro_matches_response")

        matches = _recent_match_rows(payload, league_id=league_id)
        results: list[ReplayFetchResult] = []
        details_requested = 0
        downloaded = 0

        for match_id, _row in matches:
            if downloaded >= count or details_requested >= max_details:
                break

            destination = destination_dir / f"{match_id}.dem"
            try:
                if destination.is_file() and destination.stat().st_size > 0:
                    results.append(
                        ReplayFetchResult(
                            match_id=match_id,
                            status="UNCHANGED",
                            destination=destination,
                        )
                    )
                    continue
            except OSError:
                results.append(
                    ReplayFetchResult(
                        match_id=match_id,
                        status="FAILED",
                        destination=destination,
                        reason="destination_check_failed",
                    )
                )
                continue

            details_requested += 1
            try:
                detail = self._fetch_opendota_json(f"matches/{match_id}")
            except OpenDotaRateLimitError as exc:
                results.append(
                    ReplayFetchResult(
                        match_id=match_id,
                        status="FAILED",
                        destination=destination,
                        reason=_rate_limit_reason(exc.retry_after),
                    )
                )
                break
            except ReplayFetchError as exc:
                results.append(
                    ReplayFetchResult(
                        match_id=match_id,
                        status="FAILED",
                        destination=destination,
                        reason=str(exc),
                    )
                )
                continue

            if not isinstance(detail, Mapping):
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
                )
            )
            downloaded += 1

        return ReplayFetchSummary(
            results=tuple(results),
            details_requested=details_requested,
        )

    def _fetch_opendota_json(self, endpoint: str) -> object:
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
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                raw_body = response.read()
        except HTTPError as exc:
            if exc.code == 429:
                raise OpenDotaRateLimitError(
                    _safe_retry_after(exc, api_key=self._api_key)
                ) from exc
            raise OpenDotaRequestError(f"opendota_http_{exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise OpenDotaRequestError("opendota_request_timed_out") from exc
        except (URLError, OSError) as exc:
            raise OpenDotaRequestError("opendota_request_failed") from exc

        try:
            return json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenDotaResponseError("opendota_invalid_json") from exc

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
            decompressed_size = _decompress_bzip2(compressed, decompressed)
            if decompressed_size < 1:
                raise ReplayDownloadError("empty_decompressed_replay")

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
                with self._urlopen(request, timeout=self._timeout) as response:
                    while True:
                        chunk = response.read(_COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
        except HTTPError as exc:
            raise ReplayDownloadError(f"replay_http_{exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ReplayDownloadError("replay_download_timed_out") from exc
        except (URLError, OSError) as exc:
            raise ReplayDownloadError("replay_download_failed") from exc


def fetch_pro_replays(
    *,
    output_dir: str | Path,
    count: int = 10,
    max_details: int = 25,
    league_id: int | None = None,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
    api_base_url: str = OPENDOTA_API_BASE_URL,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    sleep_func: Callable[[float], None] = time.sleep,
) -> ReplayFetchSummary:
    fetcher = ReplayFetcher(
        api_key=api_key,
        timeout=timeout,
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
    )


def format_replay_fetch_result(result: ReplayFetchResult) -> str:
    fields = [result.status, f"match_id={result.match_id}"]
    if result.destination is not None and result.status in ("DOWNLOADED", "UNCHANGED"):
        fields.append(f"path={result.destination.as_posix()}")
    if result.reason is not None:
        fields.append(f"reason={result.reason}")
    return " ".join(fields)


def format_replay_fetch_summary(summary: ReplayFetchSummary) -> str:
    return (
        f"SUMMARY DOWNLOADED={summary.downloaded} "
        f"UNCHANGED={summary.unchanged} "
        f"SKIPPED={summary.skipped} "
        f"FAILED={summary.failed}"
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
        raise ReplayDownloadError("corrupt_bzip2_replay") from exc
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
