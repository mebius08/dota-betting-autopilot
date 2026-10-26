"""Read-only selection of catalog metadata using the project's tournament allowlist.

--as-of fixes the inclusive 366-day window. Default output is a JSON audit;
--ids-only prints one target match_id per line, without fetching any DEM.
Selection proves allowlist membership, not replay or game-state availability.
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sys

import psycopg
from psycopg.rows import dict_row

from app.storage.postgres import postgres_connection_options_from_env
from app.tournament_allowlist import (
    TOURNAMENT_ALLOWLIST_PATH,
    load_tournament_allowlist,
)


WINDOW_DAYS = 366
READ_ONLY = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
TOTALS = "SELECT COUNT(*) AS matches FROM opendota_pro_matches"
LEAGUE_COUNTS = (
    "SELECT league_id, COUNT(*) AS matches FROM opendota_pro_matches "
    "WHERE start_time BETWEEN %s AND %s GROUP BY league_id"
)
TARGET_IDS = (
    "SELECT match_id FROM opendota_pro_matches "
    "WHERE start_time BETWEEN %s AND %s AND league_id = ANY(%s) "
    "ORDER BY match_id"
)


def parse_as_of(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of requires an ISO timestamp") from exc
    if timestamp.tzinfo is None or timestamp.microsecond:
        raise argparse.ArgumentTypeError(
            "--as-of requires an explicit timezone and whole seconds"
        )
    epoch = int(timestamp.timestamp())
    if epoch <= 0:
        raise argparse.ArgumentTypeError("--as-of must be after the Unix epoch")
    return epoch


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def audit_catalog(connection, allowlist, *, as_of):
    """Return an audit and deterministic IDs from one consistent read-only snapshot."""
    if not allowlist.entries:
        raise ValueError("Tournament allowlist is empty; refusing target selection")
    if isinstance(as_of, bool) or not isinstance(as_of, int) or as_of <= 0:
        raise ValueError("as_of must be a positive integer Unix timestamp")
    cutoff = as_of - WINDOW_DAYS * 86400
    with connection.transaction():
        connection.execute(READ_ONLY)
        total = connection.execute(TOTALS).fetchone()["matches"]
        counts = {
            row["league_id"]: row["matches"]
            for row in connection.execute(LEAGUE_COUNTS, (cutoff, as_of)).fetchall()
        }
        ids = [
            row["match_id"]
            for row in connection.execute(
                TARGET_IDS, (cutoff, as_of, list(allowlist.league_ids))
            ).fetchall()
        ]

    allowed_ids = set(allowlist.league_ids)
    selected_count = sum(counts.get(league_id, 0) for league_id in allowed_ids)
    if ids != sorted(set(ids)) or len(ids) != selected_count:
        raise ValueError("Inconsistent target IDs/counts; refusing target selection")
    window_count = sum(counts.values())
    return {
        "source": "opendota_pro_matches",
        "allowlist_path": str(TOURNAMENT_ALLOWLIST_PATH),
        "allowlist_version": allowlist.version,
        "window_days": WINDOW_DAYS,
        "from_utc": _iso(cutoff),
        "through_utc": _iso(as_of),
        "catalog_matches": total,
        "window_matches": window_count,
        "outside_window_or_unknown_date_matches": total - window_count,
        "configured_leagues": len(allowlist.entries),
        "selected_leagues": sum(counts.get(i, 0) > 0 for i in allowed_ids),
        "selected_matches": selected_count,
        "excluded_matches": window_count - selected_count,
        "excluded_leagues": sum(
            i is not None and i > 0 and i not in allowed_ids for i in counts
        ),
        "unknown_league_matches": counts.get(None, 0) + counts.get(0, 0),
        "leagues": [
            asdict(entry) | {"matches": counts.get(entry.league_id, 0)}
            for entry in allowlist.entries
        ],
        "dem_availability": "NOT_CHECKED_BY_THIS_SELECTOR",
    }, ids


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of", required=True, type=parse_as_of,
        help="Inclusive end of the 366-day window, e.g. 2026-10-26T01:52:53Z",
    )
    parser.add_argument(
        "--ids-only", action="store_true",
        help="Print only allowlisted match IDs, one per line (no downloads)",
    )
    args = parser.parse_args(argv)
    try:
        allowlist = load_tournament_allowlist()
        if not allowlist.entries:
            raise ValueError("Tournament allowlist is empty; refusing target selection")
        with psycopg.connect(
            **postgres_connection_options_from_env(),
            connect_timeout=5, row_factory=dict_row, autocommit=True,
            options="-c default_transaction_read_only=on",
        ) as connection:
            report, ids = audit_catalog(connection, allowlist, as_of=args.as_of)
        if args.ids_only:
            for match_id in ids:
                print(match_id)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
    except (ValueError, psycopg.Error) as exc:
        print(f"Target selection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
