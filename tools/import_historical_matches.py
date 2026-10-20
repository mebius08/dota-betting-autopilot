"""Validate or import a historical Dota 2 match catalog from SQLite to PostgreSQL."""

import argparse
from collections.abc import Sequence
from dataclasses import fields
from enum import Enum
from pathlib import Path
import sqlite3
import sys

import psycopg
from psycopg import sql

from app.history.domain import HistoricalMatch
from app.storage import SQLiteRepository
from app.storage.postgres import postgres_connection_options_from_env


COLUMNS = tuple(field.name for field in fields(HistoricalMatch))


def validate_matches(matches: Sequence[HistoricalMatch]) -> None:
    ids, identities = set(), set()
    for match in matches:
        identity = (match.source, match.source_match_id)
        if match.id in ids or identity in identities:
            raise ValueError("Duplicate match id or source identity in SQLite catalog.")
        ids.add(match.id)
        identities.add(identity)
        if match.winner_side not in (None, "team_a", "team_b"):
            raise ValueError("Incompatible HistoricalMatch field: winner_side.")
        for name in ("started_at", "ended_at", "ingested_at"):
            value = getattr(match, name)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"HistoricalMatch field {name} needs a timezone.")
        if match.best_of is not None and not -(2**31) <= match.best_of < 2**31:
            raise ValueError(
                "HistoricalMatch field best_of exceeds PostgreSQL INTEGER."
            )
        if any(
            isinstance(getattr(match, name), str) and "\x00" in getattr(match, name)
            for name in COLUMNS
        ):
            raise ValueError(
                "HistoricalMatch text contains a PostgreSQL-incompatible NUL."
            )


def apply_matches(matches: Sequence[HistoricalMatch]) -> int:
    if not matches:
        return 0
    query = sql.SQL(
        "INSERT INTO historical_matches ({}) VALUES ({}) "
        "ON CONFLICT (source, source_match_id) DO NOTHING"
    ).format(
        sql.SQL(", ").join(sql.Identifier(name) for name in COLUMNS),
        sql.SQL(", ").join(sql.Placeholder() for _ in COLUMNS),
    )
    rows = [
        tuple(
            value.value if isinstance(value, Enum) else value
            for value in (getattr(match, name) for name in COLUMNS)
        )
        for match in matches
    ]
    with psycopg.connect(
        **postgres_connection_options_from_env(), connect_timeout=5
    ) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(query, rows)
            inserted = cursor.rowcount
    return inserted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-path", type=Path, required=True, help="Existing SQLite catalog file."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write matches in one PostgreSQL transaction.",
    )
    args = parser.parse_args(argv)
    try:
        matches = SQLiteRepository.read_historical_matches(args.sqlite_path)
        validate_matches(matches)
        if not args.apply:
            print(
                f"DRY RUN: {len(matches)} historical matches; {len(COLUMNS)} compatible fields."
            )
            print("PostgreSQL was not contacted. Use --apply to import.")
        else:
            inserted = apply_matches(matches)
            print(
                f"APPLY: source={len(matches)}, inserted={inserted}, skipped={len(matches) - inserted}."
            )
        return 0
    except (OSError, sqlite3.Error, psycopg.Error, ValueError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
