from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import csv
import json
from pathlib import Path
import sqlite3

import psycopg
import pytest

from app.replay_pov_features import extract_pov_features
from tests.test_import_compact_economy import compact_fixture
from tests.test_replay_pov_features import _inputs
from tools import import_replay_batch as cli


AT = datetime(2026, 10, 26, tzinfo=timezone.utc)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "OUTPUT", tmp_path / "output")

    def make(mid):
        folder = tmp_path / "output" / str(mid)
        folder.mkdir(parents=True)
        dem = folder / f"{mid}.dem"
        dem.write_bytes(b"PBDEMS2\0" + str(mid).encode())
        sha = cli.sha256(dem)
        compact = compact_fixture(mid)
        compact["replay_sha256"] = sha
        spatial, visibility = _inputs(0.0002, 60.0003)
        clocks = ("clock_normalization_method", "zero_replay_tick", "pause_ticks",
                  "pause_seconds", "pause_witnessed", "game_end_time_seconds")
        for doc in (spatial, visibility):
            doc.update(match_id=mid, replay_sha256=sha,
                       **{k: compact[k] for k in clocks})
        metadata = dict(match_id=mid, leagueid=19719, start_time=1780000000,
                        duration=65, radiant_team_id=101, dire_team_id=202,
                        radiant_win=True, series_id=123,
                        league=dict(leagueid=19719, name="Selected league"))
        for name, doc in ((f"{mid}.compact.json", compact),
                          (f"{mid}.compact.spatial.json", spatial),
                          (f"{mid}.compact.visibility.json", visibility),
                          ("opendota.json", metadata)):
            (folder / name).write_text(json.dumps(doc))
        extract_pov_features(folder / f"{mid}.compact.spatial.json",
                             folder / f"{mid}.compact.visibility.json", folder / "pov")
        files = sorted(p for p in folder.rglob("*") if p.is_file())
        return dict(match_id=mid, league_id=19719, status="PARSE_OK", existing=False,
                    dem_path=str(dem.relative_to(tmp_path)), dem_sha256=sha,
                    channels=dict(economy_snapshots=2, pov_team_rows=4),
                    artifacts=[dict(path=str(p.relative_to(tmp_path)), sha256=cli.sha256(p))
                               for p in files])
    return make


def manifest(path, entries):
    path.write_text(json.dumps(dict(matches={str(e["match_id"]): e for e in entries})))
    return path


def refresh(entry, root, suffix):
    item = next(a for a in entry["artifacts"] if a["path"].endswith(suffix))
    item["sha256"] = cli.sha256(root / item["path"])


def test_manifest_status_filter_and_identity_conflicts(sources, tmp_path):
    one, two = sources(1), sources(2)
    first = manifest(tmp_path / "output/first.json", [one])
    second = manifest(tmp_path / "output/second.json", [dict(one, existing=True), two,
        dict(match_id=3, status="PARSE_ERROR"), dict(match_id=4, status="RETRYABLE_ERROR")])
    entries, excluded = cli.discover([first, second])
    assert [e["match_id"] for e in entries] == [1, 2]
    assert len(excluded) == 3
    second = manifest(second, [dict(one, existing=True, dem_sha256="f" * 64)])
    with pytest.raises(ValueError, match="Conflicting manifest"):
        cli.discover([first, second])


def test_prepare_preserves_actual_time_null_and_original_sources(sources, tmp_path):
    entry = sources(1)
    hashes = {a["path"]: a["sha256"] for a in entry["artifacts"]}
    bundle = cli.prepare(entry, AT)
    assert bundle.match.source_match_id == "1"
    assert bundle.match.team_a_name == "RAD" and bundle.match.winner_side == "team_a"
    assert bundle.match.ended_at is None and bundle.match.best_of is None
    assert bundle.match.series_source_id == "123"
    assert bundle.economy[0]["actual_game_time_seconds"] == 0.0002
    assert bundle.economy[1]["actual_game_time_seconds"] == 60.0003
    assert [r["pov_team"] for r in bundle.pov[:2]] == ["RADIANT", "DIRE"]
    assert all(r["nearest_visible_enemy_distance"] is None for r in bundle.pov)
    assert all(r["replay_sha256"] == entry["dem_sha256"] for r in bundle.pov)
    assert bundle.retained["hero_rows"] == 20
    assert bundle.retained["spatial_snapshots"] == 2
    assert bundle.retained["enemy_observation_rows"] == 20
    assert all(cli.sha256(tmp_path / path) == digest for path, digest in hashes.items())


@pytest.mark.parametrize("damage", ["sha", "match", "sidecar", "pov"])
def test_bad_sources_are_rejected_before_database_use(sources, tmp_path, damage):
    entry = sources(1)
    folder = tmp_path / "output/1"
    if damage == "sha":
        (folder / "1.compact.json").write_text("{}")
    elif damage == "match":
        path = folder / "opendota.json"
        doc = json.loads(path.read_text())
        doc["match_id"] = 2
        path.write_text(json.dumps(doc))
        refresh(entry, tmp_path, "/opendota.json")
    elif damage == "sidecar":
        path = folder / "1.compact.spatial.json"
        doc = json.loads(path.read_text())
        doc["replay_sha256"] = "f" * 64
        path.write_text(json.dumps(doc))
        refresh(entry, tmp_path, ".compact.spatial.json")
    else:
        path = folder / "pov/team_pov_features.csv"
        with path.open() as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            columns = reader.fieldnames
        rows[0]["own_team_spread"] = "12345"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        refresh(entry, tmp_path, "/team_pov_features.csv")
    with pytest.raises(ValueError):
        cli.prepare(entry, AT)


def test_different_source_durations_are_diagnostic_not_invented_end_time(sources, tmp_path):
    entry = sources(1)
    path = tmp_path / "output/1/opendota.json"
    doc = json.loads(path.read_text())
    doc["duration"] = 64
    path.write_text(json.dumps(doc))
    refresh(entry, tmp_path, "/opendota.json")
    bundle = cli.prepare(entry, AT)
    assert bundle.match.ended_at is None
    assert bundle.retained["duration_disagreement"]["opendota_duration_seconds"] == 64
    assert bundle.retained["duration_disagreement"]["compact_game_end_time_seconds"] == "65.0"
    assert bundle.economy[-1]["actual_game_time_seconds"] == 60.0003


class Cursor:
    def __init__(self, connection, raw):
        self.connection, self.raw = connection, raw

    def execute(self, query, params=()):
        text = query if isinstance(query, str) else query.as_string()
        self.connection.events.append(text)
        self.raw.execute(text.replace("%s", "?"), params)

    def executemany(self, query, rows):
        text = query.as_string()
        self.connection.events.append(text)
        rows = [tuple(v.isoformat() if isinstance(v, datetime) else v for v in r) for r in rows]
        if ("historical_match_pov_snapshots" in text and rows
                and rows[0][1] == self.connection.fail_pov_match):
            raise psycopg.IntegrityError("POV write failed")
        self.raw.executemany(text.replace("%s", "?"), rows)

    @property
    def rowcount(self):
        return self.raw.rowcount

    def fetchall(self):
        columns = [d[0] for d in self.raw.description]
        return [tuple(datetime.fromisoformat(v) if c in ("started_at", "ended_at", "ingested_at")
                      and isinstance(v, str) else v for c, v in zip(columns, row, strict=True))
                for row in self.raw.fetchall()]


class Connection:
    def __init__(self, raw):
        self.raw, self.events, self.fail_pov_match = raw, [], None

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

    def execute(self, query):
        assert query == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

    @contextmanager
    def cursor(self):
        raw = self.raw.cursor()
        try:
            yield Cursor(self, raw)
        finally:
            raw.close()

    def counts(self):
        return [self.raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table, _ in cli.TABLES]


@pytest.fixture
def database():
    with sqlite3.connect(":memory:", isolation_level=None) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        for schema in ("postgres_schema.sql", "postgres_economy_schema.sql", "postgres_pov_schema.sql"):
            raw.executescript((Path("app/storage") / schema).read_text())
        yield Connection(raw)


def test_shared_transaction_and_idempotence_preserve_first_ingestion(sources, database):
    bundle = cli.prepare(sources(1), AT)
    inserted = cli.apply_bundle(database, bundle)
    assert list(inserted.values()) == [1, 2, 4]
    repeat = replace(bundle, match=replace(bundle.match, ingested_at=AT + timedelta(days=1)))
    assert list(cli.apply_bundle(database, repeat).values()) == [0, 0, 0]
    assert database.counts() == [1, 2, 4]
    assert not any("CREATE" in e for e in database.events)
    actual = database.raw.execute("SELECT ingested_at FROM historical_matches").fetchone()[0]
    assert datetime.fromisoformat(actual) == AT


def test_pov_failure_rolls_back_whole_match_and_preserves_previous(sources, database):
    first, second = cli.prepare(sources(1), AT), cli.prepare(sources(2), AT)
    cli.apply_bundle(database, first)
    database.fail_pov_match = "2"
    with pytest.raises(psycopg.IntegrityError):
        cli.apply_bundle(database, second)
    assert database.counts() == [1, 2, 4]
    assert database.raw.execute("SELECT source_match_id FROM historical_matches").fetchall() == [("1",)]


def test_conflicting_data_is_not_overwritten(sources, database):
    bundle = cli.prepare(sources(1), AT)
    cli.apply_bundle(database, bundle)
    database.raw.execute("UPDATE historical_match_economy_snapshots SET radiant_net_worth=99999")
    with pytest.raises(ValueError, match="conflict"):
        cli.apply_bundle(database, bundle)
    assert database.counts() == [1, 2, 4]
    assert database.raw.execute("SELECT MIN(radiant_net_worth) FROM historical_match_economy_snapshots").fetchone()[0] == 99999


def configure_main(monkeypatch, database, source_manifest):
    monkeypatch.setattr(cli, "MANIFESTS", (source_manifest,))
    monkeypatch.setattr(cli, "schema_preflight", lambda c: None)
    monkeypatch.setattr(cli, "postgres_connection_options_from_env", lambda: {})
    calls = []

    def connect(**options):
        calls.append(options)
        return database

    monkeypatch.setattr(cli.psycopg, "connect", connect)
    return calls


def test_cli_preflight_is_database_read_only(sources, database, tmp_path, monkeypatch):
    path = manifest(tmp_path / "output/manifest.json", [sources(1)])
    calls = configure_main(monkeypatch, database, path)
    assert cli.main([]) == 0
    assert calls[0]["options"] == "-c default_transaction_read_only=on"
    assert len(calls) == 1 and database.counts() == [0, 0, 0]
    assert all(e.startswith("SELECT") for e in database.events)


@pytest.mark.parametrize("destination", ["manifest", "compact", "dem"])
def test_report_never_overwrites_source_files(sources, database, tmp_path, monkeypatch, destination):
    entry = sources(1)
    path = manifest(tmp_path / "output/manifest.json", [entry])
    calls = configure_main(monkeypatch, database, path)
    target = path if destination == "manifest" else tmp_path / "output/1" / (
        "1.compact.json" if destination == "compact" else "1.dem"
    )
    before = target.read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--apply", "--report", str(target)])
    assert exc.value.code == 2
    assert target.read_bytes() == before
    assert not calls and database.counts() == [0, 0, 0]


def test_cli_limit_resume_and_repeated_import(sources, database, tmp_path, monkeypatch):
    path = manifest(tmp_path / "output/manifest.json", [sources(1), sources(2)])
    configure_main(monkeypatch, database, path)
    for _ in range(2):
        assert cli.main(["--apply", "--limit", "1"]) == 0
    assert database.counts() == [2, 4, 8]
    assert cli.main(["--apply"]) == 0
    report = json.loads((tmp_path / "output/postgres-replay-import/report.json").read_text())
    assert all(r["status"] == "SKIPPED" for r in report["matches"])


def test_cli_records_failure_but_commits_later_match(sources, database, tmp_path, monkeypatch):
    path = manifest(tmp_path / "output/manifest.json", [sources(1), sources(2)])
    configure_main(monkeypatch, database, path)
    database.fail_pov_match = "1"
    assert cli.main(["--apply"]) == 1
    assert database.counts() == [1, 2, 4]
    assert database.raw.execute("SELECT source_match_id FROM historical_matches").fetchall() == [("2",)]
    report = json.loads((tmp_path / "output/postgres-replay-import/report.json").read_text())
    assert report["errors"][0]["match_id"] == "1"
    assert report["matches"][0]["match_id"] == "2"
