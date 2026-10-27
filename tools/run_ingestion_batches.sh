#!/bin/bash
# macOS Bash 3.2; orchestration only. Never starts Docker or initializes a DB.
# Export POSTGRES_HOST/PORT/DB/USER/PASSWORD before --run. No .env is sourced.
set -euo pipefail
umask 077

usage() {
    cat <<'HELP'
Usage (from repository root):
  bash tools/run_ingestion_batches.sh [--run] [--max-batches N]
       [--batch-limit N] [--min-free-gib N]
Defaults: dry-run, 1 batch, 100 candidates, 40 GiB minimum free disk.
40 GiB = 42949672960 bytes; ingestion keeps another 2 GiB working reserve.
Confirmed download circuit breaker: import verified matches, wait 600 seconds,
then retry within --max-batches. Three such batches without new PARSE_OK stop.
N must be a positive integer. Dry-run reads local state and prints the plan;
it does not contact PostgreSQL or invoke ingestion/import CLIs.
--run requires exported POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
POSTGRES_USER, POSTGRES_PASSWORD and the existing populated database.
Logs: tools/replay_probe/output/ingestion-batches/<UTC timestamp>-<PID>/
HELP
}

die() { final_reason=$*; printf 'STOP: %s\n' "$*" >&2; exit 1; }
run=0; max_batches=1; batch_limit=100; min_free_gib=40
new_parsed_total=0; imported_new_total=0; network_stops=0; no_progress_network=0
final_reason='unexpected error'
while (($#)); do
    case "$1" in
        --run) run=1; shift ;;
        --max-batches|--batch-limit|--min-free-gib)
            (($# >= 2)) || die "$1 needs a value"
            [[ "$2" =~ ^[1-9][0-9]{0,8}$ ]] || die "$1 needs a positive integer (up to 9 digits)"
            case "$1" in
                --max-batches) max_batches=$2 ;;
                --batch-limit) batch_limit=$2 ;;
                --min-free-gib) min_free_gib=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown argument: $1 (use --help)" ;;
    esac
done
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ "$(pwd -P)" == "$ROOT" ]] || die "Run from repository root: $ROOT"
PYTHON="$ROOT/../venv/bin/python"
OUTPUT="$ROOT/tools/replay_probe/output"
CHECKPOINT="$OUTPUT/target-replays/manifest.json"
AS_OF=2026-10-26T01:52:53Z
[[ -x "$PYTHON" ]] || die "Missing Python: $PYTHON"
[[ -f "$CHECKPOINT" ]] || die "Existing checkpoint is missing: $CHECKPOINT"
for module in ingest_target_replays import_replay_batch import_hero_snapshots import_spatial_snapshots import_enemy_observations; do
    [[ -f "$ROOT/tools/$module.py" ]] || die "Missing tools/$module.py"
done

# mkdir is atomic on macOS; unlike flock it needs no extra executable.
# SIGKILL can leave a stale lock. Inspect its PID directory before manually
# removing ONLY the empty PID/lock directories; never remove replay data.
LOCK="$OUTPUT/.ingestion-batches.lock"
mkdir "$LOCK" 2>/dev/null || die "Another runner or stale lock: $LOCK (inspect owner PID directory)"
child_pid=''
cleanup() {
    local status=$?
    if [[ -d "${LOG_DIR:-}" ]]; then
        printf 'SUMMARY: new_PARSE_OK=%s; fully_imported_new=%s; download_stops=%s; reason=%s; exit=%s\n' \
            "$new_parsed_total" "$imported_new_total" "$network_stops" "$final_reason" "$status" \
            | tee -a "$LOG_DIR/runner.log" || :
    fi
    rmdir "$LOCK/$$" "$LOCK" 2>/dev/null || :
}
trap cleanup EXIT
mkdir "$LOCK/$$"
LOG_DIR="$OUTPUT/ingestion-batches/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$LOG_DIR"
say() { printf '%s\n' "$*" | tee -a "$LOG_DIR/runner.log"; }
interrupted() {
    trap '' INT TERM
    final_reason="interrupted (${1:-signal})"
    if [[ -n "$child_pid" ]]; then
        # Ingester's existing SIGTERM handler saves INTERRUPTED; SQL tools
        # leave prior matches committed and roll back the active transaction.
        kill -TERM -- "-$child_pid" 2>/dev/null || kill -TERM "$child_pid" 2>/dev/null || :
        wait "$child_pid" 2>/dev/null || :
    fi
    say "STOP: interrupted; checkpoint/data retained. Logs: $LOG_DIR"
    exit 130
}
trap 'interrupted SIGINT' INT
trap 'interrupted SIGTERM' TERM

# Small read-only checks, not an ingestion implementation. Times/targets and
# replay payloads are exclusively handled by the existing five CLI modules.
inspect_state() {
    "$PYTHON" -B - "$CHECKPOINT" "$@" <<'PY'
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

path, action, *args = sys.argv[1:]
path = Path(path)
try:
    raw = path.read_bytes()
    state = json.loads(raw)
    if state['as_of'] != 1792979573 or not isinstance(state['matches'], dict):
        raise ValueError('Checkpoint does not match the fixed as-of window/schema')
    parsed = {k: v['dem_sha256'] for k, v in state['matches'].items() if v['status'] == 'PARSE_OK'}
    if action in ('disk', 'preflight'):
        from tools.ingest_target_replays import GIB, WORKING_RESERVE_BYTES
        free = shutil.disk_usage(path.parent).free
        required = int(args[0]) * GIB + WORKING_RESERVE_BYTES
        if free < required:
            raise ValueError(f'Low disk: {free / GIB:.1f} GiB free; need {args[0]} GiB + 2 GiB working reserve')
        if action == 'preflight':
            import psycopg
            from app.storage.postgres import postgres_connection_options_from_env
            if state.get('resume_after_epoch', 0) > time.time():
                raise ValueError(f"HTTP 429 cooldown until epoch {state['resume_after_epoch']}")
            tables = ('historical_matches', 'opendota_pro_matches',
                      'historical_match_economy_snapshots', 'historical_match_pov_snapshots',
                      'historical_match_hero_snapshots', 'historical_match_hero_items',
                      'historical_match_spatial_frames', 'historical_match_spatial_heroes',
                      'historical_match_enemy_observations', 'historical_match_death_predictions')
            with psycopg.connect(**postgres_connection_options_from_env(), connect_timeout=5,
                                 options='-c default_transaction_read_only=on') as connection:
                for table in tables:
                    if connection.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0] is None:
                        raise ValueError(f'Existing database lacks {table}; no schema will be initialized')
                if not connection.execute('SELECT 1 FROM historical_matches LIMIT 1').fetchone():
                    raise ValueError('Historical catalog is empty; check POSTGRES_*')
        print(f'Checks OK: {free / 1024**3:.1f} GiB free; {len(parsed)} PARSE_OK in checkpoint')
    elif action == 'decision':
        before_raw = Path(args[0]).read_bytes()
        before = json.loads(before_raw)
        previous = {k: v['dem_sha256'] for k, v in before['matches'].items() if v['status'] == 'PARSE_OK'}
        if any(parsed.get(k) != sha for k, sha in previous.items()):
            raise ValueError('Previously verified match/SHA changed')
        rc, reason = int(args[1]), state.get('stop_reason')
        normal = reason in ('LIMIT_REACHED', 'QUEUE_EXHAUSTED')
        partial = reason in ('REPEATED_ERRORS', 'HTTP_429', 'LOW_DISK_SPACE')
        if ((rc == 0 and not normal) or (rc == 1 and not partial) or rc not in (0, 1)
                or (rc != 0 and hashlib.sha256(raw).digest() == hashlib.sha256(before_raw).digest())):
            raise ValueError(f'Unconfirmed ingestion result: exit={rc}, checkpoint={reason}; no imports')
        breaker = 'none'
        if reason == 'REPEATED_ERRORS':
            breaker = 'unknown'
            # Only the fresh per-batch CLI log is evidence for auto-resume.
            # Missing, duplicate or inconsistent events must fail closed.
            events = []
            try:
                with Path(args[2]).open(encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and event.get('event') == 'circuit_breaker':
                            events.append(event)
            except OSError:
                pass
            if len(events) == 1:
                event = events[0]
                download, parser = event.get('download_errors'), event.get('parser_errors')
                if type(download) is int and type(parser) is int and min(download, parser) >= 0:
                    new_invalid = any(
                        v.get('status') == 'INVALID_ARTIFACT'
                        and before['matches'].get(k, {}).get('status') != 'INVALID_ARTIFACT'
                        for k, v in state['matches'].items()
                    )
                    if event.get('stage') == 'download' and download >= 3 and parser < 3 and not new_invalid:
                        breaker = 'download'
                    elif event.get('stage') == 'parser' and parser >= 3 and download < 3:
                        breaker = 'parser'
        print(len(parsed.keys() - previous.keys()), reason, breaker)
    elif action == 'report':
        report = json.loads(Path(args[0]).read_text())
        allowed = ('COMPATIBLE',) if args[1] == 'preflight' else ('IMPORTED', 'SKIPPED')
        rows = report['matches']
        if (report.get('errors') or report.get('fatal_error')
                or len(rows) != report['compatible_matches']
                or any(r['status'] not in allowed for r in rows)):
            raise ValueError('Importer report has errors or incomplete results')
        print(', '.join(f'{status}={sum(r["status"] == status for r in rows)}' for status in allowed))
    else:
        print(f'{len(parsed)} PARSE_OK; checkpoint stop={state.get("stop_reason")}')
except Exception as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
PY
}

run_cli() {
    local log=$1 rc
    shift
    # Own process group lets Ctrl-C stop the CLI and its parser descendants.
    "$PYTHON" -B -c 'import os,sys; os.setsid(); os.execv(sys.executable, [sys.executable,"-B","-m",*sys.argv[1:]])' \
        "$@" >"$log" 2>&1 &
    child_pid=$!
    if wait "$child_pid"; then rc=0; else rc=$?; fi
    child_pid=''
    return "$rc"
}

say "Logs: $LOG_DIR"
if ((run == 0)); then
    inspect_state summary | tee -a "$LOG_DIR/runner.log"
    say "DRY RUN: no DB connection, downloads, parsing or imports."
    say "Plan: up to $max_batches batches; limit=$batch_limit; minimum=$min_free_gib GiB."
    say "$PYTHON -B -m tools.ingest_target_replays --as-of $AS_OF --limit $batch_limit --min-free-gib $min_free_gib --run"
    say "Then preflight and --apply: import_replay_batch -> import_hero_snapshots -> import_spatial_snapshots -> import_enemy_observations."
    say "--run checks exported POSTGRES_* and the populated existing DB before every batch."
    final_reason='DRY_RUN'
    exit 0
fi

for ((batch=1; batch<=max_batches; batch++)); do
    say "Batch $batch/$max_batches: preflight"
    if checked=$(inspect_state preflight "$min_free_gib" 2>&1); then say "$checked"; else say "$checked"; die "Preflight failed; no batch started"; fi
    prefix="$LOG_DIR/batch-$batch"
    cp "$CHECKPOINT" "$prefix-checkpoint-before.json"
    say "Batch $batch: ingestion (limit $batch_limit); log: $prefix-ingest.log"
    ingest_rc=0
    run_cli "$prefix-ingest.log" tools.ingest_target_replays --as-of "$AS_OF" \
        --limit "$batch_limit" --min-free-gib "$min_free_gib" --run || ingest_rc=$?
    if decision=$(inspect_state decision "$prefix-checkpoint-before.json" "$ingest_rc" "$prefix-ingest.log" 2>&1); then
        read -r new_parsed reason breaker <<< "$decision"
    else
        say "$decision"; die "Ingestion failed without a confirmed safe checkpoint; see $prefix-ingest.log"
    fi
    say "Batch $batch: new PARSE_OK=$new_parsed; stop=$reason; breaker=$breaker; exit=$ingest_rc"
    new_parsed_total=$((new_parsed_total + new_parsed))
    if ((new_parsed > 0)); then no_progress_network=0; fi
    if [[ "$reason" == REPEATED_ERRORS && "$breaker" == download ]]; then
        network_stops=$((network_stops + 1))
        if ((new_parsed == 0)); then no_progress_network=$((no_progress_network + 1)); fi
    fi
    [[ "$reason" != LOW_DISK_SPACE ]] || die "LOW_DISK_SPACE; verified files retained for later import"
    if checked=$(inspect_state disk "$min_free_gib" 2>&1); then say "$checked"; else say "$checked"; die "Disk check failed; verified files retained for later import"; fi
    for module in import_replay_batch import_hero_snapshots import_spatial_snapshots import_enemy_observations; do
        for mode in preflight apply; do
            report="$prefix-$module-$mode.json"
            if ! checked=$(inspect_state disk "$min_free_gib" 2>&1); then
                say "$checked"; die "Disk check failed before $module $mode; progress retained"
            fi
            say "Batch $batch: $module $mode"
            args=(--report "$report")
            [[ "$mode" != apply ]] || args+=(--apply)
            run_cli "$prefix-$module-$mode.log" "tools.$module" "${args[@]}" || die "$module $mode failed; see $prefix-$module-$mode.log"
            if checked=$(inspect_state report "$report" "$mode" 2>&1); then say "$checked"; else say "$checked"; die "$module report verification failed"; fi
        done
    done
    imported_new_total=$((imported_new_total + new_parsed))
    case "$reason" in
        REPEATED_ERRORS)
            [[ "$breaker" == download ]] || die "REPEATED_ERRORS ($breaker breaker); imports verified, no auto-resume"
            if ((no_progress_network >= 3)); then
                die "Source persistently unavailable: 3 consecutive download-breaker batches without new PARSE_OK"
            fi
            if ((batch >= max_batches)); then
                final_reason="max-batches=$max_batches reached after download breaker"
                say "STOP: $final_reason"; exit 0
            fi
            say "Download breaker: imports verified; cooldown 600 seconds (no-progress streak=$no_progress_network/3)"
            sleep 600 &
            child_pid=$!
            if wait "$child_pid"; then child_pid=''; else
                cooldown_rc=$?; child_pid=''; die "Cooldown failed (exit $cooldown_rc)"
            fi
            continue ;;
        HTTP_429) die "HTTP_429; imports verified, no auto-resume" ;;
        QUEUE_EXHAUSTED) final_reason='queue exhausted'; say "STOP: $final_reason; imports verified"; exit 0 ;;
    esac
    if ((new_parsed == 0)); then
        final_reason='no new verified replay'; say "STOP: $final_reason; no next batch"; exit 0
    fi
    say "Batch $batch complete; all four importers verified"
done
final_reason="max-batches=$max_batches reached"
say "STOP: $final_reason. Logs: $LOG_DIR"
