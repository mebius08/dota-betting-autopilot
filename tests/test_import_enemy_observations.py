from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
import csv
import json
import sqlite3

import psycopg
import pytest

from tests.test_import_hero_snapshots import Cursor as HeroCursor, Database
from tests.test_import_replay_batch import sources as sources
from tools import import_enemy_observations as cli
from tools import import_hero_snapshots as heroes


@pytest.fixture
def inputs(sources, tmp_path, monkeypatch):
    for module in (cli, heroes):
        monkeypatch.setattr(module, 'ROOT', tmp_path)
        monkeypatch.setattr(module, 'OUTPUT', tmp_path / 'output')
    def make(mid=1):
        entry = sources(mid)
        artifact = next(a for a in entry['artifacts'] if a['path'].endswith('/enemy_observations.csv'))
        path = tmp_path / artifact['path']
        with path.open() as f:
            rows = list(csv.DictReader(f))
        first = '0.000200000000000000123456789'
        for row in rows:
            if row['replay_tick'] == '1000':
                row['game_time_seconds'] = first
            key = row['pov_team'], row['enemy_player_slot']
            if key in (('RADIANT', '5'), ('DIRE', '0')):
                x = '0.12345678901234567890123456789' if key[0] == 'RADIANT' else '20.25'
                current = row['replay_tick'] == '1000'
                row.update(visibility_state='VISIBLE' if current or key[0] == 'DIRE' else 'HIDDEN',
                           current_position_status='OBSERVED' if current else
                               'VISIBLE_POSITION_UNAVAILABLE' if key[0] == 'DIRE' else 'HIDDEN',
                           current_x=x if current else '', current_y='17' if current else '',
                           last_seen_status='OBSERVED_NOW' if current else 'LAST_SEEN',
                           last_seen_game_time_seconds=first, last_seen_x=x, last_seen_y='17',
                           last_seen_age_seconds='0' if current else str(Decimal(row['game_time_seconds']) - Decimal(first)))
            if key == ('RADIANT', '6') and row['replay_tick'] != '1000':
                row.update(visibility_state='UNKNOWN', current_position_status='UNKNOWN')
        write_rows(path, rows)
        artifact['sha256'] = heroes.sha256(path)
        return entry, (mid, entry['dem_sha256'], artifact)
    return make


def write_rows(path, rows, fields=cli.ENEMY_COLUMNS):
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def change(record, root, mutate):
    path = root / record[2]['path']
    with path.open() as f:
        rows = list(csv.DictReader(f))
    mutate(rows)
    write_rows(path, rows)
    record[2]['sha256'] = heroes.sha256(path)


def test_observer_isolation_exact_decimal_null_and_last_seen(inputs):
    data = cli.prepare(inputs()[1])
    rows = [dict(zip(cli.COLUMNS, row, strict=True)) for row in data.rows]
    assert len(rows) == 20 and len(data.frames) == 4 and len(data.roster) == 10
    first, hidden = [r for r in rows if r['pov_team'] == 'RADIANT' and r['enemy_player_slot'] == 5]
    assert first['actual_game_time_seconds'] == Decimal('0.000200000000000000123456789')
    assert first['current_x'] == Decimal('0.12345678901234567890123456789')
    assert hidden['enemy_team'] == 'DIRE' and hidden['enemy_hero_id'] == 6
    assert hidden['current_x'] is hidden['current_y'] is None
    assert hidden['last_seen_x'] == first['current_x']
    assert hidden['last_seen_game_time_seconds'] == first['actual_game_time_seconds']
    dire = next(r for r in rows if r['pov_team'] == 'DIRE' and r['replay_tick'] == 2800 and r['enemy_player_slot'] == 0)
    assert dire['enemy_team'] == 'RADIANT' and dire['enemy_hero_id'] == 1
    assert dire['visibility_state'] == 'VISIBLE' and dire['current_x'] is None
    assert dire['current_position_status'] == 'VISIBLE_POSITION_UNAVAILABLE'
    assert dire['last_seen_x'] == Decimal('20.25') != hidden['last_seen_x']
    assert {r['provenance'] for r in rows} == {'HISTORICAL_TEAM_POV'}
    assert any(r['visibility_state'] == 'UNKNOWN' for r in rows)
    assert not {'life_state', 'global_x', 'target', 'future_delta', 'prediction'} & set(cli.COLUMNS)


@pytest.mark.parametrize('damage', [
    'match', 'sha', 'tick', 'time', 'side', 'hero', 'duplicate', 'incomplete',
    'hidden_position', 'future_seen', 'invented_seen', 'seen_from_other_observer',
    'age', 'null_pair', 'nonfinite', 'status', 'backwards',
])
def test_invalid_or_leaking_rows_are_rejected(inputs, tmp_path, damage):
    _, record = inputs()
    def mutate(rows):
        r = next(r for r in rows if r['pov_team'] == 'RADIANT' and r['enemy_player_slot'] == '5' and r['replay_tick'] == '2800')
        if damage == 'match':
            r['match_id'] = '2'
        elif damage == 'sha':
            r['replay_sha256'] = 'f' * 64
        elif damage == 'tick':
            r['replay_tick'] = '2801'
        elif damage == 'time':
            r['game_time_seconds'] = '60.00031'
        elif damage == 'side':
            r['enemy_team'] = r['pov_team']
        elif damage == 'hero':
            r['enemy_hero_id'] = '7'
        elif damage == 'duplicate':
            rows.append(dict(r))
        elif damage == 'incomplete':
            rows.remove(r)
        elif damage == 'hidden_position':
            r['current_x'], r['current_y'] = '9999', '9999'
        elif damage == 'future_seen':
            r['last_seen_game_time_seconds'] = '60.00030000000000000000001'
        elif damage == 'invented_seen':
            r['last_seen_x'] = '9999'
        elif damage == 'seen_from_other_observer':
            r['last_seen_x'] = '20.25'
        elif damage == 'age':
            r['last_seen_age_seconds'] = '0'
        elif damage == 'null_pair':
            r['last_seen_y'] = ''
        elif damage == 'nonfinite':
            r['last_seen_x'] = 'NaN'
        elif damage == 'status':
            r['visibility_state'] = 'DEAD'
        elif damage == 'backwards':
            for row in rows:
                if row['replay_tick'] == '2800':
                    row['replay_tick'] = '999'
    change(record, tmp_path, mutate)
    with pytest.raises(ValueError):
        cli.prepare(record)


def test_header_future_fields_hash_and_manifest_roles(inputs, tmp_path):
    entry, record = inputs()
    path = tmp_path / record[2]['path']
    original = path.read_text()
    path.write_text(original.replace('last_seen_y', 'target'))
    with pytest.raises(ValueError, match='SHA'):
        cli.prepare(record)
    record[2]['sha256'] = heroes.sha256(path)
    with pytest.raises(ValueError, match='schema'):
        cli.prepare(record)
    path.write_text(original)
    record[2]['sha256'] = heroes.sha256(path)
    a, b = tmp_path / 'output/a.json', tmp_path / 'output/b.json'
    a.write_text(json.dumps(dict(matches=[entry])))
    b.write_text(json.dumps(dict(matches={'1': dict(entry, existing=True),
        '2': dict(match_id=2, status='PARSE_ERROR')})))
    records, excluded, protected = cli.discover((a, b))
    assert records == [record] and path in protected and a in protected
    assert excluded == [dict(match_id=2, status='PARSE_ERROR')]
    entry['artifacts'] = [a for a in entry['artifacts'] if not a['path'].endswith('/enemy_observations.csv')]
    b.write_text(json.dumps(dict(matches=[entry])))
    with pytest.raises(ValueError, match='one enemy'):
        cli.discover((b,))


class Cursor(HeroCursor):
    def fetchall(self):
        if self.result is not None:
            return self.result
        cols = [d[0] for d in self.raw.description]
        numeric = {'actual_game_time_seconds', 'current_x', 'current_y',
                   'last_seen_x', 'last_seen_y', 'last_seen_game_time_seconds', 'last_seen_age_seconds'}
        # Existing Team POV time uses DOUBLE, while the spatial clock uses TEXT
        # as a test double for NUMERIC; keep only actual NUMERIC columns Decimal.
        return [tuple(Decimal(v) if c in numeric and isinstance(v, str) else v
                      for c, v in zip(cols, row, strict=True)) for row in self.raw.fetchall()]


@pytest.fixture
def database(monkeypatch):
    with sqlite3.connect(':memory:', isolation_level=None) as raw:
        raw.execute('PRAGMA foreign_keys=ON')
        raw.executescript((cli.SCHEMA.parent / 'postgres_schema.sql').read_text())
        raw.executescript((cli.SCHEMA.parent / 'postgres_pov_schema.sql').read_text())
        raw.execute('CREATE TABLE historical_match_spatial_frames (source TEXT,source_match_id TEXT,actual_game_time_seconds TEXT,replay_tick BIGINT,replay_sha256 TEXT)')
        raw.execute('CREATE TABLE historical_match_hero_snapshots (source TEXT,source_match_id TEXT,player_slot INTEGER,team TEXT,hero_id INTEGER)')
        for table in ('historical_match_economy_snapshots','historical_match_death_predictions',
                      'opendota_pro_matches','historical_match_hero_items','historical_match_spatial_heroes'):
            raw.execute(f'CREATE TABLE {table} (sentinel TEXT)')
            raw.execute(f"INSERT INTO {table} VALUES ('GLOBAL_CANARY_9999')")
        db = Database(raw)
        @contextmanager
        def cursor():
            cr = raw.cursor()
            try:
                yield Cursor(db, cr)
            finally:
                cr.close()
        monkeypatch.setattr(db, 'cursor', cursor)
        execute = db.execute
        def exact_schema(text):
            # Preserve NUMERIC literals as TEXT only in this SQLite double;
            # numeric CHECK comparisons must not use lexical TEXT order.
            for expr in ('actual_game_time_seconds >= 0', 'last_seen_game_time_seconds >= 0',
                         'last_seen_age_seconds >= 0', 'last_seen_age_seconds = 0',
                         'last_seen_game_time_seconds <= actual_game_time_seconds'):
                parts = expr.split()
                parts[0] = f'CAST({parts[0]} AS REAL)'
                if parts[-1] == 'actual_game_time_seconds':
                    parts[-1] = 'CAST(actual_game_time_seconds AS REAL)'
                text = text.replace(expr, ' '.join(parts))
            execute(text.replace(' NUMERIC,', ' TEXT,'))
        monkeypatch.setattr(db, 'execute', exact_schema)
        yield db


def seed(db, data):
    mid = str(data.match_id)
    db.raw.execute("INSERT INTO historical_matches (id,source,source_match_id,started_at,team_a_name,team_b_name) VALUES (?,?,?,'2026-01-01','R','D')", (f'replay_clarity:{mid}', cli.SOURCE, mid))
    for (tick, team), (at, visible, positioned, hidden) in data.frames.items():
        db.raw.execute('INSERT INTO historical_match_pov_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (cli.SOURCE,mid,data.replay_sha256,float(at),tick,team,visible,positioned,hidden,
             None,None,'INSUFFICIENT_OWN_POSITIONS','NO_OWN_POSITION'))
    for at, tick in {(v[0],tick) for (tick,_),v in data.frames.items()}:
        db.raw.execute('INSERT INTO historical_match_spatial_frames VALUES (?,?,?,?,?)',
                       (cli.SOURCE,mid,str(at),tick,data.replay_sha256))
    for slot, team, hero in data.roster:
        db.raw.execute('INSERT INTO historical_match_hero_snapshots VALUES (?,?,?,?,?)',
                       (cli.SOURCE,mid,slot,team,hero))


def old_rows(db):
    names = [r[0] for r in db.raw.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] != cli.TABLE]
    return {t: db.raw.execute(f'SELECT * FROM {t}').fetchall() for t in names}


def test_bulk_insert_fk_decimal_idempotence_and_preservation(inputs, database, monkeypatch):
    data = cli.prepare(inputs()[1])
    seed(database, data)
    before = old_rows(database)
    database.execute(cli.SCHEMA.read_text())
    monkeypatch.setattr(heroes, 'PAGE_SIZE', 7)
    assert cli.apply_match(database, data) == 20
    assert cli.apply_match(database, data) == 0
    assert len([q for q,_ in database.events if q.startswith('INSERT')]) == 6
    assert before == old_rows(database)
    with database.cursor() as cr:
        cr.execute(f'SELECT actual_game_time_seconds,current_x FROM {cli.TABLE} WHERE replay_tick=1000 AND pov_team=%s AND enemy_player_slot=5', ('RADIANT',))
        assert cr.fetchone() == (Decimal('0.000200000000000000123456789'),Decimal('0.12345678901234567890123456789'))
    assert not any('historical_match_spatial_heroes' in q for q,_ in database.events)
    assert all('SELECT x' not in q for q,_ in database.events)
    with pytest.raises(psycopg.IntegrityError), database.transaction():
        with database.cursor() as cr:
            bad = (*data.rows[0][:2],9999,*data.rows[0][3:])
            heroes.insert_pages(cr,cli.TABLE,cli.COLUMNS,cli.KEY,[bad])
    database.raw.execute(f'UPDATE {cli.TABLE} SET last_seen_x=? WHERE current_x IS NULL AND last_seen_x IS NOT NULL', ('9999',))
    with pytest.raises(ValueError, match='conflict'):
        cli.apply_match(database, data)


def test_late_page_error_rolls_back_current_match_but_preserves_previous(inputs, database, monkeypatch):
    one, two = [cli.prepare(inputs(mid)[1]) for mid in (1,2)]
    seed(database,one)
    seed(database,two)
    database.execute(cli.SCHEMA.read_text())
    monkeypatch.setattr(heroes,'PAGE_SIZE',7)
    cli.apply_match(database,one)
    wrong = (*two.rows[-1][:2],9999,*two.rows[-1][3:])
    with pytest.raises(psycopg.IntegrityError):
        cli.apply_match(database,replace(two,rows=[*two.rows[:-1],wrong]))
    assert database.raw.execute(f'SELECT count(*) FROM {cli.TABLE}').fetchone()[0] == 20
    assert database.raw.execute(f'SELECT DISTINCT source_match_id FROM {cli.TABLE}').fetchall() == [('1',)]


@pytest.mark.parametrize('damage',['clock','aggregate','sha','roster'])
def test_existing_parent_identity_and_both_time_tick_boundaries(inputs,database,damage):
    data=cli.prepare(inputs()[1])
    seed(database,data)
    if damage == 'clock':
        database.raw.execute("UPDATE historical_match_spatial_frames SET actual_game_time_seconds='0.000200000000000000123456790' WHERE replay_tick=1000")
    elif damage == 'aggregate':
        database.raw.execute('UPDATE historical_match_pov_snapshots SET replay_tick=replay_tick+1')
    elif damage == 'sha':
        database.raw.execute('UPDATE historical_match_pov_snapshots SET replay_sha256=?',('f'*64,))
    else:
        database.raw.execute('UPDATE historical_match_hero_snapshots SET hero_id=999 WHERE player_slot=0')
    with pytest.raises(ValueError,match='conflict'):
        cli.check_parent(database,data)


def test_read_only_preflight_limit_resume_and_protected_inputs(inputs,database,tmp_path,monkeypatch):
    entries=[]
    for mid in (1,2):
        entry,record=inputs(mid)
        entries.append(entry)
        seed(database,cli.prepare(record))
    manifest=tmp_path/'output/manifest.json'
    manifest.write_text(json.dumps(dict(matches=entries)))
    monkeypatch.setattr(cli,'MANIFESTS',(manifest,))
    monkeypatch.setattr(cli,'postgres_connection_options_from_env',lambda: {})
    calls=[]
    def connect(**kwargs):
        calls.append(kwargs)
        return database
    monkeypatch.setattr(cli.psycopg,'connect',connect)
    before=old_rows(database)
    database.events.clear()
    assert cli.main([])==0
    assert len(calls)==1 and calls[0]['options']=='-c default_transaction_read_only=on'
    assert all(q.startswith('SELECT') for q,_ in database.events)
    assert database.raw.execute('SELECT name FROM sqlite_master WHERE name=?',(cli.TABLE,)).fetchone() is None
    assert cli.main(['--apply','--limit','1'])==0
    assert database.raw.execute(f'SELECT count(*) FROM {cli.TABLE}').fetchone()[0]==20
    assert cli.main(['--apply','--limit','1'])==0
    assert cli.main(['--apply'])==0
    report=json.loads((tmp_path/'output/postgres-enemy-import/report.json').read_text())
    assert report['planned_missing']==0 and [m['status'] for m in report['matches']]==['SKIPPED','SKIPPED']
    assert before==old_rows(database)
    with pytest.raises(SystemExit):
        cli.main(['--report',str(tmp_path/entries[0]['artifacts'][0]['path'])])
