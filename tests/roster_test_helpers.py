from datetime import datetime

from app.history import (
    PlayerIdentity,
    RosterCoach,
    RosterSnapshot,
    TeamOrganization,
    build_player_roster_fingerprint,
    build_roster_snapshot_id,
    build_roster_snapshot_source_id,
    build_staff_roster_fingerprint,
)


def make_player(source_player_id: str, name: str | None = None) -> PlayerIdentity:
    return PlayerIdentity(
        source="pandascore",
        source_player_id=source_player_id,
        name=name or f"Player {source_player_id}",
    )


def make_organization(
    source_team_id: str = "team-1",
    name: str = "Team Spirit",
) -> TeamOrganization:
    return TeamOrganization(
        source="pandascore",
        source_team_id=source_team_id,
        name=name,
    )


def make_coach(
    source_coach_id: str | None = "coach-1",
    name: str = "Coach",
) -> RosterCoach:
    return RosterCoach(
        source="pandascore",
        source_coach_id=source_coach_id,
        name=name,
    )


def make_roster_snapshot(
    snapshot_label: str = "snapshot-1",
    *,
    organization: TeamOrganization | None = None,
    players: list[PlayerIdentity] | None = None,
    coach: RosterCoach | None = None,
    observed_at: datetime | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    tournament_source_id: str | None = "300",
    tournament_name: str | None = "DreamLeague",
) -> RosterSnapshot:
    organization = organization or make_organization()
    players = players or [
        make_player("p1"),
        make_player("p2"),
        make_player("p3"),
        make_player("p4"),
        make_player("p5"),
    ]
    player_fingerprint = build_player_roster_fingerprint(players)
    staff_fingerprint = (
        build_staff_roster_fingerprint(players, coach)
        if coach is not None
        else None
    )
    source_snapshot_id = build_roster_snapshot_source_id(
        source="pandascore",
        source_context=f"tournament-{snapshot_label}",
        tournament_source_id=tournament_source_id,
        organization=organization,
        player_roster_fingerprint=player_fingerprint,
        staff_roster_fingerprint=staff_fingerprint,
        valid_from=valid_from,
        valid_until=valid_until,
    )
    return RosterSnapshot(
        id=build_roster_snapshot_id("pandascore", source_snapshot_id),
        source="pandascore",
        source_snapshot_id=source_snapshot_id,
        organization=organization,
        observed_at=observed_at or _dt("2026-01-02T00:00:00Z"),
        players=tuple(players),
        coach=coach,
        source_context=f"tournament-{snapshot_label}",
        tournament_source_id=tournament_source_id,
        tournament_name=tournament_name,
        valid_from=valid_from,
        valid_until=valid_until,
        player_roster_fingerprint=player_fingerprint,
        staff_roster_fingerprint=staff_fingerprint,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
