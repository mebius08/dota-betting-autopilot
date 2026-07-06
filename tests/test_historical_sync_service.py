from datetime import datetime, timezone
from pathlib import Path

from app.history import HistoricalCollectionResult, sync_historical_matches
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_sync_inserts_valid_matches_and_counts_summary(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    collector = _FakeCollector(
        HistoricalCollectionResult(
            matches=[
                make_historical_match("history-1"),
                make_historical_match("history-2", winner_side=None),
            ],
            fetched_rows=3,
            skipped_rows=1,
            warnings=["one bad row"],
        )
    )

    result = sync_historical_matches(
        repository=repository,
        collector=collector,
        since=None,
        until=None,
        page_size=50,
        max_pages=10,
    )

    assert result.fetched_rows == 3
    assert result.mapped_matches == 2
    assert result.usable_matches == 1
    assert result.inserted == 2
    assert result.updated == 0
    assert result.skipped == 1
    assert repository.count_historical_matches() == 2
    assert repository.list_bets() == []


def test_repeated_sync_is_idempotent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match = make_historical_match("history-1")
    collector = _FakeCollector(
        HistoricalCollectionResult(
            matches=[match],
            fetched_rows=1,
            skipped_rows=0,
            warnings=[],
        )
    )

    first = sync_historical_matches(
        repository=repository,
        collector=collector,
        since=None,
        until=None,
        page_size=50,
        max_pages=10,
    )
    second = sync_historical_matches(
        repository=repository,
        collector=collector,
        since=None,
        until=None,
        page_size=50,
        max_pages=10,
    )

    assert first.inserted == 1
    assert second.unchanged == 1
    assert repository.count_historical_matches() == 1


def test_sync_updates_when_provider_returns_richer_metadata(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    incomplete = make_historical_match(
        "history-1",
        ended_at=None,
        winner_side=None,
    )
    complete = make_historical_match(
        "history-1",
        ended_at=datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
        winner_side="team_b",
    )

    sync_historical_matches(
        repository=repository,
        collector=_FakeCollector(
            HistoricalCollectionResult(
                matches=[incomplete],
                fetched_rows=1,
                skipped_rows=0,
                warnings=[],
            )
        ),
        since=None,
        until=None,
        page_size=50,
        max_pages=10,
    )
    result = sync_historical_matches(
        repository=repository,
        collector=_FakeCollector(
            HistoricalCollectionResult(
                matches=[complete],
                fetched_rows=1,
                skipped_rows=0,
                warnings=[],
            )
        ),
        since=None,
        until=None,
        page_size=50,
        max_pages=10,
    )

    assert result.updated == 1
    stored = repository.get_historical_match("history-1")
    assert stored is not None
    assert stored.winner_side == "team_b"


class _FakeCollector:
    def __init__(self, result: HistoricalCollectionResult) -> None:
        self.result = result

    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        max_pages: int,
    ) -> HistoricalCollectionResult:
        assert page_size == 50
        assert max_pages == 10
        return self.result
