from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.storage import SQLiteRepository, get_connection
from app.tournaments import CompetitiveStage, TournamentRound
from tests.history_test_helpers import make_historical_match


def test_historical_table_created(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path)

    with closing(get_connection(db_path)) as connection:
        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'historical_matches'
            """
        ).fetchone()

    assert row is not None


def test_insert_and_read_historical_match(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match = make_historical_match(
        "history-1",
        competitive_stage=CompetitiveStage.UPPER_BRACKET,
        normalized_round=TournamentRound.UPPER_BRACKET_FINAL,
    )

    result = repository.upsert_historical_match(match)

    assert result == "inserted"
    assert repository.count_historical_matches() == 1
    assert repository.count_historical_matches(usable_only=True) == 1
    assert repository.get_historical_match("history-1") == match
    by_source = repository.get_historical_match_by_source("pandascore", "history-1")
    assert by_source == match


def test_repeated_upsert_is_idempotent(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    first = make_historical_match("history-1")
    second = make_historical_match(
        "history-1",
        ingested_at=datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc),
    )

    assert repository.upsert_historical_match(first) == "inserted"
    assert repository.upsert_historical_match(second) == "unchanged"
    assert repository.count_historical_matches() == 1


def test_upsert_updates_richer_final_metadata(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    incomplete = make_historical_match(
        "history-1",
        ended_at=None,
        winner_side=None,
    )
    complete = make_historical_match(
        "history-1",
        ended_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        winner_side="team_b",
    )

    assert repository.upsert_historical_match(incomplete) == "inserted"
    assert repository.upsert_historical_match(complete) == "updated"

    stored = repository.get_historical_match("history-1")
    assert stored is not None
    assert stored.ended_at == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert stored.winner_side == "team_b"
    assert repository.count_historical_matches() == 1


def test_round_trips_stage_winner_and_timestamps(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match = make_historical_match(
        "history-1",
        competitive_stage=CompetitiveStage.LOWER_BRACKET,
        normalized_round=TournamentRound.LOWER_BRACKET_SEMIFINAL,
        raw_stage_label="Lower Bracket Semifinal",
        winner_side="team_b",
    )

    repository.save_historical_match(match)
    stored = repository.get_historical_match("history-1")

    assert stored is not None
    assert stored.competitive_stage is CompetitiveStage.LOWER_BRACKET
    assert stored.normalized_round is TournamentRound.LOWER_BRACKET_SEMIFINAL
    assert stored.winner_side == "team_b"
    assert stored.started_at == match.started_at
    assert stored.ended_at == match.ended_at


def test_api_token_is_never_stored(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    repository.save_historical_match(make_historical_match("history-1"))

    with closing(get_connection(db_path)) as connection:
        rows = connection.execute("SELECT * FROM historical_matches").fetchall()

    serialized_values = " ".join(
        str(value)
        for row in rows
        for value in dict(row).values()
        if value is not None
    )
    assert "PANDASCORE_TOKEN" not in serialized_values
    assert "secret-token" not in serialized_values


def test_empty_history_list_works(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    assert repository.list_historical_matches() == []
    assert repository.list_historical_matches_before(
        datetime(2026, 1, 1, tzinfo=timezone.utc)
    ) == []
