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


def _match_from_row(row: Mapping[str, Any]) -> HistoricalMatch:
    values = dict(row)
    values["competitive_stage"] = CompetitiveStage(values["competitive_stage"])
    values["normalized_round"] = TournamentRound(values["normalized_round"])
    return HistoricalMatch(**values)


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
        return [_match_from_row(row) for row in rows]

    def get_historical_match_by_source(
        self, source: str, source_match_id: str
    ) -> HistoricalMatch | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM historical_matches "
                "WHERE source = %s AND source_match_id = %s",
                (source, source_match_id),
            ).fetchone()
        if row is None:
            return None
        return _match_from_row(row)

    def list_historical_match_economy(
        self, source: str, source_match_id: str
    ) -> list[dict[str, int | float]]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT actual_game_time_seconds, replay_tick, "
                "radiant_net_worth, dire_net_worth, "
                "radiant_current_gold, dire_current_gold, "
                "radiant_total_xp, dire_total_xp "
                "FROM historical_match_economy_snapshots "
                "WHERE source = %s AND source_match_id = %s "
                "ORDER BY actual_game_time_seconds, replay_tick",
                (source, source_match_id),
            ).fetchall()

    def list_historical_match_pov(
        self, source: str, source_match_id: str
    ) -> list[dict[str, int | float | str | None]]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT actual_game_time_seconds, replay_tick, pov_team, "
                "visible_enemy_count, visible_enemy_with_position_count, "
                "hidden_enemy_count, own_team_spread, nearest_visible_enemy_distance, "
                "own_team_spread_status, nearest_visible_enemy_status "
                "FROM historical_match_pov_snapshots "
                "WHERE source = %s AND source_match_id = %s "
                "ORDER BY actual_game_time_seconds, replay_tick, pov_team",
                (source, source_match_id),
            ).fetchall()
