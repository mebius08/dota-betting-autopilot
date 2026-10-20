"""Minimal PostgreSQL storage for historical match API records."""

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.history.domain import HistoricalMatch
from app.tournaments import CompetitiveStage, TournamentRound


CONNECTION_ENV = {
    "host": "POSTGRES_HOST",
    "port": "POSTGRES_PORT",
    "dbname": "POSTGRES_DB",
    "user": "POSTGRES_USER",
    "password": "POSTGRES_PASSWORD",
}


def postgres_connection_options_from_env() -> dict[str, str]:
    missing = [name for name in CONNECTION_ENV.values() if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing PostgreSQL settings: " + ", ".join(missing))
    return {key: os.environ[name] for key, name in CONNECTION_ENV.items()}


class PostgresMatchRepository:
    def __init__(self, connection_options: Mapping[str, str]) -> None:
        self._connection_options = dict(connection_options)
        schema = (
            Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        )
        with self._connect() as connection:
            connection.execute(schema)

    @classmethod
    def from_env(cls) -> "PostgresMatchRepository":
        return cls(postgres_connection_options_from_env())

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(
            **self._connection_options, connect_timeout=5, row_factory=dict_row
        )

    def list_historical_matches(self) -> list[HistoricalMatch]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM historical_matches "
                "ORDER BY started_at, source, source_match_id"
            ).fetchall()
        matches = []
        for row in rows:
            values = dict(row)
            values["competitive_stage"] = CompetitiveStage(values["competitive_stage"])
            values["normalized_round"] = TournamentRound(values["normalized_round"])
            matches.append(HistoricalMatch(**values))
        return matches
