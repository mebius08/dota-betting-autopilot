from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from app.history import RosterCoach
from app.storage import SQLiteRepository, get_connection
from tests.roster_test_helpers import (
    make_organization,
    make_player,
    make_roster_snapshot,
)


def test_player_identity_upsert_uses_source_and_provider_id(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    assert repository.upsert_player_identity(make_player("p1", "Old Nick")) == (
        "inserted"
    )
    assert repository.upsert_player_identity(make_player("p1", "Old Nick")) == (
        "unchanged"
    )
    assert repository.upsert_player_identity(make_player("p1", "New Nick")) == (
        "updated"
    )
    assert repository.upsert_player_identity(make_player("p2", "New Nick")) == (
        "inserted"
    )

    assert repository.count_players() == 2
    stored = repository.get_player_identity("pandascore", "p1")
    assert stored is not None
    assert stored.name == "New Nick"


def test_team_organization_upsert_does_not_alias_same_names(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    assert repository.upsert_team_organization(
        make_organization("org-1", "Old Name")
    ) == "inserted"
    assert repository.upsert_team_organization(
        make_organization("org-1", "New Name")
    ) == "updated"
    assert repository.upsert_team_organization(
        make_organization("org-2", "New Name")
    ) == "inserted"

    assert repository.count_team_organizations() == 2
    assert repository.get_team_organization("pandascore", "org-1") != (
        repository.get_team_organization("pandascore", "org-2")
    )


def test_roster_snapshot_and_memberships_are_idempotent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    players = [
        make_player("p1"),
        make_player("p2"),
        make_player("p3"),
        make_player("p4"),
        make_player("p5"),
        make_player("p1", "Duplicate"),
    ]
    snapshot = make_roster_snapshot(
        "main",
        players=players,
        coach=RosterCoach("pandascore", "coach-1", "Coach"),
    )

    assert repository.upsert_roster_snapshot(snapshot) == "inserted"
    assert repository.upsert_roster_snapshot(snapshot) == "unchanged"

    stored = repository.get_roster_snapshot(snapshot.id)
    assert stored is not None
    assert len(stored.players) == 5
    assert stored.coach is not None
    assert repository.count_players() == 5
    assert repository.count_team_organizations() == 1
    assert repository.count_roster_snapshots() == 1
    assert repository.count_roster_memberships(role="player") == 5
    assert repository.count_roster_memberships(role="coach") == 1


def test_repeated_identical_later_observation_preserves_earliest_observed_at(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    early = make_roster_snapshot(
        "same-key",
        observed_at=datetime.fromisoformat("2026-07-01T00:00:00+00:00"),
    )
    later_same_semantics = make_roster_snapshot(
        "same-key",
        observed_at=datetime.fromisoformat("2026-07-10T00:00:00+00:00"),
    )

    assert repository.upsert_roster_snapshot(early) == "inserted"
    assert repository.upsert_roster_snapshot(later_same_semantics) == "unchanged"

    stored = repository.get_roster_snapshot(early.id)
    assert stored is not None
    assert stored.observed_at == early.observed_at


def test_roster_snapshot_upsert_rolls_back_partial_membership_failure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    with closing(get_connection(db_path)) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_roster_membership_insert
            BEFORE INSERT ON roster_memberships
            BEGIN
                SELECT RAISE(ABORT, 'membership failure');
            END;
            """
        )
        connection.commit()

    with pytest.raises(sqlite3.IntegrityError):
        repository.upsert_roster_snapshot(make_roster_snapshot("main"))

    assert repository.count_players() == 0
    assert repository.count_team_organizations() == 0
    assert repository.count_roster_snapshots() == 0


def test_tundra_1w_foundation_semantics_do_not_merge_organizations(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    players = [
        make_player("pure", "Pure"),
        make_player("bzm", "bzm"),
        make_player("33", "33"),
        make_player("ari", "Ari"),
        make_player("whitemon", "Whitemon"),
    ]
    tundra_snapshot = make_roster_snapshot(
        "tundra",
        organization=make_organization("fake-tundra-id", "Tundra Esports"),
        players=players,
    )
    one_w_snapshot = make_roster_snapshot(
        "one-w",
        organization=make_organization("fake-1w-id", "1W"),
        players=list(reversed(players)),
    )

    repository.upsert_roster_snapshot(tundra_snapshot)
    repository.upsert_roster_snapshot(one_w_snapshot)

    assert repository.count_team_organizations() == 2
    assert tundra_snapshot.player_roster_fingerprint == (
        one_w_snapshot.player_roster_fingerprint
    )
    assert len(
        repository.list_roster_snapshots_containing_player("pandascore", "pure")
    ) == 2
    assert repository.list_roster_snapshots_for_organization(
        "pandascore",
        "fake-tundra-id",
    ) == [tundra_snapshot]
    assert repository.list_roster_snapshots_for_organization(
        "pandascore",
        "fake-1w-id",
    ) == [one_w_snapshot]


def test_heroic_lgd_foundation_semantics_keep_ancient_lgd_separate(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    transferred_players = [
        make_player("yuma", "Yuma"),
        make_player("tailung", "TaiLung"),
        make_player("wisper", "Wisper"),
        make_player("thiolicor", "Thiolicor"),
        make_player("kj", "KJ"),
    ]
    ancient_lgd_players = [
        make_player("old-lgd-1"),
        make_player("old-lgd-2"),
        make_player("old-lgd-3"),
        make_player("old-lgd-4"),
        make_player("old-lgd-5"),
    ]
    heroic_snapshot = make_roster_snapshot(
        "heroic",
        organization=make_organization("fake-heroic-id", "HEROIC"),
        players=transferred_players,
    )
    lgd_transferred_snapshot = make_roster_snapshot(
        "lgd-transferred",
        organization=make_organization("fake-lgd-id", "LGD Gaming"),
        players=transferred_players,
    )
    ancient_lgd_snapshot = make_roster_snapshot(
        "lgd-ancient",
        organization=make_organization("fake-lgd-id", "LGD Gaming"),
        players=ancient_lgd_players,
        tournament_source_id="100",
    )

    repository.upsert_roster_snapshot(heroic_snapshot)
    repository.upsert_roster_snapshot(lgd_transferred_snapshot)
    repository.upsert_roster_snapshot(ancient_lgd_snapshot)

    assert repository.count_team_organizations() == 2
    assert heroic_snapshot.player_roster_fingerprint == (
        lgd_transferred_snapshot.player_roster_fingerprint
    )
    assert heroic_snapshot.player_roster_fingerprint != (
        ancient_lgd_snapshot.player_roster_fingerprint
    )
    assert {
        snapshot.id
        for snapshot in repository.list_roster_snapshots_for_organization(
            "pandascore",
            "fake-lgd-id",
        )
    } == {ancient_lgd_snapshot.id, lgd_transferred_snapshot.id}
    assert len(
        repository.list_roster_snapshots_containing_player("pandascore", "yuma")
    ) == 2
