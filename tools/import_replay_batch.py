"""Import manifest-confirmed replay matches/economy/POV; default is DB-read-only preflight.

Each match is committed separately through existing importers on one connection.
No schema creation, downloads, replay parsing, predictions or source-file writes.
"""

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from app.history.domain import HistoricalMatch
from app.replay_pov_features import build_pov_feature_rows
from app.storage.postgres import postgres_connection_options_from_env
from tools import import_compact_economy as economy
from tools import import_historical_matches as matches
from tools import import_pov_features as pov
from tools.ingest_target_replays import GOLD_LIMITATION, save, sha256


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tools/replay_probe/output"
MANIFESTS = (OUTPUT / "new-replay-batch-20261026/manifest.json",
             OUTPUT / "target-replays/manifest.json")
TABLES = (("historical_matches", matches.COLUMNS),
          ("historical_match_economy_snapshots", economy.COLUMNS),
          ("historical_match_pov_snapshots", pov.COLUMNS))
KEYS = ({"source", "source_match_id"},
        {"source", "source_match_id", "replay_tick"},
        {"source", "source_match_id", "replay_tick", "pov_team"})


@dataclass
class Bundle:
    match: HistoricalMatch
    economy: list[dict]
    pov: list[dict]
    replay_sha256: str
    retained: dict


def source_path(value):
    path = (ROOT / value).resolve()
    if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():
        raise ValueError(f"Missing or unsafe manifest source: {value}")
    return path


def discover(manifests, *, protected=None):
    protected = set() if protected is None else protected
    accepted, excluded, lineages, replays = {}, [], {}, {}
    for path in manifests:
        manifest_path = source_path(path)
        protected.add(manifest_path)
        document = json.loads(manifest_path.read_text())
        rows = document["matches"]
        if isinstance(rows, dict):
            if any(str(e["match_id"]) != key for key, e in rows.items()):
                raise ValueError("Manifest key/match_id mismatch")
            rows = list(rows.values())
        for entry in rows:
            for item in entry.get("artifacts", []):
                protected.add((ROOT / item["path"]).resolve())
            if entry.get("dem_path"):
                protected.add((ROOT / entry["dem_path"]).resolve())
            mid, sha = entry["match_id"], entry.get("dem_sha256")
            if type(mid) is not int or mid <= 0:
                raise ValueError("Invalid manifest match_id")
            if sha is not None:
                if (lineages.setdefault(mid, sha) != sha
                        or replays.setdefault(sha, mid) != mid):
                    raise ValueError("Conflicting manifest match/SHA identities")
            status = entry.get("status", entry.get("parser_status"))
            if entry.get("existing") or status not in ("PARSE_OK", "PASSED"):
                excluded.append(dict(match_id=mid, status=status,
                                     reason="EXISTING_REFERENCE" if entry.get("existing") else status))
                continue
            if mid in accepted:
                raise ValueError(f"Duplicate successful manifest entry: {mid}")
            accepted[mid] = entry
    return [accepted[mid] for mid in sorted(accepted)], excluded


def build_match(compact, metadata, entry, observed_at):
    mid = entry["match_id"]
    if (metadata.get("match_id") != mid or compact["match_id"] != mid
            or compact["league_id"] != entry["league_id"]
            or metadata.get("leagueid") != entry["league_id"]):
        raise ValueError("Compact/metadata/manifest identity or league mismatch")
    start = metadata.get("start_time")
    if type(start) is not int or start <= 0:
        raise ValueError("Missing factual start_time")
    if type(metadata.get("radiant_win")) is not bool or (
        metadata["radiant_win"] != (compact["winner"] == 2)
    ):
        raise ValueError("Winner disagreement")
    names, ids = [], []
    for side in ("radiant", "dire"):
        cid, oid = compact.get(f"{side}_team_id"), metadata.get(f"{side}_team_id")
        if cid and oid and cid != oid:
            raise ValueError(f"{side} team ID disagreement")
        names.append(compact.get(f"{side}_team_tag") or metadata.get(f"{side}_name"))
        if not isinstance(names[-1], str) or not names[-1].strip():
            raise ValueError(f"Missing factual {side} team name")
        ids.append(str(cid or oid) if cid or oid else None)
    if ids[0] is not None and ids[0] == ids[1]:
        raise ValueError("Opposing team IDs are identical")
    winner = 0 if compact["winner"] == 2 else 1
    league = metadata.get("league") or {}
    if league.get("leagueid", entry["league_id"]) != entry["league_id"]:
        raise ValueError("League metadata identity mismatch")
    league_name = league.get("name") or entry.get("league_name")
    series = metadata.get("series_id")
    result = HistoricalMatch(
        id=f"{economy.SOURCE}:{mid}", source=economy.SOURCE, source_match_id=str(mid),
        started_at=datetime.fromtimestamp(start, timezone.utc), ended_at=None,
        team_a_name=names[0], team_b_name=names[1],
        team_a_source_id=ids[0], team_b_source_id=ids[1],
        winner_name=names[winner], winner_source_id=ids[winner],
        winner_side="team_a" if winner == 0 else "team_b",
        tournament_name=league_name, league_name=league_name,
        league_source_id=str(entry["league_id"]),
        series_source_id=str(series) if type(series) is int and series > 0 else None,
        status="completed", ingested_at=observed_at,
    )
    matches.validate_matches([result])
    return result


def prepare(entry, observed_at):
    records = {}
    for item in entry["artifacts"]:
        path = source_path(item["path"])
        if path in records or sha256(path) != item["sha256"]:
            raise ValueError(f"Duplicate artifact or SHA mismatch: {path}")
        records[path] = item["sha256"]
    dem = source_path(entry["dem_path"])
    digest = records.get(dem) or sha256(dem)
    if digest != entry["dem_sha256"]:
        raise ValueError("DEM SHA mismatch")

    def role(suffixes):
        found = [p for p in records if any(str(p).endswith(s) for s in suffixes)]
        if len(found) != 1:
            raise ValueError(f"Expected exactly one manifest artifact: {suffixes}")
        return found[0]

    compact_path = role((".compact.json",))
    compact = economy.load_replay_trajectory(compact_path)
    if compact["replay_sha256"] != digest:
        raise ValueError("Compact replay SHA mismatch")
    metadata = json.loads(role((".opendota.json", "/opendota.json")).read_text())
    match = build_match(compact, metadata, entry, observed_at)
    econ = economy.build_snapshots(ROOT, compacts={entry["match_id"]: compact_path})
    spatial_path = role((".compact.spatial.json",))
    visibility_path = role((".compact.visibility.json",))
    spatial, visibility = [json.loads(p.read_text()) for p in (spatial_path, visibility_path)]
    for source in (spatial, visibility):
        if source["match_id"] != entry["match_id"] or source["replay_sha256"] != digest:
            raise ValueError("Sidecar identity/SHA mismatch")
        for name in ("clock_normalization_method", "zero_replay_tick", "pause_ticks",
                     "pause_seconds", "pause_witnessed", "game_end_time_seconds"):
            if source[name] != compact[name]:
                raise ValueError(f"Sidecar clock mismatch: {name}")
    expected, enemies = build_pov_feature_rows(spatial, visibility)
    rows = pov.build_pov_rows(role(("/team_pov_features.csv",)), expected_rows={
        entry["match_id"]: entry["channels"]["pov_team_rows"],
    })
    expected_values = {
        (r["replay_tick"], r["pov_team"]):
        (r["game_time_seconds"], *(r[k] for k in pov.FEATURES)) for r in expected
    }
    actual_values = {
        (r["replay_tick"], r["pov_team"]):
        (r["actual_game_time_seconds"], *(r[k] for k in pov.FEATURES)) for r in rows
    }
    if (actual_values != expected_values
            or any(r["replay_sha256"] != digest for r in rows)
            or len(econ) != entry["channels"]["economy_snapshots"]):
        raise ValueError("Economy/POV source completeness or sidecar disagreement")
    retained = dict(
        hero_snapshots=len(compact["snapshots"]),
        hero_rows=sum(len(f["players"]) for f in compact["snapshots"]),
        item_entries=sum(len(p["items"]) for f in compact["snapshots"] for p in f["players"]),
        spatial_snapshots=len(spatial["snapshots"]),
        spatial_hero_rows=sum(len(f["players"]) for f in spatial["snapshots"]),
        visibility_snapshots=len(visibility["snapshots"]),
        enemy_observation_rows=len(enemies),
        source_artifacts=[str(p.relative_to(ROOT)) for p in records],
    )
    if math.floor(compact["game_end_time_seconds"]) != metadata.get("duration"):
        # These two source values need not describe identical end boundaries.
        # Neither is a destination field: do not invent ended_at or alter clocks.
        retained["duration_disagreement"] = dict(
            compact_game_end_time_seconds=str(compact["game_end_time_seconds"]),
            opendota_duration_seconds=metadata.get("duration"),
            action="VALUES_RETAINED_IN_SOURCE; ENDED_AT_NULL; SNAPSHOT_TIMES_UNCHANGED",
        )
    return Bundle(match, econ, rows, digest, retained)


def schema_preflight(connection):
    """SELECT only: verify columns and the exact conflict keys used by importers."""
    with connection.cursor() as cursor:
        for (table, columns), expected_key in zip(TABLES, KEYS, strict=True):
            cursor.execute(sql.SQL("SELECT {} FROM {} LIMIT 0").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table)))
            cursor.execute(
                "SELECT array_agg(a.attname ORDER BY k.ordinality) "
                "FROM pg_constraint c JOIN LATERAL unnest(c.conkey) "
                "WITH ORDINALITY k(attnum, ordinality) ON true "
                "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum "
                "WHERE c.conrelid=%s::regclass AND c.contype IN ('p','u') GROUP BY c.oid",
                (table,),
            )
            if expected_key not in [set(row[0]) for row in cursor.fetchall()]:
                raise ValueError(f"Missing required unique key: {table}")


def match_values(match):
    values = asdict(match)
    return {k: v.value if isinstance(v, Enum) else v for k, v in values.items()}


def check_bundle(connection, bundle, *, complete=False):
    missing = {}
    identity = (bundle.match.source, bundle.match.source_match_id)
    values = match_values(bundle.match)
    with connection.cursor() as cursor:
        for (table, columns), records in zip(TABLES, ([values], bundle.economy, bundle.pov), strict=True):
            # ingested_at describes the first import, never a reason to rewrite a match.
            columns = tuple(c for c in columns if c != "ingested_at")
            cursor.execute(sql.SQL("SELECT {} FROM {} WHERE source=%s AND source_match_id=%s").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table)), identity)
            actual = set(cursor.fetchall())
            expected = {tuple(r[c] for c in columns) for r in records}
            if not actual <= expected or (complete and actual != expected):
                raise ValueError(f"Existing/source data conflict: {table}, {identity[1]}")
            missing[table] = len(expected - actual)
    return missing


def apply_bundle(connection, bundle):
    with connection.transaction():
        check_bundle(connection, bundle)
        inserted = dict(
            historical_matches=matches.apply_matches([bundle.match], connection=connection),
            historical_match_economy_snapshots=economy.apply_snapshots(bundle.economy, connection=connection),
            historical_match_pov_snapshots=pov.apply_rows(bundle.pov, connection=connection),
        )
        check_bundle(connection, bundle, complete=True)
    return inserted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="append", type=Path)
    parser.add_argument("--limit", type=int, help="Maximum non-complete matches to attempt")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=OUTPUT / "postgres-replay-import/report.json")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if not args.report.resolve().is_relative_to(OUTPUT.resolve()):
        parser.error("--report must be inside replay_probe/output")
    report = dict(mode="APPLY" if args.apply else "PREFLIGHT", matches=[], errors=[],
                  provenance=dict(economy="HISTORICAL_ORACLE", pov="HISTORICAL_TEAM_POV"),
                  current_gold_limitation=GOLD_LIMITATION)
    inputs = args.manifest or MANIFESTS
    protected = {(ROOT / path).resolve() for path in inputs}
    if args.report.resolve() in protected:
        parser.error("--report must not overwrite an input manifest")
    try:
        entries, report["excluded"] = discover(inputs, protected=protected)
        if args.report.resolve() in protected:
            parser.error("--report must not overwrite a source artifact")
        bundles = []
        observed_at = datetime.now(timezone.utc)
        for entry in entries:
            try:
                bundles.append(prepare(entry, observed_at))
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
                report["errors"].append(dict(match_id=entry["match_id"], phase="SOURCE", reason=str(exc)))
        planned = []
        with psycopg.connect(
            **postgres_connection_options_from_env(), connect_timeout=5, autocommit=True,
            options="-c default_transaction_read_only=on",
        ) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                schema_preflight(connection)
                for bundle in bundles:
                    try:
                        missing = check_bundle(connection, bundle)
                        planned.append((bundle, missing))
                    except ValueError as exc:
                        report["errors"].append(dict(match_id=bundle.match.source_match_id,
                                                     phase="DATABASE", reason=str(exc)))
        report["compatible_matches"] = len(planned)
        report["source_diagnostics"] = [
            dict(match_id=b.match.source_match_id, **b.retained["duration_disagreement"])
            for b, _ in planned if "duration_disagreement" in b.retained
        ]
        report["planned_missing"] = {table: sum(m[table] for _, m in planned) for table, _ in TABLES}
        report["retained_outside_database"] = {
            key: sum(b.retained[key] for b, _ in planned)
            for key in ("hero_snapshots", "hero_rows", "item_entries", "spatial_snapshots",
                        "spatial_hero_rows", "visibility_snapshots", "enemy_observation_rows")
        }
        if args.apply:
            attempted = 0
            with psycopg.connect(**postgres_connection_options_from_env(), connect_timeout=5,
                                 autocommit=True) as connection:
                for bundle, missing in planned:
                    mid = bundle.match.source_match_id
                    if not any(missing.values()):
                        report["matches"].append(dict(match_id=mid, status="SKIPPED"))
                        continue
                    if args.limit is not None and attempted >= args.limit:
                        continue
                    attempted += 1
                    try:
                        inserted = apply_bundle(connection, bundle)
                        report["matches"].append(dict(match_id=mid, status="IMPORTED",
                                                      inserted=inserted, replay_sha256=bundle.replay_sha256))
                    except (ValueError, psycopg.Error) as exc:
                        report["errors"].append(dict(match_id=mid, phase="APPLY", reason=str(exc)))
                    save(args.report, report)
        else:
            report["matches"] = [dict(match_id=b.match.source_match_id, status="COMPATIBLE",
                                      missing=m, replay_sha256=b.replay_sha256) for b, m in planned]
        save(args.report, report)
        print(json.dumps({k: v for k, v in report.items() if k not in ("matches", "excluded")}, indent=2))
        print("Results: " + json.dumps(dict(Counter(r["status"] for r in report["matches"]))))
        return 1 if report["errors"] else 0
    except (OSError, ValueError, KeyError, TypeError, psycopg.Error) as exc:
        report["fatal_error"] = str(exc)
        if args.report.resolve() not in protected:
            save(args.report, report)
        print(f"Replay import stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
