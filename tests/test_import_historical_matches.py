from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import psycopg
import pytest

from app.storage import SQLiteRepository
from app.storage.postgres import CONNECTION_ENV
from tests.history_test_helpers import make_historical_match
from tools import import_historical_matches as importer


@pytest.fixture(autouse=True)
def no_real_postgres(monkeypatch):
    connect = MagicMock(
        side_effect=AssertionError("Real PostgreSQL access is forbidden")
    )
    monkeypatch.setattr(psycopg, "connect", connect)
    for name in CONNECTION_ENV.values():
        monkeypatch.delenv(name, raising=False)
    return connect


def source_catalog(tmp_path, count=1):
    path = tmp_path / "catalog.sqlite3"
    repository = SQLiteRepository(path)
    for index in range(count):
        match = make_historical_match(
            f"history-{index}", winner_side=None if index % 2 else "team_a"
        )
        if index % 2:
            match = replace(match, ended_at=None, tournament_name=None)
        repository.save_historical_match(match)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE private_notes (note TEXT)")
        connection.execute("INSERT INTO private_notes VALUES ('not-for-import')")
    return path


@pytest.fixture
def postgres_double(tmp_path, monkeypatch):
    """Transactional SQL double; all storage is temporary SQLite, never PostgreSQL."""
    target = tmp_path / "target.sqlite3"
    schema = Path("app/storage/postgres_schema.sql").read_text()
    with sqlite3.connect(target) as connection:
        connection.executescript(schema)
    settings = dict(
        host="test-host",
        port="5432",
        dbname="analytics",
        user="analytics",
        password="test-password",
    )
    for key, name in CONNECTION_ENV.items():
        monkeypatch.setenv(name, settings[key])
    calls = []

    class Cursor:
        def __init__(self, raw):
            self.raw = raw

        def executemany(self, query, rows):
            parameters = [
                tuple(
                    value.isoformat() if hasattr(value, "isoformat") else value
                    for value in row
                )
                for row in rows
            ]
            try:
                self.raw.executemany(query.as_string().replace("%s", "?"), parameters)
            except sqlite3.IntegrityError as exc:
                raise psycopg.IntegrityError("write constraint failed") from exc

        @property
        def rowcount(self):
            return self.raw.rowcount

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        @contextmanager
        def cursor(self):
            with closing(self.raw.cursor()) as cursor:
                yield Cursor(cursor)

    @contextmanager
    def connect(**kwargs):
        calls.append(kwargs)
        with closing(sqlite3.connect(target)) as connection:
            with connection:
                yield Connection(connection)

    monkeypatch.setattr(psycopg, "connect", connect)
    return target, calls, settings


@pytest.mark.parametrize("apply", [False, True])
def test_empty_source_is_successful_without_postgres_access(
    tmp_path, capsys, no_real_postgres, apply
):
    source = source_catalog(tmp_path, count=0)
    before = source.read_bytes()
    args = ["--sqlite-path", str(source)] + (["--apply"] if apply else [])
    assert importer.main(args) == 0
    assert "0" in capsys.readouterr().out
    assert source.read_bytes() == before
    no_real_postgres.assert_not_called()


def test_dry_run_validates_populated_catalog_without_writes(
    tmp_path, capsys, no_real_postgres
):
    source = source_catalog(tmp_path, count=2)
    before = source.read_bytes()
    assert importer.main(["--sqlite-path", str(source)]) == 0
    output = capsys.readouterr().out
    assert "DRY RUN: 2 historical matches; 24 compatible fields" in output
    assert "PostgreSQL was not contacted" in output
    assert source.read_bytes() == before
    no_real_postgres.assert_not_called()


def test_repeated_apply_preserves_all_fields_without_duplicates_or_other_tables(
    tmp_path, capsys, postgres_double
):
    source = source_catalog(tmp_path, count=2)
    before = source.read_bytes()
    target, calls, settings = postgres_double
    args = ["--sqlite-path", str(source), "--apply"]
    assert importer.main(args) == 0
    assert "inserted=2, skipped=0" in capsys.readouterr().out
    assert importer.main(args) == 0
    assert "inserted=0, skipped=2" in capsys.readouterr().out
    with sqlite3.connect(source) as connection:
        expected = connection.execute(
            "SELECT * FROM historical_matches ORDER BY id"
        ).fetchall()
    with sqlite3.connect(target) as connection:
        assert (
            connection.execute(
                "SELECT * FROM historical_matches ORDER BY id"
            ).fetchall()
            == expected
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("historical_matches",)]
    assert source.read_bytes() == before
    assert len(calls) == 2
    assert all(call == {**settings, "connect_timeout": 5} for call in calls)


def test_existing_source_identity_is_skipped_without_overwriting_target(
    tmp_path, capsys, postgres_double
):
    source = source_catalog(tmp_path)
    target, _, _ = postgres_double
    with sqlite3.connect(target) as connection:
        connection.execute(
            "INSERT INTO historical_matches "
            "(id, source, source_match_id, started_at, team_a_name, team_b_name) "
            "VALUES ('target-id', 'pandascore', 'history-0', '2026-01-01', 'Existing A', 'Existing B')"
        )
    assert importer.main(["--sqlite-path", str(source), "--apply"]) == 0
    assert "inserted=0, skipped=1" in capsys.readouterr().out
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT id, team_a_name FROM historical_matches"
        ).fetchall() == [("target-id", "Existing A")]


def test_write_error_rolls_back_every_new_row(tmp_path, capsys, postgres_double):
    source = source_catalog(tmp_path, count=2)
    target, _, _ = postgres_double
    with sqlite3.connect(target) as connection:
        connection.execute(
            "INSERT INTO historical_matches "
            "(id, source, source_match_id, started_at, team_a_name, team_b_name) "
            "VALUES ('history-1', 'other-source', 'other-id', '2026-01-01', 'Existing A', 'Existing B')"
        )
    assert importer.main(["--sqlite-path", str(source), "--apply"]) == 1
    assert "Import failed: write constraint failed" in capsys.readouterr().err
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT id, source FROM historical_matches"
        ).fetchall() == [("history-1", "other-source")]


@pytest.mark.parametrize(
    "kind", ["missing", "missing-table", "missing-field", "not-sqlite"]
)
def test_incompatible_source_fails_without_creation_or_mutation(
    tmp_path, capsys, no_real_postgres, kind
):
    path = tmp_path / "source.sqlite3"
    if kind == "missing-table":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
    elif kind == "missing-field":
        path = source_catalog(tmp_path)
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE historical_matches DROP COLUMN best_of")
    elif kind == "not-sqlite":
        path.write_text("not a SQLite database")
    before = path.read_bytes() if path.exists() else None
    assert importer.main(["--sqlite-path", str(path)]) == 1
    assert "Import failed:" in capsys.readouterr().err
    assert (path.read_bytes() if path.exists() else None) == before
    no_real_postgres.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("best_of", 3.5, "best_of"),
        ("best_of", 2**40, "PostgreSQL INTEGER"),
        ("winner_side", "unknown", "winner_side"),
        ("started_at", "2026-01-01T10:00:00", "timezone"),
        ("status", "bad\x00text", "NUL"),
    ],
)
def test_incompatible_values_are_rejected_before_postgres(
    tmp_path, capsys, no_real_postgres, field, value, message
):
    source = source_catalog(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(f"UPDATE historical_matches SET {field} = ?", (value,))
    before = source.read_bytes()
    assert importer.main(["--sqlite-path", str(source), "--apply"]) == 1
    assert message in capsys.readouterr().err
    assert source.read_bytes() == before
    no_real_postgres.assert_not_called()


def test_missing_destination_settings_fail_only_when_applying(
    tmp_path, capsys, no_real_postgres
):
    source = source_catalog(tmp_path)
    assert importer.main(["--sqlite-path", str(source), "--apply"]) == 1
    assert "Missing PostgreSQL settings" in capsys.readouterr().err
    no_real_postgres.assert_not_called()
