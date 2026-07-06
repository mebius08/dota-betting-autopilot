from collections.abc import Mapping
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.collectors.pandascore_match_collector import (
    PandaScoreConfigurationError,
    PandaScoreMatchCollector,
    PandaScoreRequestError,
    PandaScoreResponseError,
    fetch_pandascore_matches,
    map_pandascore_match,
    map_pandascore_matches,
)


def test_map_valid_upcoming_match() -> None:
    match = map_pandascore_match(_payload(status="not_started"))

    assert match is not None
    assert match.id == "pandascore-123"
    assert match.external_id == "123"
    assert match.team_a == "Team Spirit"
    assert match.team_b == "PARIVISION"
    assert match.tournament_name == "DreamLeague / Season 25 / Group Stage"
    assert match.format == "bo3"
    assert match.status == "upcoming"


def test_map_running_match_to_live() -> None:
    match = map_pandascore_match(_payload(status="running"))

    assert match is not None
    assert match.status == "live"


def test_map_uses_normalized_team_names_for_duplicate_detection() -> None:
    payload = _payload(
        team_a=" TEAM   SPIRIT ",
        team_b="Team\tSpirit",
    )

    assert map_pandascore_match(payload) is None


def test_map_skips_unusable_payloads() -> None:
    payloads: list[dict[str, object]] = [
        {},
        _payload(match_id=None),
        _payload(opponents=[]),
        _payload(opponents=[_opponent("Team Spirit")]),
        _payload(status="mystery"),
        _payload(begin_at="not-a-timestamp"),
    ]

    for payload in payloads:
        assert map_pandascore_match(payload) is None


def test_map_mixed_provider_rows_skips_invalid_rows() -> None:
    matches = map_pandascore_matches(
        [
            _payload(match_id=1, status="not_started"),
            _payload(match_id=2, status="mystery"),
            "not-a-dict",
            _payload(match_id=3, status="running"),
        ],
        status_filter="live",
    )

    assert [match.external_id for match in matches] == ["3"]
    assert all(match.status == "live" for match in matches)


def test_collect_fetches_and_maps_matches() -> None:
    collector = PandaScoreMatchCollector(
        token="token",
        urlopen_func=_fake_urlopen('[{"id": 123, "status": "running", '
                                  '"number_of_games": 3, "begin_at": null, '
                                  '"opponents": [{"opponent": {"name": "A"}}, '
                                  '{"opponent": {"name": "B"}}]}]'),
    )

    matches = collector.collect(session_id="session-1")

    assert len(matches) == 1
    assert matches[0].session_id == "session-1"
    assert matches[0].status == "live"


def test_collector_missing_token_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PANDASCORE_TOKEN", raising=False)
    collector = PandaScoreMatchCollector()

    with pytest.raises(PandaScoreConfigurationError, match="PandaScore token"):
        collector.collect()


def test_fetch_pandascore_matches_builds_bearer_request() -> None:
    seen_headers: dict[str, str | None] = {}

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        seen_headers["authorization"] = request.get_header("Authorization")
        seen_headers["accept"] = request.get_header("Accept")
        seen_headers["timeout"] = str(timeout)
        assert "per_page=7" in request.full_url
        return _FakeResponse(b"[]")

    payload = fetch_pandascore_matches(
        token="secret-token",
        limit=7,
        timeout=2.5,
        urlopen_func=fake_urlopen,
    )

    assert payload == []
    assert seen_headers["authorization"] == "Bearer secret-token"
    assert seen_headers["accept"] == "application/json"
    assert seen_headers["timeout"] == "2.5"


@pytest.mark.parametrize(
    ("body", "expected_error"),
    [
        ("not-json", PandaScoreResponseError),
        ('{"id": 1}', PandaScoreResponseError),
    ],
)
def test_fetch_handles_invalid_response(
    body: str,
    expected_error: type[Exception],
) -> None:
    with pytest.raises(expected_error):
        fetch_pandascore_matches(token="token", urlopen_func=_fake_urlopen(body))


@pytest.mark.parametrize(
    ("status_code", "expected_text"),
    [
        (401, "HTTP 401"),
        (403, "HTTP 403"),
        (500, "HTTP 500"),
    ],
)
def test_fetch_handles_http_errors_without_leaking_token(
    status_code: int,
    expected_text: str,
) -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise HTTPError(request.full_url, status_code, "error", hdrs=None, fp=None)

    with pytest.raises(PandaScoreRequestError) as exc_info:
        fetch_pandascore_matches(token="secret-token", urlopen_func=fake_urlopen)

    message = str(exc_info.value)
    assert expected_text in message
    assert "secret-token" not in message


def test_fetch_handles_timeout_without_leaking_token() -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise TimeoutError

    with pytest.raises(PandaScoreRequestError) as exc_info:
        fetch_pandascore_matches(token="secret-token", urlopen_func=fake_urlopen)

    assert "timed out" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

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


def _fake_urlopen(body: str):
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        return _FakeResponse(body.encode("utf-8"))

    return fake_urlopen


def _payload(
    *,
    match_id: int | None = 123,
    status: str = "not_started",
    team_a: str = "Team Spirit",
    team_b: str = "PARIVISION",
    opponents: list[Mapping[str, object]] | None = None,
    begin_at: object = "2026-07-06T12:00:00Z",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "number_of_games": 3,
        "begin_at": begin_at,
        "league": {"name": "DreamLeague"},
        "serie": {"full_name": "Season 25"},
        "tournament": {"name": "Group Stage"},
        "opponents": (
            [_opponent(team_a), _opponent(team_b)]
            if opponents is None
            else opponents
        ),
    }
    if match_id is not None:
        payload["id"] = match_id
    return payload


def _opponent(name: str) -> dict[str, object]:
    return {"opponent": {"name": name}}
