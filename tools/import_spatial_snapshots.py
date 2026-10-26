"""Import manifest-confirmed GLOBAL spatial observations; default is read-only preflight.

No Team POV/visibility/last-seen payloads, DEM parsing or interpolation. Exact
NUMERIC observations inherit replay SHA/time/provenance through their frame FK.
--apply creates only the two additive tables and commits one complete match at
a time. --limit bounds incomplete matches attempted; completed matches are checked.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sys

import psycopg
from psycopg import sql

from app.replay_trajectory import load_replay_trajectory
from app.storage.postgres import postgres_connection_options_from_env
from tools import import_hero_snapshots as heroes


ROOT, OUTPUT, MANIFESTS = heroes.ROOT, heroes.OUTPUT, heroes.MANIFESTS
SCHEMA = ROOT / "app/storage/postgres_spatial_schema.sql"
FRAME_TABLE = "historical_match_spatial_frames"
HERO_TABLE = "historical_match_spatial_heroes"
KEY = ("source", "source_match_id", "replay_tick")
FRAME_COLUMNS = (*KEY, "actual_game_time_seconds", "replay_sha256", "provenance",
                 "clock_normalization_status", "clock_normalization_method",
                 "coordinate_system", "coordinate_units")
IDENTITY = ("player_slot", "team", "hero_id", "selected_hero_handle",
            "hero_entity_handle", "hero_entity_class")
POSITION = ("life_state", "position_status", "x", "y", "cell_x", "cell_y", "offset_x", "offset_y")
HERO_COLUMNS = (*KEY, *IDENTITY, *POSITION)
TABLES = ((FRAME_TABLE, FRAME_COLUMNS, KEY),
          (HERO_TABLE, HERO_COLUMNS, (*KEY, "player_slot")))
CLOCK = ("clock_normalization_status", "clock_normalization_method", "zero_replay_tick",
         "pause_ticks", "pause_seconds", "pause_witnessed", "game_end_time_seconds")
SOURCE = "replay_clarity"


@dataclass
class SpatialData:
    match_id: int
    replay_sha256: str
    frames: list[tuple]
    players: list[tuple]
    compact_clocks: set[tuple]
    roster: set[tuple]


def discover(manifests):
    records, excluded, protected = heroes.discover(manifests)
    sidecars = {}
    for manifest in manifests:
        entries = json.loads(heroes.batch.source_path(manifest).read_text())["matches"]
        for entry in entries.values() if isinstance(entries, dict) else entries:
            if entry.get("status", entry.get("parser_status")) not in ("PARSE_OK", "PASSED"):
                continue
            found = [a for a in entry["artifacts"] if a["path"].endswith(".compact.spatial.json")]
            if len(found) != 1:
                raise ValueError(f"Expected one canonical spatial sidecar: {entry['match_id']}")
            if sidecars.setdefault(entry["match_id"], found[0]) != found[0]:
                raise ValueError(f"Conflicting spatial manifest references: {entry['match_id']}")
    return [(*record, sidecars[record[0]]) for record in records], excluded, protected


def integer(value, name, *, maximum=2**63 - 1, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def number(value, name, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite():
        raise ValueError(f"Invalid/nonfinite {name}")
    return Decimal(value)


def prepare(record):
    mid, digest, compact_artifact, spatial_artifact = record
    paths = []
    for artifact in (compact_artifact, spatial_artifact):
        path = heroes.batch.source_path(artifact["path"])
        if heroes.sha256(path) != artifact["sha256"]:
            raise ValueError(f"Artifact SHA mismatch: {mid}, {path}")
        paths.append(path)
    load_replay_trajectory(paths[0])  # Existing compact schema/aggregate validation.
    compact, spatial = [json.loads(p.read_text(), parse_float=Decimal) for p in paths]
    for doc in (compact, spatial):
        if doc["match_id"] != mid or doc["replay_sha256"] != digest:
            raise ValueError(f"Match/replay SHA mismatch: {mid}")
    coordinate = spatial["coordinate_system"]
    if (spatial["artifact_kind"] != "replay_hero_positions" or spatial["schema_version"] != 1
            or spatial["snapshot_interval_seconds"] != 10
            or coordinate["name"] != "SOURCE2_NATIVE_CELL_GRID"
            or not isinstance(coordinate["units"], str) or not coordinate["units"]
            or spatial["clock_normalization_status"] != "PROVEN"):
        raise ValueError("Expected confirmed GLOBAL Source 2 spatial sidecar")
    if any(spatial[k] != compact[k] for k in CLOCK):
        raise ValueError("Compact/spatial replay clock mismatch")
    roster = {(p["player_slot"], p["team"], p["hero_id"])
              for p in compact["snapshots"][0]["players"]}
    compact_clocks = {(number(f["source_game_time_seconds"], "compact time"), f["replay_tick"])
                      for f in compact["snapshots"]}
    clocks = {tick: at for at, tick in compact_clocks}
    frames, players = [], []
    previous_time, previous_tick = Decimal(-1), -1
    if not isinstance(spatial["snapshots"], list) or not spatial["snapshots"]:
        raise ValueError("Spatial snapshots must be a nonempty list")
    for frame in spatial["snapshots"]:
        at = number(frame["source_game_time_seconds"], "actual time")
        tick = integer(frame["replay_tick"], "replay tick")
        if (at < 0 or at <= previous_time or tick <= previous_tick
                or at != frame["game_time_seconds"] or frame.get("game_paused") is True
                or frame.get("synthetic") is True):
            raise ValueError("Non-increasing/invalid actual unpaused time/tick")
        previous_time, previous_tick = at, tick
        if tick in clocks and clocks[tick] != at:
            raise ValueError("Conflicting actual time for replay tick")
        clocks[tick] = at
        key = (SOURCE, str(mid), tick)
        frames.append((*key, at, digest, "HISTORICAL_ORACLE", spatial["clock_normalization_status"],
                       spatial["clock_normalization_method"], coordinate["name"], coordinate["units"]))
        current, handles = set(), set()
        if not isinstance(frame["players"], list) or len(frame["players"]) != 10:
            raise ValueError("Spatial frame requires ten primary heroes")
        for p in sorted(frame["players"], key=lambda p: p["player_slot"]):
            slot = integer(p["player_slot"], "player slot", maximum=9)
            hero = integer(p["hero_id"], "hero ID", maximum=2**31 - 1)
            current.add((slot, p["team"], hero))
            selected, entity = [integer(p[k], k, nullable=True)
                                for k in ("selected_hero_handle", "hero_entity_handle")]
            if entity is not None:
                if entity in handles:
                    raise ValueError("Duplicate primary hero entity handle")
                handles.add(entity)
            if selected is not None and entity is not None and selected != entity:
                raise ValueError("Entity is not the selected primary hero")
            life = integer(p["life_state"], "life state", maximum=2**31 - 1, nullable=True)
            status = p["position_status"]
            if not isinstance(status, str) or not status:
                raise ValueError("Missing source position status")
            coordinates = [number(p[k], k, nullable=True) for k in ("x", "y")]
            cells = [integer(p[k], k, maximum=65535, nullable=True) for k in ("cell_x", "cell_y")]
            offsets = [number(p[k], k, nullable=True) for k in ("offset_x", "offset_y")]
            name = p["hero_entity_class"]
            if name is not None and (not isinstance(name, str) or not name.startswith("CDOTA_Unit_Hero_")):
                raise ValueError("Invalid hero entity class")
            values = (*coordinates, *cells, *offsets)
            if status == "OBSERVED":
                if (any(v is None for v in values) or life != 0 or selected is None
                        or entity is None or name is None):
                    raise ValueError("Observed position has missing/contradictory identity or life state")
                for world, cell, offset in zip(coordinates, cells, offsets, strict=True):
                    # The exporter sums binary floats. Validate provenance within its
                    # existing tolerance; store every original decimal unchanged.
                    if not 0 <= offset < 256 or abs(world - (cell * 128 + offset)) > Decimal("0.000001"):
                        raise ValueError("Invalid Source 2 cell/offset coordinates")
            elif any(v is not None for v in values):
                raise ValueError("Unavailable position contains coordinates")
            if status == "HERO_NOT_ALIVE" and life in (None, 0):
                raise ValueError("HERO_NOT_ALIVE contradicts life state")
            players.append((*key, slot, p["team"], hero, selected, entity, name, life, status,
                            *coordinates, *cells, *offsets))
        if (current != roster or len({r[0] for r in current}) != 10
                or len({r[2] for r in current}) != 10
                or Counter(r[1] for r in current) != {"RADIANT": 5, "DIRE": 5}):
            raise ValueError("Invalid/duplicate/changing primary hero roster or teams")
    ordered = [at for _, at in sorted(clocks.items())]
    # Equal game times can be witnessed at different ticks around pauses. Never
    # derive ticks from seconds or require a fixed tick/time rate.
    if any(a > b for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Compact/spatial time runs backwards with replay ticks")
    return SpatialData(mid, digest, frames, players, compact_clocks, roster)


def check_parent(connection, data):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM historical_matches WHERE source=%s AND source_match_id=%s",
                       (SOURCE, str(data.match_id)))
        if cursor.fetchone() is None:
            raise ValueError(f"Match is absent from PostgreSQL: {data.match_id}")
        cursor.execute("SELECT DISTINCT actual_game_time_seconds,replay_tick,replay_sha256 "
                       "FROM historical_match_hero_snapshots WHERE source=%s AND source_match_id=%s",
                       (SOURCE, str(data.match_id)))
        if set(cursor.fetchall()) != {(at, tick, data.replay_sha256) for at, tick in data.compact_clocks}:
            raise ValueError(f"Stored hero replay SHA/clock conflict: {data.match_id}")
        cursor.execute("SELECT DISTINCT player_slot,team,hero_id FROM historical_match_hero_snapshots "
                       "WHERE source=%s AND source_match_id=%s", (SOURCE, str(data.match_id)))
        if set(cursor.fetchall()) != data.roster:
            raise ValueError(f"Stored hero roster conflict: {data.match_id}")


def check_existing(connection, data, present):
    missing = {}
    with connection.cursor() as cursor:
        for (table, columns, _), rows in zip(TABLES, (data.frames, data.players), strict=True):
            actual = set()
            if present[table]:
                cursor.execute(sql.SQL("SELECT {} FROM {} WHERE source=%s AND source_match_id=%s").format(
                    sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table)),
                    (SOURCE, str(data.match_id)))
                actual = set(cursor.fetchall())
            expected = set(rows)
            if not actual <= expected:
                raise ValueError(f"Existing GLOBAL spatial data conflict: {table}, {data.match_id}")
            missing[table] = len(expected - actual)
    return missing


def apply_match(connection, data):
    with connection.transaction():
        check_parent(connection, data)
        check_existing(connection, data, {t: True for t, _, _ in TABLES})
        with connection.cursor() as cursor:
            inserted = {table: heroes.insert_pages(cursor, table, cols, key, rows)
                        for (table, cols, key), rows in zip(TABLES, (data.frames, data.players), strict=True)}
        if any(check_existing(connection, data, {t: True for t, _, _ in TABLES}).values()):
            raise ValueError("Incomplete spatial transaction")
    return inserted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report", type=Path, default=OUTPUT / "postgres-spatial-import/report.json")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if not args.report.resolve().is_relative_to(OUTPUT.resolve()):
        parser.error("--report must be inside replay_probe/output")
    if args.report.resolve() in {Path(p).resolve() for p in MANIFESTS}:
        parser.error("--report cannot overwrite an input manifest")
    report = dict(mode="APPLY" if args.apply else "PREFLIGHT", provenance="HISTORICAL_ORACLE",
                  errors=[], matches=[], source_counts=dict(frames=0, hero_positions=0),
                  null_positions_by_reason={})
    try:
        records, report["excluded"], protected = discover(MANIFESTS)
        if args.report.resolve() in protected:
            parser.error("--report cannot overwrite a source artifact")
        prepared = []
        nulls = Counter()
        for record in records:
            try:
                data = prepare(record)
                prepared.append(data)
                report["source_counts"]["frames"] += len(data.frames)
                report["source_counts"]["hero_positions"] += len(data.players)
                for p in data.players:
                    row = dict(zip(HERO_COLUMNS, p, strict=True))
                    if row["x"] is None:
                        nulls[f"{row['position_status']}/life_state={row['life_state']}"] += 1
            except (ValueError, OSError, KeyError, TypeError, ArithmeticError) as exc:
                report["errors"].append(dict(match_id=record[0], phase="SOURCE", reason=str(exc)))
        report["null_positions_by_reason"] = dict(nulls)
        planned = []
        with psycopg.connect(**postgres_connection_options_from_env(), autocommit=True,
                             connect_timeout=5, options="-c default_transaction_read_only=on") as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                with connection.cursor() as cursor:
                    present = {t: cursor.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is not None
                               for t, _, _ in TABLES}
                for data in prepared:
                    try:
                        check_parent(connection, data)
                        planned.append((data, check_existing(connection, data, present)))
                    except ValueError as exc:
                        report["errors"].append(dict(match_id=data.match_id, phase="DATABASE", reason=str(exc)))
        report["compatible_matches"] = len(planned)
        report["planned_missing"] = {t: sum(m[t] for _, m in planned) for t, _, _ in TABLES}
        if args.apply and planned:
            with psycopg.connect(**postgres_connection_options_from_env(), autocommit=True,
                                 connect_timeout=5) as connection:
                with connection.transaction():
                    connection.execute(SCHEMA.read_text())
                attempted = 0
                for data, missing in planned:
                    if not any(missing.values()):
                        report["matches"].append(dict(match_id=data.match_id, status="SKIPPED"))
                        continue
                    if args.limit is not None and attempted >= args.limit:
                        continue
                    attempted += 1
                    try:
                        inserted = apply_match(connection, data)
                        report["matches"].append(dict(match_id=data.match_id, status="IMPORTED",
                                                      inserted=inserted, replay_sha256=data.replay_sha256))
                    except (ValueError, psycopg.Error) as exc:
                        report["errors"].append(dict(match_id=data.match_id, phase="APPLY", reason=str(exc)))
                    heroes.save(args.report, report)
        else:
            report["matches"] = [dict(match_id=d.match_id, status="COMPATIBLE", missing=m)
                                  for d, m in planned]
        heroes.save(args.report, report)
        print(json.dumps({k: v for k, v in report.items() if k not in ("matches", "excluded")}, indent=2))
        print("Results: " + json.dumps(dict(Counter(e["status"] for e in report["matches"]))))
        return 1 if report["errors"] else 0
    except (ValueError, OSError, KeyError, TypeError, psycopg.Error) as exc:
        print(f"Spatial import stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
