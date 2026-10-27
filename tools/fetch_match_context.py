"""Small historical context cache; default is local-only, file-read-only dry-run.

Targets: canonical compact artifacts of PARSE_OK in the existing checkpoint.
Reuse saved match JSON first. --run permits only free GET /matches/{match_id};
--limit caps GET attempts (local/cache reuse does not consume the limit).
No DEM, parser, database, POST requests or full API-response copies.
Full drafts are retrospective context, not proof of pre-draft/LIVE availability.
"""

import argparse
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import time

from app.replay_fetcher import ReplayFetcher, ReplayFetchError
from app.replay_trajectory import load_replay_trajectory
from tools.ingest_target_replays import ROOT, OUTPUT, GIB, WORKING_RESERVE_BYTES, save, sha256


CHECKPOINT = OUTPUT / "target-replays/manifest.json"
DESTINATION = OUTPUT / "match-context"
FLOOR = 40 * GIB
WRITE_RESERVE = WORKING_RESERVE_BYTES  # Keep the existing additional 2 GiB buffer.
PROVENANCE = "HISTORICAL_MATCH_CONTEXT"
CORE = ("match_id", "replay_sha256", "start_time", "league_id", "radiant_team_id",
        "dire_team_id", "players", "picks_bans", "patch")


class StorageError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def integer(value, name, *, minimum=1):
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise ValueError(f"Invalid {name}: {value!r}")
    return value


def identity(value, name):
    # Zero IDs and OpenDota's anonymous account sentinel are not player/team identities.
    if type(value) is int and (value == 0 or (name == "account_id" and value == 4294967295)):
        return None
    return integer(value, name)


def merge(api, known, name):
    if api is not None and known is not None and api != known:
        raise ValueError(f"{name} mismatch: OpenDota={api}, compact/catalog={known}")
    return api if api is not None else known


def source_path(value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(OUTPUT.resolve()):
        raise ValueError(f"Unsafe input path: {value}")
    return path


def discover(checkpoint):
    state = json.loads(source_path(checkpoint).read_text())
    records, protected = [], {source_path(checkpoint)}
    catalog = {r["match_id"]: r for r in state["candidates"]}
    for key, entry in state["matches"].items():
        if str(entry["match_id"]) != key:
            raise ValueError("Checkpoint match/key mismatch")
        for artifact in entry.get("artifacts", []):
            protected.add(source_path(artifact["path"]))
        if entry["status"] != "PARSE_OK":
            continue
        compact = [a for a in entry["artifacts"] if a["path"].endswith(".compact.json")]
        metadata = [a for a in entry["artifacts"] if a["path"].endswith((".opendota.json", "/opendota.json"))]
        if len(compact) != 1 or len(metadata) > 1 or entry["match_id"] not in catalog:
            raise ValueError(f"Ambiguous/missing canonical inputs: {entry['match_id']}")
        records.append(dict(entry=entry, compact=compact[0], metadata=metadata[0] if metadata else None,
                            catalog=catalog[entry["match_id"]]))
    canonical = {source_path(r["compact"]["path"]) for r in records}
    # Audit filenames only within replay output; never use this scan to select targets.
    other = []
    compact_files = sorted(OUTPUT.rglob("*.compact.json"))
    for path in compact_files:
        if path.resolve() not in canonical:
            try:
                document = json.loads(path.read_text())
                mid = document["match_id"]
                entry = state["matches"].get(str(mid), {})
                status = entry.get("status", "NOT_IN_CHECKPOINT")
                other.append(dict(path=str(path.relative_to(ROOT)), match_id=mid, status=status,
                                  checkpoint_reason=entry.get("reason"),
                                  clock_normalization_status=document.get("clock_normalization_status"),
                                  reason="DUPLICATE_NONCANONICAL_COPY" if status == "PARSE_OK" else status))
            except (ValueError, OSError, KeyError) as exc:
                other.append(dict(path=str(path), error=str(exc)))
    audit = dict(parse_ok_matches=len(records), canonical_compacts=len(canonical),
                 canonical_compacts_available=sum(p.is_file() for p in canonical),
                 compact_files=len(compact_files),
                 unique_compact_match_ids=len({r["entry"]["match_id"] for r in records}
                                              | {r["match_id"] for r in other if "match_id" in r}),
                 noncanonical_compacts=other)
    return sorted(records, key=lambda r: r["entry"]["match_id"]), audit, protected


def load_artifact(artifact, *, compact=False):
    path = source_path(artifact["path"])
    if sha256(path) != artifact["sha256"]:
        raise ValueError(f"Artifact SHA mismatch: {artifact['path']}")
    return load_replay_trajectory(path) if compact else json.loads(path.read_text())


def normalize(metadata, compact, record):
    if not isinstance(metadata, dict):
        raise ValueError("OpenDota match response must be an object")
    mid, sha = record["entry"]["match_id"], record["entry"]["dem_sha256"]
    if compact["match_id"] != mid or compact["replay_sha256"] != sha or integer(metadata.get("match_id"), "match_id") != mid:
        raise ValueError("match_id/replay SHA mismatch")
    league = merge(identity(metadata.get("leagueid"), "leagueid"),
                   integer(compact.get("league_id"), "compact league_id"), "league_id")
    merge(league, record["entry"]["league_id"], "checkpoint league_id")
    result = dict(match_id=mid, replay_sha256=sha, league_id=league,
        start_time=merge(identity(metadata.get("start_time"), "start_time"),
                         identity(record["catalog"].get("start_time"), "catalog start_time"), "start_time"),
        patch=integer(metadata.get("patch"), "patch", minimum=0))
    for side in ("radiant", "dire"):
        field = f"{side}_team_id"
        result[field] = merge(identity(metadata.get(field), field), identity(compact.get(field), field), field)
    roster = compact["snapshots"][0]["players"]
    if (len(roster) != 10 or {r["player_slot"] for r in roster} != set(range(10))
            or len({r["hero_id"] for r in roster}) != 10
            or Counter(r["team"] for r in roster) != {"RADIANT": 5, "DIRE": 5}):
        raise ValueError("Invalid compact primary hero roster")
    players = {}
    source_players = metadata.get("players")
    if source_players is not None and (not isinstance(source_players, list)
                                      or any(not isinstance(r, dict) for r in source_players)):
        raise ValueError("OpenDota players must be an array of objects or NULL")
    for row in source_players or []:
        raw = integer(row.get("player_slot"), "OpenDota player_slot", minimum=0)
        if raw not in (*range(5), *range(128, 133)):
            raise ValueError(f"Unsupported OpenDota slot: {raw}")
        slot, team = (raw, "RADIANT") if raw < 128 else (raw - 128 + 5, "DIRE")
        if slot in players:
            raise ValueError(f"Duplicate OpenDota player slot: {raw}")
        expected = next(r for r in roster if r["player_slot"] == slot)
        if integer(row.get("hero_id"), "OpenDota hero_id") != expected["hero_id"] or expected["team"] != team:
            raise ValueError(f"Hero/side mismatch at slot {raw}: API={row.get('hero_id')}, compact={expected['hero_id']}")
        if row.get("isRadiant") is not None and (type(row["isRadiant"]) is not bool
                                                or row["isRadiant"] != (team == "RADIANT")):
            raise ValueError(f"OpenDota side flag mismatch: {raw}")
        players[slot] = dict(player_slot=slot, opendota_player_slot=raw, team=team,
                             account_id=identity(row.get("account_id"), "account_id"), hero_id=expected["hero_id"])
    result["players"] = [players.get(r["player_slot"], dict(player_slot=r["player_slot"],
        opendota_player_slot=None, team=r["team"], account_id=None, hero_id=r["hero_id"]))
        for r in sorted(roster, key=lambda r: r["player_slot"])]
    draft = [dict(is_pick=r["is_pick"], team={2: "RADIANT", 3: "DIRE"}[r["team"]],
                  hero_id=r["hero_id"], order=None) for r in compact["picks_bans"]]
    indexed = {(r["is_pick"], r["team"], r["hero_id"]): r for r in draft}
    if len(indexed) != len(draft):
        raise ValueError("Duplicate compact draft action")
    seen, orders = set(), set()
    source_draft = metadata.get("picks_bans")
    if source_draft is not None and (not isinstance(source_draft, list)
                                    or any(not isinstance(r, dict) for r in source_draft)):
        raise ValueError("OpenDota picks_bans must be an array of objects or NULL")
    for row in source_draft or []:
        if type(row.get("is_pick")) is not bool or type(row.get("team")) is not int or row["team"] not in (0, 1):
            raise ValueError("Invalid OpenDota draft side/pick flag")
        key = row["is_pick"], ("RADIANT", "DIRE")[row["team"]], integer(row.get("hero_id"), "draft hero_id")
        if key not in indexed or key in seen:
            raise ValueError(f"Draft mismatch/duplicate: {key}")
        seen.add(key)
        order = integer(row.get("order"), "draft order", minimum=0)
        if order is not None and (order in orders or order >= len(draft)):
            raise ValueError(f"Invalid/duplicate draft order: {order}")
        if order is not None:
            orders.add(order)
        indexed[key]["order"] = order
    result["picks_bans"] = draft or None  # Missing draft is not proof of zero actions.
    return result


def coverage(context):
    players, draft = context["players"], context["picks_bans"]
    accounts = sum(r["account_id"] is not None for r in players)
    teams = sum(context[f"{s}_team_id"] is not None for s in ("radiant", "dire"))
    order = sum(r["order"] is not None for r in draft) if draft is not None else None
    return dict(account_id=accounts, account_id_null=10-accounts, teams=teams, team_null=2-teams,
        heroes=len(players), hero_null=10-len(players),
        picks=sum(r["is_pick"] for r in draft) if draft is not None else None,
        bans=sum(not r["is_pick"] for r in draft) if draft is not None else None,
        draft_null=int(draft is None), order=order,
        order_null=len(draft)-order if draft is not None else None,
        patch=int(context["patch"] is not None), patch_null=int(context["patch"] is None))


def cached_context(path, compact, record):
    value = json.loads(path.read_text())
    if (set(value) != set(CORE) | {"schema_version", "provenance", "content_sha256"}
            or value["schema_version"] != 1
            or value["content_sha256"] != digest({k: v for k, v in value.items() if k != "content_sha256"})
            or value["provenance"]["kind"] != PROVENANCE
            or value["provenance"]["compact_sha256"] != record["compact"]["sha256"]
            or value["provenance"]["metadata_source"] not in ("LOCAL_JSON", "OPENDOTA_GET")):
        raise ValueError("Invalid/stale context cache or checksum")
    metadata = dict(match_id=value["match_id"], start_time=value["start_time"], leagueid=value["league_id"],
        radiant_team_id=value["radiant_team_id"], dire_team_id=value["dire_team_id"], patch=value["patch"],
        players=[dict(player_slot=r["opendota_player_slot"], hero_id=r["hero_id"], account_id=r["account_id"])
                 for r in value["players"] if r["opendota_player_slot"] is not None],
        picks_bans=[dict(r, team={"RADIANT": 0, "DIRE": 1}[r["team"]]) for r in value["picks_bans"] or []])
    if normalize(metadata, compact, record) != {k: value[k] for k in CORE}:
        raise ValueError("Cached context/compact identity mismatch")
    return value


def check_space(size=0):
    free = shutil.disk_usage(OUTPUT).free
    if free < FLOOR + WRITE_RESERVE + size:
        raise StorageError(f"LOW_DISK_SPACE: free={free}, floor={FLOOR}, working_reserve={WRITE_RESERVE}")
    return free


def store_json(path, value):
    check_space(len(json.dumps(value, allow_nan=False).encode()) + 1)
    try:
        save(path, value)
    except OSError as exc:
        raise StorageError(f"WRITE_ERROR: {path}: {exc}") from exc


def cooldown(error):
    raw = getattr(error, "retry_after", None)
    try:
        seconds = float(raw)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - time.time()
        except (ValueError, TypeError, AttributeError):
            seconds = 120
    return time.time() + max(120, math.ceil(seconds) if math.isfinite(seconds) else 120)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--limit", type=int, help="Maximum GET attempts; local/cache reuse is unlimited")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    report = dict(mode="RUN" if args.run else "DRY_RUN", matches=[], requests=0, stop_reason="COMPLETE")
    try:
        records, report["input_audit"], protected = discover(args.checkpoint)
        if any(p.is_relative_to(DESTINATION.resolve()) for p in protected):
            raise ValueError("Context destination overlaps source artifacts")
        free = check_space()
        report["free_bytes"] = free
        report["warnings"] = ["Near 40 GiB floor + 2 GiB buffer; metadata only, no DEMs"] if free < FLOOR + WRITE_RESERVE + GIB else []
        if args.run:
            DESTINATION.mkdir(parents=True, exist_ok=True)
        lock = (DESTINATION / ".lock").open("a") if args.run else nullcontext()
        with lock as stream:
            if args.run:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state_path = DESTINATION / "state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            blocked = state.get("resume_after_epoch", 0) > time.time()
            if blocked:
                report["stop_reason"] = "COOLDOWN"
                report["resume_after_epoch"] = state["resume_after_epoch"]
            fetcher = ReplayFetcher(api_key="", request_delay_seconds=3, timeout=20) if args.run else None
            failures = 0
            for record in records:
                mid = record["entry"]["match_id"]
                item = dict(match_id=mid, status="ERROR", error=None, coverage=None)
                requested = False
                if report["stop_reason"] in ("LOW_DISK_SPACE", "WRITE_ERROR"):
                    item["status"] = "DEFERRED"
                    report["matches"].append(item)
                    continue
                try:
                    compact = load_artifact(record["compact"], compact=True)
                    baseline = normalize(dict(match_id=mid), compact, record)
                    item["coverage"] = coverage(baseline)
                    target = DESTINATION / f"{mid}.context.json"
                    if target.exists():
                        value = cached_context(target, compact, record)
                        item.update(status="CACHED", coverage=coverage(value))
                    else:
                        artifact = record["metadata"]
                        local = artifact is not None and source_path(artifact["path"]).is_file()
                        metadata = load_artifact(artifact) if local else None
                        if metadata is None and (not args.run or blocked or
                                (args.limit is not None and report["requests"] >= args.limit)):
                            item["status"] = "NEEDS_API" if not args.run else "DEFERRED"
                            if args.run and not blocked:
                                report["stop_reason"] = "LIMIT_REACHED"
                        else:
                            if metadata is None:
                                check_space()
                                report["requests"] += 1
                                requested = True
                                metadata = fetcher._fetch_opendota_json(f"matches/{mid}", max_attempts=1).payload
                            core = normalize(metadata, compact, record)
                            value = dict(core, schema_version=1, provenance=dict(kind=PROVENANCE,
                                metadata_source="LOCAL_JSON" if local else "OPENDOTA_GET",
                                metadata_path=artifact["path"] if local else None,
                                metadata_payload_sha256=digest(metadata), compact_path=record["compact"]["path"],
                                compact_sha256=record["compact"]["sha256"],
                                endpoint=f"https://api.opendota.com/api/matches/{mid}",
                                retrieved_at_utc=None if local else datetime.now(timezone.utc).isoformat(),
                                availability="HISTORICAL; LIVE/PREDRAFT availability not established; team_id is not a fixed roster"))
                            value["content_sha256"] = digest(value)
                            if args.run:
                                store_json(target, value)
                            item.update(status="LOCAL" if local else "FETCHED", coverage=coverage(core))
                            failures = 0 if not local else failures
                except ReplayFetchError as exc:
                    item["error"] = str(exc)[:500]
                    status = getattr(exc, "http_status", None)
                    item["http_status"] = status
                    if status == 429:
                        state["resume_after_epoch"] = cooldown(exc)
                        report.update(stop_reason="HTTP_429", resume_after_epoch=state["resume_after_epoch"])
                        try:
                            store_json(state_path, state)
                        except StorageError as storage:
                            report["stop_reason"] = "WRITE_ERROR"
                            item["error"] += f"; cannot persist cooldown: {storage}"
                        blocked = True
                    elif status is not None and 400 <= status < 500 and status not in (404, 410):
                        report["stop_reason"] = f"HTTP_{status}"
                        blocked = True
                    elif status not in (404, 410):
                        failures += 1
                    else:
                        failures = 0
                except StorageError as exc:
                    item["error"] = str(exc)
                    report["stop_reason"] = "LOW_DISK_SPACE" if str(exc).startswith("LOW_DISK_SPACE") else "WRITE_ERROR"
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    item["error"] = str(exc)[:500]
                    if requested:
                        failures += 1
                report["matches"].append(item)
                if failures >= 3 and not blocked:
                    report["stop_reason"] = "SOURCE_UNAVAILABLE"
                    blocked = True
            report["statuses"] = dict(Counter(r["status"] for r in report["matches"]))
            available = [r["coverage"] for r in report["matches"] if r["coverage"] is not None]
            report["coverage_totals"] = {key: sum(r[key] or 0 for r in available)
                                          for key in available[0]} if available else {}
            report["coverage_null_matches"] = {key: sum(r[key] is None for r in available)
                                                for key in available[0]} if available else {}
            report["free_bytes"] = shutil.disk_usage(OUTPUT).free
            if report["free_bytes"] < FLOOR + WRITE_RESERVE + GIB and not report["warnings"]:
                report["warnings"].append("Near 40 GiB floor + 2 GiB buffer; metadata only, no DEMs")
            if args.run and report["stop_reason"] not in ("LOW_DISK_SPACE", "WRITE_ERROR"):
                store_json(DESTINATION / "report.json", report)
        print(json.dumps(report, indent=2, allow_nan=False))
        return int(any(r["error"] for r in report["matches"]) or blocked)
    except (ValueError, OSError, KeyError, TypeError, StorageError, ReplayFetchError) as exc:
        print(f"Match context stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
