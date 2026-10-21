from collections import Counter
from contextlib import closing, contextmanager
import csv
from hashlib import sha256
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import psycopg
import pytest

from app.storage.postgres import CONNECTION_ENV
from tools import import_pov_features as importer


def write_csv(path, rows, columns=importer.TEAM_COLUMNS):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture(autouse=True)
def no_real_postgres(monkeypatch):
    connect = MagicMock(side_effect=AssertionError("Real PostgreSQL is forbidden"))
    monkeypatch.setattr(psycopg, "connect", connect)
    for name in CONNECTION_ENV.values():
        monkeypatch.delenv(name, raising=False)
    return connect


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    rows = []
    for mid, count in importer.EXPECTED_ROWS.items():
        for frame in range(count // 2):
            for team in importer.TEAMS:
                row = dict(
                    match_id=str(mid), replay_sha256=f"{mid:064x}",
                    game_time_seconds=str(frame * 10 + (0.0334 if frame else 0)),
                    replay_tick=str(1000 + frame * 300), pov_team=team,
                    own_alive_hero_count="5", own_valid_position_count="5",
                    own_unknown_life_state_count="0", own_team_spread="1234.56789",
                    visible_enemy_count="3", visible_enemy_with_position_count="2",
                    hidden_enemy_count="2", unknown_enemy_count="0",
                    nearest_visible_enemy_distance="2345.67891",
                    own_team_spread_status="OK", nearest_visible_enemy_status="OK",
                )
                if team == "DIRE":
                    row.update(
                        visible_enemy_count="0", visible_enemy_with_position_count="0",
                        hidden_enemy_count="4", unknown_enemy_count="1",
                        nearest_visible_enemy_distance="",
                        nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION",
                    )
                elif frame == 0:
                    row.update(own_team_spread="0", nearest_visible_enemy_distance="0")
                elif frame == 1:
                    # Retained flags with no verified living own/enemy positions.
                    row.update(
                        own_alive_hero_count="0", own_valid_position_count="0",
                        visible_enemy_with_position_count="0", own_team_spread="",
                        nearest_visible_enemy_distance="",
                        own_team_spread_status="INSUFFICIENT_OWN_POSITIONS",
                        nearest_visible_enemy_status="NO_OWN_POSITION",
                    )
                rows.append(row)
    path = tmp_path / "pov-features.csv"
    write_csv(path, rows)
    monkeypatch.setattr(importer, "INPUT_PATH", path)
    return path, rows


@pytest.fixture
def destination(tmp_path, monkeypatch):
    """Execute compatible SQL with real constraints/rollback in a temporary double."""
    path = tmp_path / "destination.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(Path("app/storage/postgres_schema.sql").read_text())
        connection.executemany(
            "INSERT INTO historical_matches "
            "(id, source, source_match_id, started_at, team_a_name, team_b_name) "
            "VALUES (?, ?, ?, '2026-01-01', 'RAD', 'DIR')",
            [(f"{importer.SOURCE}:{mid}", importer.SOURCE, str(mid))
             for mid in importer.EXPECTED_ROWS],
        )
    for name in CONNECTION_ENV.values():
        monkeypatch.setenv(name, "test-setting")

    class Cursor:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, query, parameters=()):
            text = query if isinstance(query, str) else query.as_string()
            self.raw.execute(text.replace("%s", "?"), parameters)

        def executemany(self, query, parameters):
            try:
                self.raw.executemany(query.as_string().replace("%s", "?"), parameters)
            except sqlite3.IntegrityError as exc:
                raise psycopg.IntegrityError("POV constraint failed") from exc

        @property
        def rowcount(self):
            return self.raw.rowcount

        def fetchall(self):
            return self.raw.fetchall()

    class Connection:
        def __init__(self, raw):
            self.raw = raw

        @contextmanager
        def cursor(self):
            with closing(self.raw.cursor()) as cursor:
                yield Cursor(cursor)

    @contextmanager
    def connect(**kwargs):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.execute("BEGIN")
                yield Connection(connection)

    monkeypatch.setattr(psycopg, "connect", connect)
    return path


def test_dry_run_copies_exact_team_frames_values_and_nulls(inputs, no_real_postgres, capsys):
    path, raw = inputs
    digest = sha256(path.read_bytes()).hexdigest()
    rows = importer.build_pov_rows(path)
    assert len(rows) == 2242
    assert Counter(int(row["source_match_id"]) for row in rows) == importer.EXPECTED_ROWS
    source = {(r["match_id"], int(r["replay_tick"]), r["pov_team"]): r for r in raw}
    for row in rows:
        original = source[row["source_match_id"], row["replay_tick"], row["pov_team"]]
        assert row["source"] == "replay_clarity"
        assert row["replay_sha256"] == original["replay_sha256"]
        assert row["actual_game_time_seconds"] == float(original["game_time_seconds"])
        for name in importer.FEATURES:
            expected = original[name]
            expected = expected if name.endswith("_status") else (
                None if expected == "" else float(expected)
            )
            assert row[name] == expected
        assert set(row) == set(importer.COLUMNS)
    assert any(r["actual_game_time_seconds"] == 10.0334 for r in rows)
    assert any(r["visible_enemy_count"] == 3 and r["visible_enemy_with_position_count"] == 0
               and r["nearest_visible_enemy_distance"] is None for r in rows)
    assert importer.main([]) == 0
    assert "DRY RUN: 2242 historical POV rows ready" in capsys.readouterr().out
    no_real_postgres.assert_not_called()
    assert sha256(path.read_bytes()).hexdigest() == digest


def test_apply_is_idempotent_and_preserves_nulls_and_catalog(inputs, destination, capsys):
    path, _ = inputs
    expected = {tuple(row[name] for name in importer.COLUMNS)
                for row in importer.build_pov_rows(path)}
    assert importer.main(["--csv-path", str(path), "--apply"]) == 0
    assert "inserted=2242, skipped=0" in capsys.readouterr().out
    assert importer.main(["--apply"]) == 0
    assert "inserted=0, skipped=2242" in capsys.readouterr().out
    with sqlite3.connect(destination) as connection:
        rows = connection.execute(
            "SELECT " + ", ".join(importer.COLUMNS) + " FROM historical_match_pov_snapshots"
        ).fetchall()
        assert set(rows) == expected
        assert connection.execute("SELECT COUNT(*) FROM historical_matches").fetchone()[0] == 4


def test_missing_parent_rolls_back_rows_and_new_table(inputs, destination, capsys):
    with sqlite3.connect(destination) as connection:
        connection.execute("DELETE FROM historical_matches WHERE source_match_id = '8960991322'")
    assert importer.main(["--apply"]) == 1
    assert "POV constraint failed" in capsys.readouterr().err
    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='historical_match_pov_snapshots'"
        ).fetchall() == []


@pytest.mark.parametrize(("column", "value"), [
    ("visible_enemy_count", 4), ("replay_sha256", "b" * 64),
    ("actual_game_time_seconds", 0.0001),
    ("nearest_visible_enemy_distance", 999),
])
def test_conflict_never_overwrites_data_and_rolls_back_gap_fills(
    inputs, destination, capsys, column, value
):
    assert importer.main(["--apply"]) == 0
    capsys.readouterr()
    with sqlite3.connect(destination) as connection:
        # visible=4 remains consistent with hidden=1 for this deliberately conflicting row.
        if column == "visible_enemy_count":
            connection.execute(
                "UPDATE historical_match_pov_snapshots SET hidden_enemy_count=1 "
                "WHERE source_match_id='8960991322' AND replay_tick=1000 AND pov_team='RADIANT'"
            )
        connection.execute(
            f"UPDATE historical_match_pov_snapshots SET {column}=? "
            "WHERE source_match_id='8960991322' AND replay_tick=1000 AND pov_team='RADIANT'",
            (value,),
        )
        connection.execute(
            "DELETE FROM historical_match_pov_snapshots "
            "WHERE source_match_id='8960991322' AND replay_tick=1300"
        )
        before = connection.execute("SELECT * FROM historical_match_pov_snapshots").fetchall()
    assert importer.main(["--apply"]) == 1
    assert "conflict with the source CSV" in capsys.readouterr().err
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT * FROM historical_match_pov_snapshots").fetchall() == before


@pytest.mark.parametrize("problem", [
    "empty", "truncated", "duplicate", "missing_pair", "time_disagreement",
    "tick_order", "wrong_sha", "sha_disagreement", "wrong_team", "overflow_tick",
    "null_count", "position_exceeds_flags", "null_with_ok", "nan", "wrong_status",
    "global_column", "future_column",
])
def test_invalid_or_incomplete_source_fails_before_connection(
    inputs, no_real_postgres, capsys, problem
):
    path, rows = inputs
    columns = importer.TEAM_COLUMNS
    if problem == "empty":
        rows = []
    elif problem == "truncated":
        rows = rows[:-2]
    elif problem == "duplicate":
        rows.append(rows[0].copy())
    elif problem == "missing_pair":
        rows[1]["replay_tick"] = "1001"
    elif problem == "time_disagreement":
        rows[1]["game_time_seconds"] = "0.0001"
    elif problem == "tick_order":
        for row in rows[2:4]:
            row["replay_tick"] = "999"
    elif problem == "wrong_sha":
        rows[0]["replay_sha256"] = "z" * 64
    elif problem == "sha_disagreement":
        rows[1]["replay_sha256"] = "b" * 64
    elif problem == "wrong_team":
        rows[0]["pov_team"] = "GLOBAL"
    elif problem == "overflow_tick":
        rows[0]["replay_tick"] = str(2**63)
    elif problem == "null_count":
        rows[0]["visible_enemy_count"] = ""
    elif problem == "position_exceeds_flags":
        rows[0]["visible_enemy_with_position_count"] = "4"
    elif problem == "null_with_ok":
        rows[0]["nearest_visible_enemy_distance"] = ""
    elif problem == "nan":
        rows[0]["own_team_spread"] = "NaN"
    elif problem == "wrong_status":
        rows[0]["own_team_spread_status"] = "UNVERIFIED"
    else:
        extra = "global_enemy_x" if problem == "global_column" else "own_deaths_next_30s"
        columns = (*columns, extra)
        rows = [dict(row, **{extra: "1"}) for row in rows]
    write_csv(path, rows, columns)
    assert importer.main(["--apply"]) == 1
    assert "POV import failed:" in capsys.readouterr().err
    no_real_postgres.assert_not_called()
