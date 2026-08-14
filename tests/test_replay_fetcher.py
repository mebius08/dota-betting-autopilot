from __future__ import annotations

import bz2
from collections.abc import Callable
from email.message import Message
from http.client import IncompleteRead
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest
import zstandard

from app import cli
import app.replay_fetcher as replay_fetcher
from app.replay_fetcher import (
    DEFAULT_REPLAY_RESPONSE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    OPENDOTA_API_KEY_ENV,
    OPENDOTA_USER_AGENT,
    ReplayFetchFailure,
    ReplayFetchSummary,
    fetch_pro_replays,
    format_replay_fetch_failure,
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


def test_zstandard_download_with_bzip2_url_suffix_is_decompressed(
    tmp_path: Path,
) -> None:
    replay = b"valid Zstandard Dota replay bytes"
    opener = _FakeOpen(
        {
            "/api/proMatches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/101.dem.bz2": [zstandard.ZstdCompressor().compress(replay)],
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
            "/101.dem.bz2": [b"BZh-not a valid bzip2 stream"],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path)

    assert summary.failed == 1
    assert summary.results[0].reason == "corrupt_bzip2_replay"
    assert list(tmp_path.iterdir()) == []


def test_truncated_zstandard_is_failed_and_all_partial_files_are_removed(
    tmp_path: Path,
) -> None:
    compressed = zstandard.ZstdCompressor().compress(b"replay data" * 100)
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/101.dem.bz2": [compressed[:-1]],
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
    )

    assert summary.failed == 1
    assert summary.results[0].reason == "corrupt_zstd_replay"
    assert summary.results[0].stage == "replay_decompression"
    assert list(tmp_path.iterdir()) == []


def test_unknown_compression_is_failed_and_all_partial_files_are_removed(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/101.dem.bz2": [b"not a recognized replay compression"],
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
    )

    assert summary.failed == 1
    assert summary.results[0].reason == "unsupported_replay_compression"
    assert summary.results[0].stage == "replay_decompression"
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


def test_replay_first_byte_timeout_continues_to_later_success_and_count(
    tmp_path: Path,
) -> None:
    def first_byte_timeout(_request: Request) -> _FakeResponse:
        raise TimeoutError("response headers never arrived")

    opener = _FakeOpen(
        {
            "/api/proMatches": [
                _json_bytes(
                    [
                        _pro_match(103, start_time=300),
                        _pro_match(102, start_time=200),
                        _pro_match(101, start_time=100),
                    ]
                )
            ],
            "/api/matches/103": [
                _json_bytes({"replay_url": "https://replays.test/103.dem.bz2"})
            ],
            "/103.dem.bz2": [first_byte_timeout],
            "/api/matches/102": [
                _json_bytes({"replay_url": "https://replays.test/102.dem.bz2"})
            ],
            "/102.dem.bz2": [bz2.compress(b"later replay")],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path, count=1, max_details=3)

    assert [result.match_id for result in summary.results] == [103, 102]
    assert [result.status for result in summary.results] == ["FAILED", "DOWNLOADED"]
    assert summary.results[0].reason == "replay_first_byte_timed_out"
    assert summary.details_requested == 2
    assert summary.downloaded == 1
    assert (tmp_path / "102.dem").read_bytes() == b"later replay"
    assert [path.name for path in tmp_path.iterdir()] == ["102.dem"]
    assert _requested_paths(opener) == [
        "/api/proMatches",
        "/api/matches/103",
        "/103.dem.bz2",
        "/api/matches/102",
        "/102.dem.bz2",
    ]


def test_replay_first_byte_timeouts_respect_max_details(tmp_path: Path) -> None:
    def first_byte_timeout(_request: Request) -> _FakeResponse:
        raise URLError(TimeoutError("response headers never arrived"))

    opener = _FakeOpen(
        {
            "/api/proMatches": [
                _json_bytes(
                    [
                        _pro_match(3, start_time=300),
                        _pro_match(2, start_time=200),
                        _pro_match(1, start_time=100),
                    ]
                )
            ],
            "/api/matches/3": [
                _json_bytes({"replay_url": "https://replays.test/3.dem.bz2"})
            ],
            "/3.dem.bz2": [first_byte_timeout],
            "/api/matches/2": [
                _json_bytes({"replay_url": "https://replays.test/2.dem.bz2"})
            ],
            "/2.dem.bz2": [first_byte_timeout],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path, count=1, max_details=2)

    assert summary.details_requested == 2
    assert summary.downloaded == 0
    assert [result.match_id for result in summary.results] == [3, 2]
    assert all(
        result.reason == "replay_first_byte_timed_out"
        for result in summary.results
    )
    assert "/api/matches/1" not in _requested_paths(opener)
    assert list(tmp_path.iterdir()) == []


def test_replay_response_uses_short_start_timeout_and_long_stream_timeout(
    tmp_path: Path,
) -> None:
    replay = bytes(range(256)) * 8192
    response = _ProgressingResponse(bz2.compress(replay), chunk_size=17)
    opener = _FakeOpen(
        {
            "/api/proMatches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/101.dem.bz2": [response],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path, count=1)

    assert summary.downloaded == 1
    assert (tmp_path / "101.dem").read_bytes() == replay
    assert response.read_count > 1
    assert response.socket_timeouts == [DEFAULT_TIMEOUT_SECONDS]
    assert opener.timeouts == [
        DEFAULT_TIMEOUT_SECONDS,
        DEFAULT_TIMEOUT_SECONDS,
        DEFAULT_REPLAY_RESPONSE_TIMEOUT_SECONDS,
    ]


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


def test_all_league_matches_discovers_every_match_and_ignores_count(
    tmp_path: Path,
) -> None:
    replay = bz2.compress(b"league replay")
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _json_bytes([_pro_match(101), _pro_match(102), _pro_match(103)])
            ],
            "/api/matches/103": [
                _json_bytes({"replay_url": "https://replays.test/103.dem.bz2"})
            ],
            "/api/matches/102": [
                _json_bytes({"replay_url": "https://replays.test/102.dem.bz2"})
            ],
            "/api/matches/101": [
                _json_bytes({"replay_url": "https://replays.test/101.dem.bz2"})
            ],
            "/103.dem.bz2": [replay],
            "/102.dem.bz2": [replay],
            "/101.dem.bz2": [replay],
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        count=1,
    )

    assert summary.downloaded == 3
    assert [result.match_id for result in summary.results] == [103, 102, 101]
    assert _requested_paths(opener) == [
        "/api/leagues/7/matches",
        "/api/matches/103",
        "/103.dem.bz2",
        "/api/matches/102",
        "/102.dem.bz2",
        "/api/matches/101",
        "/101.dem.bz2",
    ]


def test_all_league_matches_deduplicates_match_ids(tmp_path: Path) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _json_bytes(
                    [
                        _pro_match(101),
                        _pro_match(101, start_time=200),
                        _pro_match(102),
                    ]
                )
            ],
            "/api/matches/102": [_json_bytes({})],
            "/api/matches/101": [_json_bytes({})],
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
    )

    assert [result.match_id for result in summary.results] == [102, 101]
    assert summary.details_requested == 2


def test_all_league_matches_orders_by_numeric_match_id(tmp_path: Path) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _json_bytes(
                    [
                        _pro_match(10, start_time=999),
                        _pro_match(12, start_time=1),
                        _pro_match(11, start_time=500),
                    ]
                )
            ],
            "/api/matches/12": [_json_bytes({})],
            "/api/matches/11": [_json_bytes({})],
            "/api/matches/10": [_json_bytes({})],
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
    )

    assert [result.match_id for result in summary.results] == [12, 11, 10]


def test_all_league_matches_continues_after_existing_and_failed_replays(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "303.dem"
    existing.write_bytes(b"existing replay")
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _json_bytes([_pro_match(301), _pro_match(303), _pro_match(302)])
            ],
            "/api/matches/302": [
                _json_bytes({"replay_url": "https://replays.test/302.dem.bz2"})
            ],
            "/302.dem.bz2": [b"BZh-corrupt replay"],
            "/api/matches/301": [
                _json_bytes({"replay_url": "https://replays.test/301.dem.bz2"})
            ],
            "/301.dem.bz2": [bz2.compress(b"downloaded replay")],
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
    )

    assert [result.status for result in summary.results] == [
        "UNCHANGED",
        "FAILED",
        "DOWNLOADED",
    ]
    assert summary.results[1].reason == "corrupt_bzip2_replay"
    assert existing.read_bytes() == b"existing replay"
    assert (tmp_path / "301.dem").read_bytes() == b"downloaded replay"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "301.dem",
        "303.dem",
    ]


def test_all_league_matches_respects_max_details(tmp_path: Path) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _json_bytes([_pro_match(1), _pro_match(3), _pro_match(2)])
            ],
            "/api/matches/3": [_json_bytes({})],
            "/api/matches/2": [_json_bytes({})],
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        max_details=2,
    )

    assert summary.details_requested == 2
    assert [result.match_id for result in summary.results] == [3, 2]
    assert _requested_paths(opener) == [
        "/api/leagues/7/matches",
        "/api/matches/3",
        "/api/matches/2",
    ]


def test_transient_league_discovery_error_retries_then_succeeds(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _http_error(503, retry_after="7"),
                _json_bytes([_pro_match(101)]),
            ],
            "/api/matches/101": [_json_bytes({})],
        }
    )
    sleeps: list[float] = []

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        sleep_func=sleeps.append,
    )

    assert summary.discovered == 1
    assert summary.skipped == 1
    assert summary.discovery_failed == 0
    assert sleeps == [7.0]
    assert _requested_paths(opener) == [
        "/api/leagues/7/matches",
        "/api/leagues/7/matches",
        "/api/matches/101",
    ]


def test_transient_match_detail_timeout_retries_then_succeeds(
    tmp_path: Path,
) -> None:
    def time_out(_request: Request) -> _FakeResponse:
        raise TimeoutError("transient timeout")

    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [time_out, _json_bytes({})],
        }
    )
    sleeps: list[float] = []

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        sleep_func=sleeps.append,
    )

    assert summary.skipped == 1
    assert summary.failed == 0
    assert sleeps == [1.0]
    assert _requested_paths(opener).count("/api/matches/101") == 2


def test_incomplete_opendota_read_retries_without_parsing_partial_json(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _IncompleteResponse(_json_bytes([])),
                _json_bytes([_pro_match(101)]),
            ],
            "/api/matches/101": [_json_bytes({})],
        }
    )
    sleeps: list[float] = []

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        sleep_func=sleeps.append,
    )

    assert summary.discovered == 1
    assert summary.skipped == 1
    assert summary.discovery_failed == 0
    assert sleeps == [1.0]
    assert _requested_paths(opener) == [
        "/api/leagues/7/matches",
        "/api/leagues/7/matches",
        "/api/matches/101",
    ]


def test_repeated_incomplete_opendota_reads_exhaust_existing_retry_budget(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [
                _IncompleteResponse(b'{"replay_url":') for _ in range(5)
            ],
        }
    )
    sleeps: list[float] = []

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        sleep_func=sleeps.append,
    )

    assert summary.detail_failed == 1
    assert summary.results[0].reason == "opendota_response_incomplete"
    assert summary.results[0].attempts == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert _requested_paths(opener).count("/api/matches/101") == 5


def test_incomplete_match_detail_does_not_prevent_later_download(
    tmp_path: Path,
) -> None:
    replay = b"later candidate replay"
    opener = _FakeOpen(
        {
            "/api/proMatches": [
                _json_bytes(
                    [
                        _pro_match(102, start_time=100),
                        _pro_match(101, start_time=200),
                    ]
                )
            ],
            "/api/matches/101": [_IncompleteResponse(b"partial")],
            "/api/matches/102": [
                _json_bytes({"replay_url": "https://replays.test/102.dem.bz2"})
            ],
            "/102.dem.bz2": [bz2.compress(replay)],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path, count=1, max_details=2)

    assert [result.status for result in summary.results] == [
        "FAILED",
        "DOWNLOADED",
    ]
    assert summary.results[0].reason == "opendota_response_incomplete"
    assert summary.details_requested == 2
    assert summary.downloaded == 1
    assert (tmp_path / "102.dem").read_bytes() == replay


def test_retry_exhaustion_uses_deterministic_exponential_backoff(
    tmp_path: Path,
) -> None:
    def connection_failure(_request: Request) -> _FakeResponse:
        raise URLError("connection refused")

    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [connection_failure] * 5,
        }
    )
    sleeps: list[float] = []

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        sleep_func=sleeps.append,
    )

    assert summary.detail_failed == 1
    assert summary.results[0].reason == "connection_failure"
    assert summary.results[0].attempts == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert _requested_paths(opener).count("/api/matches/101") == 5


def test_non_retryable_detail_http_4xx_fails_once(tmp_path: Path) -> None:
    opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [_http_error(404)],
        }
    )
    sleeps: list[float] = []

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=7,
        all_league_matches=True,
        sleep_func=sleeps.append,
    )

    assert format_replay_fetch_result(summary.results[0]) == (
        "FAILED league_id=7 match_id=101 stage=match_detail "
        "reason=http_error status=404 attempts=1"
    )
    assert sleeps == []
    assert _requested_paths(opener).count("/api/matches/101") == 1


def test_discovery_http_status_diagnostic_reports_retry_exhaustion(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {"/api/leagues/20053/matches": [_http_error(503)] * 5}
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=20053,
        all_league_matches=True,
    )

    assert summary.discovery_failure is not None
    assert format_replay_fetch_failure(summary.discovery_failure) == (
        "FAILED league_id=20053 stage=league_discovery "
        "reason=http_error status=503 attempts=5"
    )
    assert summary.failed == 1


def test_match_detail_timeout_diagnostic_reports_attempts(tmp_path: Path) -> None:
    def time_out(_request: Request) -> _FakeResponse:
        raise URLError(TimeoutError("request timed out"))

    opener = _FakeOpen(
        {
            "/api/leagues/19891/matches": [_json_bytes([_pro_match(123)])],
            "/api/matches/123": [time_out] * 5,
        }
    )

    summary = _fetch(
        opener,
        output_dir=tmp_path,
        league_id=19891,
        all_league_matches=True,
    )

    assert format_replay_fetch_result(summary.results[0]) == (
        "FAILED league_id=19891 match_id=123 stage=match_detail "
        "reason=request_timeout attempts=5"
    )


def test_league_summary_separates_discovery_and_detail_failures(
    tmp_path: Path,
) -> None:
    detail_opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _json_bytes([_pro_match(101), _pro_match(102)])
            ],
            "/api/matches/102": [_http_error(404)],
            "/api/matches/101": [_json_bytes({})],
        }
    )
    detail_summary = _fetch(
        detail_opener,
        output_dir=tmp_path / "details",
        league_id=7,
        all_league_matches=True,
    )
    discovery_summary = ReplayFetchSummary(
        results=(),
        details_requested=0,
        league_mode=True,
        league_id=8,
        discovery_failure=ReplayFetchFailure(
            league_id=8,
            stage="league_discovery",
            reason="request_timeout",
            attempts=5,
        ),
    )

    assert format_replay_fetch_summary(detail_summary) == (
        "SUMMARY DISCOVERED=2 DOWNLOADED=0 UNCHANGED=0 SKIPPED=1 "
        "DETAIL_FAILED=1 DISCOVERY_FAILED=0"
    )
    assert format_replay_fetch_summary(discovery_summary) == (
        "SUMMARY DISCOVERED=0 DOWNLOADED=0 UNCHANGED=0 SKIPPED=0 "
        "DETAIL_FAILED=0 DISCOVERY_FAILED=1"
    )


def test_league_failure_diagnostics_hide_api_keys_and_replay_urls(
    tmp_path: Path,
) -> None:
    api_key = "secret-api-key"
    discovery_opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [
                _http_error(503, retry_after=api_key)
            ]
            * 5
        }
    )
    discovery_summary = fetch_pro_replays(
        output_dir=tmp_path / "discovery",
        league_id=7,
        all_league_matches=True,
        api_key=api_key,
        request_delay_seconds=0,
        urlopen_func=discovery_opener,
        sleep_func=lambda _seconds: None,
    )
    assert discovery_summary.discovery_failure is not None
    discovery_output = format_replay_fetch_failure(
        discovery_summary.discovery_failure
    )

    replay_secret = "https://replays.test/101.dem.bz2?token=replay-secret"
    replay_opener = _FakeOpen(
        {
            "/api/leagues/7/matches": [_json_bytes([_pro_match(101)])],
            "/api/matches/101": [_json_bytes({"replay_url": replay_secret})],
            "/101.dem.bz2": [_http_error(403)],
        }
    )
    replay_summary = _fetch(
        replay_opener,
        output_dir=tmp_path / "replay",
        league_id=7,
        all_league_matches=True,
    )
    replay_output = format_replay_fetch_result(replay_summary.results[0])

    assert api_key not in discovery_output
    assert "?api_key=" not in discovery_output
    assert replay_secret not in replay_output
    assert "replay-secret" not in replay_output
    assert replay_output == (
        "FAILED league_id=7 match_id=101 stage=replay_download "
        "reason=http_error status=403 attempts=1"
    )


def test_recent_mode_still_uses_pro_matches_and_stops_at_count(
    tmp_path: Path,
) -> None:
    opener = _FakeOpen(
        {
            "/api/proMatches": [
                _json_bytes(
                    [_pro_match(101, start_time=100), _pro_match(102, start_time=200)]
                )
            ],
            "/api/matches/102": [
                _json_bytes({"replay_url": "https://replays.test/102.dem.bz2"})
            ],
            "/102.dem.bz2": [bz2.compress(b"recent replay")],
        }
    )

    summary = _fetch(opener, output_dir=tmp_path, count=1)

    assert summary.downloaded == 1
    assert [result.match_id for result in summary.results] == [102]
    assert _requested_paths(opener) == [
        "/api/proMatches",
        "/api/matches/102",
        "/102.dem.bz2",
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
        "all_league_matches": False,
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


def test_cli_requires_league_id_for_all_league_matches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["fetch-pro-replays", "--all-league-matches"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert "--all-league-matches requires --league-id" in captured.err


def test_cli_prints_explicit_league_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = ReplayFetchSummary(
        results=(),
        details_requested=0,
        league_mode=True,
        league_id=20053,
        discovery_failure=ReplayFetchFailure(
            league_id=20053,
            stage="league_discovery",
            reason="http_error",
            http_status=503,
            attempts=5,
        ),
    )
    monkeypatch.setattr(
        replay_fetcher,
        "fetch_pro_replays",
        lambda **_kwargs: summary,
    )

    exit_code = cli.main(
        ["fetch-pro-replays", "--all-league-matches", "--league-id", "20053"]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err == ""
    assert captured.out == (
        "FAILED league_id=20053 stage=league_discovery "
        "reason=http_error status=503 attempts=5\n"
        "SUMMARY DISCOVERED=0 DOWNLOADED=0 UNCHANGED=0 SKIPPED=0 "
        "DETAIL_FAILED=0 DISCOVERY_FAILED=1\n"
    )


def _fetch(
    opener: _FakeOpen,
    *,
    output_dir: Path,
    count: int = 10,
    max_details: int = 25,
    league_id: int | None = None,
    all_league_matches: bool = False,
    sleep_func: Callable[[float], None] = lambda _seconds: None,
) -> ReplayFetchSummary:
    return fetch_pro_replays(
        output_dir=output_dir,
        count=count,
        max_details=max_details,
        league_id=league_id,
        all_league_matches=all_league_matches,
        api_key="",
        request_delay_seconds=0,
        urlopen_func=opener,
        sleep_func=sleep_func,
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


def _http_error(
    status: int,
    *,
    retry_after: str | None = None,
) -> _RouteFactory:
    def raise_error(request: Request) -> _FakeResponse:
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        raise HTTPError(
            request.full_url,
            status,
            "controlled HTTP error",
            hdrs=headers,
            fp=None,
        )

    return raise_error


def _requested_paths(opener: _FakeOpen) -> list[str]:
    return [urlsplit(request.full_url).path for request in opener.requests]


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._offset = 0
        self.socket_timeouts: list[float] = []
        self.fp = _FakeSocketFile(self.socket_timeouts)

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


class _ProgressingResponse(_FakeResponse):
    def __init__(self, body: bytes, *, chunk_size: int) -> None:
        super().__init__(body)
        self._chunk_size = chunk_size
        self.read_count = 0

    def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        return super().read(min(size, self._chunk_size))


class _IncompleteResponse(_FakeResponse):
    def __init__(self, partial: bytes) -> None:
        super().__init__(b"")
        self._partial = partial

    def read(self, size: int = -1) -> bytes:
        raise IncompleteRead(self._partial, 1)


class _FakeSocket:
    def __init__(self, timeouts: list[float]) -> None:
        self._timeouts = timeouts

    def settimeout(self, timeout: float) -> None:
        self._timeouts.append(timeout)


class _FakeRawSocketFile:
    def __init__(self, timeouts: list[float]) -> None:
        self._sock = _FakeSocket(timeouts)


class _FakeSocketFile:
    def __init__(self, timeouts: list[float]) -> None:
        self.raw = _FakeRawSocketFile(timeouts)


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
        self.timeouts: list[float] = []

    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float = 10.0,
    ) -> _FakeResponse | _InterruptedResponse:
        assert data is None
        assert timeout > 0
        self.requests.append(request)
        self.timeouts.append(timeout)
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
