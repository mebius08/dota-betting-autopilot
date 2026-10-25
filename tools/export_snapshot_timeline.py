"""Export historical observation timelines from the four confirmed local replays.

Dry-run is the default. Each paired POV anchor produces GLOBAL, RADIANT_POV and
DIRE_POV via the existing builder/projection. No targets, predictions or new
features are added, and no historical source is claimed available LIVE.
"""

import argparse
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import TextIO

from app.analytics.historical_snapshot import (
    MatchIdentity, _row_time_tick, build_historical_snapshot,
)
from app.analytics.snapshot_views import CHANNEL_ORIGINS, VIEWS, project_snapshot
from tools import inspect_historical_snapshot as inspector


def _load_match(match_id: str) -> tuple:
    """Load each of the four files once, reusing the inspector's exact adapters."""
    if match_id not in inspector.MATCH_SOURCES:
        raise ValueError(f"Unsupported match ID {match_id}.")
    paths = [inspector.ROOT / path for path in inspector.MATCH_SOURCES[match_id]]
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Match {match_id}: required source file is missing: {path}")
    compact_path, spatial_path, pov_path, enemy_path = paths
    compact = inspector._json_source(compact_path, match_id)
    spatial = inspector._json_source(spatial_path, match_id)
    sha = compact["replay_sha256"]
    if spatial["replay_sha256"] != sha or spatial.get("artifact_kind") != "replay_hero_positions":
        raise ValueError(f"Match {match_id}: conflicting spatial SHA or artifact kind.")
    for field in ("clock_normalization_method", "zero_replay_tick", "pause_ticks", "pause_seconds", "pause_witnessed"):
        if field in compact and field in spatial and compact[field] != spatial[field]:
            raise ValueError(f"Match {match_id}: conflicting replay clock metadata {field}.")
    pov = inspector._csv_source(pov_path, inspector.TEAM_COLUMNS, match_id, sha)
    enemies = inspector._csv_source(enemy_path, inspector.ENEMY_OBSERVATION_FIELDS, match_id, sha)
    lineage = dict(source=inspector.SOURCE, source_match_id=match_id, match_id=match_id, replay_sha256=sha)
    sources = dict(
        hero_records=[lineage | frame for frame in compact["snapshots"]],
        spatial_records=[lineage | frame for frame in spatial["snapshots"]],
        enemy_records=enemies, replay_sha256=sha,
    )
    return (MatchIdentity(inspector.SOURCE, match_id), sha,
            inspector._economy_rows(compact["snapshots"], match_id), inspector._pov_rows(pov, match_id), sources)


def _paired_anchors(match: MatchIdentity, pov: list[dict]) -> list[tuple[Decimal, int]]:
    frames = {}
    for row in pov:
        time, tick = _row_time_tick(row, match)
        team = row["pov_team"]
        observed, teams = frames.setdefault(tick, (time, set()))
        if team not in inspector.TEAMS or team in teams or time != observed:
            raise ValueError(f"Match {match.source_match_id}: duplicate or inconsistent POV anchor {tick}.")
        teams.add(team)
    if not frames or any(teams != set(inspector.TEAMS) for _, teams in frames.values()):
        raise ValueError(f"Match {match.source_match_id}: anchors require complete Radiant/Dire pairs.")
    return sorted((time, tick) for tick, (time, _) in frames.items())


def iter_timeline_records() -> Iterator[dict]:
    """Build once per exact anchor; project all views without rereading sources."""
    replay_matches = {}
    for match_id in sorted(inspector.MATCH_SOURCES, key=int):
        match, sha, economy, pov, sources = _load_match(match_id)
        if replay_matches.setdefault(sha, match_id) != match_id:
            raise ValueError("A replay SHA is shared by different match IDs.")
        for time, tick in _paired_anchors(match, pov):
            try:
                snapshot = build_historical_snapshot(
                    match, time, economy, pov, requested_replay_tick=tick, **sources,
                )
                for view in VIEWS:
                    # Projection validates every present observation's time/tick,
                    # age, provenance and payload; Team POV filtering stays there.
                    payload = project_snapshot(snapshot, view, requested_replay_tick=tick)
                    yield payload | {"replay_sha256": sha}
            except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                raise ValueError(f"Match {match_id}, anchor {time}/{tick}: {error}") from error


def _counts() -> dict:
    return dict(
        anchors=0, records_by_view={view: 0 for view in VIEWS},
        channel_statuses={channel: {"AVAILABLE": 0, "NO_PRECEDING_SNAPSHOT": 0} for channel in CHANNEL_ORIGINS},
        ages={channel: [] for channel in ("economy", "pov")},
    )


def _freshness(ages: list[Decimal]) -> dict:
    ordered = sorted(ages)
    median = None
    if ordered:
        low, high = ordered[(len(ordered) - 1) // 2], ordered[len(ordered) // 2]
        with localcontext() as context:
            context.prec = max(low.adjusted(), high.adjusted()) - min(
                low.as_tuple().exponent, high.as_tuple().exponent,
            ) + 3
            median = (low + high) / 2
    return dict(observations=len(ages), min_age_seconds=min(ages, default=None),
                median_age_seconds=median, max_age_seconds=max(ages, default=None))


def _summary(counts: dict) -> dict:
    return dict(
        anchors=counts["anchors"], records=sum(counts["records_by_view"].values()),
        records_by_view=counts["records_by_view"], channel_statuses=counts["channel_statuses"],
        freshness={channel: _freshness(ages) for channel, ages in counts["ages"].items()},
    )


def _run(stream: TextIO | None) -> dict:
    overall, matches, identities = _counts(), {}, set()
    for payload in iter_timeline_records():
        mid, view = payload["match"]["source_match_id"], payload["view"]
        identity = (mid, payload["requested_game_time_seconds"], payload["requested_replay_tick"], view)
        if identity in identities:
            raise ValueError(f"Duplicate timeline identity: {identity}")
        identities.add(identity)
        match_counts = matches.setdefault(mid, _counts())
        for counts in (overall, match_counts):
            counts["records_by_view"][view] += 1
            if view == inspector.DEFAULT_VIEW:
                if set(payload["observations"]) != set(CHANNEL_ORIGINS):
                    raise ValueError("GLOBAL timeline requires all five observation channels.")
                counts["anchors"] += 1
                for channel, observation in payload["observations"].items():
                    counts["channel_statuses"][channel][observation["status"]] += 1
                    if channel in counts["ages"] and observation["status"] == "AVAILABLE":
                        counts["ages"][channel].append(observation["age_seconds"])
        # Dry-run also validates JSON serialization, without creating any file.
        line = json.dumps(payload, default=inspector._json_decimal, ensure_ascii=False,
                          allow_nan=False, sort_keys=True, separators=(",", ":"))
        if stream is not None:
            stream.write(line + "\n")
    return dict(
        **_summary(overall), match_count=len(matches),
        matches=[dict(match_id=mid, **_summary(counts)) for mid, counts in matches.items()],
        coverage_note="Channel coverage counts each GLOBAL anchor once. Oracle channels excluded from Team POV are not missing observations.",
    )


def export_timeline(out: Path | None = None) -> dict:
    """Dry-run without writes, or atomically publish one explicitly named JSONL."""
    protected = {(inspector.ROOT / path).resolve() for paths in inspector.MATCH_SOURCES.values() for path in paths}
    if out is not None and out.resolve() in protected:
        raise ValueError("Output must not overwrite a source artifact.")
    temporary = None
    try:
        with ExitStack() as stack:
            stream = None
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                stream = stack.enter_context(tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="", dir=out.parent,
                    prefix=f".{out.name}.", delete=False,
                ))
                temporary = Path(stream.name)
            report = _run(stream)
        if out is not None:
            os.replace(temporary, out)
        return report | {"mode": "EXPORTED" if out is not None else "DRY_RUN", "out": str(out) if out is not None else None}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Explicit JSONL destination; omission performs a read-only dry-run.")
    args = parser.parse_args(argv)
    try:
        report = export_timeline(args.out)
        print(json.dumps(report, default=inspector._json_decimal, ensure_ascii=False, allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as error:
        print(f"Historical timeline error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
