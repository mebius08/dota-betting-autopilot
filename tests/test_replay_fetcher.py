from __future__ import annotations

import bz2
from collections.abc import Callable
from email.message import Message
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from app import cli
import app.replay_fetcher as replay_fetcher
from app.replay_fetcher import (
    OPENDOTA_API_KEY_ENV,
    OPENDOTA_USER_AGENT,
    ReplayFetchSummary,
    fetch_pro_replays,
    format_replay_fetch_result,
    format_replay_fetch_summary,
)


def test_successful_download_is_decompressed_and_written_atomically(
    tmp_path: Path,
) -> None:
    replay = b"valid Dota replay bytes"
    opener = _FakeOpen(
        {
            "/api/proMatches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/101.dem.bz2": [bz2.compress(replay)],
        }
    )
    output_dir = tmp_path / "replays"

    summary = _fetch(opener, output_dir=output_dir, count=1, max_details=1)

    assert summary.downloaded == 1
    assert summary.failed == 0
    assert (output_dir / "101.dem").read_bytes() == replay
    assert [path.name for path in output_dir.iterdir()] == ["101.dem"]


def test_missing_replay_url_is_skipped(tmp_path: Path) -> None:
    opener = _FakeOpen(
        {
            "/api/proMatches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes(
                    {
                        "match_id": 101,
                        "cluster": 123,
                        "replay_salt": 456,
                    }
                )
            ],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path)

    assert summary.skipped == 1
    assert summary.results[0].reason == "no_replay_url"
    assert list(tmp_path.iterdir()) == []
    assert _requested_paths(opener) == [
        "/api/proMatches",
        "/api/matches/101",
    ]


def test_league_filtering_happens_before_detail_requests(tmp_path: Path) -> None:
    opener = _FakeOpen(
        {
            "/api/proMatches": [
                _json_bytes(
                    [
                        _pro_match(102, league_id=9, start_time=200),
                        _pro_match(101, league_id=7, start_time=100),
                    ]
                )
            ],
            "/api/matches/101": [_json_bytes({})],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path, league_id=7)

    assert [result.match_id for result in summary.results] == [101]
    assert _requested_paths(opener) == [
        "/api/proMatches",
        "/api/matches/101",
    ]


def test_existing_non_empty_replay_is_unchanged_without_detail_request(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "101.dem"
    destination.write_bytes(b"existing replay")
    opener = _FakeOpen(
        {"/api/proMatches": [_json_bytes([_pro_match(101)])]}
    )

    summary = _fetch(opener, output_dir=tmp_path)

    assert summary.unchanged == 1
    assert summary.details_requested == 0
    assert destination.read_bytes() == b"existing replay"
    assert _requested_paths(opener) == ["/api/proMatches"]


def test_corrupt_bzip2_is_failed_and_all_partial_files_are_removed(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/proMatches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/101.dem.bz2": [b"not a bzip2 stream"],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path)

    assert summary.failed == 1
    assert summary.results[0].reason == "corrupt_bzip2_replay"
    assert list(tmp_path.iterdir()) == []


def test_interrupted_download_removes_compressed_and_decompressed_partials(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/proMatches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/101.dem.bz2": [_InterruptedResponse()],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path)

    assert summary.failed == 1
    assert summary.results[0].reason == "replay_download_failed"
    assert list(tmp_path.iterdir()) == []


def test_match_detail_limit_is_respected_in_newest_first_order(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/proMatches": [
                _json_bytes(
                    [
                        _pro_match(1, start_time=100),
                        _pro_match(2, start_time=300),
                        _pro_match(3, start_time=200),
                    ]
                )
            ],
            "/api/matches/2": [_json_bytes({})],
            "/api/matches/3": [_json_bytes({})],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path, max_details=2)

    assert summary.details_requested == 2
    assert [result.match_id for result in summary.results] == [2, 3]
    assert _requested_paths(opener) == [
        "/api/proMatches",
        "/api/matches/2",
        "/api/matches/3",
    ]


def test_http_429_reports_retry_after_and_stops_without_hammering(
    tmp_path: Path,
) -> None:
    def rate_limited(request: Request) -> _FakeResponse:
        headers = Message()
        headers["Retry-After"] = "30"
        raise HTTPError(
            request.full_url,
            429,
            "rate limited",
            hdrs=headers,
            fp=None,
        )

    opener = _FakeOpen(
        {
            "/api/proMatches": [
                _json_bytes(
                    [
                        _pro_match(2, start_time=200),
                        _pro_match(1, start_time=100),
                    ]
                )
            ],
            "/api/matches/2": [rate_limited],
        }
    )
    sleeps: list[float] = []

    summary = fetch_pro_replays(
        output_dir=tmp_path,
        count=1,
        max_details=2,
        request_delay_seconds=0.25,
        urlopen_func=opener,
        sleep_func=sleeps.append,
    )

    assert summary.failed == 1
    assert summary.results[0].reason == (
        "opendota_rate_limited retry_after=30"
    )
    assert _requested_paths(opener) == [
        "/api/proMatches",
        "/api/matches/2",
    ]
    assert sleeps == [0.25]


def test_api_key_is_passed_to_opendota_without_appearing_in_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    api_key = "secret-token"
    monkeypatch.setenv(OPENDOTA_API_KEY_ENV, api_key)
    opener = _FakeOpen(
        {
            "/api/proMatches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [_json_bytes({})],
        }
    )

    summary = fetch_pro_replays(
        output_dir=tmp_path,
        request_delay_seconds=0,
        urlopen_func=opener,
    )
    for result in summary.results:
        print(format_replay_fetch_result(result))
    print(format_replay_fetch_summary(summary))
    output = capsys.readouterr().out

    assert all(
        parse_qs(urlsplit(request.full_url).query)["api_key"] == [api_key]
        for request in opener.requests
    )
    assert api_key not in output
    assert all(
        request.get_header("User-agent") == OPENDOTA_USER_AGENT
        for request in opener.requests
    )


def test_cli_wires_fetch_pro_replays_defaults_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch(**kwargs: object) -> ReplayFetchSummary:
        captured.update(kwargs)
        return ReplayFetchSummary(results=(), details_requested=0)

    def sleep_func(_seconds: float) -> None:
        pass

    monkeypatch.setattr(replay_fetcher, "fetch_pro_replays", fake_fetch)

    exit_code = cli.main(["fetch-pro-replays"], sleep_func=sleep_func)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert captured == {
        "league_id": None,
        "count": 10,
        "output_dir": Path("local-data/replays"),
        "max_details": 25,
        "sleep_func": sleep_func,
    }
    assert output == "SUMMARY DOWNLOADED=0 UNCHANGED=0 SKIPPED=0 FAILED=0\n"


def test_cli_rejects_download_count_above_25(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["fetch-pro-replays", "--count", "26"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "must be at most 25" in captured.err


def _fetch(
    opener: _FakeOpen,
    *,
    output_dir: Path,
    count: int = 10,
    max_details: int = 25,
    league_id: int | None = None,
) -> ReplayFetchSummary:
    return fetch_pro_replays(
        output_dir=output_dir,
        count=count,
        max_details=max_details,
        league_id=league_id,
        api_key="",
        request_delay_seconds=0,
        urlopen_func=opener,
    )


def _pro_match(
    match_id: int,
    *,
    league_id: int = 7,
    start_time: int = 100,
) -> dict[str, int]:
    return {
        "match_id": match_id,
        "leagueid": league_id,
        "start_time": start_time,
    }


def _json_bytes(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def _requested_paths(opener: _FakeOpen) -> list[str]:
    return [urlsplit(request.full_url).path for request in opener.requests]


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        start = self._offset
        self._offset = min(len(self._body), self._offset + size)
        return self._body[start : self._offset]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False


class _InterruptedResponse:
    def __init__(self) -> None:
        self._read_count = 0

    def read(self, size: int = -1) -> bytes:
        self._read_count += 1
        if self._read_count == 1:
            return b"BZh"
        raise OSError("connection interrupted")

    def __enter__(self) -> _InterruptedResponse:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False


_RouteFactory = Callable[[Request], _FakeResponse]
_RouteValue = bytes | _FakeResponse | _InterruptedResponse | _RouteFactory


class _FakeOpen:
    def __init__(self, routes: dict[str, list[_RouteValue]]) -> None:
        self._routes = routes
        self.requests: list[Request] = []

    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float = 10.0,
    ) -> _FakeResponse | _InterruptedResponse:
        assert data is None
        assert timeout > 0
        self.requests.append(request)
        path = urlsplit(request.full_url).path
        route = self._routes.get(path)
        if not route:
            raise AssertionError(f"Unexpected request: {path}")
        value = route.pop(0)
        if callable(value):
            return value(request)
        if isinstance(value, bytes):
            return _FakeResponse(value)
        return value
