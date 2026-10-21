from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import psycopg
import pytest

from app.replay_trajectory import AGGREGATE_FIELDS
from app.storage.postgres import CONNECTION_ENV
from tools import import_compact_economy as importer


def compact_fixture(mid):
    snapshots = []
    for minute in range(2):
        players = []
        for slot in range(10):
            player = dict(
                player_slot=slot,
                team="RADIANT" if slot < 5 else "DIRE",
                hero_id=slot + 1,
                hero_name=f"hero_{slot + 1}",
                level=1,
                items=[],
                **{name: 0 for name in AGGREGATE_FIELDS},
            )
            player.update(
                net_worth=500 + slot + minute,
                current_gold=20 + slot,
                total_xp=100 * minute + slot,
            )
            players.append(player)
        teams = [
            dict(
                team=side,
                **{
                    name: sum(p[name] for p in players if p["team"] == side)
                    for name in AGGREGATE_FIELDS
                },
            )
            for side in ("DIRE", "RADIANT")
        ]
        snapshots.append(
            dict(
                game_time_seconds=minute * 60,
                source_game_time_seconds=minute * 60 + (0.0003 if minute else 0.0002),
                replay_tick=1000 + minute * 1800,
                game_state=5,
                teams=teams,
                players=players,
            )
        )
    return dict(
        schema_version=1,
        match_id=mid,
        replay_sha256=f"{mid:064x}",
        clarity_version="4.0.1",
        game_mode=2,
        league_id=19719,
        winner=2,
        radiant_team_id=101,
        dire_team_id=202,
        radiant_team_tag="RAD",
        dire_team_tag="DIR",
        picks_bans=[],
        clock_normalization_status="PROVEN",
        clock_normalization_method="GAME_START_TIME_AND_PAUSE_TICKS",
        zero_replay_tick=1000,
        game_end_time_seconds=65.0,
        pause_ticks=0,
        pause_seconds=0.0,
        pause_witnessed=False,
        snapshots=snapshots,
    )


@pytest.fixture(autouse=True)
def no_real_postgres(monkeypatch):
    connect = MagicMock(
        side_effect=AssertionError("Real PostgreSQL access is forbidden")
    )
    monkeypatch.setattr(psycopg, "connect", connect)
    for name in CONNECTION_ENV.values():
        monkeypatch.delenv(name, raising=False)
    return connect


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    for mid, relative in importer.COMPACTS.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(compact_fixture(mid)))
    monkeypatch.setattr(importer, "ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def destination(tmp_path, monkeypatch):
    """Transactional SQL double with real uniqueness/FK checks; no PostgreSQL server."""
    path = tmp_path / "destination.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(Path("app/storage/postgres_schema.sql").read_text())
        connection.executemany(
            "INSERT INTO historical_matches "
            "(id, source, source_match_id, started_at, team_a_name, team_b_name) "
            "VALUES (?, ?, ?, '2026-01-01', 'RAD', 'DIR')",
            [
                (f"{importer.SOURCE}:{mid}", importer.SOURCE, str(mid))
                for mid in importer.COMPACTS
            ],
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
                raise psycopg.IntegrityError("snapshot constraint failed") from exc

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


def test_dry_run_preserves_actual_callback_time_ticks_and_economy(
    inputs, no_real_postgres, capsys
):
    before = {
        path: sha256(path.read_bytes()).hexdigest()
        for path in (inputs / relative for relative in importer.COMPACTS.values())
    }
    rows = importer.build_snapshots(inputs)
    assert len(rows) == 8
    for mid in importer.COMPACTS:
        snapshots = [row for row in rows if row["source_match_id"] == str(mid)]
        assert [row["actual_game_time_seconds"] for row in snapshots] == [
            0.0002,
            60.0003,
        ]
        assert [row["replay_tick"] for row in snapshots] == [1000, 2800]
        assert snapshots[0]["radiant_net_worth"] == sum(500 + slot for slot in range(5))
        assert snapshots[0]["dire_current_gold"] == sum(
            20 + slot for slot in range(5, 10)
        )
        assert snapshots[1]["radiant_total_xp"] == sum(100 + slot for slot in range(5))
        assert snapshots[0]["replay_sha256"] == f"{mid:064x}"
        assert "game_time_seconds" not in snapshots[0]
    assert importer.main([]) == 0
    assert "DRY RUN: 8 economic snapshots ready" in capsys.readouterr().out
    assert all(
        sha256(path.read_bytes()).hexdigest() == digest
        for path, digest in before.items()
    )
    no_real_postgres.assert_not_called()


def test_repeated_apply_is_idempotent_and_preserves_both_team_values(
    inputs, destination, capsys
):
    rows = importer.build_snapshots(inputs)
    assert importer.main(["--apply"]) == 0
    assert "inserted=8, skipped=0" in capsys.readouterr().out
    assert importer.main(["--apply"]) == 0
    assert "inserted=0, skipped=8" in capsys.readouterr().out
    with sqlite3.connect(destination) as connection:
        stored = connection.execute(
            "SELECT "
            + ", ".join(importer.COLUMNS)
            + " FROM historical_match_economy_snapshots"
        ).fetchall()
        assert set(stored) == {
            tuple(row[name] for name in importer.COLUMNS) for row in rows
        }
        assert (
            connection.execute("SELECT COUNT(*) FROM historical_matches").fetchone()[0]
            == 4
        )


def test_missing_catalog_parent_rolls_back_whole_batch_and_table(
    inputs, destination, capsys
):
    with sqlite3.connect(destination) as connection:
        connection.execute(
            "DELETE FROM historical_matches WHERE source_match_id = ?",
            (str(next(reversed(importer.COMPACTS))),),
        )
    assert importer.main(["--apply"]) == 1
    assert "snapshot constraint failed" in capsys.readouterr().err
    with sqlite3.connect(destination) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='historical_match_economy_snapshots'"
            ).fetchall()
            == []
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM historical_matches").fetchone()[0]
            == 3
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("radiant_net_worth", 999999),
        ("replay_sha256", "b" * 64),
        ("actual_game_time_seconds", 0.0004),
    ],
)
def test_conflicting_existing_data_is_not_overwritten_and_new_rows_roll_back(
    inputs, destination, capsys, field, value
):
    assert importer.main(["--apply"]) == 0
    capsys.readouterr()
    mid = str(next(iter(importer.COMPACTS)))
    with sqlite3.connect(destination) as connection:
        connection.execute(
            f"UPDATE historical_match_economy_snapshots SET {field} = ? WHERE source_match_id = ? AND replay_tick = 1000",
            (value, mid),
        )
        connection.execute(
            "DELETE FROM historical_match_economy_snapshots WHERE source_match_id = ? AND replay_tick = 2800",
            (mid,),
        )
        before = connection.execute(
            "SELECT * FROM historical_match_economy_snapshots ORDER BY source_match_id, replay_tick"
        ).fetchall()
    assert importer.main(["--apply"]) == 1
    assert "conflict with the canonical" in capsys.readouterr().err
    with sqlite3.connect(destination) as connection:
        assert (
            connection.execute(
                "SELECT * FROM historical_match_economy_snapshots ORDER BY source_match_id, replay_tick"
            ).fetchall()
            == before
        )


@pytest.mark.parametrize(
    "problem",
    [
        "missing_metric",
        "null_metric",
        "duplicate_tick",
        "duplicate_time",
        "unproven_clock",
        "wrong_match",
        "overflow_tick",
    ],
)
def test_invalid_input_is_rejected_before_connecting(
    inputs, no_real_postgres, capsys, problem
):
    mid = next(iter(importer.COMPACTS))
    path = inputs / importer.COMPACTS[mid]
    document = json.loads(path.read_text())
    if problem == "missing_metric":
        del document["snapshots"][0]["teams"][0]["net_worth"]
    elif problem == "null_metric":
        document["snapshots"][0]["teams"][0]["net_worth"] = None
    elif problem == "duplicate_tick":
        document["snapshots"][1]["replay_tick"] = document["snapshots"][0][
            "replay_tick"
        ]
    elif problem == "duplicate_time":
        document["snapshots"][1]["source_game_time_seconds"] = document["snapshots"][0][
            "source_game_time_seconds"
        ]
    elif problem == "unproven_clock":
        document["clock_normalization_status"] = "UNPROVEN"
    elif problem == "wrong_match":
        document["match_id"] = 123
    else:
        document["snapshots"][1]["replay_tick"] = 2**63
    path.write_text(json.dumps(document))
    assert importer.main(["--apply"]) == 1
    assert "Economy import failed:" in capsys.readouterr().err
    no_real_postgres.assert_not_called()
