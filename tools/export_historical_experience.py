"""Local-only experience JSONL export; no --out means read-only dry-run.

One record per target match, with two teams and ten players. --match-id and
--limit restrict targets, never the history sample. All weights/causal filtering
come from build_historical_experience. Coverage/IDs are metadata, not default
predictors; zero observed matches is not evidence of zero career experience.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile

from app.analytics.historical_experience import build_historical_experience
from app.analytics.historical_match_context import (
    DEFAULT_CACHE_DIR, _match_id, load_historical_match_context,
)


OUTPUT = Path(__file__).resolve().parents[1] / "tools/replay_probe/output"


def load_catalog(cache_dir: Path) -> dict:
    """Read only immediate canonical context files; no repository traversal."""
    if not cache_dir.is_dir():
        raise ValueError(f"Context cache directory does not exist: {cache_dir}")
    catalog = {}
    for path in sorted(cache_dir.glob("*.context.json")):
        mid = _match_id(path.name.removesuffix(".context.json"))
        if path.name != f"{mid}.context.json" or mid in catalog:
            raise ValueError(f"Duplicate/noncanonical context identity: {path.name}")
        try:
            catalog[mid] = load_historical_match_context(mid, cache_dir=cache_dir)
        except ValueError as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
    if not catalog:
        raise ValueError(f"No context caches found in {cache_dir}")
    return catalog


def build_records(catalog: dict, cache_dir: Path, *, match_ids=None, limit=None) -> list:
    targets = set(catalog) if match_ids is None else {_match_id(mid) for mid in match_ids}
    if targets - catalog.keys():
        raise ValueError(f"Target match IDs absent from cache: {sorted(targets - catalog.keys())}")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("--limit must be positive")
    # Unknown times remain unknown and sort last; ID only breaks ordering ties.
    ordered = sorted(targets, key=lambda mid: (
        catalog[mid]["start_time"] is None, catalog[mid]["start_time"] or 0, mid,
    ))
    if limit is not None:
        ordered = ordered[:limit]
    history_ids = sorted(catalog)
    known_times = sum(row["start_time"] is not None for row in catalog.values())
    records = []
    for mid in ordered:
        record = build_historical_experience(mid, history_ids, cache_dir=cache_dir)
        # A compact size/availability summary replaces the quadratic prior-ID list.
        record["coverage"].pop("prior_match_ids")
        record["coverage"].update(available_context_matches=len(catalog),
                                  known_start_time_matches=known_times,
                                  unknown_start_time_matches=len(catalog)-known_times)
        for player in record["players"]:
            player["team_id"] = record["teams"][player["team"]]["team_id"]
        records.append(record)
    return records


def output_path(path: Path, cache_dir: Path) -> Path:
    destination = path.resolve()
    if (not destination.is_relative_to(OUTPUT.resolve())
            or destination.is_relative_to(cache_dir.resolve())
            or destination.suffix != ".jsonl" or not destination.parent.is_dir()):
        raise ValueError("--out must be a .jsonl file in an existing replay output directory, outside the context cache")
    return destination


def write_jsonl(path: Path, records: list):
    """Atomic replacement: a failed export leaves an existing result intact."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            for record in records:
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--match-id", type=_match_id, action="append",
                        help="Target ID, repeatable; history still uses the whole local cache")
    parser.add_argument("--limit", type=int, help="Maximum targets, earliest known start_time first")
    parser.add_argument("--out", type=Path, help="Write JSONL; omitted for dry-run")
    args = parser.parse_args(argv)
    try:
        if args.limit is not None and args.limit <= 0:
            raise ValueError("--limit must be positive")
        destination = output_path(args.out, args.cache_dir) if args.out else None
        catalog = load_catalog(args.cache_dir)
        records = build_records(catalog, args.cache_dir, match_ids=args.match_id, limit=args.limit)
        if destination is not None:
            write_jsonl(destination, records)
        print(json.dumps(dict(
            mode="EXPORT" if destination is not None else "DRY_RUN",
            available_context_matches=len(catalog), target_matches=len(records),
            team_rows=sum(len(row["teams"]) for row in records),
            player_rows=sum(len(row["players"]) for row in records),
            team_statuses=dict(Counter(team["status"] for row in records for team in row["teams"].values())),
            player_statuses=dict(Counter(player["status"] for row in records for player in row["players"])),
            player_hero_statuses=dict(Counter(player["hero_status"] for row in records for player in row["players"])),
            complete_career_history=False, out=str(destination) if destination else None,
        ), sort_keys=True))
        return 0
    except (ValueError, OSError) as exc:
        print(f"Historical experience export stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
