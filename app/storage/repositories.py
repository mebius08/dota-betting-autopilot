from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import cast

from app.domain import (
    Bet,
    BetCandidate,
    BetResult,
    BetStatus,
    ExecutionMode,
    Match,
    MatchStatus,
    OddsSnapshot,
    Session,
)
from app.storage.database import get_connection, init_db


class SQLiteRepository:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        init_db(self.db_path)

    def save_session(self, session: Session) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    id,
                    name,
                    tournament_keyword,
                    streamer_channel,
                    execution_mode,
                    target_bets_per_match,
                    max_bets_per_match,
                    score_threshold,
                    active,
                    created_at,
                    ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.name,
                    session.tournament_keyword,
                    session.streamer_channel,
                    session.execution_mode,
                    session.target_bets_per_match,
                    session.max_bets_per_match,
                    session.score_threshold,
                    int(session.active),
                    _datetime_to_text(session.created_at),
                    _datetime_to_text(session.ended_at),
                ),
            )
            connection.commit()

    def save_match(self, match: Match) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO matches (
                    id,
                    session_id,
                    tournament_name,
                    team_a,
                    team_b,
                    format,
                    status,
                    start_time,
                    external_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match.id,
                    match.session_id,
                    match.tournament_name,
                    match.team_a,
                    match.team_b,
                    match.format,
                    match.status,
                    _datetime_to_text(match.start_time),
                    match.external_id,
                ),
            )
            connection.commit()

    def save_odds_snapshot(self, snapshot: OddsSnapshot) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO odds_snapshots (
                    id,
                    session_id,
                    match_id,
                    external_market_id,
                    market,
                    selection,
                    line,
                    odds,
                    phase,
                    is_live,
                    is_suspended,
                    bookmaker,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.session_id,
                    snapshot.match_id,
                    snapshot.external_market_id,
                    snapshot.market,
                    snapshot.selection,
                    snapshot.line,
                    snapshot.odds,
                    snapshot.phase,
                    int(snapshot.is_live),
                    int(snapshot.is_suspended),
                    snapshot.bookmaker,
                    _datetime_to_text(snapshot.created_at),
                ),
            )
            connection.commit()

    def save_bet_candidate(self, candidate: BetCandidate) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO bet_candidates (
                    id,
                    session_id,
                    match_id,
                    market,
                    selection,
                    line,
                    odds,
                    phase,
                    market_score,
                    phase_score,
                    line_score,
                    streamer_score,
                    risk_score,
                    final_score,
                    decision,
                    explanation,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.id,
                    candidate.session_id,
                    candidate.match_id,
                    candidate.market,
                    candidate.selection,
                    candidate.line,
                    candidate.odds,
                    candidate.phase,
                    candidate.market_score,
                    candidate.phase_score,
                    candidate.line_score,
                    candidate.streamer_score,
                    candidate.risk_score,
                    candidate.final_score,
                    candidate.decision,
                    candidate.explanation,
                    _datetime_to_text(candidate.created_at),
                ),
            )
            connection.commit()

    def save_bet(self, bet: Bet) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO bets (
                    id,
                    session_id,
                    match_id,
                    candidate_id,
                    mode,
                    market,
                    selection,
                    line,
                    odds,
                    stake_pct,
                    status,
                    result,
                    profit_units,
                    created_at,
                    settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bet.id,
                    bet.session_id,
                    bet.match_id,
                    bet.candidate_id,
                    bet.mode,
                    bet.market,
                    bet.selection,
                    bet.line,
                    bet.odds,
                    bet.stake_pct,
                    bet.status,
                    bet.result,
                    bet.profit_units,
                    _datetime_to_text(bet.created_at),
                    _datetime_to_text(bet.settled_at),
                ),
            )
            connection.commit()

    def list_bets(self) -> list[Bet]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM bets
                ORDER BY created_at, id
                """
            ).fetchall()

        return [_row_to_bet(row) for row in rows]

    def list_bets_by_session(self, session_id: str) -> list[Bet]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM bets
                WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()

        return [_row_to_bet(row) for row in rows]

    def list_sessions(self) -> list[Session]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM sessions
                ORDER BY created_at, id
                """
            ).fetchall()

        return [_row_to_session(row) for row in rows]

    def list_matches_by_session(self, session_id: str) -> list[Match]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM matches
                WHERE session_id = ?
                ORDER BY start_time, id
                """,
                (session_id,),
            ).fetchall()

        return [_row_to_match(row) for row in rows]


def _datetime_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _datetime_from_text(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _required_datetime_from_text(value: object) -> datetime:
    parsed = _datetime_from_text(value)
    if parsed is None:
        raise ValueError("Expected datetime value, got NULL")
    return parsed


def _row_to_session(row: object) -> Session:
    data = cast("dict[str, object]", row)
    return Session(
        id=str(data["id"]),
        name=str(data["name"]),
        tournament_keyword=str(data["tournament_keyword"]),
        streamer_channel=str(data["streamer_channel"]),
        execution_mode=cast(ExecutionMode, data["execution_mode"]),
        target_bets_per_match=_required_float(data["target_bets_per_match"]),
        max_bets_per_match=_required_int(data["max_bets_per_match"]),
        score_threshold=_required_float(data["score_threshold"]),
        active=_required_bool(data["active"]),
        created_at=_required_datetime_from_text(data["created_at"]),
        ended_at=_datetime_from_text(data["ended_at"]),
    )


def _row_to_match(row: object) -> Match:
    data = cast("dict[str, object]", row)
    return Match(
        id=str(data["id"]),
        session_id=str(data["session_id"]),
        tournament_name=str(data["tournament_name"]),
        team_a=str(data["team_a"]),
        team_b=str(data["team_b"]),
        format=str(data["format"]),
        status=cast(MatchStatus, data["status"]),
        start_time=_datetime_from_text(data["start_time"]),
        external_id=_optional_text(data["external_id"]),
    )


def _row_to_bet(row: object) -> Bet:
    data = cast("dict[str, object]", row)
    return Bet(
        id=str(data["id"]),
        session_id=str(data["session_id"]),
        match_id=str(data["match_id"]),
        candidate_id=str(data["candidate_id"]),
        mode=cast(ExecutionMode, data["mode"]),
        market=str(data["market"]),
        selection=str(data["selection"]),
        line=_optional_float(data["line"]),
        odds=_required_float(data["odds"]),
        stake_pct=_required_float(data["stake_pct"]),
        status=cast(BetStatus, data["status"]),
        result=cast(BetResult, data["result"]),
        profit_units=_required_float(data["profit_units"]),
        created_at=_required_datetime_from_text(data["created_at"]),
        settled_at=_datetime_from_text(data["settled_at"]),
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _required_float(value)


def _required_float(value: object) -> float:
    return float(str(value))


def _required_int(value: object) -> int:
    return int(str(value))


def _required_bool(value: object) -> bool:
    return bool(_required_int(value))


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
