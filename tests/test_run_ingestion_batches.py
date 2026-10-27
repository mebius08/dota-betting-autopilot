"""Bash runner integration checks using fake CLI, DB/disk checks and sleep.

All state lives in tmp_path. No real downloads, parser, database or cooldown.
The script's actual checkpoint/report/log decisions still execute unchanged.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/run_ingestion_batches.sh"
BACKEND = r'''
import json, os, signal, sys, time
from pathlib import Path

def event(kind, **values):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(kind=kind, **values)) + '\n')

args = sys.argv[1:]
if args[0] == '-B': args = args[1:]
failure = os.environ.get('MOCK_FAILURE', '')
if args[0] == '-':
    action = args[2]
    if action in ('preflight', 'disk'):
        event(action)
        if action == 'preflight' and failure == 'db':
            print('mock database unavailable', file=sys.stderr); sys.exit(1)
        print('mock existing DB/disk checks OK'); sys.exit(0)
    os.execv(REAL_PYTHON, [REAL_PYTHON, '-B', *args])

assert args[0] == '-c'
os.setsid()
module, flags = args[2], args[3:]
event('cli', module=module, flags=flags)
checkpoint = Path('tools/replay_probe/output/target-replays/manifest.json')
state = json.loads(checkpoint.read_text())
if module == 'tools.ingest_target_replays':
    seq = json.loads(os.environ['MOCK_SEQUENCE'])
    counter = Path('run-count')
    index = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(index + 1))
    profile = seq[index] if index < len(seq) else dict(stop='queue', new=0)
    stop = profile['stop']
    if stop == 'stale': sys.exit(1)
    for _ in range(profile.get('new', 0)):
        mid = str(max(2000000, max(map(int, state['matches'])) + 1))
        state['matches'][mid] = dict(match_id=int(mid), status='PARSE_OK', dem_sha256=mid.zfill(64))
    if stop == 'hold_ingestion':
        def interrupted(*_):
            state['stop_reason'] = 'INTERRUPTED'
            checkpoint.write_text(json.dumps(state)); sys.exit(130)
        signal.signal(signal.SIGTERM, interrupted)
        state['stop_reason'] = 'RUNNING'
        checkpoint.write_text(json.dumps(state))
        Path('ingestion-ready').touch()
        while True: time.sleep(.03)
    reason = dict(normal='LIMIT_REACHED', queue='QUEUE_EXHAUSTED', low='LOW_DISK_SPACE',
                  rate='HTTP_429', unknown='UNKNOWN_FAILURE').get(stop, 'REPEATED_ERRORS')
    state['stop_reason'] = reason
    if reason == 'REPEATED_ERRORS':
        for n in range(3):
            mid = str(1000000 + index * 3 + n)
            state['matches'][mid] = dict(match_id=int(mid), attempts=1,
                status='INVALID_ARTIFACT' if stop == 'invalid_artifact' else 'RETRYABLE_ERROR')
    state['last_run_attempted'] = index + 1
    checkpoint.write_text(json.dumps(state))
    cb = dict(event='circuit_breaker', stage='download', download_errors=3, parser_errors=0)
    if stop == 'parser': cb.update(stage='parser', download_errors=0, parser_errors=3)
    if stop == 'contradictory': cb.update(download_errors=2)
    if reason == 'REPEATED_ERRORS' and stop != 'missing':
        print(json.dumps(cb), flush=True)
        if stop == 'duplicate': print(json.dumps(cb), flush=True)
    sys.exit(0 if stop in ('normal', 'queue') else 1)

apply = '--apply' in flags
report = Path(flags[flags.index('--report') + 1])
saved = Path(module + '-imported.json')
known = set(json.loads(saved.read_text())) if saved.exists() else {'1'}
ids = [k for k, v in state['matches'].items() if v['status'] == 'PARSE_OK']
rows = [dict(match_id=k, status=('SKIPPED' if k in known else 'IMPORTED') if apply else 'COMPATIBLE') for k in ids]
errors = ['mock integrity error'] if failure == 'integrity' else []
report.write_text(json.dumps(dict(matches=rows, compatible_matches=len(rows), errors=errors)))
if failure == 'import' and module == 'tools.import_hero_snapshots' and apply: sys.exit(7)
if apply: saved.write_text(json.dumps(ids))
'''
SLEEP = r'''
import json, os, signal, sys, time
from pathlib import Path
assert sys.argv[1:] == ['600']
with Path('events.jsonl').open('a') as stream:
    stream.write(json.dumps(dict(kind='sleep', seconds=600)) + '\n')
if os.environ.get('MOCK_SLEEP_HOLD'):
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Path('cooldown-ready').touch()
    while True: time.sleep(.03)
'''


@pytest.fixture
def runner(tmp_path):
    root = tmp_path / "repo"
    output = root / "tools/replay_probe/output/target-replays"
    output.mkdir(parents=True)
    shutil.copyfile(SCRIPT, root / "tools/run_ingestion_batches.sh")
    for module in ("ingest_target_replays", "import_replay_batch", "import_hero_snapshots",
                   "import_spatial_snapshots", "import_enemy_observations"):
        (root / f"tools/{module}.py").touch()
    (output / "manifest.json").write_text(json.dumps(dict(
        as_of=1792979573, stop_reason="LIMIT_REACHED",
        matches={"1": dict(match_id=1, status="PARSE_OK", dem_sha256="a" * 64)},
    )))
    py = tmp_path / "venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text(f"#!{sys.executable}\nREAL_PYTHON={sys.executable!r}\n" + BACKEND)
    py.chmod(0o755)
    sleep = tmp_path / "bin/sleep"
    sleep.parent.mkdir()
    sleep.write_text(f"#!{sys.executable}\n" + SLEEP)
    sleep.chmod(0o755)

    def start(sequence, *, batches=10, failure="", hold=False):
        env = dict(os.environ, MOCK_SEQUENCE=json.dumps(sequence), MOCK_FAILURE=failure,
                   PATH=str(sleep.parent) + os.pathsep + os.environ["PATH"])
        if hold:
            env["MOCK_SLEEP_HOLD"] = "1"
        return subprocess.Popen(
            ["/bin/bash", "tools/run_ingestion_batches.sh", "--run", "--max-batches", str(batches)],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    return root, start


def finish(process):
    try:
        out, err = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        pytest.fail("Mock runner did not finish; no real 600-second sleep is permitted")
    return process.returncode, out + err


def events(root, kind):
    path = root / "events.jsonl"
    return [event for line in path.read_text().splitlines()
            if (event := json.loads(line))["kind"] == kind]


def test_download_breaker_imports_then_cools_down_then_checks_next_batch(runner):
    root, start = runner
    code, out = finish(start([dict(stop="network", new=2), dict(stop="normal", new=1)], batches=2))
    assert code == 0 and "new_PARSE_OK=3; fully_imported_new=3; download_stops=1" in out
    all_events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    pause = next(i for i, e in enumerate(all_events) if e["kind"] == "sleep")
    ingests = [i for i, e in enumerate(all_events) if e.get("module") == "tools.ingest_target_replays"]
    applies = [i for i, e in enumerate(all_events)
               if e.get("module") == "tools.import_enemy_observations" and "--apply" in e["flags"]]
    assert applies[0] < pause < ingests[1] < applies[1]
    assert any(e["kind"] == "preflight" for e in all_events[pause + 1:ingests[1]])
    assert len(events(root, "preflight")) == 2 and len(events(root, "sleep")) == 1


@pytest.mark.parametrize("stop", ["parser", "missing", "duplicate", "contradictory", "invalid_artifact", "rate"])
def test_non_download_breakers_never_auto_resume(runner, stop):
    root, start = runner
    code, out = finish(start([dict(stop=stop, new=1)]))
    assert code == 1 and "fully_imported_new=1; download_stops=0" in out
    assert not events(root, "sleep") and len(events(root, "preflight")) == 1
    assert len(events(root, "cli")) == 9  # Ingest + four preflight/apply pairs.


def test_three_empty_network_batches_stop_with_prolonged_source_outage(runner):
    root, start = runner
    code, out = finish(start([dict(stop="network", new=0)] * 4))
    assert code == 1 and "Source persistently unavailable" in out
    assert "download_stops=3" in out and len(events(root, "preflight")) == 3
    assert len(events(root, "sleep")) == 2


@pytest.mark.parametrize("recovery", ["normal", "network"])
def test_any_batch_with_new_parse_ok_resets_no_progress_streak(runner, recovery):
    root, start = runner
    seq = [dict(stop="network", new=0)] * 2 + [dict(stop=recovery, new=1)] + [dict(stop="network", new=0)] * 3
    code, out = finish(start(seq))
    assert code == 1 and "Source persistently unavailable" in out
    assert "fully_imported_new=1" in out and len(events(root, "preflight")) == 6


@pytest.mark.parametrize("stop", ["low", "unknown", "stale"])
def test_low_disk_and_unconfirmed_results_stop_before_import(runner, stop):
    root, start = runner
    code, _ = finish(start([dict(stop=stop, new=1)]))
    assert code == 1 and len(events(root, "cli")) == 1
    assert not events(root, "sleep")


@pytest.mark.parametrize("failure", ["db", "import", "integrity"])
def test_database_import_and_integrity_failures_do_not_wait_or_resume(runner, failure):
    root, start = runner
    code, _ = finish(start([dict(stop="network", new=1)], failure=failure))
    assert code == 1 and not events(root, "sleep")
    assert len(events(root, "preflight")) == 1
    assert not any(e.get("module") == "tools.import_spatial_snapshots" for e in events(root, "cli"))


@pytest.mark.parametrize("batches", [1, 2])
def test_max_batches_includes_network_stops_and_skips_unnecessary_cooldown(runner, batches):
    root, start = runner
    code, out = finish(start([dict(stop="network", new=1)] * 3, batches=batches))
    assert code == 0 and f"max-batches={batches} reached" in out
    assert len(events(root, "preflight")) == batches and len(events(root, "sleep")) == batches - 1


def test_sigint_interrupts_cooldown_and_preserves_checkpoint_and_imports(runner):
    root, start = runner
    process = start([dict(stop="network", new=1)], hold=True)
    try:
        deadline = time.monotonic() + 10
        while not (root / "cooldown-ready").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / "cooldown-ready").exists()
        checkpoint = root / "tools/replay_probe/output/target-replays/manifest.json"
        before = checkpoint.read_bytes()
        process.send_signal(signal.SIGINT)
        code, out = finish(process)
        assert code == 130 and "interrupted (SIGINT)" in out
        assert "fully_imported_new=1; download_stops=1" in out
        assert checkpoint.read_bytes() == before and len(events(root, "preflight")) == 1
        assert not (root / "tools/replay_probe/output/.ingestion-batches.lock").exists()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)


def test_sigint_stops_ingestion_and_keeps_tool_checkpoint_without_import(runner):
    root, start = runner
    process = start([dict(stop="hold_ingestion", new=1)])
    try:
        deadline = time.monotonic() + 10
        while not (root / "ingestion-ready").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (root / "ingestion-ready").exists()
        process.send_signal(signal.SIGINT)
        code, out = finish(process)
        checkpoint = json.loads((root / "tools/replay_probe/output/target-replays/manifest.json").read_text())
        assert code == 130 and "interrupted (SIGINT)" in out
        assert checkpoint["stop_reason"] == "INTERRUPTED"
        assert checkpoint["matches"]["2000000"]["status"] == "PARSE_OK"
        assert len(events(root, "cli")) == 1 and not events(root, "sleep")
        assert not (root / "tools/replay_probe/output/.ingestion-batches.lock").exists()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)
