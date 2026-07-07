from datetime import datetime
import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.history import (
    PandaScoreRequestError,
    PandaScoreResponseError,
    PandaScoreRosterCollector,
    fetch_pandascore_tournament_roster_payload,
    map_pandascore_tournament_rosters,
)


def test_fetch_tournament_roster_uses_endpoint_and_auth() -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["accept"] = request.get_header("Accept")
        seen["timeout"] = timeout
        return _FakeResponse(_roster_payload())

    payload = fetch_pandascore_tournament_roster_payload(
        token="secret-token",
        tournament_source_id="300",
        timeout=2.5,
        urlopen_func=fake_urlopen,
    )

    assert payload == _roster_payload()
    assert str(seen["url"]) == "https://api.pandascore.co/tournaments/300/rosters"
    assert "/dota2/tournaments/300" not in str(seen["url"])
    assert seen["authorization"] == "Bearer secret-token"
    assert seen["accept"] == "application/json"
    assert seen["timeout"] == 2.5


def test_fetch_handles_invalid_json_shape() -> None:
    def fake_urlopen(request: Request, timeout: float) -> _RawFakeResponse:
        return _RawFakeResponse(json.dumps({"id": 300}).encode("utf-8"))

    with pytest.raises(PandaScoreResponseError):
        fetch_pandascore_tournament_roster_payload(
            token="token",
            tournament_source_id="300",
            urlopen_func=fake_urlopen,
        )


def test_fetch_http_error_does_not_leak_token() -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise HTTPError(request.full_url, 401, "error", hdrs=None, fp=None)

    with pytest.raises(PandaScoreRequestError) as exc_info:
        fetch_pandascore_tournament_roster_payload(
            token="secret-token",
            tournament_source_id="300",
            urlopen_func=fake_urlopen,
        )

    assert "HTTP 401" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)


def test_map_valid_tournament_roster_with_missing_coach() -> None:
    result = map_pandascore_tournament_rosters(
        _roster_payload(coach=None),
        tournament_source_id="300",
        tournament_name=None,
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )

    assert result.skipped_records == 0
    assert len(result.snapshots) == 1
    snapshot = result.snapshots[0]
    assert snapshot.organization.source_team_id == "10"
    assert snapshot.organization.name == "Team Spirit"
    assert [player.source_player_id for player in snapshot.players] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert snapshot.coach is None
    assert snapshot.tournament_source_id == "300"
    assert snapshot.tournament_name is None


def test_map_roster_with_coach_and_duplicate_players_normalizes_members() -> None:
    payload = _roster_payload(
        players=[
            _player(1, "A"),
            _player(2, "B"),
            _player(1, "A duplicate"),
        ],
        coach={"id": 99, "name": "Coach"},
    )

    result = map_pandascore_tournament_rosters(
        payload,
        tournament_source_id="300",
        tournament_name="Group Stage",
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )

    assert len(result.snapshots) == 1
    snapshot = result.snapshots[0]
    assert [player.source_player_id for player in snapshot.players] == ["1", "2"]
    assert snapshot.coach is not None
    assert snapshot.coach.source_coach_id == "99"


def test_missing_stable_ids_are_not_mapped_as_name_only_identities() -> None:
    payload = _roster_payload(
        players=[
            {"name": "Name Only"},
            _player(2, "Stable Player"),
        ]
    )

    result = map_pandascore_tournament_rosters(
        payload,
        tournament_source_id="300",
        tournament_name=None,
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )

    assert result.skipped_records == 1
    assert len(result.snapshots) == 1
    assert [player.source_player_id for player in result.snapshots[0].players] == [
        "2"
    ]
    assert "Name Only" not in {
        player.source_player_id for player in result.snapshots[0].players
    }


def test_missing_team_id_and_empty_roster_are_skipped() -> None:
    result = map_pandascore_tournament_rosters(
        [
            {"name": "Name Only", "players": [_player(1, "A")]},
            {"id": 10, "name": "Empty", "players": []},
        ],
        tournament_source_id="300",
        tournament_name=None,
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )

    assert result.snapshots == []
    assert result.skipped_records == 2


def test_collect_is_bounded_by_max_tournaments() -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        requested_urls.append(request.full_url)
        return _FakeResponse(_roster_payload())

    collector = PandaScoreRosterCollector(
        token="token",
        urlopen_func=fake_urlopen,
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )

    result = collector.collect(
        tournament_source_ids=["100", "200", "300"],
        max_tournaments=2,
    )

    assert result.tournaments_requested == 2
    assert len(requested_urls) == 2
    assert requested_urls == [
        "https://api.pandascore.co/tournaments/100/rosters",
        "https://api.pandascore.co/tournaments/200/rosters",
    ]
    assert len(result.snapshots) == 2


def test_name_only_coach_is_ignored_conservatively() -> None:
    result = map_pandascore_tournament_rosters(
        _roster_payload(coach={"name": "Name Only Coach"}),
        tournament_source_id="300",
        tournament_name=None,
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )

    assert len(result.snapshots) == 1
    assert result.snapshots[0].coach is None


class _FakeResponse:
    def __init__(self, body: object) -> None:
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


def _roster_payload(
    *,
    players: list[dict[str, object]] | None = None,
    coach: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    team: dict[str, object] = {
        "id": 10,
        "name": "Team Spirit",
        "players": players
        if players is not None
        else [
            _player(1, "A"),
            _player(2, "B"),
            _player(3, "C"),
            _player(4, "D"),
            _player(5, "E"),
        ],
    }
    if coach is not None:
        team["coach"] = coach
    return [team]


def _player(player_id: int, name: str) -> dict[str, object]:
    return {"id": player_id, "name": name}


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
