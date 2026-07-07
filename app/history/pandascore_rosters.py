from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from app.collectors.pandascore_match_collector import (
    DEFAULT_URL_OPEN,
    PANDASCORE_MISSING_TOKEN_MESSAGE,
    PANDASCORE_TOKEN_ENV,
    PandaScoreConfigurationError,
    PandaScoreRequestError,
    PandaScoreResponseError,
    _UrlOpen,
    _display_text,
    _http_error,
    _text_from_value,
)
from app.history.pandascore import PANDASCORE_HISTORY_SOURCE
from app.history.roster_service import RosterCollectionResult
from app.history.rosters import (
    PlayerIdentity,
    RosterCoach,
    RosterSnapshot,
    TeamOrganization,
    build_player_roster_fingerprint,
    build_roster_snapshot_id,
    build_roster_snapshot_source_id,
    build_staff_roster_fingerprint,
)


PANDASCORE_TOURNAMENT_ROSTERS_URL_TEMPLATE = (
    "https://api.pandascore.co/tournaments/{tournament_id_or_slug}/rosters"
)
PANDASCORE_ROSTER_CONTEXT = "tournament"


@dataclass(frozen=True)
class PandaScoreRosterMappingResult:
    snapshots: list[RosterSnapshot]
    skipped_records: int
    warnings: list[str]


class PandaScoreRosterCollector:
    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 10.0,
        endpoint_template: str = PANDASCORE_TOURNAMENT_ROSTERS_URL_TEMPLATE,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
        observed_at: datetime | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")

        self.token = (
            token if token is not None else os.environ.get(PANDASCORE_TOKEN_ENV)
        )
        self.timeout = timeout
        self.endpoint_template = endpoint_template
        self.urlopen_func = urlopen_func
        self.observed_at = observed_at

    def collect(
        self,
        *,
        tournament_source_ids: list[str],
        max_tournaments: int,
    ) -> RosterCollectionResult:
        if max_tournaments < 1:
            raise ValueError("max_tournaments must be at least 1")

        bounded_tournament_ids = tournament_source_ids[:max_tournaments]
        observed_at = self.observed_at or datetime.now(timezone.utc)
        snapshots: list[RosterSnapshot] = []
        skipped_records = 0
        warnings: list[str] = []

        for tournament_source_id in bounded_tournament_ids:
            payload = fetch_pandascore_tournament_roster_payload(
                token=self._required_token(),
                tournament_source_id=tournament_source_id,
                timeout=self.timeout,
                endpoint_template=self.endpoint_template,
                urlopen_func=self.urlopen_func,
            )
            mapped = map_pandascore_tournament_rosters(
                payload,
                tournament_source_id=tournament_source_id,
                tournament_name=None,
                observed_at=observed_at,
            )
            snapshots.extend(mapped.snapshots)
            skipped_records += mapped.skipped_records
            warnings.extend(mapped.warnings)

        return RosterCollectionResult(
            snapshots=snapshots,
            tournaments_requested=len(bounded_tournament_ids),
            fetched_rows=len(bounded_tournament_ids),
            skipped_records=skipped_records,
            warnings=warnings,
        )

    def _required_token(self) -> str:
        token = (self.token or "").strip()
        if not token:
            raise PandaScoreConfigurationError(PANDASCORE_MISSING_TOKEN_MESSAGE)
        return token


def fetch_pandascore_tournament_roster_payload(
    *,
    token: str,
    tournament_source_id: str,
    timeout: float = 10.0,
    endpoint_template: str = PANDASCORE_TOURNAMENT_ROSTERS_URL_TEMPLATE,
    urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
) -> list[object]:
    token = token.strip()
    if not token:
        raise PandaScoreConfigurationError(PANDASCORE_MISSING_TOKEN_MESSAGE)
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    if not tournament_source_id.strip():
        raise ValueError("tournament_source_id must not be empty")

    tournament_id_or_slug = quote(tournament_source_id.strip(), safe="")
    request = Request(
        endpoint_template.format(tournament_id_or_slug=tournament_id_or_slug),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )

    try:
        with urlopen_func(request, timeout=timeout) as response:
            raw_body = response.read()
    except HTTPError as exc:
        raise _http_error(exc) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise PandaScoreRequestError("PandaScore request timed out.") from exc
    except (URLError, OSError) as exc:
        raise PandaScoreRequestError("PandaScore network request failed.") from exc

    return _decode_provider_list(raw_body)


def map_pandascore_tournament_rosters(
    payload: Iterable[object],
    *,
    tournament_source_id: str,
    tournament_name: str | None,
    observed_at: datetime,
) -> PandaScoreRosterMappingResult:
    snapshots: list[RosterSnapshot] = []
    skipped_records = 0
    warnings: list[str] = []

    for index, item in enumerate(payload, start=1):
        if not isinstance(item, Mapping):
            skipped_records += 1
            warnings.append(
                f"Skipped malformed roster entry for tournament {tournament_source_id}."
            )
            continue

        team = _team_payload(item)
        team_source_id = _object_id(team)
        team_name = _object_name(team)
        if team_source_id is None or team_name is None:
            skipped_records += 1
            warnings.append(
                "Skipped roster entry without stable provider team id "
                f"for tournament {tournament_source_id} at index {index}."
            )
            continue

        players, missing_player_ids = _players_from_item(item, team)
        if missing_player_ids:
            skipped_records += missing_player_ids
            warnings.append(
                "Skipped player entries without stable provider player id "
                f"for team {team_source_id}."
            )
        if not players:
            skipped_records += 1
            warnings.append(
                f"Skipped empty roster for team {team_source_id} "
                f"in tournament {tournament_source_id}."
            )
            continue

        organization = TeamOrganization(
            source=PANDASCORE_HISTORY_SOURCE,
            source_team_id=team_source_id,
            name=team_name,
        )
        coach = _coach_from_item(item, team)
        player_fingerprint = build_player_roster_fingerprint(players)
        staff_fingerprint = (
            build_staff_roster_fingerprint(players, coach)
            if coach is not None
            else None
        )
        source_snapshot_id = build_roster_snapshot_source_id(
            source=PANDASCORE_HISTORY_SOURCE,
            source_context=PANDASCORE_ROSTER_CONTEXT,
            tournament_source_id=tournament_source_id,
            organization=organization,
            player_roster_fingerprint=player_fingerprint,
            staff_roster_fingerprint=staff_fingerprint,
            valid_from=None,
            valid_until=None,
        )

        snapshots.append(
            RosterSnapshot(
                id=build_roster_snapshot_id(
                    PANDASCORE_HISTORY_SOURCE,
                    source_snapshot_id,
                ),
                source=PANDASCORE_HISTORY_SOURCE,
                source_snapshot_id=source_snapshot_id,
                organization=organization,
                observed_at=observed_at,
                players=tuple(players),
                coach=coach,
                source_context=PANDASCORE_ROSTER_CONTEXT,
                tournament_source_id=tournament_source_id,
                tournament_name=tournament_name,
                valid_from=None,
                valid_until=None,
                player_roster_fingerprint=player_fingerprint,
                staff_roster_fingerprint=staff_fingerprint,
            )
        )

    return PandaScoreRosterMappingResult(
        snapshots=snapshots,
        skipped_records=skipped_records,
        warnings=warnings,
    )


def _decode_provider_list(raw_body: bytes) -> list[object]:
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PandaScoreResponseError("PandaScore returned invalid JSON.") from exc

    if not isinstance(decoded, list):
        raise PandaScoreResponseError(
            "PandaScore returned unexpected JSON shape: expected a list."
        )
    return decoded


def _team_payload(item: Mapping[str, object]) -> Mapping[str, object]:
    team = item.get("team")
    if isinstance(team, Mapping):
        return team
    return item


def _players_from_item(
    item: Mapping[str, object],
    team: Mapping[str, object],
) -> tuple[list[PlayerIdentity], int]:
    players: list[PlayerIdentity] = []
    missing_player_ids = 0
    for player_payload in _player_items(item, team):
        if not isinstance(player_payload, Mapping):
            continue

        player_source_id = _object_id(player_payload)
        if player_source_id is None:
            missing_player_ids += 1
            continue

        players.append(
            PlayerIdentity(
                source=PANDASCORE_HISTORY_SOURCE,
                source_player_id=player_source_id,
                name=_object_name(player_payload) or player_source_id,
            )
        )
    return players, missing_player_ids


def _player_items(
    item: Mapping[str, object],
    team: Mapping[str, object],
) -> list[object]:
    for value in (item.get("players"), item.get("roster"), team.get("players")):
        if isinstance(value, list):
            return value
    return []


def _coach_from_item(
    item: Mapping[str, object],
    team: Mapping[str, object],
) -> RosterCoach | None:
    for value in (item.get("coach"), team.get("coach")):
        if isinstance(value, Mapping):
            source_coach_id = _object_id(value)
            if source_coach_id is None:
                return None
            name = _object_name(value)
            if name is None:
                return None
            return RosterCoach(
                source=PANDASCORE_HISTORY_SOURCE,
                source_coach_id=source_coach_id,
                name=name,
            )

    for value in (item.get("coaches"), team.get("coaches")):
        if isinstance(value, list):
            return _first_coach(value)

    return None


def _first_coach(values: Iterable[object]) -> RosterCoach | None:
    for value in values:
        if not isinstance(value, Mapping):
            continue
        source_coach_id = _object_id(value)
        if source_coach_id is None:
            continue
        name = _object_name(value)
        if name is None:
            continue
        return RosterCoach(
            source=PANDASCORE_HISTORY_SOURCE,
            source_coach_id=source_coach_id,
            name=name,
        )
    return None


def _object_name(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return (
        _display_text(
            _text_from_value(value.get("full_name"))
            or _text_from_value(value.get("name"))
            or _text_from_value(value.get("slug"))
        )
        or None
    )


def _object_id(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _text_from_value(value.get("id"))
