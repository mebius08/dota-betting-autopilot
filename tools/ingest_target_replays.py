"""Bounded, resumable replay batches using the existing selector/fetcher/Clarity.

Only --run downloads/parses. --limit bounds candidate attempts per invocation,
including deferred retries. Unattempted candidates precede previous failures;
terminal unavailable/invalid entries and SHA-verified PARSE_OK entries are skipped.
Retryable entries get at most three invocations. No database writes are performed.
"""

import argparse
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from app.replay_fetcher import (
    OpenDotaRequestError, ReplayDownloadError,
    ReplayFetcher, ReplayFetchError,
)
from app.replay_pov_features import build_pov_feature_rows, extract_pov_features
from app.replay_spatial_features import _position
from app.replay_trajectory import load_replay_trajectory
from app.storage.postgres import postgres_connection_options_from_env
from app.tournament_allowlist import load_tournament_allowlist
from tools.inspect_historical_snapshot import MATCH_SOURCES
from tools.select_target_matches import READ_ONLY, audit_catalog, parse_as_of


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tools/replay_probe/output"
PREVIOUS = OUTPUT / "new-replay-batch-20261026/manifest.json"
GOLD_LIMITATION = {
    "status": "NOT_FULLY_VERIFIABLE",
    "formula": "reliable_gold + unreliable_gold",
    "parser_can_default_missing_component_to_zero": True,
    "per_snapshot_component_missingness_exported": False,
    "absence_of_zero_substitution_proven": False,
}
TERMINAL = {"PARSE_OK", "PARSE_ERROR", "REPLAY_UNAVAILABLE", "INVALID_ARTIFACT"}
DOWNLOAD_DEADLINE_SECONDS = 300
GIB = 1024**3
# Retain this headroom above the user's floor for temporary files, parser bursts
# and checkpoint/log writes. The parser is checked every 250 ms as it runs.
WORKING_RESERVE_BYTES = 2 * GIB


class LowDiskSpace(RuntimeError):
    pass


def gib_to_bytes(value):
    """Both CLI spellings use binary GiB; round fractional byte floors upward."""
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 1:
            raise ValueError("Minimum free space must be finite and at least 1 GiB")
        return int((amount * GIB).to_integral_value(rounding=ROUND_CEILING))
    except InvalidOperation as exc:
        raise ValueError("Minimum free space must be a GiB number") from exc


def require_space(directory, min_free_bytes, next_write_bytes=0):
    if min_free_bytes <= 0:  # Explicitly disabled only by synthetic unit tests.
        return
    free = shutil.disk_usage(directory).free
    required = min_free_bytes + WORKING_RESERVE_BYTES + next_write_bytes
    if free < required:
        raise LowDiskSpace(
            f"low_disk_space: free={free}, required={required}; "
            f"floor={min_free_bytes}, working_reserve={WORKING_RESERVE_BYTES}, "
            f"next_write={next_write_bytes} bytes"
        )


@contextmanager
def download_deadline(seconds=DOWNLOAD_DEADLINE_SECONDS):
    """Bound the existing streaming downloader, including slow trickle responses."""
    def expired(signum, frame):
        raise ReplayDownloadError("replay_transfer_deadline_exceeded")

    started = time.monotonic()
    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def diverse_order(rows):
    """Canonical checkpoint order retained for compatibility, not execution priority."""
    groups = defaultdict(deque)
    seen = set()
    for row in sorted(rows, key=lambda r: (-r["start_time"], -r["match_id"])):
        if row["match_id"] in seen:
            raise ValueError("Duplicate candidate match_id")
        seen.add(row["match_id"])
        groups[row["league_id"]].append(row)
    ordered = []
    while groups:
        for league in list(groups):
            ordered.append(groups[league].popleft())
            if not groups[league]:
                del groups[league]
    return ordered


def candidates(connection, allowlist, as_of):
    report, ids = audit_catalog(connection, allowlist, as_of=as_of)
    with connection.transaction():
        connection.execute(READ_ONLY)
        rows = connection.execute(
            "SELECT match_id, league_id, league_name, start_time "
            "FROM opendota_pro_matches WHERE match_id = ANY(%s)", (ids,),
        ).fetchall()
    if len(rows) != len(ids) or {r["match_id"] for r in rows} != set(ids):
        raise ValueError("Catalog changed during selection; retry selection")
    for row in rows:
        allowlist.require(row["league_id"])
        if not as_of - 366 * 86400 <= row["start_time"] <= as_of:
            raise ValueError("Candidate outside the confirmed window")
    return report, diverse_order(rows)


def artifact(path):
    path = Path(path).resolve()
    return dict(path=str(path.relative_to(ROOT)), sha256=sha256(path),
                bytes=path.stat().st_size)


def verify_saved(entry):
    for item in entry["artifacts"]:
        path = (ROOT / item["path"]).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f"Missing/unsafe artifact for {entry['match_id']}: {path}")
        if sha256(path) != item["sha256"]:
            raise ValueError(f"SHA mismatch for {entry['match_id']}: {path}")
    dem = ROOT / entry["dem_path"]
    if sha256(dem) != entry["dem_sha256"]:
        raise ValueError(f"DEM SHA mismatch for {entry['match_id']}")


def seed_existing():
    """Index the eight confirmed outputs; never invoke a parser on these DEMs."""
    entries = {}
    for mid, paths in MATCH_SOURCES.items():
        compact = json.loads((ROOT / paths[0]).read_text())
        dem = ROOT / f"local-data/replays/{mid}.dem"
        sha = sha256(dem)
        if compact["match_id"] != int(mid) or compact["replay_sha256"] != sha:
            raise ValueError(f"Existing compact/DEM mismatch: {mid}")
        entries[mid] = dict(
            match_id=int(mid), league_id=compact["league_id"], existing=True,
            status="PARSE_OK", dem_path=str(dem.relative_to(ROOT)),
            dem_sha256=sha, artifacts=[artifact(ROOT / p) for p in paths],
            verification="PREVIOUSLY_PARSED_ARTIFACTS_SHA_VERIFIED",
        )
    previous = json.loads(PREVIOUS.read_text())
    for old in previous["matches"]:
        if old["parser_status"] != "PASSED":
            raise ValueError("Previous manifest does not confirm successful parsing")
        mid = str(old["match_id"])
        if mid in entries:
            raise ValueError("Duplicate identity in existing corpus")
        entry = dict(
            match_id=old["match_id"], league_id=old["league_id"], existing=True,
            status="PARSE_OK", dem_path=old["dem_path"],
            dem_sha256=old["dem_sha256"], artifacts=old["artifacts"],
            verification="PREVIOUSLY_PARSED_ARTIFACTS_SHA_VERIFIED",
        )
        verify_saved(entry)
        entries[mid] = entry
    shas = [e["dem_sha256"] for e in entries.values()]
    if len(shas) != len(set(shas)):
        raise ValueError("Different existing match IDs share a DEM SHA")
    return entries


def validate_parsed(directory, mid, league, sha, *, space_guard=None):
    compact_path = directory / f"{mid}.compact.json"
    compact = load_replay_trajectory(compact_path)
    spatial_path = directory / f"{mid}.compact.spatial.json"
    visibility_path = directory / f"{mid}.compact.visibility.json"
    spatial = json.loads(spatial_path.read_text())
    visibility = json.loads(visibility_path.read_text())
    clocks = ("clock_normalization_method", "zero_replay_tick", "pause_ticks",
              "pause_seconds", "pause_witnessed", "game_end_time_seconds")
    for source in (compact, spatial, visibility):
        if (source["match_id"] != mid or source["replay_sha256"] != sha
                or source["clock_normalization_status"] != "PROVEN"):
            raise ValueError("Parsed match/SHA/clock mismatch")
        if any(source[k] != compact[k] for k in clocks):
            raise ValueError("Conflicting replay clocks across channels")
    if compact["league_id"] != league:
        raise ValueError("Parsed league differs from selected catalog league")
    # Existing trajectory and POV validators check aggregates, 10 heroes, 5/5,
    # exact sidecar joins and raw visibility semantics. Add cross-channel roster
    # and exact Decimal clock checks; paused game time may stay unchanged.
    roster = None
    witnesses = {}
    for source in (compact, spatial, visibility):
        previous_time, previous_tick = Decimal(-1), -1
        if not source["snapshots"]:
            raise ValueError("Required channel has no snapshots")
        for frame in source["snapshots"]:
            observed = Decimal(str(frame["source_game_time_seconds"]))
            tick = frame["replay_tick"]
            if (not observed.is_finite() or observed < previous_time
                    or type(tick) is not int or tick <= previous_tick):
                raise ValueError("Nonmonotonic actual time/tick")
            if tick in witnesses and witnesses[tick] != observed:
                raise ValueError("Contradictory actual time at one replay tick")
            witnesses[tick] = observed
            current = {(p["player_slot"], p["hero_id"], p["team"])
                       for p in frame["players"]}
            if (len(frame["players"]) != 10 or len(current) != 10
                    or len({p[0] for p in current}) != 10
                    or len({p[1] for p in current}) != 10
                    or Counter(p[2] for p in current) != {"RADIANT": 5, "DIRE": 5}
                    or (roster is not None and current != roster)):
                raise ValueError("Invalid/unstable ten-hero 5/5 roster")
            roster = current
            previous_time, previous_tick = observed, tick
    times = [witnesses[tick] for tick in sorted(witnesses)]
    if times != sorted(times):
        raise ValueError("Cross-channel game clocks disagree")
    positions = Counter()
    for frame in spatial["snapshots"]:
        for player in frame["players"]:
            positions[player["position_status"]] += 1
            if player["position_status"] == "OBSERVED" and _position(player) is None:
                raise ValueError("OBSERVED GLOBAL position is not usable")
            if player["position_status"] != "OBSERVED" and (
                player["x"] is not None or player["y"] is not None
            ):
                raise ValueError("Unavailable GLOBAL position has fabricated coordinates")
    teams, enemies = build_pov_feature_rows(spatial, visibility)
    for row in enemies:
        if row["current_position_status"] != "OBSERVED" and (
            row["current_x"] is not None or row["current_y"] is not None
        ):
            raise ValueError("Unavailable enemy position has current coordinates")
    if space_guard:
        # CSVs contain these fixed, validated scalar fields. Twice their JSON
        # size plus headers conservatively budgets the remaining POV outputs.
        space_guard(4096 + 2 * sum(len(json.dumps(r)) for r in (*teams, *enemies)))
    extract_pov_features(spatial_path, visibility_path, directory / "pov")
    if space_guard:
        space_guard(0)
    return dict(
        economy_snapshots=len(compact["snapshots"]),
        hero_rows=10 * len(compact["snapshots"]),
        item_entries=sum(len(p["items"]) for f in compact["snapshots"] for p in f["players"]),
        spatial_snapshots=len(spatial["snapshots"]),
        visibility_snapshots=len(visibility["snapshots"]), pov_team_rows=len(teams),
        enemy_observation_rows=len(enemies), position_status_counts=dict(positions),
        monotonic_actual_time_and_tick=True, stable_ten_heroes_5v5=True,
        hidden_current_coordinates_absent=True, current_gold=GOLD_LIMITATION,
    )


def parse_replay(directory, mid, league, sha, *, space_guard=None):
    env = dict(os.environ)
    env.pop("JAVA_HOME", None)
    if space_guard:
        space_guard(0)
    with (directory / "parser.log").open("w") as log:
        with subprocess.Popen(
            [str(ROOT / "tools/replay_probe/invoke.sh"), "probeCompactReplay",
             str(directory / f"{mid}.dem"), str(directory / f"{mid}.compact.json")],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        ) as parser:
            deadline = time.monotonic() + 600
            try:
                while True:
                    if space_guard:
                        space_guard(0)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(parser.args, 600)
                    try:
                        code = parser.wait(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if code:
                    raise subprocess.CalledProcessError(code, parser.args)
            except BaseException:
                # Stop the entire owned parser group immediately on low disk,
                # timeout or interruption; keep all DEMs and produced artifacts.
                try:
                    os.killpg(parser.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                parser.wait()
                raise
    return validate_parsed(directory, mid, league, sha, space_guard=space_guard)


def check_url(url, mid):
    if not isinstance(url, str):
        raise ValueError("Replay URL is not text")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or not parsed.hostname.endswith((".valve.net", ".dota2.com.cn"))
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith(f"/570/{mid}_")
            or not parsed.path.endswith((".dem.bz2", ".dem.zst"))):
        raise ValueError("Replay URL is not the selected match's Valve artifact")


def process(entry, directory, fetcher, checkpoint, *, min_free_bytes=0):
    mid, league = entry["match_id"], entry["league_id"]
    guard = None
    if min_free_bytes:
        def guard(size):
            require_space(directory, min_free_bytes, size)
        # directory is created below, so check its existing parent first.
        require_space(directory.parent, min_free_bytes)
    directory.mkdir(parents=True, exist_ok=True)
    dem = directory / f"{mid}.dem"
    metadata_path = directory / "opendota.json"
    entry["source"] = f"https://api.opendota.com/api/matches/{mid}"
    if entry.get("dem_sha256"):
        if not dem.is_file() or sha256(dem) != entry["dem_sha256"]:
            raise ValueError("Existing downloaded DEM is missing or SHA changed")
        metadata = json.loads(metadata_path.read_text())
        if metadata.get("match_id") != mid or metadata.get("leagueid") != league:
            raise ValueError("Saved OpenDota metadata conflicts with downloaded DEM")
    else:
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text())
        else:
            metadata = fetcher._fetch_opendota_json(f"matches/{mid}", max_attempts=1).payload
            save(metadata_path, metadata)
        if (not isinstance(metadata, dict) or metadata.get("match_id") != mid
                or metadata.get("leagueid") != league):
            raise ValueError("OpenDota metadata conflicts with candidate identity/league")
        url = metadata.get("replay_url")
        if not url:
            entry.update(status="REPLAY_UNAVAILABLE", reason="NO_REPLAY_URL")
            return
        check_url(url, mid)
        entry.update(replay_url=url, source=f"https://api.opendota.com/api/matches/{mid}")
        # Recovery after a stop between atomic DEM rename and manifest save:
        # adopt only this exact match's file; parser must still prove identity.
        if not dem.exists():
            transferred = [0]

            def progress(event):
                transferred[0] = event.downloaded_bytes
                if getattr(event, "outcome", None) == "failure":
                    print(json.dumps(dict(
                        event="replay_download_failure", match_id=mid,
                        downloaded_bytes=event.downloaded_bytes,
                        total_bytes=getattr(event, "total_bytes", None),
                    )), flush=True)
                elif guard:
                    total = getattr(event, "total_bytes", None)
                    guard(max(0, total - event.downloaded_bytes) if total is not None else 0)

            with download_deadline():
                fetcher._download_replay(
                    replay_url=url, match_id=mid, destination=dem, progress_callback=progress,
                    space_guard=guard,
                )
            entry["downloaded_compressed_bytes"] = transferred[0]
        with dem.open("rb") as stream:
            if stream.read(8) != b"PBDEMS2\x00":
                raise ValueError("Invalid Source 2 DEM header")
        entry.update(status="DOWNLOAD_OK", download_status="DOWNLOAD_OK",
                     dem_path=str(dem.relative_to(ROOT)), dem_sha256=sha256(dem),
                     dem_bytes=dem.stat().st_size)
        checkpoint()
    channels = parse_replay(directory, mid, league, entry["dem_sha256"], space_guard=guard)
    if guard:
        guard(0)
    save(directory / "quality.json", channels)
    paths = [dem, metadata_path, directory / "quality.json",
             directory / f"{mid}.compact.json", directory / f"{mid}.compact.spatial.json",
             directory / f"{mid}.compact.visibility.json",
             directory / "pov/team_pov_features.csv", directory / "pov/enemy_observations.csv"]
    entry.update(status="PARSE_OK", channels=channels,
                 artifacts=[artifact(path) for path in paths])


def pending_order(state):
    """Newest actual start_time first, independent of league; do not edit checkpoint.

    Freeze both parts before processing: a new failure cannot re-enter this run.
    Previously interrupted/downloaded work is also deferred behind fresh IDs.
    Equal dates use ascending match_id; deferred work stays behind all fresh IDs.
    """
    fresh, deferred, seen = [], [], set()
    for row in state["candidates"]:
        key = str(row["match_id"])
        entry = state["matches"].get(key, {})
        if key in seen or entry.get("status") in TERMINAL or entry.get("attempts", 0) >= 3:
            continue
        seen.add(key)
        prior = entry.get("attempts", 0) > 0 or entry.get("status") in (
            "RETRYABLE_ERROR", "IN_PROGRESS", "DOWNLOAD_OK",
        )
        (deferred if prior else fresh).append(row)
    def priority(row):
        return -row["start_time"], row["match_id"]
    return sorted(fresh, key=priority) + sorted(deferred, key=priority)


def execute_batch(state, path, fetcher, *, limit, min_free_bytes, max_errors=3):
    attempts, consecutive_errors = 0, 0
    state["stop_reason"] = "RUNNING"
    state.pop("disk_space_diagnostic", None)
    for row in pending_order(state):
        key = str(row["match_id"])
        if attempts >= limit:
            state["stop_reason"] = "LIMIT_REACHED"
            break
        try:
            require_space(path.parent, min_free_bytes)
        except LowDiskSpace as exc:
            state["stop_reason"] = "LOW_DISK_SPACE"
            state["disk_space_diagnostic"] = str(exc)
            break
        attempts += 1
        entry = state["matches"].setdefault(key, dict(row, existing=False))
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["status"] = "IN_PROGRESS"
        entry.pop("reason", None)
        entry.pop("http_status", None)
        entry.pop("disk_space_diagnostic", None)
        save(path, state)
        try:
            process(entry, path.parent / key, fetcher, lambda: save(path, state),
                    min_free_bytes=min_free_bytes)
            consecutive_errors = 0
        except LowDiskSpace as exc:
            entry.update(status="RETRYABLE_ERROR", reason="low_disk_space",
                         disk_space_diagnostic=str(exc))
            state["stop_reason"] = "LOW_DISK_SPACE"
            state["disk_space_diagnostic"] = str(exc)
        except (OpenDotaRequestError, ReplayDownloadError) as exc:
            status = getattr(exc, "http_status", None)
            unavailable = status in (404, 410)
            invalid = isinstance(exc, ReplayDownloadError) and exc.stage == "replay_decompression"
            entry.update(status="REPLAY_UNAVAILABLE" if unavailable else
                         "INVALID_ARTIFACT" if invalid else "RETRYABLE_ERROR",
                         reason=str(exc), http_status=status)
            consecutive_errors = 0 if unavailable else consecutive_errors + 1
            if status == 429:
                # Stop rather than repeatedly consume the free API quota.
                retry_after = getattr(exc, "retry_after", None)
                headers = getattr(exc.__cause__, "headers", None)
                if retry_after is None and headers is not None:
                    retry_after = headers.get("Retry-After")
                try:
                    delay = max(120, int(retry_after))
                except (ValueError, TypeError):
                    from email.utils import parsedate_to_datetime
                    try:
                        delay = max(120, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError, AttributeError):
                        delay = 120
                state["resume_after_epoch"] = time.time() + delay
                state["stop_reason"] = "HTTP_429"
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            entry.update(status="INVALID_ARTIFACT", reason=str(exc))
            consecutive_errors += 1
        except subprocess.CalledProcessError as exc:
            entry.update(status="PARSE_ERROR", reason=str(exc),
                         parser_log=str((path.parent / key / "parser.log").relative_to(ROOT)))
            consecutive_errors += 1
        except (subprocess.SubprocessError, OSError, ReplayFetchError) as exc:
            entry.update(status="RETRYABLE_ERROR", reason=str(exc))
            consecutive_errors += 1
        finally:
            save(path, state)
        print(json.dumps({k: entry.get(k) for k in
                          ("match_id", "league_id", "status", "reason", "channels")}), flush=True)
        if state["stop_reason"] in ("HTTP_429", "LOW_DISK_SPACE"):
            break
        if consecutive_errors >= max_errors:
            state["stop_reason"] = "REPEATED_ERRORS"
            break
    else:
        state["stop_reason"] = "QUEUE_EXHAUSTED"
    state["last_run_attempted"] = attempts
    save(path, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=parse_as_of, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT / "target-replays")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--min-free-gib", "--min-free-gb", dest="min_free_bytes",
                        type=gib_to_bytes, default=40 * GIB,
                        help="Remaining binary GiB (2^30 bytes), default 40, plus 2 GiB working reserve. "
                             "--min-free-gb is a legacy alias with the SAME GiB units.")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    directory = args.output_dir.resolve()
    if not directory.is_relative_to(OUTPUT.resolve()):
        parser.error("All generated files must be inside gitignored replay_probe/output")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    # A second invocation cannot download or overwrite this checkpoint concurrently.
    try:
        with (directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            allowlist = load_tournament_allowlist()
            with psycopg.connect(
                **postgres_connection_options_from_env(), autocommit=True,
                options="-c default_transaction_read_only=on", connect_timeout=5,
                row_factory=dict_row,
            ) as connection:
                report, rows = candidates(connection, allowlist, args.as_of)
            if path.exists():
                state = json.loads(path.read_text())
                if (state["as_of"] != args.as_of
                        or state["allowlist_ids"] != list(allowlist.league_ids)
                        or state["candidates"] != rows):
                    raise ValueError("Checkpoint selection changed; use a separate output directory")
                for entry in state["matches"].values():
                    if entry["status"] == "PARSE_OK":
                        verify_saved(entry)
            else:
                state = dict(version=1, as_of=args.as_of, selection=report,
                             allowlist_ids=list(allowlist.league_ids), candidates=rows,
                             matches=seed_existing(), current_gold_limitation=GOLD_LIMITATION)
            if state.get("resume_after_epoch", 0) > time.time():
                raise ValueError(f"Rate limit cooldown until {state['resume_after_epoch']}")
            print(json.dumps(dict(selected=len(rows), verified_existing=sum(
                e.get("existing", False) for e in state["matches"].values()),
                mode="RUN" if args.run else "DRY_RUN")), flush=True)
            if args.run:
                fetcher = ReplayFetcher(api_key="", request_delay_seconds=3,
                                        timeout=30, replay_response_timeout=15)
                save(path, state)
                try:
                    execute_batch(state, path, fetcher, limit=args.limit,
                                  min_free_bytes=args.min_free_bytes)
                except KeyboardInterrupt:
                    state["stop_reason"] = "INTERRUPTED"
                    save(path, state)
                    return 130
                print(json.dumps(dict(stop_reason=state["stop_reason"], statuses=dict(
                    Counter(e["status"] for e in state["matches"].values())))), flush=True)
                return 0 if state["stop_reason"] in ("LIMIT_REACHED", "QUEUE_EXHAUSTED") else 1
    except (ValueError, OSError, KeyError, psycopg.Error) as exc:
        print(f"Replay batch stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
