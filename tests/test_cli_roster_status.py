from pathlib import Path
import sqlite3

import pytest

from app import cli
import app.history
from app.storage import SQLiteRepository
from tests.roster_test_helpers import make_roster_snapshot


def test_cli_help_includes_roster_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "roster-status" in output


def test_roster_status_missing_db_is_friendly_and_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["roster-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Roster snapshots: 0" in output
    assert "No roster snapshots found." in output
    assert not db_path.exists()


def test_roster_status_existing_db_without_roster_table_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE legacy (id TEXT PRIMARY KEY)")

    exit_code = cli.main(["roster-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Roster snapshots: 0" in output
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'roster_snapshots'
            """
        ).fetchone()
    assert row is None


def test_roster_status_prints_populated_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path).upsert_roster_snapshot(make_roster_snapshot("main"))

    exit_code = cli.main(["roster-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Roster history dataset" in output
    assert "Players: 5" in output
    assert "Organizations: 1" in output
    assert "Roster snapshots: 1" in output
    assert "Player memberships: 5" in output
    assert "Coach memberships: 0" in output
    assert "Snapshots with temporal validity: 0" in output
    assert "Snapshots without explicit validity: 1" in output
    assert "Unique player-roster fingerprints: 1" in output


def test_roster_status_does_not_touch_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path)

    def exploding_provider(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider should not be constructed")

    monkeypatch.setattr(app.history, "PandaScoreRosterCollector", exploding_provider)

    exit_code = cli.main(["roster-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Roster snapshots: 0" in output
