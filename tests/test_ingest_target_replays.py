from copy import deepcopy
from contextlib import nullcontext
import bz2
from io import BytesIO
import json
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
import subprocess
import time
from urllib.error import HTTPError

import pytest

from app.replay_fetcher import OpenDotaRateLimitError, ReplayDownloadError
from app import replay_fetcher
from app.replay_trajectory import AGGREGATE_FIELDS
from app.tournament_allowlist import load_tournament_allowlist
from tools import ingest_target_replays as cli


def candidate(mid, league=19719, start=100):
    return dict(match_id=mid, league_id=league, league_name="Selected", start_time=start)


def test_canonical_checkpoint_queue_stays_compatible_and_deterministic():
    rows = [candidate(5, 1, 90), candidate(6, 1, 100),
            candidate(3, 2, 70), candidate(4, 2, 80), candidate(2, 3, 60)]
    assert [r["match_id"] for r in cli.diverse_order(rows)] == [6, 4, 2, 5, 3]
    assert cli.diverse_order(rows) == cli.diverse_order(list(reversed(rows)))
    with pytest.raises(ValueError, match="Duplicate"):
        cli.diverse_order(rows + [rows[0]])


def test_execution_is_globally_newest_first_and_preserves_checkpoint(batch, monkeypatch):
    state, path = batch
    # The old round-robin queue puts older league 2 before newer league 1.
    state["candidates"] = cli.diverse_order([
        candidate(10, 1, 500), candidate(11, 1, 490), candidate(20, 2, 100),
        candidate(12, 1, 490), candidate(99, 3, 900), candidate(98, 3, 800),
    ])
    state["matches"] = {
        "99": dict(candidate(99, 3, 900), status="PARSE_OK", dem_sha256="a" * 64),
        "98": dict(candidate(98, 3, 800), status="RETRYABLE_ERROR", attempts=1),
    }
    original = deepcopy(state)
    calls = []
    def process(entry, *args, **kwargs):
        calls.append(entry["match_id"])
        entry["status"] = "PARSE_OK"
    monkeypatch.setattr(cli, "process", process)
    cli.execute_batch(state, path, None, limit=100, min_free_bytes=0)
    assert calls == [10, 11, 12, 20, 98]  # Equal start_time: ascending match_id.
    assert state["candidates"] == original["candidates"]
    assert state["matches"]["99"] == original["matches"]["99"]
    again = deepcopy(original)
    again["candidates"].reverse()
    assert [r["match_id"] for r in cli.pending_order(again)] == calls


def test_gib_conversion_is_binary_and_rounds_up():
    assert cli.gib_to_bytes("40") == 42_949_672_960 != 40_000_000_000
    assert cli.gib_to_bytes("40.5") == 40 * cli.GIB + cli.GIB // 2
    assert cli.gib_to_bytes("40.00000000001") == 40 * cli.GIB + 1


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-40", "invalid"])
def test_invalid_disk_limits_are_rejected(value):
    with pytest.raises(ValueError):
        cli.gib_to_bytes(value)


@pytest.mark.parametrize("flag", [None, "--min-free-gib", "--min-free-gb"])
def test_cli_default_and_legacy_alias_keep_40_binary_gib(tmp_path, monkeypatch, flag):
    monkeypatch.setattr(cli, "OUTPUT", tmp_path)
    monkeypatch.setattr(cli, "postgres_connection_options_from_env", lambda: {})
    monkeypatch.setattr(cli.psycopg, "connect", lambda **kwargs: nullcontext(None))
    monkeypatch.setattr(cli, "load_tournament_allowlist", lambda: SimpleNamespace(league_ids=[]))
    monkeypatch.setattr(cli, "candidates", lambda *args: ({}, []))
    monkeypatch.setattr(cli, "seed_existing", lambda: {})
    def execute(state, path, fetcher, *, min_free_bytes, **kwargs):
        assert min_free_bytes == 42_949_672_960
        state["stop_reason"] = "QUEUE_EXHAUSTED"
    monkeypatch.setattr(cli, "execute_batch", execute)
    args = ["--as-of", "2026-10-26T01:52:53Z", "--output-dir", str(tmp_path / "output"), "--run"]
    assert cli.main(args + ([flag, "40"] if flag else [])) == 0


@pytest.mark.parametrize("free,ok", [
    (40_000_000_000 + 2 * 1024**3, False),
    (41 * 1024**3, False), (42 * 1024**3, True),
])
def test_bash_disk_check_uses_same_binary_floor_and_working_reserve(tmp_path, monkeypatch, free, ok):
    script = (Path(__file__).resolve().parents[1] / "tools/run_ingestion_batches.sh").read_text()
    helper = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    checkpoint = tmp_path / "manifest.json"
    checkpoint.write_text(json.dumps(dict(as_of=1792979573, matches={})))
    monkeypatch.setattr(sys, "argv", ["inline", str(checkpoint), "disk", "40"])
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))
    if ok:
        exec(compile(helper, "bash-helper", "exec"), {})
    else:
        with pytest.raises(SystemExit) as error:
            exec(compile(helper, "bash-helper", "exec"), {})
        assert error.value.code == 1


def test_disk_reserve_checked_before_attempt_and_before_next_write(batch, monkeypatch):
    state, path = batch
    floor = 40 * cli.GIB
    available = floor + cli.WORKING_RESERVE_BYTES - 1
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=available))
    fetcher = Fetcher()
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=floor)
    assert state["stop_reason"] == "LOW_DISK_SPACE" and state["last_run_attempted"] == 0
    assert not fetcher.details and not state["matches"]
    assert "working_reserve" in state["disk_space_diagnostic"]
    available += 1
    cli.require_space(path.parent, floor)
    with pytest.raises(cli.LowDiskSpace):
        cli.require_space(path.parent, floor, 1)


def test_known_archive_size_is_budgeted_before_download_body(batch, monkeypatch):
    state, path = batch
    floor = 40 * cli.GIB
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=43 * cli.GIB))
    fetcher = Fetcher()
    def download(*, progress_callback, **kwargs):
        progress_callback(SimpleNamespace(outcome="downloading", downloaded_bytes=0, total_bytes=2 * cli.GIB))
        pytest.fail("Body must not be downloaded without space for archive + reserve")
    monkeypatch.setattr(fetcher, "_download_replay", download)
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=floor)
    assert state["stop_reason"] == "LOW_DISK_SPACE" and state["last_run_attempted"] == 1
    assert state["matches"]["1"]["reason"] == "low_disk_space"
    assert len(fetcher.details) == 1 and "2" not in state["matches"]


@pytest.mark.parametrize("phase", ["download", "bzip2", "zstd"])
def test_real_stream_and_unpack_guards_preserve_existing_dem(tmp_path, phase):
    payload = b"PBDEMS2\0" + b"replay" * 1000
    body = replay_fetcher.zstandard.ZstdCompressor().compress(payload) if phase == "zstd" else bz2.compress(payload)
    class Response(BytesIO):
        headers = {"Content-Length": str(len(body))}
    fetcher = replay_fetcher.ReplayFetcher(api_key="", request_delay_seconds=0,
        urlopen_func=lambda *args, **kwargs: Response(body))
    writes = []
    def guard(size):
        if size:
            writes.append(size)
            if phase == "download" or len(writes) == 2:
                raise cli.LowDiskSpace("low_disk_space")
    dem = tmp_path / "1.dem"
    dem.write_bytes(b"KEEP EXISTING DEM")
    with pytest.raises(cli.LowDiskSpace):
        fetcher._download_replay(replay_url="https://replay1.valve.net/570/1_1.dem.bz2",
            match_id=1, destination=dem, space_guard=guard)
    assert writes == ([len(body)] if phase == "download" else [len(body), len(payload)])
    assert dem.read_bytes() == b"KEEP EXISTING DEM" and sorted(tmp_path.iterdir()) == [dem]


def test_low_disk_during_parse_keeps_download_and_stops_immediately(batch, monkeypatch):
    state, path = batch
    available = 80 * cli.GIB
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=available))
    def parse(*args, space_guard):
        nonlocal available
        available = 41 * cli.GIB
        space_guard(0)
        pytest.fail("Parser must stop")
    monkeypatch.setattr(cli, "parse_replay", parse)
    fetcher = Fetcher()
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=40 * cli.GIB)
    entry = json.loads(path.read_text())["matches"]["1"]
    assert state["stop_reason"] == "LOW_DISK_SPACE" and state["last_run_attempted"] == 1
    assert entry["status"] == "RETRYABLE_ERROR" and entry["reason"] == "low_disk_space"
    assert entry["download_status"] == "DOWNLOAD_OK"
    assert cli.sha256(path.parent / "1/1.dem") == entry["dem_sha256"]
    assert fetcher.downloads == [1] and "2" not in state["matches"]


def test_low_disk_during_response_stops_before_write_and_next_candidate(batch, monkeypatch):
    state, path = batch
    available = 80 * cli.GIB
    body = bz2.compress(b"PBDEMS2\0" + b"data" * 1000)
    class Response(BytesIO):
        headers = {"Content-Length": str(len(body))}
        def read(self, size=-1):
            nonlocal available
            chunk = super().read(size)
            available = 41 * cli.GIB
            return chunk
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=available))
    fetcher = Fetcher()
    real = replay_fetcher.ReplayFetcher(api_key="", request_delay_seconds=0,
        urlopen_func=lambda *args, **kwargs: Response(body))
    monkeypatch.setattr(fetcher, "_download_replay", real._download_replay)
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=40 * cli.GIB)
    assert state["stop_reason"] == "LOW_DISK_SPACE" and state["last_run_attempted"] == 1
    assert state["matches"]["1"]["status"] == "RETRYABLE_ERROR"
    assert "dem_sha256" not in state["matches"]["1"] and "2" not in state["matches"]
    assert not (path.parent / "1/1.dem").exists() and len(fetcher.details) == 1


def test_remaining_pov_outputs_are_budgeted_before_writing(tmp_path):
    write_sources(tmp_path, sources())
    original = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    def guard(size):
        assert size > 4096
        raise cli.LowDiskSpace("not enough space for POV output")
    with pytest.raises(cli.LowDiskSpace):
        cli.validate_parsed(tmp_path, 1, 19719, "a" * 64, space_guard=guard)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == original


@pytest.mark.parametrize("failure", ["disk", "timeout", "interrupt"])
def test_parser_group_stops_on_low_disk_timeout_or_interrupt(tmp_path, monkeypatch, failure):
    (tmp_path / "1.dem").write_bytes(b"existing dem")
    artifact = tmp_path / "1.compact.json"
    artifact.write_text("existing parsed fragment")
    available = 80 * cli.GIB
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=available))
    calls = []
    class Parser:
        pid, args = 987654, ["mock parser"]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait(self, timeout=None):
            nonlocal available
            if timeout is None:
                return -9
            if failure == "interrupt":
                raise KeyboardInterrupt
            available = 41 * cli.GIB
            raise subprocess.TimeoutExpired(self.args, timeout)
    def popen(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return Parser()
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(cli.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    if failure == "timeout":
        times = iter((0, 601))
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    error = {"disk": cli.LowDiskSpace, "timeout": subprocess.TimeoutExpired,
             "interrupt": KeyboardInterrupt}[failure]
    with pytest.raises(error):
        cli.parse_replay(tmp_path, 1, 19719, "a" * 64,
                         space_guard=lambda size: cli.require_space(tmp_path, 40 * cli.GIB, size))
    assert calls == [(987654, signal.SIGKILL)]
    assert (tmp_path / "1.dem").read_bytes() == b"existing dem"
    assert artifact.read_text() == "existing parsed fragment"


@pytest.mark.parametrize("damage", ["league", "time", "identity"])
def test_candidate_selection_remains_read_only_and_allowlisted(monkeypatch, damage):
    from contextlib import contextmanager

    row = candidate(1, start=2_000_000_000)
    if damage == "league":
        row["league_id"] = 20053
    elif damage == "time":
        row["start_time"] += 1
    else:
        row["match_id"] = 2
    monkeypatch.setattr(cli, "audit_catalog", lambda *a, **k: ({}, [1]))

    class Connection:
        @contextmanager
        def transaction(self):
            yield

        def execute(self, query, params=None):
            assert query == cli.READ_ONLY or query.startswith("SELECT ")
            if params is not None:
                assert params == ([1],)
            return SimpleNamespace(fetchall=lambda: [row])

    with pytest.raises(ValueError):
        cli.candidates(Connection(), load_tournament_allowlist(), 2_000_000_000)


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    path = tmp_path / "batch/manifest.json"
    path.parent.mkdir()
    state = dict(matches={}, candidates=[candidate(1), candidate(2)], as_of=100)
    return state, path


class Fetcher:
    def __init__(self):
        self.details = []
        self.downloads = []
        self.no_url = False
        self.failure = None

    def _fetch_opendota_json(self, endpoint, *, max_attempts):
        self.details.append(endpoint)
        assert max_attempts == 1  # No capped Retry-After retry on HTTP 429.
        mid = int(endpoint.split("/")[-1])
        return SimpleNamespace(payload=dict(
            match_id=mid, leagueid=19719,
            replay_url=None if self.no_url else f"http://replay1.valve.net/570/{mid}_1.dem.bz2",
        ))

    def _download_replay(self, *, replay_url, match_id, destination, progress_callback, space_guard=None):
        self.downloads.append(match_id)
        if self.failure:
            raise self.failure
        payload = b"PBDEMS2\0" + str(match_id).encode()
        if space_guard:
            space_guard(len(payload))
        destination.write_bytes(payload)
        progress_callback(SimpleNamespace(downloaded_bytes=123))


@pytest.fixture
def parsed(monkeypatch):
    def parse(directory, mid, league, sha, *, space_guard=None):
        if space_guard:
            space_guard(0)
        for suffix in ("compact.json", "compact.spatial.json", "compact.visibility.json"):
            (directory / f"{mid}.{suffix}").write_text("{}")
        (directory / "pov").mkdir(exist_ok=True)
        for name in ("team_pov_features.csv", "enemy_observations.csv"):
            (directory / "pov" / name).write_text("header\n")
        return dict(economy_snapshots=1, spatial_snapshots=6, pov_team_rows=12)
    monkeypatch.setattr(cli, "parse_replay", parse)
    return parse


def test_bounded_run_and_resume_never_redownload_completed(batch, parsed):
    state, path = batch
    fetcher = Fetcher()
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "PARSE_OK"
    assert state["stop_reason"] == "LIMIT_REACHED"
    saved = json.loads(path.read_text())
    cli.verify_saved(saved["matches"]["1"])
    cli.execute_batch(saved, path, fetcher, limit=1, min_free_bytes=0)
    assert fetcher.downloads == [1, 2]
    assert all(e["status"] == "PARSE_OK" for e in saved["matches"].values())
    cli.execute_batch(saved, path, fetcher, limit=100, min_free_bytes=0)
    assert saved["last_run_attempted"] == 0
    assert fetcher.downloads == [1, 2]


def test_parse_failure_resumes_download_ok_without_download(batch, parsed, monkeypatch):
    state, path = batch
    state["candidates"] = state["candidates"][:1]
    fetcher = Fetcher()

    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired("Clarity", 600)

    monkeypatch.setattr(cli, "parse_replay", failed)
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "RETRYABLE_ERROR"
    assert state["matches"]["1"]["download_status"] == "DOWNLOAD_OK"
    monkeypatch.setattr(cli, "parse_replay", parsed)
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "PARSE_OK"
    assert fetcher.downloads == [1] and len(fetcher.details) == 1
    assert "reason" not in state["matches"]["1"]


def test_parser_error_preserves_dem_and_is_not_retried_forever(batch, monkeypatch):
    state, path = batch
    state["candidates"] = state["candidates"][:1]
    fetcher = Fetcher()

    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "Clarity")

    monkeypatch.setattr(cli, "parse_replay", failed)
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "PARSE_ERROR"
    assert state["matches"]["1"]["dem_sha256"]
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert fetcher.downloads == [1]


@pytest.mark.parametrize("http_status", [404, 410])
def test_expired_replay_is_terminal_and_not_retried(batch, http_status):
    state, path = batch
    fetcher = Fetcher()
    fetcher.failure = ReplayDownloadError("expired", http_status=http_status)
    cli.execute_batch(state, path, fetcher, limit=2, min_free_bytes=0)
    assert {e["status"] for e in state["matches"].values()} == {"REPLAY_UNAVAILABLE"}
    cli.execute_batch(state, path, fetcher, limit=2, min_free_bytes=0)
    assert len(fetcher.downloads) == 2


def test_absent_url_does_not_download(batch):
    state, path = batch
    fetcher = Fetcher()
    fetcher.no_url = True
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["reason"] == "NO_REPLAY_URL"
    assert fetcher.downloads == []


def test_retryable_match_has_bounded_attempts(batch):
    state, path = batch
    state["candidates"] = state["candidates"][:1]
    fetcher = Fetcher()
    fetcher.failure = ReplayDownloadError("network error")
    for _ in range(4):
        cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert fetcher.downloads == [1, 1, 1]
    assert state["matches"]["1"]["attempts"] == 3
    assert state["last_run_attempted"] == 0


def test_429_stops_immediately_and_preserves_full_retry_after(batch, monkeypatch):
    state, path = batch
    fetcher = Fetcher()

    def rate_limited(*args, **kwargs):
        raise OpenDotaRateLimitError("900", attempts=1)

    monkeypatch.setattr(fetcher, "_fetch_opendota_json", rate_limited)
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=0)
    assert state["stop_reason"] == "HTTP_429"
    assert state["resume_after_epoch"] == 1900
    assert len(state["matches"]) == 1
    assert json.loads(path.read_text())["matches"]["1"]["status"] == "RETRYABLE_ERROR"


def test_valve_429_also_preserves_original_retry_after(batch, monkeypatch):
    state, path = batch
    fetcher = Fetcher()
    fetcher.failure = ReplayDownloadError("rate limited", http_status=429)
    fetcher.failure.__cause__ = HTTPError(
        "http://replay1.valve.net/570/1_1.dem.bz2", 429, "rate limited",
        {"Retry-After": "900"}, None,
    )
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=0)
    assert state["resume_after_epoch"] == 1900
    assert state["stop_reason"] == "HTTP_429"
    assert fetcher.downloads == [1]


def test_disk_and_repeated_network_errors_stop_safely(batch, monkeypatch):
    state, path = batch
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    fetcher = Fetcher()
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=1)
    assert state["stop_reason"] == "LOW_DISK_SPACE" and not fetcher.details
    state["candidates"] = [candidate(i) for i in range(1, 10)]
    fetcher.failure = ReplayDownloadError("network failed")
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=0)
    assert state["stop_reason"] == "REPEATED_ERRORS"
    assert len(fetcher.downloads) == 3


def queued_corpus():
    completed = [candidate(i, i % 3 + 1, 5000 - i) for i in range(1, 166)]
    retries = [candidate(i, i % 3 + 1, 10000 - i) for i in range(201, 204)]
    fresh = [candidate(i, i % 3 + 1, 3000 - i) for i in range(1001, 1031)]
    matches = {str(r["match_id"]): dict(
        r, status="PARSE_OK", attempts=1, dem_sha256=f"{r['match_id']:064x}",
        artifacts=[dict(path=f"saved/{r['match_id']}.dem", sha256="a" * 64)],
    ) for r in completed}
    matches.update({str(r["match_id"]): dict(
        r, status="RETRYABLE_ERROR", attempts=1, reason="replay_download_incomplete",
    ) for r in retries})
    return dict(matches=matches, candidates=cli.diverse_order(completed + retries + fresh), as_of=100)


def test_165_completed_and_old_failures_do_not_block_newest_fresh_queue(batch, monkeypatch):
    _, path = batch
    state = queued_corpus()
    original = deepcopy(state)
    expected = sorted(r["match_id"] for r in state["candidates"] if r["match_id"] >= 1001)
    attempted = []

    def success(entry, *args, **kwargs):
        attempted.append(entry["match_id"])
        entry.update(status="PARSE_OK", dem_sha256=f"{entry['match_id']:064x}")

    monkeypatch.setattr(cli, "process", success)
    cli.execute_batch(state, path, None, limit=6, min_free_bytes=0)
    assert attempted == expected[:6] and state["stop_reason"] == "LIMIT_REACHED"
    resumed = json.loads(path.read_text())
    cli.execute_batch(resumed, path, None, limit=6, min_free_bytes=0)
    assert attempted == expected[:12] and resumed["last_run_attempted"] == 6
    assert resumed["candidates"] == original["candidates"]
    assert all(resumed["matches"][k] == v for k, v in original["matches"].items())


def test_new_network_failures_still_trip_breaker_before_deferred_retries(batch, monkeypatch):
    _, path = batch
    state = queued_corpus()
    old = deepcopy(state["matches"])
    expected = sorted(r["match_id"] for r in state["candidates"] if r["match_id"] >= 1001)
    attempted = []

    def failed(entry, *args, **kwargs):
        attempted.append(entry["match_id"])
        raise ReplayDownloadError("replay_download_incomplete")

    monkeypatch.setattr(cli, "process", failed)
    cli.execute_batch(state, path, None, limit=100, min_free_bytes=0)
    assert attempted == expected[:3]
    assert state["stop_reason"] == "REPEATED_ERRORS" and state["last_run_attempted"] == 3
    assert all(state["matches"][k] == v for k, v in old.items())
    assert all(state["matches"][str(i)]["status"] == "RETRYABLE_ERROR" for i in attempted)


def test_each_id_runs_once_and_new_failures_are_not_requeued(batch, monkeypatch):
    state, path = batch
    state["candidates"] = [candidate(3), candidate(1), candidate(3), candidate(2), candidate(1)]
    state["matches"]["3"] = dict(candidate(3), status="RETRYABLE_ERROR", attempts=1)
    attempted = []

    def process(entry, *args, **kwargs):
        attempted.append(entry["match_id"])
        if entry["match_id"] in (1, 3):
            raise ReplayDownloadError("replay_download_incomplete")
        entry["status"] = "PARSE_OK"

    monkeypatch.setattr(cli, "process", process)
    cli.execute_batch(state, path, None, limit=100, min_free_bytes=0)
    assert attempted == [1, 2, 3]
    assert state["last_run_attempted"] == 3 and state["stop_reason"] == "QUEUE_EXHAUSTED"
    assert state["matches"]["1"]["attempts"] == 1 and state["matches"]["3"]["attempts"] == 2
    assert all(state["matches"][str(i)]["reason"] == "replay_download_incomplete" for i in (1, 3))


@pytest.mark.parametrize("status", ["RETRYABLE_ERROR", "IN_PROGRESS", "DOWNLOAD_OK"])
def test_previous_work_is_deferred_and_counts_towards_same_limit(batch, monkeypatch, status):
    state, path = batch
    state["matches"]["1"] = dict(candidate(1), status=status, attempts=1, dem_sha256="a" * 64)
    calls = []

    def success(entry, *args, **kwargs):
        calls.append(entry["match_id"])
        entry["status"] = "PARSE_OK"

    monkeypatch.setattr(cli, "process", success)
    cli.execute_batch(state, path, None, limit=2, min_free_bytes=0)
    assert calls == [2, 1] and state["last_run_attempted"] == 2
    assert state["matches"]["1"]["dem_sha256"] == "a" * 64


def test_deferred_retries_only_remain_bounded_after_fresh_work_exhausted(batch, monkeypatch):
    state, path = batch
    state["candidates"] = [candidate(1)]
    state["matches"]["1"] = dict(candidate(1), status="RETRYABLE_ERROR", attempts=1)
    calls = []

    def failed(entry, *args, **kwargs):
        calls.append(entry["match_id"])
        raise ReplayDownloadError("replay_download_incomplete")

    monkeypatch.setattr(cli, "process", failed)
    for _ in range(3):
        cli.execute_batch(state, path, None, limit=1, min_free_bytes=0)
        state = json.loads(path.read_text())
    assert calls == [1, 1]  # One per run, at most three lifetime attempts.
    assert state["matches"]["1"]["status"] == "RETRYABLE_ERROR"
    assert state["matches"]["1"]["attempts"] == 3
    assert state["last_run_attempted"] == 0 and state["stop_reason"] == "QUEUE_EXHAUSTED"


def test_deferred_rate_limit_still_stops_and_retains_cooldown(batch, monkeypatch):
    state, path = batch
    state["candidates"].append(candidate(3))
    for mid in (1, 3):
        state["matches"][str(mid)] = dict(candidate(mid), status="RETRYABLE_ERROR", attempts=1)
    calls = []

    def process(entry, *args, **kwargs):
        calls.append(entry["match_id"])
        if entry["match_id"] == 1:
            raise OpenDotaRateLimitError("900", attempts=1)
        entry["status"] = "PARSE_OK"

    monkeypatch.setattr(cli, "process", process)
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    cli.execute_batch(state, path, None, limit=100, min_free_bytes=0)
    assert calls == [2, 1]
    assert state["stop_reason"] == "HTTP_429" and state["resume_after_epoch"] == 1900
    assert state["matches"]["3"]["attempts"] == 1


@pytest.mark.parametrize("empty", [True, False])
def test_no_eligible_candidates_finishes_without_changing_records(batch, monkeypatch, empty):
    state, path = batch
    statuses = ["PARSE_OK", "PARSE_ERROR", "REPLAY_UNAVAILABLE", "INVALID_ARTIFACT", "RETRYABLE_ERROR"]
    state["candidates"] = [] if empty else [candidate(i) for i in range(1, 6)]
    state["matches"] = {str(i): dict(candidate(i), status=s, attempts=3) for i, s in enumerate(statuses, 1)}
    original = deepcopy(state["matches"])

    def unexpected(*args):
        pytest.fail("No eligible candidate should reach downloader/parser")

    monkeypatch.setattr(cli, "process", unexpected)
    cli.execute_batch(state, path, None, limit=100, min_free_bytes=0)
    assert state["last_run_attempted"] == 0 and state["stop_reason"] == "QUEUE_EXHAUSTED"
    assert json.loads(path.read_text())["matches"] == original


@pytest.mark.parametrize("expected_bytes", [1024, None])
def test_incomplete_reason_and_available_transfer_diagnostics_are_retained(batch, monkeypatch, capsys, expected_bytes):
    state, path = batch
    fetcher = Fetcher()

    def incomplete(*, progress_callback, match_id, **kwargs):
        progress_callback(SimpleNamespace(outcome="failure", downloaded_bytes=256,
                                           total_bytes=expected_bytes))
        raise ReplayDownloadError("replay_download_incomplete", reason="replay_download_incomplete")

    monkeypatch.setattr(fetcher, "_download_replay", incomplete)
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == dict(event="replay_download_failure", match_id=1,
                            downloaded_bytes=256, total_bytes=expected_bytes)
    assert lines[-1]["reason"] == state["matches"]["1"]["reason"] == "replay_download_incomplete"
    assert state["matches"]["1"]["status"] == "RETRYABLE_ERROR" and len(fetcher.details) == 1


def test_sha_tampering_and_identity_mismatch_are_explicit(batch, parsed):
    state, path = batch
    cli.execute_batch(state, path, Fetcher(), limit=1, min_free_bytes=0)
    (path.parent / "1/1.dem").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA mismatch"):
        cli.verify_saved(state["matches"]["1"])
    entry = candidate(3, league=20395)
    with pytest.raises(ValueError, match="metadata conflicts"):
        cli.process(entry, path.parent / "3", Fetcher(), lambda: None)


@pytest.mark.parametrize("url", [
    "https://example.org/570/1_2.dem.bz2", "http://replay1.valve.net/570/2_2.dem.bz2",
    "file:///570/1_2.dem.bz2", "http://evilvalve.net/570/1_2.dem.bz2",
])
def test_never_substitutes_another_source_or_match(url):
    with pytest.raises(ValueError, match="selected match"):
        cli.check_url(url, 1)


def test_confirmed_opendota_chinese_replay_cdn_is_supported():
    cli.check_url("http://replay413.dota2.com.cn/570/8960762254_1790012770.dem.bz2", 8960762254)


def test_atomic_checkpoint_does_not_destroy_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    cli.save(path, {"saved": True})

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(cli.os, "replace", fail)
    with pytest.raises(OSError):
        cli.save(path, {"saved": False})
    assert json.loads(path.read_text()) == {"saved": True}
    assert list(tmp_path.iterdir()) == [path]


def test_streaming_deadline_raises_and_restores_signal_handler():
    handler = signal.getsignal(signal.SIGALRM)
    with pytest.raises(ReplayDownloadError, match="deadline_exceeded"):
        with cli.download_deadline(0.01):
            time.sleep(1)
    assert signal.getsignal(signal.SIGALRM) == handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def sources():
    header = dict(schema_version=1, match_id=1, replay_sha256="a" * 64,
                  clarity_version="4.0.1", clock_normalization_status="PROVEN",
                  clock_normalization_method="GAME_START_TIME_AND_PAUSE_TICKS",
                  zero_replay_tick=1000, game_end_time_seconds=65.0,
                  pause_ticks=0, pause_seconds=0.0, pause_witnessed=False)
    players = [dict(player_slot=i, team="RADIANT" if i < 5 else "DIRE",
                    hero_id=i+1, hero_name=f"hero_{i}", level=1, items=[],
                    **{f: 0 for f in AGGREGATE_FIELDS}) for i in range(10)]
    compact = dict(header, game_mode=2, league_id=19719, winner=2,
                   radiant_team_id=None, radiant_team_tag=None,
                   dire_team_id=None, dire_team_tag=None, picks_bans=[], snapshots=[])
    spatial = dict(header, artifact_kind="replay_hero_positions", snapshot_interval_seconds=10,
                   coordinate_system={"name": "SOURCE2_NATIVE_CELL_GRID"}, snapshots=[])
    visibility = dict(header, artifact_kind="replay_hero_visibility",
                      snapshot_interval_seconds=10, snapshots=[])
    for actual, tick, nominal in ((0.0, 1000, 0), (60.0002, 2800, 60)):
        frame = dict(source_game_time_seconds=actual, replay_tick=tick)
        compact["snapshots"].append(dict(frame, game_time_seconds=nominal, game_state=5,
            players=deepcopy(players), teams=[dict(team=team, **{f: 0 for f in AGGREGATE_FIELDS})
                                            for team in ("RADIANT", "DIRE")]))
        sf, vf = dict(frame, game_time_seconds=actual, players=[]), dict(frame, game_time_seconds=actual, players=[])
        for p in players:
            identity = dict(player_slot=p["player_slot"], team=p["team"], hero_id=p["hero_id"],
                            selected_hero_handle=p["hero_id"], hero_entity_handle=p["hero_id"],
                            hero_entity_class="CDOTA_Unit_Hero_Test")
            sf["players"].append(dict(identity, life_state=2, position_status="HERO_NOT_ALIVE",
                                      x=None, y=None))
            vf["players"].append(dict(identity, visible_to_radiant="HIDDEN", visible_to_dire="VISIBLE"))
        spatial["snapshots"].append(sf)
        visibility["snapshots"].append(vf)
    return compact, spatial, visibility


def write_sources(directory, data):
    for suffix, payload in zip(("compact.json", "compact.spatial.json", "compact.visibility.json"), data):
        (directory / f"1.{suffix}").write_text(json.dumps(payload))


def test_actual_channel_validation_preserves_dead_nulls_and_gold_limitation(tmp_path):
    write_sources(tmp_path, sources())
    result = cli.validate_parsed(tmp_path, 1, 19719, "a" * 64)
    assert result["economy_snapshots"] == 2
    assert result["spatial_snapshots"] == 2 and result["pov_team_rows"] == 4
    assert result["position_status_counts"] == {"HERO_NOT_ALIVE": 20}
    assert result["hidden_current_coordinates_absent"]
    assert result["current_gold"]["absence_of_zero_substitution_proven"] is False


@pytest.mark.parametrize("damage", ["sha", "roster", "tick", "coordinate", "observed_null"])
def test_corrupt_channels_never_become_parse_ok(tmp_path, damage):
    compact, spatial, visibility = sources()
    if damage == "sha":
        spatial["replay_sha256"] = "b" * 64
    elif damage == "roster":
        spatial["snapshots"][0]["players"][0]["hero_id"] = 2
    elif damage == "tick":
        spatial["snapshots"][1]["replay_tick"] = 999
    elif damage == "coordinate":
        spatial["snapshots"][0]["players"][0]["x"] = 0
    else:
        spatial["snapshots"][0]["players"][0]["position_status"] = "OBSERVED"
    write_sources(tmp_path, (compact, spatial, visibility))
    with pytest.raises(ValueError):
        cli.validate_parsed(tmp_path, 1, 19719, "a" * 64)
