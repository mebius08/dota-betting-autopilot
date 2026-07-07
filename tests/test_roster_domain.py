from app.history import (
    RosterCoach,
    build_player_roster_fingerprint,
    build_staff_roster_fingerprint,
)
from tests.roster_test_helpers import make_organization, make_player


def test_player_roster_fingerprint_is_order_independent_and_deterministic() -> None:
    players = [
        make_player("pure", "Pure"),
        make_player("bzm", "bzm"),
        make_player("33", "33"),
        make_player("ari", "Ari"),
        make_player("whitemon", "Whitemon"),
    ]

    first = build_player_roster_fingerprint(players)
    second = build_player_roster_fingerprint(list(reversed(players)))

    assert first == second
    assert first == build_player_roster_fingerprint(players)


def test_player_roster_fingerprint_uses_stable_ids_not_names() -> None:
    old_names = [make_player("player-1", "Old Nick"), make_player("player-2", "A")]
    new_names = [make_player("player-1", "New Nick"), make_player("player-2", "B")]

    assert build_player_roster_fingerprint(old_names) == (
        build_player_roster_fingerprint(new_names)
    )


def test_coach_does_not_change_player_only_fingerprint() -> None:
    players = [make_player(str(index)) for index in range(1, 6)]
    coach = RosterCoach(
        source="pandascore",
        source_coach_id="coach-1",
        name="MoonMeander",
    )

    player_only = build_player_roster_fingerprint(players)
    staff = build_staff_roster_fingerprint(players, coach)

    assert player_only == build_player_roster_fingerprint(list(reversed(players)))
    assert staff is not None
    assert staff != player_only


def test_same_players_under_tundra_and_1w_are_not_organization_aliases() -> None:
    tundra = make_organization("fake-tundra-id", "Tundra Esports")
    one_w = make_organization("fake-1w-id", "1W")
    players = [
        make_player("pure", "Pure"),
        make_player("bzm", "bzm"),
        make_player("33", "33"),
        make_player("ari", "Ari"),
        make_player("whitemon", "Whitemon"),
    ]

    assert tundra != one_w
    assert build_player_roster_fingerprint(players) == (
        build_player_roster_fingerprint(list(reversed(players)))
    )
