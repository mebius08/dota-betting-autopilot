from contextlib import closing, contextmanager
import csv
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import psycopg
import pytest

from app.storage.postgres import CONNECTION_ENV
from tools import import_death_predictions as importer


MATCHES = (8957013760, 8958734008, 8960882635, 8960991322)


def write_csv(path, rows, columns=importer.INPUT_COLUMNS):
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
    for mid in MATCHES:
        for frame, time in enumerate(("0.033400000000000000123", "10.000001")):
            for team in importer.TEAMS:
                rows.append(dict(
                    match_id=str(mid), replay_tick=str(1000 + frame * 300),
                    game_time_seconds=time, pov_team=team, horizon_seconds="30",
                    constant_probability="0" if frame == 0 else "0.1672185430463576",
                    logistic_probability="1" if team == "RADIANT" else "0.24591682435703946",
                    target="1" if team == "RADIANT" else "0",
                ))
    path = tmp_path / "oof.csv"
    write_csv(path, rows)
    monkeypatch.setattr(importer, "INPUT_PATH", path)
    return path, rows


@pytest.fixture
def destination(tmp_path, monkeypatch):
    """Transactional SQL double; NUMERIC time uses TEXT to avoid SQLite rounding."""
    path = tmp_path / "destination.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(Path("app/storage/postgres_schema.sql").read_text())
        connection.executemany(
            "INSERT INTO historical_matches "
            "(id, source, source_match_id, started_at, team_a_name, team_b_name) "
            "VALUES (?, ?, ?, '2026-01-01', 'RAD', 'DIR')",
            [(f"{importer.SOURCE}:{mid}", importer.SOURCE, str(mid)) for mid in MATCHES],
        )
    for name in CONNECTION_ENV.values():
        monkeypatch.setenv(name, "test-setting")

    class Cursor:
        def __init__(self, raw):
            self.raw = raw

        def execute(self, query, parameters=()):
            text = query if isinstance(query, str) else query.as_string()
            text = text.replace("game_time_seconds NUMERIC", "game_time_seconds TEXT")
            self.raw.execute(text.replace("%s", "?"), parameters)

        def executemany(self, query, parameters):
            converted = [tuple(str(v) if isinstance(v, Decimal) else v for v in row)
                         for row in parameters]
            try:
                self.raw.executemany(query.as_string().replace("%s", "?"), converted)
            except sqlite3.IntegrityError as exc:
                raise psycopg.IntegrityError("prediction constraint failed") from exc

        @property
        def rowcount(self):
            return self.raw.rowcount

        def fetchall(self):
            return [(*row[:3], Decimal(row[3]), *row[4:]) for row in self.raw.fetchall()]

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


def test_dry_run_preserves_identity_values_exact_time_and_source(inputs, no_real_postgres, capsys):
    path, original = inputs
    digest = sha256(path.read_bytes()).hexdigest()
    rows = importer.build_predictions(path)
    assert len(rows) == len(original) == 16
    expected = {(r["match_id"], int(r["replay_tick"]), r["pov_team"]): r for r in original}
    for row in rows:
        raw = expected[row["source_match_id"], row["replay_tick"], row["pov_team"]]
        assert set(row) == set(importer.COLUMNS)
        assert row["source"] == "replay_clarity"
        assert row["prediction_origin"] == "HISTORICAL_OUT_OF_FOLD"
        assert row["game_time_seconds"] == Decimal(raw["game_time_seconds"])
        assert row["horizon_seconds"] == 30 and row["target"] == int(raw["target"])
        for field in ("constant_probability", "logistic_probability"):
            assert row[field] == float(raw[field])
    assert importer.main([]) == 0
    assert "DRY RUN: 16 historical OOF predictions ready" in capsys.readouterr().out
    no_real_postgres.assert_not_called()
    assert sha256(path.read_bytes()).hexdigest() == digest


def test_repeated_apply_is_idempotent_with_exact_time_and_provenance(inputs, destination, capsys):
    path, _ = inputs
    expected = {tuple(row[name] for name in importer.COLUMNS)
                for row in importer.build_predictions(path)}
    assert importer.main(["--csv-path", str(path), "--apply"]) == 0
    assert "inserted=16, skipped=0" in capsys.readouterr().out
    assert importer.main(["--apply"]) == 0
    assert "inserted=0, skipped=16" in capsys.readouterr().out
    with sqlite3.connect(destination) as connection:
        raw = connection.execute(
            "SELECT " + ", ".join(importer.COLUMNS) + " FROM historical_match_death_predictions"
        ).fetchall()
        stored = {(*row[:3], Decimal(row[3]), *row[4:]) for row in raw}
        assert stored == expected and len(raw) == 16
        assert connection.execute("SELECT COUNT(*) FROM historical_matches").fetchone()[0] == 4


def test_missing_catalog_match_rolls_back_entire_import_and_schema(inputs, destination, capsys):
    with sqlite3.connect(destination) as connection:
        connection.execute("DELETE FROM historical_matches WHERE source_match_id='8960991322'")
    assert importer.main(["--apply"]) == 1
    assert "prediction constraint failed" in capsys.readouterr().err
    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='historical_match_death_predictions'"
        ).fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM historical_matches").fetchone()[0] == 3


@pytest.mark.parametrize(("field", "value"), [
    ("constant_probability", .25), ("logistic_probability", .75),
    ("game_time_seconds", "0.033400000000000000124"), ("target", 0),
])
def test_conflicting_values_are_not_overwritten_and_new_rows_roll_back(
    inputs, destination, capsys, field, value
):
    assert importer.main(["--apply"]) == 0
    capsys.readouterr()
    with sqlite3.connect(destination) as connection:
        connection.execute(
            f"UPDATE historical_match_death_predictions SET {field}=? "
            "WHERE source_match_id='8960991322' AND replay_tick=1000 AND pov_team='RADIANT'",
            (value,),
        )
        connection.execute(
            "DELETE FROM historical_match_death_predictions "
            "WHERE source_match_id='8960991322' AND replay_tick=1300"
        )
        before = connection.execute("SELECT * FROM historical_match_death_predictions").fetchall()
    assert importer.main(["--apply"]) == 1
    assert "conflict with the source CSV" in capsys.readouterr().err
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT * FROM historical_match_death_predictions").fetchall() == before


@pytest.mark.parametrize("problem", [
    "empty", "duplicate", "missing_pair", "time_disagreement", "tick_order",
    "negative_time", "nan_time", "wrong_horizon", "wrong_target", "fractional_target",
    "wrong_team", "nan_probability", "infinite_probability", "negative_probability",
    "probability_above_one", "decimal_above_one", "overflow_tick", "zero_match",
    "missing_cell", "extra_column",
])
def test_invalid_csv_fails_before_postgres_connection(inputs, no_real_postgres, capsys, problem):
    path, rows = inputs
    columns = importer.INPUT_COLUMNS
    if problem == "empty":
        rows = []
    elif problem == "duplicate":
        rows.append(rows[0].copy())
    elif problem == "missing_pair":
        rows.pop(1)
    elif problem == "time_disagreement":
        rows[1]["game_time_seconds"] = "0.033400000000000000124"
    elif problem == "tick_order":
        rows[2]["replay_tick"] = rows[3]["replay_tick"] = "999"
    elif problem == "missing_cell":
        write_csv(path, rows)
        lines = path.read_text().splitlines()
        lines[1] = lines[1].rsplit(",", 1)[0]
        path.write_text("\n".join(lines) + "\n")
    elif problem == "extra_column":
        columns = (*columns, "global_enemy_x")
        rows = [dict(row, global_enemy_x="1") for row in rows]
    else:
        field, value = {
            "negative_time": ("game_time_seconds", "-1"),
            "nan_time": ("game_time_seconds", "NaN"),
            "wrong_horizon": ("horizon_seconds", "60"),
            "wrong_target": ("target", "2"),
            "fractional_target": ("target", "1.0"),
            "wrong_team": ("pov_team", "GLOBAL"),
            "nan_probability": ("constant_probability", "NaN"),
            "infinite_probability": ("logistic_probability", "Infinity"),
            "negative_probability": ("logistic_probability", "-0.1"),
            "probability_above_one": ("constant_probability", "1.1"),
            "decimal_above_one": ("constant_probability", "1.0000000000000000000000000001"),
            "overflow_tick": ("replay_tick", str(2**63)),
            "zero_match": ("match_id", "0"),
        }[problem]
        rows[0][field] = value
    if problem != "missing_cell":
        write_csv(path, rows, columns)
    assert importer.main(["--apply"]) == 1
    assert "Death prediction import failed:" in capsys.readouterr().err
    no_real_postgres.assert_not_called()
