from __future__ import annotations

from collections.abc import Callable, Mapping
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from app import cli
from app.opendota_live import (
    OpenDotaLiveGame,
    OpenDotaLiveRequestError,
    OpenDotaLiveResponseError,
    OPENDOTA_LIVE_URL,
    deduplicate_and_sort_live_matches,
    fetch_live_games,
    filter_live_games,
    persist_live_matches,
    validate_live_game,
)


def test_cli_help_describes_bounded_opendota_live_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--help"]) == 0

    output = capsys.readouterr().out
    assert "fetch-opendota-live" in output
    assert "current bounded OpenDota live-game snapshot" in output
    assert "fetch-live-pro-matches" not in output


def test_successful_api_live_fetch() -> None:
    opener = _FakeOpen([_response(_json_bytes([_record()]))])

    records = fetch_live_games(urlopen_func=opener)

    assert records == [_record()]
    assert opener.timeouts == [10.0]


def test_fetch_requires_no_api_key_or_secret_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENDOTA_API_KEY", "must-not-be-used")
    opener = _FakeOpen([_response(b"[]")])

    fetch_live_games(urlopen_func=opener)

    request = opener.requests[0]
    assert request.data is None
    assert request.get_header("Authorization") is None
    assert request.get_header("Api-key") is None
    assert "must-not-be-used" not in request.full_url
    assert request.full_url.endswith("/api/live")


def test_fetch_uses_only_the_expected_endpoint() -> None:
    opener = _FakeOpen([_response(b"[]")])

    fetch_live_games(urlopen_func=opener)

    assert [request.full_url for request in opener.requests] == [
        "https://api.opendota.com/api/live"
    ]
    assert opener.requests[0].get_method() == "GET"


def test_league_filtering_happens_before_full_record_validation() -> None:
    records = [
        {"league_id": 7, "malformed_unselected_record": True},
        _record(match_id=2, league_id=9),
        _record(match_id=3, league_id=10),
    ]

    selection = filter_live_games(records, [9])

    assert [match.match_id for match in selection.matches] == [2]
    assert selection.skipped_source_record_count == 2


def test_no_filter_mode_retains_all_valid_records() -> None:
    selection = filter_live_games(
        [_record(match_id=2, league_id=9), _record(match_id=1, league_id=7)]
    )

    assert [(match.match_id, match.league_id) for match in selection.matches] == [
        (2, 9),
        (1, 7),
    ]
    assert selection.skipped_source_record_count == 0


@pytest.mark.parametrize("value", [0, -1, "", "1.5", "invalid", 1.0, True, None])
def test_match_id_validation(value: object) -> None:
    with pytest.raises(OpenDotaLiveResponseError, match="match_id"):
        validate_live_game(_record(match_id=value))


@pytest.mark.parametrize("value", [0, -1, "", "456.0", "invalid", 456.0, False, None])
def test_server_steam_id_validation(value: object) -> None:
    with pytest.raises(OpenDotaLiveResponseError, match="server_steam_id"):
        validate_live_game(_record(server_steam_id=value))


@pytest.mark.parametrize("value", [-1, "", "7.0", "invalid", 7.0, True, None])
def test_league_id_validation(value: object) -> None:
    with pytest.raises(OpenDotaLiveResponseError, match="league_id"):
        filter_live_games([_record(league_id=value)], [7])


@pytest.mark.parametrize("value", ["", "600.0", "invalid", 600.0, True, None])
def test_game_time_validation(value: object) -> None:
    with pytest.raises(OpenDotaLiveResponseError, match="game_time"):
        validate_live_game(_record(game_time=value))


@pytest.mark.parametrize("field", ["team_name_radiant", "team_name_dire"])
@pytest.mark.parametrize("value", [1, [], False])
def test_team_name_validation(field: str, value: object) -> None:
    record = _record()
    record[field] = value

    with pytest.raises(OpenDotaLiveResponseError, match=field):
        validate_live_game(record)


def test_missing_or_null_radiant_team_name_is_canonicalized_to_null() -> None:
    missing = _record()
    missing.pop("team_name_radiant")

    assert validate_live_game(missing).radiant_team_name is None
    assert validate_live_game(_record(radiant_team_name=None)).radiant_team_name is None


def test_missing_or_null_dire_team_name_is_canonicalized_to_null() -> None:
    missing = _record()
    missing.pop("team_name_dire")

    assert validate_live_game(missing).dire_team_name is None
    assert validate_live_game(_record(dire_team_name=None)).dire_team_name is None


def test_extra_source_fields_are_ignored() -> None:
    record = _record()
    record.update(
        {
            "players": [{"account_id": 123}],
            "radiant_score": 99,
            "undocumented": {"nested": "value"},
        }
    )

    match = validate_live_game(record)

    assert match == _match()
    assert not hasattr(match, "players")


def test_observed_decimal_string_match_and_server_ids_are_normalized() -> None:
    match = validate_live_game(
        _record(
            match_id="123",
            server_steam_id="456",
            league_id=789,
            game_time=-10,
        )
    )

    assert match == _match(game_time=-10)


def test_unusable_source_records_are_skipped_and_counted() -> None:
    missing_match_id = _record()
    missing_match_id.pop("match_id")
    missing_server_id = _record()
    missing_server_id.pop("server_steam_id")

    selection = filter_live_games(
        [None, "not-an-object", missing_match_id, missing_server_id, _record()]
    )

    assert selection.matches == (_match(),)
    assert selection.skipped_source_record_count == 4


def test_malformed_retained_record_is_rejected() -> None:
    with pytest.raises(OpenDotaLiveResponseError, match="match_id"):
        filter_live_games([_record(match_id="not-decimal")])


def test_selected_league_missing_connection_identity_is_skipped() -> None:
    record = _record(league_id=7)
    record.pop("server_steam_id")

    selection = filter_live_games([record], [7])

    assert selection.matches == ()
    assert selection.skipped_source_record_count == 1


def test_matches_sort_by_league_then_match_id() -> None:
    matches = [
        _match(match_id=3, league_id=8),
        _match(match_id=9, league_id=7),
        _match(match_id=1, league_id=7),
    ]

    ordered = deduplicate_and_sort_live_matches(matches)

    assert [(match.league_id, match.match_id) for match in ordered] == [
        (7, 1),
        (7, 9),
        (8, 3),
    ]


def test_exact_duplicate_match_collapses() -> None:
    match = _match()

    assert deduplicate_and_sort_live_matches([match, match]) == [match]


def test_conflicting_duplicate_match_is_rejected() -> None:
    with pytest.raises(OpenDotaLiveResponseError, match="Conflicting duplicate"):
        deduplicate_and_sort_live_matches(
            [_match(match_id=1), _match(match_id=1, game_time=601)]
        )


def test_empty_result_is_valid(tmp_path: Path) -> None:
    output = tmp_path / "live.json"

    status = persist_live_matches(output, [])

    assert status == "BUILT"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "match_count": 0,
        "skipped_source_record_count": 0,
        "matches": [],
    }


def test_unavailable_team_names_persist_as_json_null(tmp_path: Path) -> None:
    output = tmp_path / "live.json"

    persist_live_matches(
        output,
        [_match(radiant_team_name=None, dire_team_name=None)],
    )

    assert _payload_matches(output) == [
        {
            "match_id": 123,
            "server_steam_id": 456,
            "league_id": 789,
            "game_time": 600,
            "radiant_team_name": None,
            "dire_team_name": None,
        }
    ]


def test_timeout_is_retried_then_succeeds() -> None:
    opener = _FakeOpen([TimeoutError("secret timeout detail"), _response(b"[]")])
    sleeps: list[float] = []

    assert fetch_live_games(urlopen_func=opener, sleep_func=sleeps.append) == []
    assert sleeps == [1.0]
    assert len(opener.requests) == 2


def test_connection_failure_is_retried_then_succeeds() -> None:
    opener = _FakeOpen([URLError("connection refused"), _response(b"[]")])
    sleeps: list[float] = []

    assert fetch_live_games(urlopen_func=opener, sleep_func=sleeps.append) == []
    assert sleeps == [1.0]
    assert len(opener.requests) == 2


def test_retryable_http_status_respects_retry_after() -> None:
    opener = _FakeOpen(
        [_http_error(521, retry_after="3"), _response(_json_bytes([_record()]))]
    )
    sleeps: list[float] = []

    records = fetch_live_games(urlopen_func=opener, sleep_func=sleeps.append)

    assert records == [_record()]
    assert sleeps == [3.0]
    assert len(opener.requests) == 2


def test_terminal_deterministic_4xx_is_not_retried_or_leaked() -> None:
    opener = _FakeOpen([_http_error(404, body=b"private response body")])
    sleeps: list[float] = []

    with pytest.raises(OpenDotaLiveRequestError) as captured:
        fetch_live_games(urlopen_func=opener, sleep_func=sleeps.append)

    assert "404" in str(captured.value)
    assert "private response body" not in str(captured.value)
    assert sleeps == []
    assert len(opener.requests) == 1


def test_non_200_response_object_is_rejected_without_reading_body() -> None:
    response = _FakeResponse(b"private response body", status=201)
    opener = _FakeOpen([response])

    with pytest.raises(OpenDotaLiveRequestError, match="HTTP 201"):
        fetch_live_games(urlopen_func=opener)

    assert response.read_calls == 0


def test_five_attempt_exhaustion_is_bounded() -> None:
    opener = _FakeOpen([TimeoutError("timeout")] * 5)
    sleeps: list[float] = []

    with pytest.raises(OpenDotaLiveRequestError, match="5 attempts"):
        fetch_live_games(urlopen_func=opener, sleep_func=sleeps.append)

    assert len(opener.requests) == 5
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_invalid_utf8_is_rejected() -> None:
    opener = _FakeOpen([_response(b"[\xff]")])

    with pytest.raises(OpenDotaLiveResponseError, match="UTF-8"):
        fetch_live_games(urlopen_func=opener)


def test_malformed_json_is_rejected() -> None:
    opener = _FakeOpen([_response(b"[{not-json]")])

    with pytest.raises(OpenDotaLiveResponseError, match="valid JSON"):
        fetch_live_games(urlopen_func=opener)


@pytest.mark.parametrize("payload", [{}, None, "array", 123])
def test_non_array_source_is_rejected(payload: object) -> None:
    opener = _FakeOpen([_response(_json_bytes(payload))])

    with pytest.raises(OpenDotaLiveResponseError, match="JSON array"):
        fetch_live_games(urlopen_func=opener)


def test_output_is_deterministic_and_contains_only_required_fields(
    tmp_path: Path,
) -> None:
    first_output = tmp_path / "first" / "live.json"
    second_output = tmp_path / "second" / "live.json"
    matches = [
        _match(match_id=3, league_id=8, radiant_team_name="Радиант"),
        _match(match_id=1, league_id=7, dire_team_name="Дайр"),
    ]

    assert (
        persist_live_matches(
            first_output,
            matches,
            skipped_source_record_count=3,
        )
        == "BUILT"
    )
    assert (
        persist_live_matches(
            second_output,
            reversed(matches),
            skipped_source_record_count=3,
        )
        == "BUILT"
    )

    assert first_output.read_bytes() == second_output.read_bytes()
    payload = json.loads(first_output.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "match_count": 2,
        "skipped_source_record_count": 3,
        "matches": [
            {
                "match_id": 1,
                "server_steam_id": 456,
                "league_id": 7,
                "game_time": 600,
                "radiant_team_name": "Radiant",
                "dire_team_name": "Дайр",
            },
            {
                "match_id": 3,
                "server_steam_id": 456,
                "league_id": 8,
                "game_time": 600,
                "radiant_team_name": "Радиант",
                "dire_team_name": "Dire",
            },
        ],
    }
    rendered = first_output.read_text(encoding="utf-8")
    assert "timestamp" not in rendered
    assert "provider" not in rendered
    assert "player" not in rendered


def test_byte_identical_rerun_returns_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "live.json"

    first = persist_live_matches(output, [_match()])
    first_bytes = output.read_bytes()
    second = persist_live_matches(output, [_match()])

    assert first == "BUILT"
    assert second == "UNCHANGED"
    assert output.read_bytes() == first_bytes


def test_cli_supports_repeatable_league_filters_and_compact_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import app.opendota_live as live

    monkeypatch.setattr(
        live,
        "fetch_live_games",
        lambda **_kwargs: [
            _record(match_id=1, league_id=7),
            _record(match_id=2, league_id=8),
            _record(match_id=3, league_id=9),
        ],
    )
    output = tmp_path / "live.json"

    exit_code = cli.main(
        [
            "fetch-opendota-live",
            "--league-id",
            "7",
            "--league-id",
            "9",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "BUILT\n"
    assert [match["match_id"] for match in _payload_matches(output)] == [1, 3]
    assert _payload(output)["skipped_source_record_count"] == 1


def test_cli_no_filter_retains_all_live_games(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.opendota_live as live

    monkeypatch.setattr(
        live,
        "fetch_live_games",
        lambda **_kwargs: [
            _record(match_id=2, league_id=8),
            _record(match_id=1, league_id=7),
        ],
    )
    output = tmp_path / "live.json"

    assert cli.main(["fetch-opendota-live", "--output", str(output)]) == 0
    assert [match["match_id"] for match in _payload_matches(output)] == [1, 2]
    assert _payload(output)["skipped_source_record_count"] == 0


def _record(
    *,
    match_id: object = "123",
    server_steam_id: object = "456",
    league_id: object = 789,
    game_time: object = 600,
    radiant_team_name: object = "Radiant",
    dire_team_name: object = "Dire",
) -> dict[str, object]:
    return {
        "match_id": match_id,
        "server_steam_id": server_steam_id,
        "league_id": league_id,
        "game_time": game_time,
        "team_name_radiant": radiant_team_name,
        "team_name_dire": dire_team_name,
    }


def _match(
    *,
    match_id: int = 123,
    server_steam_id: int = 456,
    league_id: int = 789,
    game_time: int = 600,
    radiant_team_name: str | None = "Radiant",
    dire_team_name: str | None = "Dire",
) -> OpenDotaLiveGame:
    return OpenDotaLiveGame(
        match_id=match_id,
        server_steam_id=server_steam_id,
        league_id=league_id,
        game_time=game_time,
        radiant_team_name=radiant_team_name,
        dire_team_name=dire_team_name,
    )


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _response(body: bytes) -> _FakeResponse:
    return _FakeResponse(body)


def _http_error(
    status: int,
    *,
    retry_after: str | None = None,
    body: bytes | None = None,
) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        OPENDOTA_LIVE_URL,
        status,
        f"HTTP {status}",
        headers,
        BytesIO(body) if body is not None else None,
    )


def _payload_matches(path: Path) -> list[dict[str, object]]:
    payload = _payload(path)
    matches = payload["matches"]
    assert isinstance(matches, list)
    return matches


def _payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if size < 0:
            return self.body
        return self.body[:size]

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        return None


_Outcome = _FakeResponse | BaseException | Callable[[Request], _FakeResponse]


class _FakeOpen:
    def __init__(self, outcomes: list[_Outcome]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def __call__(
        self,
        request: Request,
        data: Any = None,
        timeout: float = 10.0,
    ) -> _FakeResponse:
        assert data is None
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome
