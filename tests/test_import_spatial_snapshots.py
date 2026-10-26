from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import json
import re

import psycopg
import pytest

from tests.test_import_hero_snapshots import (
    Cursor as HeroCursor, database as database,
)
from tests.test_import_replay_batch import sources as sources
from tools import import_hero_snapshots as heroes
from tools import import_spatial_snapshots as cli


@pytest.fixture
def inputs(sources, tmp_path, monkeypatch):
    for module in (cli, heroes):
        monkeypatch.setattr(module, "ROOT", tmp_path)
        monkeypatch.setattr(module, "OUTPUT", tmp_path / "output")

    def make(mid=1):
        entry = sources(mid)
        spatial = next(a for a in entry["artifacts"] if a["path"].endswith(".compact.spatial.json"))
        compact = next(a for a in entry["artifacts"] if a["path"].endswith(".compact.json"))
        path = tmp_path / spatial["path"]
        doc = json.loads(path.read_text())
        doc["coordinate_system"]["units"] = "Source 2 world units"
        dead = doc["snapshots"][0]["players"][9]
        dead.update(life_state=2, position_status="HERO_NOT_ALIVE",
                    **{k: None for k in ("x", "y", "cell_x", "cell_y", "offset_x", "offset_y")})
        path.write_text(json.dumps(doc).replace(
            '0.0002', '0.000200000000000000123456789'))
        cpath = tmp_path / compact["path"]
        cpath.write_text(cpath.read_text().replace(
            '0.0002', '0.000200000000000000123456789'))
        for artifact in (spatial, compact):
            artifact["sha256"] = heroes.sha256(tmp_path / artifact["path"])
        return entry, (mid, entry["dem_sha256"], compact, spatial)
    return make


def update(record, root, change, index=3):
    artifact = record[index]
    path = root / artifact["path"]
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(re.sub(r'0\.0002[0-9]*', '0.000200000000000000123456789', json.dumps(doc)))
    artifact["sha256"] = heroes.sha256(path)


def test_exact_decimal_identity_coordinates_null_life_and_global_only(inputs):
    _, record = inputs()
    data = cli.prepare(record)
    assert len(data.frames) == 2 and len(data.players) == 20
    assert data.frames[0][3] == Decimal('0.000200000000000000123456789')
    assert data.frames[0][5] == 'HISTORICAL_ORACLE'
    observed = dict(zip(cli.HERO_COLUMNS, data.players[0], strict=True))
    dead = dict(zip(cli.HERO_COLUMNS, data.players[9], strict=True))
    assert (observed['source_match_id'], observed['replay_tick'], observed['player_slot']) == ('1', 1000, 0)
    assert observed['hero_id'] == 1 and observed['team'] == 'RADIANT'
    assert observed['x'] == 0 and observed['y'] == 0  # Genuine observed origin, not a NULL fill.
    assert dead['x'] is None and dead['y'] is None
    assert dead['position_status'] == 'HERO_NOT_ALIVE' and dead['life_state'] == 2
    assert dead['team'] == 'DIRE' and dead['hero_id'] == 10
    assert not {'pov_team', 'visibility_state', 'current_x', 'last_seen_x', 'target'} & set(cli.HERO_COLUMNS)


def test_decimal_coordinates_are_not_converted_to_float(inputs, tmp_path):
    _, record = inputs()
    path = tmp_path / record[3]['path']
    text = path.read_text().replace('"x": 0,', '"x": 0.000000000000000000123456789,')
    text = text.replace('"offset_x": 0,', '"offset_x": 0.000000000000000000123456789,')
    path.write_text(text)
    record[3]['sha256'] = heroes.sha256(path)
    p = dict(zip(cli.HERO_COLUMNS, cli.prepare(record).players[0], strict=True))
    assert p['x'] == p['offset_x'] == Decimal('0.000000000000000000123456789')


@pytest.mark.parametrize('damage', [
    'sha', 'match', 'replay_sha', 'clock', 'tick', 'time', 'incomplete',
    'slot', 'hero', 'team', 'entity', 'alive_null', 'dead_coordinates',
    'nonfinite', 'cell', 'pov_kind', 'backwards_clock',
])
def test_invalid_sources_fail_before_writes(inputs, tmp_path, damage):
    _, record = inputs()
    def change(d):
        first, second = d['snapshots']
        p = first['players'][0]
        if damage == 'match':
            d['match_id'] = 2
        elif damage == 'replay_sha':
            d['replay_sha256'] = 'f' * 64
        elif damage == 'clock':
            d['pause_ticks'] = 123
        elif damage == 'tick':
            second['replay_tick'] = first['replay_tick']
        elif damage == 'time':
            first['game_time_seconds'] = 10
        elif damage == 'incomplete':
            first['players'].pop()
        elif damage == 'slot':
            first['players'][1]['player_slot'] = 0
        elif damage == 'hero':
            first['players'][1]['hero_id'] = 1
        elif damage == 'team':
            p['team'] = 'DIRE'
        elif damage == 'entity':
            p['hero_entity_handle'] = 999
        elif damage == 'alive_null':
            p['x'] = None
        elif damage == 'dead_coordinates':
            p['position_status'], p['life_state'] = 'HERO_NOT_ALIVE', 2
        elif damage == 'nonfinite':
            p['x'] = float('nan')
        elif damage == 'cell':
            p['cell_x'] = 1
        elif damage == 'pov_kind':
            d['artifact_kind'] = 'replay_hero_visibility'
        elif damage == 'backwards_clock':
            # Both sidecar timestamps still increase; their union with compact
            # runs backwards at tick 2800 / time 60.0003.
            second['replay_tick'] = 2801
            second['source_game_time_seconds'] = second['game_time_seconds'] = 59
    if damage == 'sha':
        record[3]['sha256'] = 'f' * 64
    else:
        update(record, tmp_path, change)
    with pytest.raises(ValueError):
        cli.prepare(record)


def test_pauses_use_source_ticks_without_seconds_to_tick_arithmetic(inputs, tmp_path):
    _, record = inputs()
    for index in (2, 3):
        def change(d):
            d.update(pause_ticks=900, pause_seconds=30, pause_witnessed=True)
            d['snapshots'][1]['replay_tick'] = 3700
        update(record, tmp_path, change, index)
    data = cli.prepare(record)
    assert data.frames[1][2:4] == (3700, Decimal('60.0003'))


def test_manifests_include_originals_deduplicate_and_protect_sources(inputs, tmp_path):
    entry, _ = inputs()
    first, second = tmp_path / 'output/a.json', tmp_path / 'output/b.json'
    first.write_text(json.dumps(dict(matches=[entry])))
    second.write_text(json.dumps(dict(matches={'1': dict(entry, existing=True),
        '2': dict(match_id=2, status='PARSE_ERROR')})))
    records, excluded, protected = cli.discover((first, second))
    assert len(records) == 1 and records[0][0] == 1
    assert excluded == [dict(match_id=2, status='PARSE_ERROR')]
    assert tmp_path / records[0][3]['path'] in protected
    doc = json.loads(second.read_text())
    a = next(a for a in doc['matches']['1']['artifacts'] if a['path'].endswith('.compact.spatial.json'))
    a['sha256'] = 'f' * 64
    second.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='Conflicting spatial'):
        cli.discover((first, second))
    doc['matches']['1']['artifacts'].remove(a)
    second.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='one canonical'):
        cli.discover((second,))


class Cursor(HeroCursor):
    def execute(self, query, params=()):
        text = query if isinstance(query, str) else query.as_string()
        if self.db.fail_spatial and text.startswith(f'INSERT INTO "{cli.HERO_TABLE}"'):
            raise psycopg.IntegrityError('spatial insert failed')
        return super().execute(query, params)

    def fetchall(self):
        if self.result is not None:
            return self.result
        cols = [d[0] for d in self.raw.description]
        numeric = {'actual_game_time_seconds', 'x', 'y', 'offset_x', 'offset_y'}
        return [tuple(Decimal(v) if c in numeric and v is not None else v
                      for c, v in zip(cols, r, strict=True)) for r in self.raw.fetchall()]


@pytest.fixture
def spatial_db(database, monkeypatch):
    database.fail_spatial = False
    @contextmanager
    def cursor():
        raw = database.raw.cursor()
        try:
            yield Cursor(database, raw)
        finally:
            raw.close()
    monkeypatch.setattr(database, 'cursor', cursor)
    execute = database.execute
    def exact_schema(text):
        # SQLite TEXT is a test double for exact PostgreSQL NUMERIC. Its CHECK
        # comparisons need explicit numeric casts rather than TEXT ordering.
        for axis in ('x', 'y'):
            name = f'offset_{axis}'
            text = text.replace(f'{name} >= 0 AND {name} < 256',
                                f'CAST({name} AS REAL) >= 0 AND CAST({name} AS REAL) < 256')
        execute(text.replace(' NUMERIC,', ' TEXT,'))
    monkeypatch.setattr(database, 'execute', exact_schema)
    database.execute(heroes.SCHEMA.read_text())
    return database


def seed(db, record):
    data = heroes.prepare(record[:3])
    db.add_parent(data)
    heroes.apply_match(db, data)


def old_rows(db):
    tables = ('historical_matches', 'historical_match_economy_snapshots', 'historical_match_pov_snapshots',
              'historical_match_death_predictions', 'opendota_pro_matches',
              heroes.HERO_TABLE, heroes.ITEM_TABLE)
    return {t: db.raw.execute(f'SELECT * FROM {t}').fetchall() for t in tables}


def test_bulk_idempotence_nulls_fk_conflicts_and_old_table_preservation(inputs, spatial_db, monkeypatch):
    _, record = inputs()
    seed(spatial_db, record)
    before = old_rows(spatial_db)
    spatial_db.execute(cli.SCHEMA.read_text())
    data = cli.prepare(record)
    monkeypatch.setattr(heroes, 'PAGE_SIZE', 7)
    spatial_db.events.clear()
    assert list(cli.apply_match(spatial_db, data).values()) == [2, 20]
    assert len([t for t, _ in spatial_db.events if t.startswith('INSERT')]) == 4
    assert list(cli.apply_match(spatial_db, data).values()) == [0, 0]
    assert before == old_rows(spatial_db)
    assert spatial_db.raw.execute(f'SELECT x,y,life_state,position_status FROM {cli.HERO_TABLE} WHERE replay_tick=1000 AND player_slot=9').fetchone() == (None, None, 2, 'HERO_NOT_ALIVE')
    with pytest.raises(psycopg.IntegrityError), spatial_db.transaction():
        with spatial_db.cursor() as cursor:
            row = (*data.players[0][:2], 9999, *data.players[0][3:])
            heroes.insert_pages(cursor, cli.HERO_TABLE, cli.HERO_COLUMNS, (*cli.KEY, 'player_slot'), [row])
    spatial_db.raw.execute(f'UPDATE {cli.HERO_TABLE} SET hero_id=999 WHERE player_slot=0')
    with pytest.raises(ValueError, match='conflict'):
        cli.apply_match(spatial_db, data)


def test_failure_rolls_back_only_current_match_and_parent_identity_is_required(inputs, spatial_db):
    one, two = inputs(1)[1], inputs(2)[1]
    seed(spatial_db, one)
    seed(spatial_db, two)
    spatial_db.execute(cli.SCHEMA.read_text())
    first, second = cli.prepare(one), cli.prepare(two)
    cli.apply_match(spatial_db, first)
    spatial_db.fail_spatial = True
    with pytest.raises(psycopg.IntegrityError):
        cli.apply_match(spatial_db, second)
    assert spatial_db.raw.execute(f'SELECT count(*) FROM {cli.FRAME_TABLE}').fetchone()[0] == 2
    assert spatial_db.raw.execute(f'SELECT count(*) FROM {cli.HERO_TABLE}').fetchone()[0] == 20
    spatial_db.fail_spatial = False
    spatial_db.raw.execute(f'UPDATE {heroes.HERO_TABLE} SET replay_sha256=? WHERE source_match_id=?', ('f' * 64, '2'))
    with pytest.raises(ValueError, match='SHA/clock'):
        cli.apply_match(spatial_db, second)
    with pytest.raises(ValueError, match='absent'):
        cli.check_parent(spatial_db, cli.prepare(inputs(3)[1]))


def test_preflight_read_only_apply_limit_resume_and_no_source_overwrite(inputs, spatial_db, tmp_path, monkeypatch):
    entries = []
    for mid in (1, 2):
        entry, record = inputs(mid)
        entries.append(entry)
        seed(spatial_db, record)
    before = old_rows(spatial_db)
    manifest = tmp_path / 'output/manifest.json'
    manifest.write_text(json.dumps(dict(matches=entries)))
    monkeypatch.setattr(cli, 'MANIFESTS', (manifest,))
    monkeypatch.setattr(cli, 'postgres_connection_options_from_env', lambda: {})
    calls = []
    def connect(**kwargs):
        calls.append(kwargs)
        return spatial_db
    monkeypatch.setattr(cli.psycopg, 'connect', connect)
    spatial_db.events.clear()
    assert cli.main([]) == 0
    assert len(calls) == 1 and calls[0]['options'] == '-c default_transaction_read_only=on'
    assert all(t.startswith('SELECT') for t, _ in spatial_db.events)
    assert spatial_db.raw.execute('SELECT name FROM sqlite_master WHERE name=?', (cli.FRAME_TABLE,)).fetchone() is None
    assert cli.main(['--apply', '--limit', '1']) == 0
    assert cli.main(['--apply', '--limit', '1']) == 0  # Skip first, resume second.
    assert cli.main(['--apply']) == 0
    report = json.loads((tmp_path / 'output/postgres-spatial-import/report.json').read_text())
    assert report['planned_missing'] == {cli.FRAME_TABLE: 0, cli.HERO_TABLE: 0}
    assert [m['status'] for m in report['matches']] == ['SKIPPED', 'SKIPPED']
    assert before == old_rows(spatial_db)
    with pytest.raises(SystemExit):
        cli.main(['--report', str(tmp_path / entries[0]['artifacts'][0]['path'])])


def test_extra_team_pov_or_future_payload_is_not_copied(inputs, tmp_path):
    _, record = inputs()
    before = deepcopy(cli.prepare(record))
    def change(d):
        for f in d['snapshots']:
            f.update(last_seen_x=999, target=1)
            for p in f['players']:
                p.update(pov_team='RADIANT', current_x=999, last_seen_x=999, target=1)
    update(record, tmp_path, change)
    after = cli.prepare(record)
    assert before == after
