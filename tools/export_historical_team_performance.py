"""Read-only PostgreSQL team-result JSONL export; no --out means dry-run.

The 24-hour default buffer is a conservative policy, not proof of wall-clock
completion/result availability. Coverage of the supplied catalog is metadata,
never a numeric predictor. No API, schema initialization or database writes.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row

from app.analytics.historical_match_context import DEFAULT_CACHE_DIR, _match_id, load_historical_match_context
from app.analytics.historical_team_performance import FIELDS, SOURCE, _positive, build_historical_team_performance
from app.storage.postgres import postgres_connection_options_from_env
from tools.export_historical_experience import OUTPUT, output_path, write_jsonl


CHECKPOINT = OUTPUT / "target-replays/manifest.json"
COLUMNS = ("match_id", *FIELDS)
QUERY = f"SELECT {', '.join(COLUMNS)} FROM public.opendota_pro_matches ORDER BY match_id"
DEFAULT_BUFFER_SECONDS = 24 * 3600


def read_catalog():
    # Enforce read-only at connection startup, without a second SQL query or a
    # repository constructor that might initialize schemas. One SELECT only.
    with psycopg.connect(**postgres_connection_options_from_env(), row_factory=dict_row,
                         connect_timeout=5,
                         options="-c default_transaction_read_only=on -c statement_timeout=30000") as connection:
        return connection.execute(QUERY).fetchall()


def catalog_coverage(rows):
    unique, duplicates = {}, 0
    for row in rows:
        mid = _positive(row.get("match_id"), "match_id")
        if mid is None:
            raise ValueError("Catalog row has no confirmed match_id")
        selected = {field: row.get(field) for field in COLUMNS}
        if mid in unique:
            if any(type(unique[mid][k]) is not type(selected[k]) or unique[mid][k] != selected[k]
                   for k in COLUMNS):
                raise ValueError(f"Conflicting catalog match_id: {mid}")
            duplicates += 1
        else:
            unique[mid] = selected
    fields = {name: Counter(known=0, null=0, zero_unknown=0) for name in (
        "start_time", "duration_seconds", "radiant_team_id", "dire_team_id", "radiant_win",
    )}
    complete = wins = losses = unknown_source = 0
    for row in unique.values():
        valid = row["source"] == SOURCE
        unknown_source += not valid
        for name, counts in fields.items():
            value = row[name]
            if name == "radiant_win":
                if value is not None and type(value) is not bool:
                    raise ValueError(f"Invalid radiant_win in match {row['match_id']}")
                known = value is not None  # False is a confirmed result.
            else:
                known = _positive(value, name) is not None
            counts["null" if value is None else "known" if known else "zero_unknown"] += 1
            valid = valid and known
        if row["radiant_team_id"] is not None and row["radiant_team_id"] == row["dire_team_id"]:
            valid = False
        complete += valid
        wins += row["radiant_win"] is True
        losses += row["radiant_win"] is False
    summary = dict(rows_read=len(rows), unique_matches=len(unique), duplicate_rows=duplicates,
                   fields={k: dict(v) for k, v in fields.items()}, usable_result_records=complete,
                   radiant_win_true=wins, radiant_win_false=losses, unconfirmed_source_records=unknown_source)
    return [unique[mid] for mid in sorted(unique)], summary


def load_targets(checkpoint, cache_dir, limit=None):
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    targets = []
    for key, entry in state["matches"].items():
        if entry["status"] != "PARSE_OK":
            continue
        mid = _match_id(entry["match_id"])
        if key != str(mid):
            raise ValueError("Checkpoint match identity mismatch")
        context = load_historical_match_context(mid, cache_dir=cache_dir)
        expected_sha = entry.get("dem_sha256")
        if not isinstance(expected_sha, str) or context["replay_sha256"].lower() != expected_sha.lower():
            raise ValueError(f"Checkpoint/context replay SHA mismatch: {mid}")
        targets.append(context)
    targets.sort(key=lambda row: (row["start_time"] is None, row["start_time"] or 0, row["match_id"]))
    if not targets:
        raise ValueError("No PARSE_OK target contexts found")
    return targets if limit is None else targets[:limit]


def buffered_history(target, rows, buffer_seconds):
    counts = Counter(target_match=0, unknown_timing=0, estimated_end_not_before_target=0, within_buffer=0)
    if target["start_time"] is None:
        return [], dict(evaluated=False, time_eligible_records=None, excluded=dict.fromkeys(counts))
    selected = []
    for row in rows:
        if row["match_id"] == target["match_id"]:
            counts["target_match"] += 1
            continue
        start, duration = row["start_time"], row["duration_seconds"]
        if start is None or duration is None or start <= 0 or duration <= 0:
            counts["unknown_timing"] += 1
        elif start + duration >= target["start_time"]:
            counts["estimated_end_not_before_target"] += 1
        elif start + duration + buffer_seconds >= target["start_time"]:
            counts["within_buffer"] += 1
        else:
            selected.append(row)
    return selected, dict(evaluated=True, time_eligible_records=len(selected), excluded=dict(counts))


def build_records(targets, rows, buffer_seconds=DEFAULT_BUFFER_SECONDS):
    if type(buffer_seconds) is not int or buffer_seconds <= 0:
        raise ValueError("Time buffer must be positive seconds")
    catalog, summary = catalog_coverage(rows)
    by_id = {row["match_id"]: row for row in catalog}
    records, seen = [], set()
    for target in sorted(targets, key=lambda r: (r["start_time"] is None, r["start_time"] or 0, r["match_id"])):
        mid = target["match_id"]
        if mid in seen:
            raise ValueError(f"Duplicate target match_id: {mid}")
        seen.add(mid)
        stored = by_id.get(mid)
        if stored is not None:
            for field in ("start_time", "radiant_team_id", "dire_team_id"):
                if target[field] is not None and stored[field] not in (None, 0) and target[field] != stored[field]:
                    raise ValueError(f"Catalog/context {field} mismatch: {mid}")
        selected, filtering = buffered_history(target, catalog, buffer_seconds)
        result = build_historical_team_performance(target, selected)
        result.update(replay_sha256=target["replay_sha256"], context_provenance=target["provenance"],
            catalog_coverage=summary, buffer_coverage=filtering,
            temporal_policy=dict(buffer_seconds=buffer_seconds,
                eligibility="start_time + duration_seconds + buffer_seconds < target_start_time",
                time_origin="OpenDota catalog start_time and duration_seconds",
                estimated_end_is_confirmed_wall_clock=False, result_availability_proven=False,
                limitation="Pauses and publication delays are not measured; buffer is conservative, not proof."))
        records.append(result)
    return records, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--buffer-hours", type=int, default=24)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.limit is not None and args.limit <= 0 or args.buffer_hours <= 0:
            raise ValueError("--limit and --buffer-hours must be positive")
        destination = output_path(args.out, args.cache_dir) if args.out else None
        targets = load_targets(args.checkpoint, args.cache_dir, args.limit)
        records, summary = build_records(targets, read_catalog(), args.buffer_hours * 3600)
        if destination is not None:
            write_jsonl(destination, records)
        print(json.dumps(dict(mode="EXPORT" if destination else "DRY_RUN", target_matches=len(records),
            catalog_coverage=summary, buffer_seconds=args.buffer_hours*3600,
            targets=[dict(match_id=r["match_id"], usable_history_records=r["coverage"]["eligible_completed_matches"],
                          relevant_history_records=r["coverage"]["eligible_relevant_matches"],
                          buffer_coverage=r["buffer_coverage"]) for r in records],
            out=str(destination) if destination else None), sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, psycopg.Error) as exc:
        print(f"Historical team-performance export stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
