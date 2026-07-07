from datetime import datetime
from pathlib import Path

import pytest

from app.history import RosterCoach
from app.storage import SQLiteRepository
from tests.roster_test_helpers import (
    make_organization,
    make_player,
    make_roster_snapshot,
)


def test_roster_observed_before_cutoff_is_available(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    snapshot = make_roster_snapshot(
        "early",
        observed_at=_dt("2026-07-04T12:00:00Z"),
    )

    repository.upsert_roster_snapshot(snapshot)

    assert repository.list_roster_snapshots_available_before(
        _dt("2026-07-05T00:00:00Z")
    ) == [snapshot]


def test_roster_observed_after_or_exactly_at_cutoff_is_excluded(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    before = make_roster_snapshot(
        "before",
        observed_at=_dt("2026-07-04T23:59:59Z"),
    )
    exact = make_roster_snapshot(
        "exact",
        observed_at=_dt("2026-07-05T00:00:00Z"),
    )
    future = make_roster_snapshot(
        "future",
        observed_at=_dt("2026-07-10T00:00:00Z"),
    )

    for snapshot in (before, exact, future):
        repository.upsert_roster_snapshot(snapshot)

    assert repository.list_roster_snapshots_available_before(
        _dt("2026-07-05T00:00:00Z")
    ) == [before]


def test_later_snapshot_does_not_replace_earlier_old_prediction_context(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    organization = make_organization("org-1", "Team Spirit")
    early = make_roster_snapshot(
        "early",
        organization=organization,
        observed_at=_dt("2026-07-01T00:00:00Z"),
        players=[make_player(f"early-{index}") for index in range(1, 6)],
    )
    late = make_roster_snapshot(
        "late",
        organization=organization,
        observed_at=_dt("2026-07-10T00:00:00Z"),
        players=[make_player(f"late-{index}") for index in range(1, 6)],
    )

    repository.upsert_roster_snapshot(early)
    repository.upsert_roster_snapshot(late)

    assert repository.get_latest_roster_snapshot_for_organization_as_of(
        "pandascore",
        "org-1",
        _dt("2026-07-05T00:00:00Z"),
    ) == early
    assert repository.get_latest_roster_snapshot_for_organization_as_of(
        "pandascore",
        "org-1",
        _dt("2026-07-11T00:00:00Z"),
    ) == late


def test_later_conflicting_roster_semantics_do_not_mutate_early_observation(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    organization = make_organization("org-1", "Team Spirit")
    early = make_roster_snapshot(
        "same-key",
        organization=organization,
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )
    later_changed_players = make_roster_snapshot(
        "same-key",
        organization=organization,
        observed_at=_dt("2026-07-10T00:00:00Z"),
        players=[make_player(f"later-{index}") for index in range(1, 6)],
    )
    later_changed_players_same_key = later_changed_players.__class__(
        id=early.id,
        source=later_changed_players.source,
        source_snapshot_id=early.source_snapshot_id,
        organization=later_changed_players.organization,
        observed_at=later_changed_players.observed_at,
        players=later_changed_players.players,
        coach=later_changed_players.coach,
        source_context=later_changed_players.source_context,
        tournament_source_id=later_changed_players.tournament_source_id,
        tournament_name=later_changed_players.tournament_name,
        valid_from=later_changed_players.valid_from,
        valid_until=later_changed_players.valid_until,
        player_roster_fingerprint=later_changed_players.player_roster_fingerprint,
        staff_roster_fingerprint=later_changed_players.staff_roster_fingerprint,
    )

    repository.upsert_roster_snapshot(early)
    with pytest.raises(ValueError, match="Conflicting roster snapshot semantics"):
        repository.upsert_roster_snapshot(later_changed_players_same_key)

    assert repository.get_roster_snapshot(early.id) == early
    assert repository.get_latest_roster_snapshot_for_organization_as_of(
        "pandascore",
        "org-1",
        _dt("2026-07-05T00:00:00Z"),
    ) == early


def test_name_only_coach_later_observation_does_not_backfill_before_cutoff(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    early = make_roster_snapshot(
        "same-key",
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )
    later_name_only_coach = make_roster_snapshot(
        "same-key",
        observed_at=_dt("2026-07-10T00:00:00Z"),
        coach=RosterCoach("pandascore", None, "Name Only Coach"),
    )

    repository.upsert_roster_snapshot(early)
    with pytest.raises(ValueError, match="Conflicting roster snapshot semantics"):
        repository.upsert_roster_snapshot(later_name_only_coach)

    old_prediction = repository.get_latest_roster_snapshot_for_organization_as_of(
        "pandascore",
        "team-1",
        _dt("2026-07-05T00:00:00Z"),
    )
    assert old_prediction is not None
    assert old_prediction.coach is None


def test_later_learned_validity_does_not_backfill_earlier_snapshot_key(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    early = make_roster_snapshot(
        "same-key",
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )
    later_validity = make_roster_snapshot(
        "same-key",
        observed_at=_dt("2026-07-10T00:00:00Z"),
    )
    later_validity_with_same_key = later_validity.__class__(
        id=early.id,
        source=later_validity.source,
        source_snapshot_id=early.source_snapshot_id,
        organization=later_validity.organization,
        observed_at=later_validity.observed_at,
        players=later_validity.players,
        coach=later_validity.coach,
        source_context=later_validity.source_context,
        tournament_source_id=later_validity.tournament_source_id,
        tournament_name=later_validity.tournament_name,
        valid_from=later_validity.valid_from,
        valid_until=_dt("2026-07-15T00:00:00Z"),
        player_roster_fingerprint=later_validity.player_roster_fingerprint,
        staff_roster_fingerprint=later_validity.staff_roster_fingerprint,
    )

    repository.upsert_roster_snapshot(early)
    with pytest.raises(ValueError, match="Conflicting roster snapshot semantics"):
        repository.upsert_roster_snapshot(later_validity_with_same_key)

    old_prediction = repository.get_latest_roster_snapshot_for_organization_as_of(
        "pandascore",
        "team-1",
        _dt("2026-07-05T00:00:00Z"),
    )
    assert old_prediction is not None
    assert old_prediction.valid_until is None


def test_explicit_valid_from_after_cutoff_is_excluded(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    snapshot = make_roster_snapshot(
        "known-future-validity",
        observed_at=_dt("2026-07-01T00:00:00Z"),
        valid_from=_dt("2026-07-07T00:00:00Z"),
    )

    repository.upsert_roster_snapshot(snapshot)

    assert repository.list_roster_snapshots_available_before(
        _dt("2026-07-05T00:00:00Z")
    ) == []


def test_player_history_can_cross_organizations_without_merging_them(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    player = make_player("transfer-player", "Transfer")
    roster_a = [player, *[make_player(f"a-{index}") for index in range(1, 5)]]
    roster_b = [player, *[make_player(f"b-{index}") for index in range(1, 5)]]
    first = make_roster_snapshot(
        "org-a",
        organization=make_organization("org-a", "HEROIC"),
        players=roster_a,
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )
    second = make_roster_snapshot(
        "org-b",
        organization=make_organization("org-b", "LGD Gaming"),
        players=roster_b,
        observed_at=_dt("2026-07-02T00:00:00Z"),
    )

    repository.upsert_roster_snapshot(first)
    repository.upsert_roster_snapshot(second)

    history = repository.list_roster_snapshots_containing_player(
        "pandascore",
        "transfer-player",
    )
    assert [snapshot.organization.source_team_id for snapshot in history] == [
        "org-a",
        "org-b",
    ]
    assert repository.count_team_organizations() == 2


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
