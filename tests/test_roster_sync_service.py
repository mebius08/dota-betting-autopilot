from datetime import datetime
from pathlib import Path

from app.history import RosterCollectionResult, sync_roster_history
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match
from tests.roster_test_helpers import make_roster_snapshot


def test_sync_roster_history_persists_snapshots_and_counts_summary(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        make_historical_match("match-1", tournament_source_id="300")
    )
    repository.save_historical_match(
        make_historical_match("match-2", tournament_source_id="300")
    )
    snapshot = make_roster_snapshot("main", tournament_source_id="300")
    collector = _FakeRosterCollector(
        RosterCollectionResult(
            snapshots=[snapshot],
            tournaments_requested=1,
            fetched_rows=1,
            skipped_records=2,
            warnings=["one skipped player"],
        )
    )

    result = sync_roster_history(
        repository=repository,
        collector=collector,
        max_tournaments=5,
    )

    assert collector.seen_tournament_source_ids == ["300"]
    assert collector.seen_max_tournaments == 5
    assert result.tournaments_requested == 1
    assert result.rosters_fetched == 1
    assert result.snapshots_inserted == 1
    assert result.unique_players_seen == 5
    assert result.unique_organizations_seen == 1
    assert result.skipped_records == 2
    assert repository.count_roster_snapshots() == 1
    assert repository.count_roster_memberships(role="player") == 5


def test_repeated_roster_sync_is_idempotent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        make_historical_match("match-1", tournament_source_id="300")
    )
    snapshot = make_roster_snapshot("main", tournament_source_id="300")
    collector = _FakeRosterCollector(
        RosterCollectionResult(
            snapshots=[snapshot],
            tournaments_requested=1,
            fetched_rows=1,
            skipped_records=0,
            warnings=[],
        )
    )

    first = sync_roster_history(
        repository=repository,
        collector=collector,
        max_tournaments=5,
    )
    second = sync_roster_history(
        repository=repository,
        collector=collector,
        max_tournaments=5,
    )

    assert first.snapshots_inserted == 1
    assert second.snapshots_unchanged == 1
    assert repository.count_roster_snapshots() == 1
    assert repository.count_players() == 5


def test_sync_without_historical_tournaments_does_not_call_provider(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    collector = _ExplodingRosterCollector()

    result = sync_roster_history(
        repository=repository,
        collector=collector,
        max_tournaments=5,
    )

    assert result.tournaments_requested == 0
    assert result.rosters_fetched == 0
    assert result.snapshots_inserted == 0
    assert result.warnings == ["No historical PandaScore tournament IDs found."]


def test_roster_sync_selects_recent_tournaments_before_lexical_provider_order(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        make_historical_match(
            "old-100",
            tournament_source_id="100",
            ended_at=_dt("2026-01-01T00:00:00Z"),
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "new-900",
            tournament_source_id="900",
            ended_at=_dt("2026-03-01T00:00:00Z"),
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "middle-500",
            tournament_source_id="500",
            ended_at=_dt("2026-02-01T00:00:00Z"),
        )
    )
    collector = _FakeRosterCollector(
        RosterCollectionResult(
            snapshots=[],
            tournaments_requested=2,
            fetched_rows=2,
            skipped_records=0,
            warnings=[],
        )
    )

    sync_roster_history(
        repository=repository,
        collector=collector,
        max_tournaments=2,
    )

    assert collector.seen_tournament_source_ids == ["900", "500"]


class _FakeRosterCollector:
    def __init__(self, result: RosterCollectionResult) -> None:
        self.result = result
        self.seen_tournament_source_ids: list[str] = []
        self.seen_max_tournaments = 0

    def collect(
        self,
        *,
        tournament_source_ids: list[str],
        max_tournaments: int,
    ) -> RosterCollectionResult:
        self.seen_tournament_source_ids = tournament_source_ids
        self.seen_max_tournaments = max_tournaments
        return self.result


class _ExplodingRosterCollector:
    def collect(
        self,
        *,
        tournament_source_ids: list[str],
        max_tournaments: int,
    ) -> RosterCollectionResult:
        raise AssertionError("provider should not be called")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
