from pathlib import Path
import sqlite3

import pytest

from app import cli
from app.storage import SQLiteRepository
from app.tournaments import CompetitiveStage, TournamentRound
from tests.history_test_helpers import make_historical_match


def test_cli_help_includes_history_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "history-status" in output


def test_history_status_missing_db_is_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["history-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical matches: 0" in output
    assert "Usable winner records: 0" in output
    assert "No historical matches found." in output
    assert not db_path.exists()


def test_history_status_existing_db_without_history_table_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE legacy (id TEXT PRIMARY KEY)")

    exit_code = cli.main(["history-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical matches: 0" in output
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'historical_matches'
            """
        ).fetchone()
    assert row is None


def test_history_status_prints_synthetic_history_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    repository.save_historical_match(
        make_historical_match(
            "history-1",
            competitive_stage=CompetitiveStage.UPPER_BRACKET,
            normalized_round=TournamentRound.UPPER_BRACKET_FINAL,
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "history-2",
            team_a_name="BetBoom Team",
            team_a_source_id="30",
            winner_side=None,
            competitive_stage=CompetitiveStage.UNKNOWN,
            normalized_round=TournamentRound.UNKNOWN,
        )
    )

    exit_code = cli.main(["history-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical Dota dataset" in output
    assert "Historical matches: 2" in output
    assert "Usable winner records: 1" in output
    assert "Point-in-time ready matches: 2" in output
    assert "Unique teams: 3" in output
    assert "Unique tournaments: 1" in output
    assert "upper_bracket: 1" in output
    assert "unknown: 1" in output
