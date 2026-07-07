from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal


RosterMemberRole = Literal["player", "coach"]


@dataclass(frozen=True)
class PlayerIdentity:
    source: str
    source_player_id: str
    name: str


@dataclass(frozen=True)
class TeamOrganization:
    source: str
    source_team_id: str
    name: str


@dataclass(frozen=True)
class RosterCoach:
    source: str
    source_coach_id: str | None
    name: str


@dataclass(frozen=True)
class RosterMember:
    role: RosterMemberRole
    source: str
    source_member_id: str | None
    name: str
    player: PlayerIdentity | None = None


@dataclass(frozen=True)
class RosterSnapshot:
    id: str
    source: str
    source_snapshot_id: str
    organization: TeamOrganization
    observed_at: datetime
    players: tuple[PlayerIdentity, ...]
    coach: RosterCoach | None = None
    source_context: str | None = None
    tournament_source_id: str | None = None
    tournament_name: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    player_roster_fingerprint: str = ""
    staff_roster_fingerprint: str | None = None
    ingested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        players = dedupe_player_identities(self.players)
        if not players:
            raise ValueError("roster snapshot must contain at least one player")
        object.__setattr__(self, "players", players)

        player_fingerprint = self.player_roster_fingerprint
        if not player_fingerprint:
            player_fingerprint = build_player_roster_fingerprint(players)
            object.__setattr__(
                self,
                "player_roster_fingerprint",
                player_fingerprint,
            )

        if self.staff_roster_fingerprint is None and self.coach is not None:
            object.__setattr__(
                self,
                "staff_roster_fingerprint",
                build_staff_roster_fingerprint(players, self.coach),
            )


def build_identity_id(namespace: str, source: str, source_id: str) -> str:
    return _stable_digest(f"{namespace}:v1:{_identity_token(source, source_id)}")


def build_roster_snapshot_id(source: str, source_snapshot_id: str) -> str:
    return _stable_digest(
        f"roster-snapshot:v1:{_identity_token(source, source_snapshot_id)}"
    )


def build_player_roster_fingerprint(
    players: tuple[PlayerIdentity, ...] | list[PlayerIdentity],
) -> str:
    tokens = sorted(
        {
            _identity_token(player.source, player.source_player_id)
            for player in players
        }
    )
    return _stable_digest("player-roster:v1:" + "|".join(tokens))


def build_staff_roster_fingerprint(
    players: tuple[PlayerIdentity, ...] | list[PlayerIdentity],
    coach: RosterCoach,
) -> str | None:
    if coach.source_coach_id is None:
        return None

    player_part = build_player_roster_fingerprint(players)
    coach_part = _identity_token(coach.source, coach.source_coach_id)
    return _stable_digest(f"staff-roster:v1:{player_part}|coach:{coach_part}")


def build_roster_snapshot_source_id(
    *,
    source: str,
    source_context: str,
    tournament_source_id: str | None,
    organization: TeamOrganization,
    player_roster_fingerprint: str,
    staff_roster_fingerprint: str | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> str:
    tournament_part = tournament_source_id or "unknown-tournament"
    staff_part = staff_roster_fingerprint or "no-staff-fingerprint"
    valid_from_part = valid_from.isoformat() if valid_from is not None else "unknown"
    valid_until_part = (
        valid_until.isoformat() if valid_until is not None else "unknown"
    )
    return (
        f"source:{source.strip().casefold()}"
        f"|context:{source_context.strip().casefold()}"
        f"|tournament:{tournament_part}"
        f"|team:{_identity_token(organization.source, organization.source_team_id)}"
        f"|players:{player_roster_fingerprint}"
        f"|staff:{staff_part}"
        f"|valid_from:{valid_from_part}"
        f"|valid_until:{valid_until_part}"
    )


def dedupe_player_identities(
    players: tuple[PlayerIdentity, ...] | list[PlayerIdentity],
) -> tuple[PlayerIdentity, ...]:
    by_key: dict[str, PlayerIdentity] = {}
    for player in players:
        key = _identity_token(player.source, player.source_player_id)
        if key not in by_key:
            by_key[key] = player
    return tuple(by_key[key] for key in sorted(by_key))


def roster_members(snapshot: RosterSnapshot) -> tuple[RosterMember, ...]:
    members = [
        RosterMember(
            role="player",
            source=player.source,
            source_member_id=player.source_player_id,
            name=player.name,
            player=player,
        )
        for player in snapshot.players
    ]
    if snapshot.coach is not None:
        members.append(
            RosterMember(
                role="coach",
                source=snapshot.coach.source,
                source_member_id=snapshot.coach.source_coach_id,
                name=snapshot.coach.name,
                player=None,
            )
        )
    return tuple(members)


def _identity_token(source: str, source_id: str) -> str:
    return f"{source.strip().casefold()}:{source_id.strip()}"


def _stable_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
