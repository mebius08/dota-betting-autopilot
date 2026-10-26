from contextlib import contextmanager
from decimal import Decimal
import json
import sqlite3

import psycopg
import pytest

from tests.test_import_replay_batch import sources as sources
from tools import import_hero_snapshots as cli


@pytest.fixture
def inputs(sources, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "OUTPUT", tmp_path / "output")

    def make(mid=1):
        entry = sources(mid)
        artifact = next(a for a in entry["artifacts"] if a["path"].endswith(".compact.json"))
        path = tmp_path / artifact["path"]
        raw = json.loads(path.read_text())
        raw["radiant_team_id"] = None
        for frame in raw["snapshots"]:
            for hero in frame["players"]:
                hero["items"] = [dict(inventory_slot=0, item_name="CDOTA_Item_IronwoodBranch")]
        # Keep a high-precision source literal through fixture serialization.
        path.write_text(json.dumps(raw).replace('"source_game_time_seconds": 0.0002',
                                                '"source_game_time_seconds": 0.000200000000000000123456789'))
        artifact["sha256"] = cli.sha256(path)
        return entry, (mid, entry["dem_sha256"], artifact)
    return make


def test_exact_decimal_identity_null_provenance_and_slot_values(inputs):
    _, record = inputs()
    data = cli.prepare(record)
    first = dict(zip(cli.HERO_COLUMNS, data.heroes[0], strict=True))
    assert first["actual_game_time_seconds"] == Decimal("0.000200000000000000123456789")
    assert first["player_slot"] == 0 and first["hero_id"] == 1 and first["team"] == "RADIANT"
    assert first["team_source_id"] is None
    assert first["provenance"] == "HISTORICAL_ORACLE"
    assert first["clock_normalization_status"] == "PROVEN"
    assert first["current_gold_status"] == "COMPONENT_COMPLETENESS_UNVERIFIED"
    assert len(data.heroes) == len(data.items) == 20
    assert data.items[0] == ("replay_clarity", "1", 1000, 0, 0, "CDOTA_Item_IronwoodBranch")
    assert data.items[10][2] == 2800  # Same item presence in another snapshot, not a purchase.
    assert "item_id" not in cli.ITEM_COLUMNS


@pytest.mark.parametrize("damage", ["duplicate_slot", "team", "roster", "item_slot", "sha", "null_stat"])
def test_invalid_sources_never_become_valid_snapshots(inputs, tmp_path, damage):
    _, record = inputs()
    path = tmp_path / record[2]["path"]
    raw = json.loads(path.read_text())
    if damage == "duplicate_slot":
        raw["snapshots"][0]["players"][1]["player_slot"] = 0
    elif damage == "team":
        raw["snapshots"][0]["players"][0]["team"] = "DIRE"
    elif damage == "roster":
        raw["snapshots"][1]["players"][0]["hero_id"] = 111
    elif damage == "item_slot":
        raw["snapshots"][0]["players"][0]["items"].append(
            dict(inventory_slot=0, item_name="CDOTA_Item_Tango"))
    elif damage == "null_stat":
        raw["snapshots"][0]["players"][0]["level"] = None
    else:
        raw["replay_sha256"] = "f" * 64
    path.write_text(json.dumps(raw))
    record[2]["sha256"] = cli.sha256(path)
    with pytest.raises(ValueError):
        cli.prepare(record)


def test_missing_inventory_entry_is_not_filled_or_called_empty(inputs, tmp_path):
    _, record = inputs()
    path = tmp_path / record[2]["path"]
    raw = json.loads(path.read_text())
    raw["snapshots"][0]["players"][0]["items"] = []
    path.write_text(json.dumps(raw))
    record[2]["sha256"] = cli.sha256(path)
    data = cli.prepare(record)
    assert len(data.items) == 19
    assert data.heroes[0][-1] == "EXPORTED_ENTRIES_ONLY"
    assert not any(r[:4] == data.heroes[0][:4] for r in data.items)


def test_manifests_include_original_references_and_deduplicate_batches(inputs, tmp_path):
    one, _ = inputs(1)
    two, _ = inputs(2)
    p1, p2 = tmp_path / "output/first.json", tmp_path / "output/second.json"
    p1.write_text(json.dumps(dict(matches=[dict(one, status="PASSED")])))
    p2.write_text(json.dumps(dict(matches={
        "1": dict(one, existing=True), "2": dict(two, existing=True),
        "3": dict(match_id=3, status="PARSE_ERROR"),
    })))
    records, excluded, protected = cli.discover([p1, p2])
    assert [r[0] for r in records] == [1, 2]
    assert excluded == [dict(match_id=3, status="PARSE_ERROR")]
    assert p1 in protected and tmp_path / one["dem_path"] in protected
    doc = json.loads(p2.read_text())
    doc["matches"]["1"]["dem_sha256"] = "f" * 64
    p2.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="Conflicting"):
        cli.discover([p1, p2])


class Cursor:
    def __init__(self, db, raw):
        self.db, self.raw, self.result = db, raw, None

    def execute(self, query, params=()):
        text = query if isinstance(query, str) else query.as_string()
        self.db.events.append((text, params))
        self.result = None
        if text == "SELECT to_regclass(%s)":
            exists = self.db.raw.execute("SELECT name FROM sqlite_master WHERE name=?", params).fetchone()
            self.result = [(params[0] if exists else None,)]
        else:
            if self.db.fail_items and text.startswith('INSERT INTO "historical_match_hero_items"'):
                raise psycopg.IntegrityError("item insert failed")
            try:
                self.raw.execute(text.replace("%s", "?"),
                                 tuple(str(v) if isinstance(v, Decimal) else v for v in params))
            except sqlite3.IntegrityError as exc:
                raise psycopg.IntegrityError(str(exc)) from exc
        return self

    def fetchall(self):
        if self.result is not None:
            return self.result
        cols = [d[0] for d in self.raw.description]
        return [tuple(Decimal(v) if c == "actual_game_time_seconds" and isinstance(v, str) else v
                      for c, v in zip(cols, row, strict=True)) for row in self.raw.fetchall()]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    @property
    def rowcount(self):
        return self.raw.rowcount


class Database:
    def __init__(self, raw):
        self.raw, self.events, self.fail_items = raw, [], False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def transaction(self):
        self.raw.execute("BEGIN")
        try:
            yield
            self.raw.commit()
        except BaseException:
            self.raw.rollback()
            raise

    @contextmanager
    def cursor(self):
        raw = self.raw.cursor()
        try:
            yield Cursor(self, raw)
        finally:
            raw.close()

    def execute(self, text):
        if text.startswith("SET TRANSACTION"):
            return
        # Test-only NUMERIC-as-TEXT preserves Decimal exactly in SQLite doubles.
        for statement in '\n'.join(line for line in text.splitlines()
                                   if not line.strip().startswith('--')).split(';'):
            if statement.strip():
                self.raw.execute(statement.replace(" NUMERIC ", " TEXT "))

    def add_parent(self, data):
        self.raw.execute("INSERT INTO historical_matches (id,source,source_match_id,started_at,team_a_name,team_b_name) VALUES (?,?,?,'2026-01-01','R','D')",
                         (f'replay_clarity:{data.match_id}', 'replay_clarity', str(data.match_id)))
        for tick, at in sorted({(r[2], float(r[4])) for r in data.heroes}):
            self.raw.execute("INSERT INTO historical_match_economy_snapshots VALUES (?,?,?,?,?)",
                             ('replay_clarity', str(data.match_id), at, tick, data.replay_sha256))

    def counts(self):
        return [self.raw.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t, _, _ in cli.TABLES]


@pytest.fixture
def database():
    with sqlite3.connect(':memory:', isolation_level=None) as raw:
        raw.execute('PRAGMA foreign_keys=ON')
        raw.executescript((cli.ROOT / 'app/storage/postgres_schema.sql').read_text() if
                         (cli.ROOT / 'app/storage/postgres_schema.sql').exists() else
                         (cli.SCHEMA.parent / 'postgres_schema.sql').read_text())
        raw.execute('CREATE TABLE historical_match_economy_snapshots (source TEXT, source_match_id TEXT, actual_game_time_seconds DOUBLE PRECISION, replay_tick BIGINT,replay_sha256 TEXT)')
        for name in ('historical_match_pov_snapshots','historical_match_death_predictions','opendota_pro_matches'):
            raw.execute(f'CREATE TABLE {name} (sentinel TEXT)')
            raw.execute(f"INSERT INTO {name} VALUES ('preserve')")
        db = Database(raw)
        yield db


def test_atomic_write_bulk_inserts_and_idempotence(inputs, database, monkeypatch):
    _, record = inputs()
    data = cli.prepare(record)
    database.add_parent(data)
    database.execute(cli.SCHEMA.read_text())
    monkeypatch.setattr(cli, 'PAGE_SIZE', 7)
    old = {t: database.raw.execute(f'SELECT * FROM {t}').fetchall() for t in
           ('historical_matches','historical_match_economy_snapshots','historical_match_pov_snapshots','historical_match_death_predictions','opendota_pro_matches')}
    assert list(cli.apply_match(database, data).values()) == [20, 20]
    writes = [e for e, _ in database.events if e.startswith('INSERT')]
    assert len(writes) == 6  # Three multi-row statements per table, not 40 individual inserts.
    assert list(cli.apply_match(database, data).values()) == [0, 0]
    assert database.counts() == [20, 20]
    at, team_id = database.raw.execute('SELECT actual_game_time_seconds,team_source_id FROM historical_match_hero_snapshots WHERE player_slot=0 AND replay_tick=1000').fetchone()
    assert Decimal(at) == Decimal('0.000200000000000000123456789') and team_id is None
    for table, values in old.items():
        assert database.raw.execute(f'SELECT * FROM {table}').fetchall() == values


def test_inventory_error_rolls_back_heroes_and_preserves_previous_match(inputs, database):
    first, second = cli.prepare(inputs(1)[1]), cli.prepare(inputs(2)[1])
    database.add_parent(first)
    database.add_parent(second)
    database.execute(cli.SCHEMA.read_text())
    cli.apply_match(database, first)
    database.fail_items = True
    with pytest.raises(psycopg.IntegrityError):
        cli.apply_match(database, second)
    assert database.counts() == [20, 20]
    assert database.raw.execute('SELECT DISTINCT source_match_id FROM historical_match_hero_snapshots').fetchall() == [('1',)]


def test_foreign_key_and_conflicting_values_are_not_overwritten(inputs, database):
    data = cli.prepare(inputs()[1])
    database.add_parent(data)
    database.execute(cli.SCHEMA.read_text())
    bad_items = [(*data.items[0][:2], 9999, *data.items[0][3:])]
    with database.transaction(), pytest.raises(psycopg.IntegrityError):
        with database.cursor() as cursor:
            cli.insert_pages(cursor, cli.ITEM_TABLE, cli.ITEM_COLUMNS, (*cli.KEY, 'inventory_slot'), bad_items)
    cli.apply_match(database, data)
    database.raw.execute('UPDATE historical_match_hero_items SET item_name=?', ('changed',))
    with pytest.raises(ValueError, match='conflict'):
        cli.apply_match(database, data)
    assert database.counts() == [20, 20]


def test_preflight_is_read_only_and_does_not_create_tables(inputs, database, tmp_path, monkeypatch):
    entry, record = inputs()
    data = cli.prepare(record)
    database.add_parent(data)
    manifest = tmp_path / 'output/manifest.json'
    manifest.write_text(json.dumps(dict(matches=[entry])))
    monkeypatch.setattr(cli, 'MANIFESTS', (manifest,))
    monkeypatch.setattr(cli, 'postgres_connection_options_from_env', lambda: {})
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return database

    monkeypatch.setattr(cli.psycopg, 'connect', connect)
    assert cli.main([]) == 0
    assert len(calls) == 1 and calls[0]['options'] == '-c default_transaction_read_only=on'
    assert all(text.startswith('SELECT') for text, _ in database.events)
    assert database.raw.execute("SELECT name FROM sqlite_master WHERE name=?", (cli.HERO_TABLE,)).fetchone() is None
    assert cli.main(['--apply','--limit','1']) == 0
    assert database.counts() == [20, 20]
    assert cli.main(['--apply','--limit','1']) == 0
    report = json.loads((tmp_path / 'output/postgres-hero-import/report.json').read_text())
    assert report['matches'] == [dict(match_id=1,status='SKIPPED')]
