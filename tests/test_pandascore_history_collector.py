from collections.abc import Callable
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from app.history import (
    PandaScoreConfigurationError,
    PandaScoreHistoricalMatchCollector,
    PandaScoreRequestError,
    PandaScoreResponseError,
    fetch_pandascore_past_match_page,
    fetch_pandascore_past_match_rows,
)


def test_fetch_past_matches_uses_endpoint_auth_window_and_pagination() -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["accept"] = request.get_header("Accept")
        seen["timeout"] = timeout
        return _FakeResponse([_payload(1)])

    rows = fetch_pandascore_past_match_page(
        token="secret-token",
        timeout=2.5,
        since=_dt("2026-01-01T00:00:00Z"),
        until=_dt("2026-01-31T23:59:59Z"),
        page_size=25,
        page_number=3,
        urlopen_func=fake_urlopen,
    )
    query = parse_qs(urlparse(str(seen["url"])).query)

    assert rows == [_payload(1)]
    assert "/dota2/matches/past" in str(seen["url"])
    assert seen["authorization"] == "Bearer secret-token"
    assert seen["accept"] == "application/json"
    assert seen["timeout"] == 2.5
    assert query["page[number]"] == ["3"]
    assert query["page[size]"] == ["25"]
    assert query["sort"] == ["begin_at"]
    assert query["range[begin_at]"] == [
        "2026-01-01T00:00:00Z,2026-01-31T23:59:59Z"
    ]


def test_fetch_pages_until_short_page() -> None:
    bodies = {
        1: [_payload(1), _payload(2)],
        2: [_payload(3)],
    }

    rows = fetch_pandascore_past_match_rows(
        token="token",
        page_size=2,
        max_pages=5,
        urlopen_func=_paged_urlopen(bodies),
    )

    assert [row["id"] for row in rows if isinstance(row, dict)] == [1, 2, 3]


def test_fetch_respects_max_pages() -> None:
    rows = fetch_pandascore_past_match_rows(
        token="token",
        page_size=1,
        max_pages=2,
        urlopen_func=_paged_urlopen(
            {
                1: [_payload(1)],
                2: [_payload(2)],
                3: [_payload(3)],
            }
        ),
    )

    assert [row["id"] for row in rows if isinstance(row, dict)] == [1, 2]


def test_fetch_without_max_pages_reads_until_provider_completion() -> None:
    requested_pages: list[int] = []
    pages = {page: [_payload(page)] for page in range(1, 13)}

    rows = fetch_pandascore_past_match_rows(
        token="token",
        page_size=1,
        max_pages=None,
        urlopen_func=_paged_urlopen(pages, requested_pages=requested_pages),
    )

    assert [row["id"] for row in rows if isinstance(row, dict)] == list(
        range(1, 13)
    )
    assert requested_pages == list(range(1, 14))


def test_fetch_stops_on_empty_terminal_page() -> None:
    requested_pages: list[int] = []

    rows = fetch_pandascore_past_match_rows(
        token="token",
        page_size=2,
        max_pages=None,
        urlopen_func=_paged_urlopen(
            {1: [_payload(1), _payload(2)], 2: []},
            requested_pages=requested_pages,
        ),
    )

    assert [row["id"] for row in rows if isinstance(row, dict)] == [1, 2]
    assert requested_pages == [1, 2]


def test_repeated_page_detection_prevents_endless_pagination() -> None:
    with pytest.raises(PandaScoreResponseError, match="repeated"):
        fetch_pandascore_past_match_rows(
            token="token",
            page_size=1,
            max_pages=None,
            urlopen_func=_paged_urlopen(
                {
                    1: [_payload(1)],
                    2: [_payload(1)],
                }
            ),
        )


def test_collect_maps_rows_and_deduplicates_provider_ids() -> None:
    collector = PandaScoreHistoricalMatchCollector(
        token="token",
        urlopen_func=_paged_urlopen(
            {
                1: [_payload(1), _payload(2)],
                2: [_payload(2), {"id": 4}],
            }
        ),
    )

    result = collector.collect(
        since=None,
        until=None,
        page_size=2,
        max_pages=2,
    )

    assert [match.source_match_id for match in result.matches] == ["1", "2"]
    assert result.fetched_rows == 4
    assert result.skipped_rows == 2
    assert any("duplicate" in warning for warning in result.warnings)


def test_one_malformed_row_does_not_destroy_batch() -> None:
    collector = PandaScoreHistoricalMatchCollector(
        token="token",
        urlopen_func=_paged_urlopen({1: [_payload(1), "bad-row"]}),
    )

    result = collector.collect(
        since=None,
        until=None,
        page_size=10,
        max_pages=1,
    )

    assert [match.source_match_id for match in result.matches] == ["1"]
    assert result.skipped_rows == 1


@pytest.mark.parametrize("body", ["not-json", {"id": 1}])
def test_fetch_handles_invalid_json_shape(body: object) -> None:
    def fake_urlopen(request: Request, timeout: float) -> _RawFakeResponse:
        raw = body if isinstance(body, str) else json.dumps(body)
        return _RawFakeResponse(raw.encode("utf-8"))

    with pytest.raises(PandaScoreResponseError):
        fetch_pandascore_past_match_page(
            token="token",
            urlopen_func=fake_urlopen,
        )


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_fetch_handles_http_errors_without_leaking_token(status_code: int) -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise HTTPError(request.full_url, status_code, "error", hdrs=None, fp=None)

    with pytest.raises(PandaScoreRequestError) as exc_info:
        fetch_pandascore_past_match_page(
            token="secret-token",
            urlopen_func=fake_urlopen,
        )

    message = str(exc_info.value)
    assert f"HTTP {status_code}" in message
    assert "secret-token" not in message


def test_fetch_handles_timeout_without_leaking_token() -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise TimeoutError

    with pytest.raises(PandaScoreRequestError) as exc_info:
        fetch_pandascore_past_match_page(
            token="secret-token",
            urlopen_func=fake_urlopen,
        )

    assert "timed out" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)


def test_missing_token_is_controlled_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PANDASCORE_TOKEN", raising=False)
    collector = PandaScoreHistoricalMatchCollector()

    with pytest.raises(PandaScoreConfigurationError):
        collector.collect(since=None, until=None)


@pytest.mark.parametrize(
    ("page_size", "max_pages"),
    [(0, 1), (101, 1), (20, 0)],
)
def test_fetch_validates_pagination(page_size: int, max_pages: int) -> None:
    with pytest.raises(ValueError):
        fetch_pandascore_past_match_rows(
            token="token",
            page_size=page_size,
            max_pages=max_pages,
        )


class _FakeResponse:
    def __init__(self, body: list[object]) -> None:
        self.body = json.dumps(body).encode("utf-8")

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


class _RawFakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "_RawFakeResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False


def _paged_urlopen(
    pages: dict[int, list[object]],
    *,
    requested_pages: list[int] | None = None,
) -> Callable[[Request, float], _FakeResponse]:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        query = parse_qs(urlparse(request.full_url).query)
        page_number = int(query.get("page[number]", ["1"])[0])
        if requested_pages is not None:
            requested_pages.append(page_number)
        return _FakeResponse(pages.get(page_number, []))

    return fake_urlopen


def _payload(match_id: int) -> dict[str, object]:
    return {
        "id": match_id,
        "status": "finished",
        "begin_at": "2026-01-01T10:00:00Z",
        "end_at": "2026-01-01T12:00:00Z",
        "number_of_games": 3,
        "winner_id": 10,
        "winner": {"id": 10, "name": "Team Spirit"},
        "league": {"id": 100, "name": "DreamLeague"},
        "serie": {"id": 200, "full_name": "Season 25"},
        "tournament": {"id": 300, "name": "Group Stage"},
        "opponents": [
            {"opponent": {"id": 10, "name": "Team Spirit"}},
            {"opponent": {"id": 20, "name": "PARIVISION"}},
        ],
    }


def _dt(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))
