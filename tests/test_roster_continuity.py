from datetime import datetime

from app.history import (
    CoachContinuity,
    RosterChronologyPoint,
    RosterChronologySource,
    RosterContinuityEvaluator,
    RosterContinuityStrength,
)
from tests.roster_test_helpers import (
    make_coach,
    make_organization,
    make_player,
    make_roster_snapshot,
)


def test_exact_continuity_uses_stable_player_ids_across_organizations() -> None:
    previous_players = [make_player(f"p{index}") for index in range(1, 6)]
    current_players = list(reversed(previous_players))
    previous = make_roster_snapshot(
        "previous",
        organization=make_organization("org-a", "Tundra Esports"),
        players=previous_players,
    )
    current = make_roster_snapshot(
        "current",
        organization=make_organization("org-b", "1W"),
        players=current_players,
    )

    evidence = _evaluate(previous, current)

    assert evidence.continuity_strength is RosterContinuityStrength.EXACT
    assert evidence.auto_link_eligible is True
    assert evidence.exact_player_set_equality is True
    assert evidence.player_roster_fingerprint_equality is True
    assert evidence.overlap_count == 5
    assert evidence.same_organization is False


def test_strong_continuity_detects_normal_four_of_five_core_overlap() -> None:
    previous = make_roster_snapshot(
        "previous",
        players=[make_player(f"p{index}") for index in range(1, 6)],
    )
    current = make_roster_snapshot(
        "current",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p4"),
            make_player("p6"),
        ],
    )

    evidence = _evaluate(previous, current)

    assert evidence.continuity_strength is RosterContinuityStrength.STRONG
    assert evidence.auto_link_eligible is True
    assert evidence.overlap_count == 4


def test_additional_substitute_shape_remains_continuity_positive() -> None:
    previous = make_roster_snapshot(
        "previous",
        players=[make_player(f"p{index}") for index in range(1, 6)],
    )
    current = make_roster_snapshot(
        "current",
        players=[make_player(f"p{index}") for index in range(1, 7)],
    )

    evidence = _evaluate(previous, current)

    assert evidence.continuity_strength is RosterContinuityStrength.STRONG
    assert evidence.auto_link_eligible is True
    assert evidence.overlap_count == 5
    assert evidence.current_player_count == 6


def test_stable_coach_supports_qualifying_three_player_overlap() -> None:
    previous = make_roster_snapshot(
        "previous",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        coach=make_coach("coach-1", "MoonMeander"),
    )
    current = make_roster_snapshot(
        "current",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p6"),
            make_player("p7"),
        ],
        coach=make_coach("coach-1", "Different Display Name"),
    )

    evidence = _evaluate(previous, current)

    assert evidence.coach_continuity is CoachContinuity.SAME_STABLE_ID
    assert evidence.continuity_strength is RosterContinuityStrength.COACH_SUPPORTED
    assert evidence.auto_link_eligible is True


def test_three_of_five_without_stable_coach_continuity_stays_weak() -> None:
    previous = make_roster_snapshot(
        "previous",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        coach=make_coach(None, "Name Only Coach"),
    )
    current = make_roster_snapshot(
        "current",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p6"),
            make_player("p7"),
        ],
        coach=make_coach(None, "Name Only Coach"),
    )

    evidence = _evaluate(previous, current)

    assert evidence.coach_continuity is CoachContinuity.UNKNOWN
    assert evidence.continuity_strength is RosterContinuityStrength.WEAK
    assert evidence.auto_link_eligible is False


def test_different_stable_coach_does_not_support_three_player_overlap() -> None:
    previous = make_roster_snapshot(
        "previous",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        coach=make_coach("coach-1", "Coach"),
    )
    current = make_roster_snapshot(
        "current",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p6"),
            make_player("p7"),
        ],
        coach=make_coach("coach-2", "Coach"),
    )

    evidence = _evaluate(previous, current)

    assert evidence.coach_continuity is CoachContinuity.DIFFERENT_STABLE_ID
    assert evidence.continuity_strength is RosterContinuityStrength.WEAK
    assert evidence.auto_link_eligible is False


def test_coach_alone_is_insufficient_for_auto_link() -> None:
    previous = make_roster_snapshot(
        "previous",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        coach=make_coach("coach-1", "Coach"),
    )
    current = make_roster_snapshot(
        "current",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p6"),
            make_player("p7"),
            make_player("p8"),
        ],
        coach=make_coach("coach-1", "Coach"),
    )

    evidence = _evaluate(previous, current)

    assert evidence.coach_continuity is CoachContinuity.SAME_STABLE_ID
    assert evidence.continuity_strength is RosterContinuityStrength.NONE
    assert evidence.auto_link_eligible is False


def test_same_organization_is_insufficient_without_player_continuity() -> None:
    organization = make_organization("org-a", "Same Org")
    previous = make_roster_snapshot(
        "previous",
        organization=organization,
        players=[make_player(f"p{index}") for index in range(1, 6)],
    )
    current = make_roster_snapshot(
        "current",
        organization=organization,
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p6"),
            make_player("p7"),
            make_player("p8"),
        ],
    )

    evidence = _evaluate(previous, current)

    assert evidence.same_organization is True
    assert evidence.auto_link_eligible is False


def test_different_organization_does_not_block_strong_continuity() -> None:
    previous = make_roster_snapshot(
        "previous",
        organization=make_organization("org-a", "Org A"),
        players=[make_player(f"p{index}") for index in range(1, 6)],
    )
    current = make_roster_snapshot(
        "current",
        organization=make_organization("org-b", "Org B"),
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p4"),
            make_player("p6"),
        ],
    )

    evidence = _evaluate(previous, current)

    assert evidence.same_organization is False
    assert evidence.continuity_strength is RosterContinuityStrength.STRONG
    assert evidence.auto_link_eligible is True


def test_display_names_are_not_player_identity() -> None:
    previous = make_roster_snapshot(
        "previous",
        players=[make_player(f"old-{index}", f"Player {index}") for index in range(5)],
    )
    current = make_roster_snapshot(
        "current",
        players=[make_player(f"new-{index}", f"Player {index}") for index in range(5)],
    )

    evidence = _evaluate(previous, current)

    assert evidence.shared_stable_player_ids == ()
    assert evidence.continuity_strength is RosterContinuityStrength.NONE
    assert evidence.auto_link_eligible is False


def _evaluate(previous, current):
    return RosterContinuityEvaluator().evaluate(
        previous,
        current,
        previous_chronology=RosterChronologyPoint(
            previous.id,
            _dt("2025-01-01T00:00:00Z"),
            RosterChronologySource.OBSERVED_AT_FALLBACK,
        ),
        current_chronology=RosterChronologyPoint(
            current.id,
            _dt("2025-02-01T00:00:00Z"),
            RosterChronologySource.OBSERVED_AT_FALLBACK,
        ),
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
