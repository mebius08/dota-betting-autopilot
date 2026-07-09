from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
from sqlite3 import Connection
from typing import cast

from app.domain import (
    Bet,
    BetCandidate,
    BetResult,
    BetStatus,
    Decision,
    ExecutionMode,
    Match,
    MatchStatus,
    OddsPhase,
    OddsSnapshot,
    Session,
    StreamerUtterance,
)
from app.draft_history.domain import (
    AdvantageMetric,
    DraftActionKind,
    DotaSide,
    DraftWinnerSide,
    HistoricalDotaAdvantagePoint,
    HistoricalDotaGame,
    HistoricalDotaPlayerFinalStats,
    HistoricalDraftAction,
    TimeSemanticsStatus,
)
from app.history.domain import HistoricalMatch, WinnerSide
from app.history.roster_lineage import HistoricalTournamentChronologyContext
from app.history.rosters import (
    PlayerIdentity,
    RosterCoach,
    RosterMemberRole,
    RosterSnapshot,
    TeamOrganization,
    build_identity_id,
    roster_members,
)
from app.tournaments import CompetitiveStage, TournamentRound
from app.storage.database import get_connection, init_db


HistoricalUpsertResult = str
DraftUpsertResult = str
RosterUpsertResult = str


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

    def save_streamer_utterance(self, utterance: StreamerUtterance) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO streamer_utterances (
                    id,
                    session_id,
                    match_id,
                    source,
                    text,
                    detected_market,
                    detected_selection,
                    detected_team,
                    signal_type,
                    strength,
                    confidence,
                    hype_flag,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utterance.id,
                    utterance.session_id,
                    utterance.match_id,
                    utterance.source,
                    utterance.text,
                    utterance.detected_market,
                    utterance.detected_selection,
                    utterance.detected_team,
                    utterance.signal_type,
                    utterance.strength,
                    utterance.confidence,
                    int(utterance.hype_flag),
                    _datetime_to_text(utterance.created_at),
                ),
            )
            connection.commit()

    def save_streamer_utterances(
        self,
        utterances: list[StreamerUtterance],
    ) -> None:
        for utterance in utterances:
            self.save_streamer_utterance(utterance)

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

    def list_recent_bets(self, limit: int) -> list[Bet]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM bets
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
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

    def get_bet(self, bet_id: str) -> Bet | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM bets
                WHERE id = ?
                """,
                (bet_id,),
            ).fetchone()

        if row is None:
            return None
        return _row_to_bet(row)

    def list_open_bets(self) -> list[Bet]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM bets
                WHERE result = 'unknown' OR status != 'settled'
                ORDER BY created_at, id
                """
            ).fetchall()

        return [_row_to_bet(row) for row in rows]

    def list_open_bets_by_session(self, session_id: str) -> list[Bet]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM bets
                WHERE session_id = ?
                  AND (result = 'unknown' OR status != 'settled')
                ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()

        return [_row_to_bet(row) for row in rows]

    def settle_bet(
        self,
        bet_id: str,
        result: BetResult,
        settled_at: datetime | None = None,
    ) -> Bet:
        bet = self.get_bet(bet_id)
        if bet is None:
            raise ValueError(f"Bet not found: {bet_id}")

        settlement_time = settled_at or datetime.now(timezone.utc)
        profit_units = calculate_profit_units(result, bet.odds, bet.stake_pct)

        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                UPDATE bets
                SET status = ?,
                    result = ?,
                    profit_units = ?,
                    settled_at = ?
                WHERE id = ?
                """,
                (
                    "settled",
                    result,
                    profit_units,
                    _datetime_to_text(settlement_time),
                    bet_id,
                ),
            )
            connection.commit()

        updated_bet = self.get_bet(bet_id)
        if updated_bet is None:
            raise ValueError(f"Bet not found after settlement: {bet_id}")
        return updated_bet

    def get_session(self, session_id: str) -> Session | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()

        if row is None:
            return None
        return _row_to_session(row)

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

    def list_matches(self) -> list[Match]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM matches
                ORDER BY start_time, id
                """
            ).fetchall()

        return [_row_to_match(row) for row in rows]

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

    def list_odds_snapshots(self) -> list[OddsSnapshot]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM odds_snapshots
                ORDER BY created_at, id
                """
            ).fetchall()

        return [_row_to_odds_snapshot(row) for row in rows]

    def list_odds_snapshots_by_session(
        self,
        session_id: str,
    ) -> list[OddsSnapshot]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM odds_snapshots
                WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()

        return [_row_to_odds_snapshot(row) for row in rows]

    def list_bet_candidates_by_session(
        self,
        session_id: str,
    ) -> list[BetCandidate]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM bet_candidates
                WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()

        return [_row_to_bet_candidate(row) for row in rows]

    def list_bet_candidates(self) -> list[BetCandidate]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM bet_candidates
                ORDER BY created_at, id
                """
            ).fetchall()

        return [_row_to_bet_candidate(row) for row in rows]

    def list_streamer_utterances(self) -> list[StreamerUtterance]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM streamer_utterances
                ORDER BY created_at, id
                """
            ).fetchall()

        return [_row_to_streamer_utterance(row) for row in rows]

    def list_streamer_utterances_by_session(
        self,
        session_id: str,
    ) -> list[StreamerUtterance]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM streamer_utterances
                WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()

        return [_row_to_streamer_utterance(row) for row in rows]

    def list_recent_streamer_utterances(
        self,
        limit: int,
    ) -> list[StreamerUtterance]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM streamer_utterances
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [_row_to_streamer_utterance(row) for row in rows]

    def list_streamer_utterances_by_match(
        self,
        match_id: str,
    ) -> list[StreamerUtterance]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM streamer_utterances
                WHERE match_id = ?
                ORDER BY created_at, id
                """,
                (match_id,),
            ).fetchall()

        return [_row_to_streamer_utterance(row) for row in rows]

    def save_historical_match(self, match: HistoricalMatch) -> None:
        self.upsert_historical_match(match)

    def upsert_historical_match(
        self,
        match: HistoricalMatch,
    ) -> HistoricalUpsertResult:
        existing = self.get_historical_match_by_source(
            match.source,
            match.source_match_id,
        )
        with closing(get_connection(self.db_path)) as connection:
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO historical_matches (
                        id,
                        source,
                        source_match_id,
                        started_at,
                        ended_at,
                        team_a_name,
                        team_b_name,
                        team_a_source_id,
                        team_b_source_id,
                        winner_name,
                        winner_source_id,
                        winner_side,
                        tournament_name,
                        tournament_source_id,
                        league_name,
                        league_source_id,
                        series_name,
                        series_source_id,
                        raw_stage_label,
                        competitive_stage,
                        normalized_round,
                        best_of,
                        status,
                        ingested_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                    """,
                    _historical_match_values(match),
                )
                connection.commit()
                return "inserted"

            if _historical_matches_equivalent(existing, match):
                return "unchanged"

            connection.execute(
                """
                UPDATE historical_matches
                SET id = ?,
                    started_at = ?,
                    ended_at = ?,
                    team_a_name = ?,
                    team_b_name = ?,
                    team_a_source_id = ?,
                    team_b_source_id = ?,
                    winner_name = ?,
                    winner_source_id = ?,
                    winner_side = ?,
                    tournament_name = ?,
                    tournament_source_id = ?,
                    league_name = ?,
                    league_source_id = ?,
                    series_name = ?,
                    series_source_id = ?,
                    raw_stage_label = ?,
                    competitive_stage = ?,
                    normalized_round = ?,
                    best_of = ?,
                    status = ?,
                    ingested_at = ?
                WHERE source = ?
                  AND source_match_id = ?
                """,
                (
                    match.id,
                    _datetime_to_text(match.started_at),
                    _datetime_to_text(match.ended_at),
                    match.team_a_name,
                    match.team_b_name,
                    match.team_a_source_id,
                    match.team_b_source_id,
                    match.winner_name,
                    match.winner_source_id,
                    match.winner_side,
                    match.tournament_name,
                    match.tournament_source_id,
                    match.league_name,
                    match.league_source_id,
                    match.series_name,
                    match.series_source_id,
                    match.raw_stage_label,
                    match.competitive_stage.value,
                    match.normalized_round.value,
                    match.best_of,
                    match.status,
                    _datetime_to_text(match.ingested_at),
                    match.source,
                    match.source_match_id,
                ),
            )
            connection.commit()
            return "updated"

    def get_historical_match(self, match_id: str) -> HistoricalMatch | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM historical_matches
                WHERE id = ?
                """,
                (match_id,),
            ).fetchone()

        if row is None:
            return None
        return _row_to_historical_match(row)

    def get_historical_match_by_source(
        self,
        source: str,
        source_match_id: str,
    ) -> HistoricalMatch | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM historical_matches
                WHERE source = ?
                  AND source_match_id = ?
                """,
                (source, source_match_id),
            ).fetchone()

        if row is None:
            return None
        return _row_to_historical_match(row)

    def list_historical_matches(self) -> list[HistoricalMatch]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM historical_matches
                ORDER BY started_at, source, source_match_id
                """
            ).fetchall()

        return [_row_to_historical_match(row) for row in rows]

    def list_historical_matches_before(
        self,
        cutoff_timestamp: datetime,
    ) -> list[HistoricalMatch]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM historical_matches
                WHERE ended_at IS NOT NULL
                  AND ended_at < ?
                  AND status IN ('finished', 'completed')
                ORDER BY ended_at, started_at, source, source_match_id
                """,
                (_datetime_to_text(cutoff_timestamp),),
            ).fetchall()

        return [_row_to_historical_match(row) for row in rows]

    def upsert_historical_dota_game(
        self,
        game: HistoricalDotaGame,
        actions: tuple[HistoricalDraftAction, ...] | list[HistoricalDraftAction],
    ) -> DraftUpsertResult:
        existing = self.get_historical_dota_game_by_source(
            game.source,
            game.source_game_id,
        )
        with closing(get_connection(self.db_path)) as connection:
            if existing is not None:
                _raise_if_conflicting_draft_game(existing, game)

            if existing is None:
                connection.execute(
                    """
                    INSERT INTO historical_dota_games (
                        id,
                        source,
                        source_game_id,
                        parent_series_source_id,
                        linked_historical_match_id,
                        started_at,
                        ended_at,
                        team_a_name,
                        team_b_name,
                        team_a_source_id,
                        team_b_source_id,
                        winner_side,
                        game_number,
                        best_of,
                        team_a_series_wins_before,
                        team_b_series_wins_before,
                        team_a_side,
                        patch,
                        draft_complete,
                        tournament_name,
                        tournament_source_id,
                        league_name,
                        league_source_id,
                        raw_stage_label,
                        ingested_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    _historical_dota_game_values(game),
                )
                result: DraftUpsertResult = "inserted"
            elif _historical_dota_games_equivalent(existing, game):
                result = "unchanged"
            else:
                connection.execute(
                    """
                    UPDATE historical_dota_games
                    SET id = ?,
                        parent_series_source_id = ?,
                        linked_historical_match_id = ?,
                        started_at = ?,
                        ended_at = ?,
                        team_a_name = ?,
                        team_b_name = ?,
                        team_a_source_id = ?,
                        team_b_source_id = ?,
                        winner_side = ?,
                        game_number = ?,
                        best_of = ?,
                        team_a_series_wins_before = ?,
                        team_b_series_wins_before = ?,
                        team_a_side = ?,
                        patch = ?,
                        draft_complete = ?,
                        tournament_name = ?,
                        tournament_source_id = ?,
                        league_name = ?,
                        league_source_id = ?,
                        raw_stage_label = ?,
                        ingested_at = ?
                    WHERE source = ?
                      AND source_game_id = ?
                    """,
                    (
                        game.id,
                        game.parent_series_source_id,
                        game.linked_historical_match_id,
                        _datetime_to_text(game.started_at),
                        _datetime_to_text(game.ended_at),
                        game.team_a_name,
                        game.team_b_name,
                        game.team_a_source_id,
                        game.team_b_source_id,
                        game.winner_side,
                        game.game_number,
                        game.best_of,
                        game.team_a_series_wins_before,
                        game.team_b_series_wins_before,
                        game.team_a_side,
                        game.patch,
                        int(game.draft_complete),
                        game.tournament_name,
                        game.tournament_source_id,
                        game.league_name,
                        game.league_source_id,
                        game.raw_stage_label,
                        _datetime_to_text(game.ingested_at),
                        game.source,
                        game.source_game_id,
                    ),
                )
                result = "updated"

            connection.execute(
                """
                DELETE FROM historical_draft_actions
                WHERE game_id = ?
                """,
                (game.id,),
            )
            for action in sorted(actions, key=lambda item: item.action_order):
                if action.game_id != game.id:
                    raise ValueError("draft action game_id must match game id")
                connection.execute(
                    """
                    INSERT INTO historical_draft_actions (
                        id,
                        game_id,
                        source,
                        source_game_id,
                        action_order,
                        action_kind,
                        team_side,
                        team_source_id,
                        hero_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _historical_draft_action_values(action),
                )
            connection.commit()
            return result

    def get_historical_dota_game_by_source(
        self,
        source: str,
        source_game_id: str,
    ) -> HistoricalDotaGame | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM historical_dota_games
                WHERE source = ?
                  AND source_game_id = ?
                """,
                (source, source_game_id),
            ).fetchone()

        if row is None:
            return None
        return _row_to_historical_dota_game(row)

    def list_historical_dota_games(self) -> list[HistoricalDotaGame]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM historical_dota_games
                ORDER BY started_at, source, source_game_id
                """
            ).fetchall()

        return [_row_to_historical_dota_game(row) for row in rows]

    def list_historical_dota_games_before(
        self,
        cutoff_timestamp: datetime,
    ) -> list[HistoricalDotaGame]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM historical_dota_games
                WHERE ended_at IS NOT NULL
                  AND ended_at < ?
                ORDER BY ended_at, started_at, source, source_game_id
                """,
                (_datetime_to_text(cutoff_timestamp),),
            ).fetchall()

        return [_row_to_historical_dota_game(row) for row in rows]

    def list_historical_draft_actions(
        self,
        game_id: str,
    ) -> list[HistoricalDraftAction]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM historical_draft_actions
                WHERE game_id = ?
                ORDER BY action_order, id
                """,
                (game_id,),
            ).fetchall()

        return [_row_to_historical_draft_action(row) for row in rows]

    def replace_historical_dota_player_final_stats(
        self,
        game_id: str,
        rows: tuple[HistoricalDotaPlayerFinalStats, ...]
        | list[HistoricalDotaPlayerFinalStats],
    ) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                DELETE FROM historical_dota_player_final_stats
                WHERE game_id = ?
                """,
                (game_id,),
            )
            for row in sorted(rows, key=lambda item: (item.team_side, item.account_id)):
                if row.game_id != game_id:
                    raise ValueError("player final stats game_id must match game id")
                connection.execute(
                    """
                    INSERT INTO historical_dota_player_final_stats (
                        id,
                        game_id,
                        source,
                        source_game_id,
                        account_id,
                        player_slot,
                        team_side,
                        team_source_id,
                        hero_id,
                        kills,
                        deaths,
                        assists,
                        net_worth,
                        last_hits,
                        denies,
                        gpm,
                        xpm,
                        level,
                        hero_damage,
                        tower_damage,
                        hero_healing,
                        final_item_ids_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?
                    )
                    """,
                    _historical_dota_player_final_stats_values(row),
                )
            connection.commit()

    def list_historical_dota_player_final_stats(
        self,
        game_id: str,
    ) -> list[HistoricalDotaPlayerFinalStats]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM historical_dota_player_final_stats
                WHERE game_id = ?
                ORDER BY team_side, player_slot, account_id
                """,
                (game_id,),
            ).fetchall()

        return [_row_to_historical_dota_player_final_stats(row) for row in rows]

    def replace_historical_dota_advantage_points(
        self,
        game_id: str,
        rows: tuple[HistoricalDotaAdvantagePoint, ...]
        | list[HistoricalDotaAdvantagePoint],
    ) -> None:
        with closing(get_connection(self.db_path)) as connection:
            connection.execute(
                """
                DELETE FROM historical_dota_advantage_points
                WHERE game_id = ?
                """,
                (game_id,),
            )
            for row in sorted(rows, key=lambda item: (item.metric, item.source_index)):
                if row.game_id != game_id:
                    raise ValueError("advantage point game_id must match game id")
                connection.execute(
                    """
                    INSERT INTO historical_dota_advantage_points (
                        id,
                        game_id,
                        source,
                        source_game_id,
                        metric,
                        source_index,
                        source_time_value,
                        normalized_time_seconds,
                        time_semantics_status,
                        value
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _historical_dota_advantage_point_values(row),
                )
            connection.commit()

    def list_historical_dota_advantage_points(
        self,
        game_id: str,
    ) -> list[HistoricalDotaAdvantagePoint]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM historical_dota_advantage_points
                WHERE game_id = ?
                ORDER BY metric, source_index
                """,
                (game_id,),
            ).fetchall()

        return [_row_to_historical_dota_advantage_point(row) for row in rows]

    def count_historical_matches(self, *, usable_only: bool = False) -> int:
        if not usable_only:
            with closing(get_connection(self.db_path)) as connection:
                return int(
                    connection.execute(
                        "SELECT COUNT(*) FROM historical_matches"
                    ).fetchone()[0]
                )

        return sum(
            1
            for match in self.list_historical_matches()
            if match.usable_for_match_winner_training
        )

    def upsert_player_identity(
        self,
        player: PlayerIdentity,
    ) -> RosterUpsertResult:
        with closing(get_connection(self.db_path)) as connection:
            result = _upsert_player_identity(connection, player)
            connection.commit()
            return result

    def get_player_identity(
        self,
        source: str,
        source_player_id: str,
    ) -> PlayerIdentity | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM players
                WHERE source = ?
                  AND source_player_id = ?
                """,
                (source, source_player_id),
            ).fetchone()

        if row is None:
            return None
        return _row_to_player_identity(row)

    def upsert_team_organization(
        self,
        organization: TeamOrganization,
    ) -> RosterUpsertResult:
        with closing(get_connection(self.db_path)) as connection:
            result = _upsert_team_organization(connection, organization)
            connection.commit()
            return result

    def get_team_organization(
        self,
        source: str,
        source_team_id: str,
    ) -> TeamOrganization | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM team_organizations
                WHERE source = ?
                  AND source_team_id = ?
                """,
                (source, source_team_id),
            ).fetchone()

        if row is None:
            return None
        return _row_to_team_organization(row)

    def upsert_roster_snapshot(
        self,
        snapshot: RosterSnapshot,
    ) -> RosterUpsertResult:
        with closing(get_connection(self.db_path)) as connection:
            _upsert_team_organization(connection, snapshot.organization)
            for player in snapshot.players:
                _upsert_player_identity(connection, player)

            existing_row = connection.execute(
                """
                SELECT *
                FROM roster_snapshots
                WHERE source = ?
                  AND source_snapshot_id = ?
                """,
                (snapshot.source, snapshot.source_snapshot_id),
            ).fetchone()

            if existing_row is None:
                connection.execute(
                    """
                    INSERT INTO roster_snapshots (
                        id,
                        source,
                        source_snapshot_id,
                        organization_id,
                        source_context,
                        tournament_source_id,
                        tournament_name,
                        observed_at,
                        valid_from,
                        valid_until,
                        player_roster_fingerprint,
                        staff_roster_fingerprint,
                        ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _roster_snapshot_values(snapshot),
                )
                _replace_roster_memberships(connection, snapshot)
                connection.commit()
                return "inserted"

            existing = _row_to_roster_snapshot(existing_row, connection)
            if not _roster_snapshot_semantics_equivalent(existing, snapshot):
                raise ValueError(
                    "Conflicting roster snapshot semantics for "
                    f"{snapshot.source}:{snapshot.source_snapshot_id}. "
                    "Use a distinct source_snapshot_id for changed roster "
                    "content or later-observed validity/staff information."
                )

            if snapshot.observed_at >= existing.observed_at:
                connection.commit()
                return "unchanged"

            connection.execute(
                """
                UPDATE roster_snapshots
                SET observed_at = ?,
                    ingested_at = ?
                WHERE source = ?
                  AND source_snapshot_id = ?
                """,
                (
                    _datetime_to_text(snapshot.observed_at),
                    _datetime_to_text(existing.ingested_at),
                    snapshot.source,
                    snapshot.source_snapshot_id,
                ),
            )
            connection.commit()
            return "updated"

    def get_roster_snapshot(self, snapshot_id: str) -> RosterSnapshot | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM roster_snapshots
                WHERE id = ?
                """,
                (snapshot_id,),
            ).fetchone()

            if row is None:
                return None
            return _row_to_roster_snapshot(row, connection)

    def get_roster_snapshot_by_source(
        self,
        source: str,
        source_snapshot_id: str,
    ) -> RosterSnapshot | None:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT *
                FROM roster_snapshots
                WHERE source = ?
                  AND source_snapshot_id = ?
                """,
                (source, source_snapshot_id),
            ).fetchone()

            if row is None:
                return None
            return _row_to_roster_snapshot(row, connection)

    def list_roster_snapshots_for_organization(
        self,
        source: str,
        source_team_id: str,
    ) -> list[RosterSnapshot]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT rs.*
                FROM roster_snapshots rs
                JOIN team_organizations org
                  ON org.id = rs.organization_id
                WHERE org.source = ?
                  AND org.source_team_id = ?
                ORDER BY rs.observed_at, rs.id
                """,
                (source, source_team_id),
            ).fetchall()

            return [_row_to_roster_snapshot(row, connection) for row in rows]

    def list_roster_snapshots_containing_player(
        self,
        source: str,
        source_player_id: str,
    ) -> list[RosterSnapshot]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT rs.*
                FROM roster_snapshots rs
                JOIN roster_memberships rm
                  ON rm.roster_snapshot_id = rs.id
                JOIN players player
                  ON player.id = rm.player_id
                WHERE rm.role = 'player'
                  AND player.source = ?
                  AND player.source_player_id = ?
                ORDER BY rs.observed_at, rs.id
                """,
                (source, source_player_id),
            ).fetchall()

            return [_row_to_roster_snapshot(row, connection) for row in rows]

    def list_roster_snapshots_available_before(
        self,
        cutoff_timestamp: datetime,
    ) -> list[RosterSnapshot]:
        cutoff_text = _datetime_to_text(cutoff_timestamp)
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM roster_snapshots
                WHERE observed_at < ?
                  AND (valid_from IS NULL OR valid_from < ?)
                  AND (valid_until IS NULL OR valid_until > ?)
                ORDER BY observed_at, id
                """,
                (cutoff_text, cutoff_text, cutoff_text),
            ).fetchall()

            return [_row_to_roster_snapshot(row, connection) for row in rows]

    def get_latest_roster_snapshot_for_organization_as_of(
        self,
        source: str,
        source_team_id: str,
        cutoff_timestamp: datetime,
    ) -> RosterSnapshot | None:
        cutoff_text = _datetime_to_text(cutoff_timestamp)
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT rs.*
                FROM roster_snapshots rs
                JOIN team_organizations org
                  ON org.id = rs.organization_id
                WHERE org.source = ?
                  AND org.source_team_id = ?
                  AND rs.observed_at < ?
                  AND (rs.valid_from IS NULL OR rs.valid_from < ?)
                  AND (rs.valid_until IS NULL OR rs.valid_until > ?)
                ORDER BY rs.observed_at DESC,
                         COALESCE(rs.valid_from, '') DESC,
                         rs.id DESC
                LIMIT 1
                """,
                (source, source_team_id, cutoff_text, cutoff_text, cutoff_text),
            ).fetchone()

            if row is None:
                return None
            return _row_to_roster_snapshot(row, connection)

    def list_historical_tournament_source_ids(
        self,
        *,
        source: str = "pandascore",
        limit: int | None = None,
    ) -> list[str]:
        with closing(get_connection(self.db_path)) as connection:
            rows = connection.execute(
                """
                SELECT tournament_source_id,
                       MAX(ended_at) AS latest_ended_at
                FROM historical_matches
                WHERE source = ?
                  AND tournament_source_id IS NOT NULL
                  AND ended_at IS NOT NULL
                GROUP BY tournament_source_id
                ORDER BY latest_ended_at DESC,
                         tournament_source_id
                """,
                (source,),
            ).fetchall()

        tournament_ids = [str(row["tournament_source_id"]) for row in rows]
        if limit is not None:
            return tournament_ids[:limit]
        return tournament_ids

    def get_historical_tournament_chronology_context(
        self,
        *,
        source: str,
        tournament_source_id: str,
        cutoff_timestamp: datetime,
    ) -> HistoricalTournamentChronologyContext | None:
        cutoff_text = _datetime_to_text(cutoff_timestamp)
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT MIN(started_at) AS earliest_started_at,
                       MAX(ended_at) AS latest_ended_at
                FROM historical_matches
                WHERE source = ?
                  AND tournament_source_id = ?
                  AND ended_at IS NOT NULL
                  AND ended_at < ?
                """,
                (source, tournament_source_id, cutoff_text),
            ).fetchone()

        if row is None:
            return None
        earliest_started_at = _datetime_from_text(row["earliest_started_at"])
        latest_ended_at = _datetime_from_text(row["latest_ended_at"])
        if earliest_started_at is None and latest_ended_at is None:
            return None
        return HistoricalTournamentChronologyContext(
            source=source,
            tournament_source_id=tournament_source_id,
            earliest_started_at=earliest_started_at,
            latest_ended_at=latest_ended_at,
        )

    def count_players(self) -> int:
        return self._count_table_rows("players")

    def count_team_organizations(self) -> int:
        return self._count_table_rows("team_organizations")

    def count_roster_snapshots(self) -> int:
        return self._count_table_rows("roster_snapshots")

    def count_roster_memberships(
        self,
        *,
        role: RosterMemberRole | None = None,
    ) -> int:
        with closing(get_connection(self.db_path)) as connection:
            if role is None:
                return int(
                    connection.execute(
                        "SELECT COUNT(*) FROM roster_memberships"
                    ).fetchone()[0]
                )
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM roster_memberships
                    WHERE role = ?
                    """,
                    (role,),
                ).fetchone()[0]
            )

    def count_roster_snapshots_with_explicit_validity(self) -> int:
        with closing(get_connection(self.db_path)) as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM roster_snapshots
                    WHERE valid_from IS NOT NULL
                       OR valid_until IS NOT NULL
                    """
                ).fetchone()[0]
            )

    def count_unique_player_roster_fingerprints(self) -> int:
        with closing(get_connection(self.db_path)) as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT player_roster_fingerprint)
                    FROM roster_snapshots
                    """
                ).fetchone()[0]
            )

    def roster_observed_at_range(self) -> tuple[datetime | None, datetime | None]:
        with closing(get_connection(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT MIN(observed_at) AS min_observed_at,
                       MAX(observed_at) AS max_observed_at
                FROM roster_snapshots
                """
            ).fetchone()

        return (
            _datetime_from_text(row["min_observed_at"]),
            _datetime_from_text(row["max_observed_at"]),
        )

    def _count_table_rows(self, table_name: str) -> int:
        with closing(get_connection(self.db_path)) as connection:
            return int(
                connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            )


def _datetime_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def calculate_profit_units(
    result: BetResult,
    odds: float,
    stake_pct: float,
) -> float:
    if result == "win":
        return stake_pct * (odds - 1)
    if result == "loss":
        return -stake_pct
    if result in ("push", "void"):
        return 0.0
    raise ValueError("result must be one of: win, loss, push, void")


def _datetime_from_text(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _required_datetime_from_text(value: object) -> datetime:
    parsed = _datetime_from_text(value)
    if parsed is None:
        raise ValueError("Expected datetime value, got NULL")
    return parsed


def _upsert_player_identity(
    connection: Connection,
    player: PlayerIdentity,
) -> RosterUpsertResult:
    player_id = build_identity_id(
        "player",
        player.source,
        player.source_player_id,
    )
    now_text = _datetime_to_text(datetime.now(timezone.utc))
    row = connection.execute(
        """
        SELECT *
        FROM players
        WHERE source = ?
          AND source_player_id = ?
        """,
        (player.source, player.source_player_id),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO players (
                id,
                source,
                source_player_id,
                name,
                ingested_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                player_id,
                player.source,
                player.source_player_id,
                player.name,
                now_text,
                now_text,
            ),
        )
        return "inserted"

    existing = _row_to_player_identity(row)
    if existing == player:
        return "unchanged"

    connection.execute(
        """
        UPDATE players
        SET id = ?,
            name = ?,
            updated_at = ?
        WHERE source = ?
          AND source_player_id = ?
        """,
        (
            player_id,
            player.name,
            now_text,
            player.source,
            player.source_player_id,
        ),
    )
    return "updated"


def _upsert_team_organization(
    connection: Connection,
    organization: TeamOrganization,
) -> RosterUpsertResult:
    organization_id = build_identity_id(
        "team-organization",
        organization.source,
        organization.source_team_id,
    )
    now_text = _datetime_to_text(datetime.now(timezone.utc))
    row = connection.execute(
        """
        SELECT *
        FROM team_organizations
        WHERE source = ?
          AND source_team_id = ?
        """,
        (organization.source, organization.source_team_id),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO team_organizations (
                id,
                source,
                source_team_id,
                name,
                ingested_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                organization_id,
                organization.source,
                organization.source_team_id,
                organization.name,
                now_text,
                now_text,
            ),
        )
        return "inserted"

    existing = _row_to_team_organization(row)
    if existing == organization:
        return "unchanged"

    connection.execute(
        """
        UPDATE team_organizations
        SET id = ?,
            name = ?,
            updated_at = ?
        WHERE source = ?
          AND source_team_id = ?
        """,
        (
            organization_id,
            organization.name,
            now_text,
            organization.source,
            organization.source_team_id,
        ),
    )
    return "updated"


def _roster_snapshot_values(snapshot: RosterSnapshot) -> tuple[object, ...]:
    return (
        snapshot.id,
        snapshot.source,
        snapshot.source_snapshot_id,
        build_identity_id(
            "team-organization",
            snapshot.organization.source,
            snapshot.organization.source_team_id,
        ),
        snapshot.source_context,
        snapshot.tournament_source_id,
        snapshot.tournament_name,
        _datetime_to_text(snapshot.observed_at),
        _datetime_to_text(snapshot.valid_from),
        _datetime_to_text(snapshot.valid_until),
        snapshot.player_roster_fingerprint,
        snapshot.staff_roster_fingerprint,
        _datetime_to_text(snapshot.ingested_at),
    )


def _replace_roster_memberships(
    connection: Connection,
    snapshot: RosterSnapshot,
) -> None:
    connection.execute(
        """
        DELETE FROM roster_memberships
        WHERE roster_snapshot_id = ?
        """,
        (snapshot.id,),
    )
    for position_index, member in enumerate(roster_members(snapshot), start=1):
        player_id: str | None = None
        if member.role == "player":
            if member.source_member_id is None:
                raise ValueError("player roster membership requires a source ID")
            player_id = build_identity_id(
                "player",
                member.source,
                member.source_member_id,
            )

        connection.execute(
            """
            INSERT INTO roster_memberships (
                id,
                roster_snapshot_id,
                role,
                player_id,
                source,
                source_member_id,
                member_name,
                position_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _membership_id(snapshot.id, member.role, position_index),
                snapshot.id,
                member.role,
                player_id,
                member.source,
                member.source_member_id,
                member.name,
                position_index,
            ),
        )


def _membership_id(
    snapshot_id: str,
    role: RosterMemberRole,
    position_index: int,
) -> str:
    return f"{snapshot_id}:{role}:{position_index}"


def _row_to_player_identity(row: object) -> PlayerIdentity:
    data = cast("dict[str, object]", row)
    return PlayerIdentity(
        source=str(data["source"]),
        source_player_id=str(data["source_player_id"]),
        name=str(data["name"]),
    )


def _row_to_team_organization(row: object) -> TeamOrganization:
    data = cast("dict[str, object]", row)
    return TeamOrganization(
        source=str(data["source"]),
        source_team_id=str(data["source_team_id"]),
        name=str(data["name"]),
    )


def _row_to_roster_snapshot(
    row: object,
    connection: Connection,
) -> RosterSnapshot:
    data = cast("dict[str, object]", row)
    organization_row = connection.execute(
        """
        SELECT *
        FROM team_organizations
        WHERE id = ?
        """,
        (data["organization_id"],),
    ).fetchone()
    if organization_row is None:
        raise ValueError("Roster snapshot is missing its organization")

    memberships = connection.execute(
        """
        SELECT *
        FROM roster_memberships
        WHERE roster_snapshot_id = ?
        ORDER BY position_index, id
        """,
        (data["id"],),
    ).fetchall()
    players: list[PlayerIdentity] = []
    coach: RosterCoach | None = None
    for membership in memberships:
        member_data = cast("dict[str, object]", membership)
        role = str(member_data["role"])
        if role == "player":
            player_row = connection.execute(
                """
                SELECT *
                FROM players
                WHERE id = ?
                """,
                (member_data["player_id"],),
            ).fetchone()
            if player_row is None:
                raise ValueError("Roster membership is missing its player")
            players.append(_row_to_player_identity(player_row))
        elif role == "coach":
            coach = RosterCoach(
                source=str(member_data["source"]),
                source_coach_id=_optional_text(member_data["source_member_id"]),
                name=str(member_data["member_name"]),
            )

    return RosterSnapshot(
        id=str(data["id"]),
        source=str(data["source"]),
        source_snapshot_id=str(data["source_snapshot_id"]),
        organization=_row_to_team_organization(organization_row),
        observed_at=_required_datetime_from_text(data["observed_at"]),
        players=tuple(players),
        coach=coach,
        source_context=_optional_text(data["source_context"]),
        tournament_source_id=_optional_text(data["tournament_source_id"]),
        tournament_name=_optional_text(data["tournament_name"]),
        valid_from=_datetime_from_text(data["valid_from"]),
        valid_until=_datetime_from_text(data["valid_until"]),
        player_roster_fingerprint=str(data["player_roster_fingerprint"]),
        staff_roster_fingerprint=_optional_text(data["staff_roster_fingerprint"]),
        ingested_at=_required_datetime_from_text(data["ingested_at"]),
    )


def _roster_snapshot_semantics_equivalent(
    existing: RosterSnapshot,
    incoming: RosterSnapshot,
) -> bool:
    return (
        existing.source == incoming.source
        and existing.source_snapshot_id == incoming.source_snapshot_id
        and _organization_identity_key(existing.organization)
        == _organization_identity_key(incoming.organization)
        and existing.source_context == incoming.source_context
        and existing.tournament_source_id == incoming.tournament_source_id
        and existing.tournament_name == incoming.tournament_name
        and existing.valid_from == incoming.valid_from
        and existing.valid_until == incoming.valid_until
        and _player_identity_keys(existing.players)
        == _player_identity_keys(incoming.players)
        and _coach_observation_key(existing.coach)
        == _coach_observation_key(incoming.coach)
        and existing.player_roster_fingerprint
        == incoming.player_roster_fingerprint
        and existing.staff_roster_fingerprint == incoming.staff_roster_fingerprint
    )


def _organization_identity_key(organization: TeamOrganization) -> tuple[str, str]:
    return (organization.source, organization.source_team_id)


def _player_identity_keys(
    players: tuple[PlayerIdentity, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple((player.source, player.source_player_id) for player in players)


def _coach_observation_key(
    coach: RosterCoach | None,
) -> tuple[str, str | None, str] | None:
    if coach is None:
        return None
    if coach.source_coach_id is None:
        return (coach.source, None, coach.name)
    return (coach.source, coach.source_coach_id, "")


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


def _row_to_odds_snapshot(row: object) -> OddsSnapshot:
    data = cast("dict[str, object]", row)
    return OddsSnapshot(
        id=str(data["id"]),
        session_id=str(data["session_id"]),
        match_id=str(data["match_id"]),
        external_market_id=_optional_text(data["external_market_id"]),
        market=str(data["market"]),
        selection=str(data["selection"]),
        line=_optional_float(data["line"]),
        odds=_required_float(data["odds"]),
        phase=cast(OddsPhase, data["phase"]),
        is_live=_required_bool(data["is_live"]),
        is_suspended=_required_bool(data["is_suspended"]),
        bookmaker=str(data["bookmaker"]),
        created_at=_required_datetime_from_text(data["created_at"]),
    )


def _row_to_bet_candidate(row: object) -> BetCandidate:
    data = cast("dict[str, object]", row)
    return BetCandidate(
        id=str(data["id"]),
        session_id=str(data["session_id"]),
        match_id=str(data["match_id"]),
        market=str(data["market"]),
        selection=str(data["selection"]),
        line=_optional_float(data["line"]),
        odds=_required_float(data["odds"]),
        phase=cast(OddsPhase, data["phase"]),
        market_score=_required_float(data["market_score"]),
        phase_score=_required_float(data["phase_score"]),
        line_score=_required_float(data["line_score"]),
        streamer_score=_required_float(data["streamer_score"]),
        risk_score=_required_float(data["risk_score"]),
        final_score=_required_float(data["final_score"]),
        decision=cast(Decision, data["decision"]),
        explanation=str(data["explanation"]),
        created_at=_required_datetime_from_text(data["created_at"]),
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


def _row_to_streamer_utterance(row: object) -> StreamerUtterance:
    data = cast("dict[str, object]", row)
    return StreamerUtterance(
        id=str(data["id"]),
        session_id=str(data["session_id"]),
        match_id=_optional_text(data["match_id"]),
        source=str(data["source"]),
        text=str(data["text"]),
        detected_market=_optional_text(data["detected_market"]),
        detected_selection=_optional_text(data["detected_selection"]),
        detected_team=_optional_text(data["detected_team"]),
        signal_type=_optional_text(data["signal_type"]),
        strength=_required_float(data["strength"]),
        confidence=_required_float(data["confidence"]),
        hype_flag=_required_bool(data["hype_flag"]),
        created_at=_required_datetime_from_text(data["created_at"]),
    )


def _row_to_historical_match(row: object) -> HistoricalMatch:
    data = cast("dict[str, object]", row)
    return HistoricalMatch(
        id=str(data["id"]),
        source=str(data["source"]),
        source_match_id=str(data["source_match_id"]),
        started_at=_required_datetime_from_text(data["started_at"]),
        ended_at=_datetime_from_text(data["ended_at"]),
        team_a_name=str(data["team_a_name"]),
        team_b_name=str(data["team_b_name"]),
        team_a_source_id=_optional_text(data["team_a_source_id"]),
        team_b_source_id=_optional_text(data["team_b_source_id"]),
        winner_name=_optional_text(data["winner_name"]),
        winner_source_id=_optional_text(data["winner_source_id"]),
        winner_side=cast(WinnerSide | None, _optional_text(data["winner_side"])),
        tournament_name=_optional_text(data["tournament_name"]),
        tournament_source_id=_optional_text(data["tournament_source_id"]),
        league_name=_optional_text(data["league_name"]),
        league_source_id=_optional_text(data["league_source_id"]),
        series_name=_optional_text(data["series_name"]),
        series_source_id=_optional_text(data["series_source_id"]),
        raw_stage_label=_optional_text(data["raw_stage_label"]),
        competitive_stage=CompetitiveStage(str(data["competitive_stage"])),
        normalized_round=TournamentRound(str(data["normalized_round"])),
        best_of=_optional_int(data["best_of"]),
        status=str(data["status"]),
        ingested_at=_required_datetime_from_text(data["ingested_at"]),
    )


def _row_to_historical_dota_game(row: object) -> HistoricalDotaGame:
    data = cast("dict[str, object]", row)
    return HistoricalDotaGame(
        id=str(data["id"]),
        source=str(data["source"]),
        source_game_id=str(data["source_game_id"]),
        parent_series_source_id=_optional_text(data["parent_series_source_id"]),
        linked_historical_match_id=_optional_text(data["linked_historical_match_id"]),
        started_at=_required_datetime_from_text(data["started_at"]),
        ended_at=_datetime_from_text(data["ended_at"]),
        team_a_name=str(data["team_a_name"]),
        team_b_name=str(data["team_b_name"]),
        team_a_source_id=_optional_text(data["team_a_source_id"]),
        team_b_source_id=_optional_text(data["team_b_source_id"]),
        winner_side=cast(DraftWinnerSide | None, _optional_text(data["winner_side"])),
        game_number=_optional_int(data["game_number"]),
        best_of=_optional_int(data["best_of"]),
        team_a_series_wins_before=_optional_int(data["team_a_series_wins_before"]),
        team_b_series_wins_before=_optional_int(data["team_b_series_wins_before"]),
        team_a_side=cast(DotaSide, str(data["team_a_side"])),
        patch=_optional_text(data["patch"]),
        draft_complete=_required_bool(data["draft_complete"]),
        tournament_name=_optional_text(data["tournament_name"]),
        tournament_source_id=_optional_text(data["tournament_source_id"]),
        league_name=_optional_text(data["league_name"]),
        league_source_id=_optional_text(data["league_source_id"]),
        raw_stage_label=_optional_text(data["raw_stage_label"]),
        ingested_at=_required_datetime_from_text(data["ingested_at"]),
    )


def _row_to_historical_draft_action(row: object) -> HistoricalDraftAction:
    data = cast("dict[str, object]", row)
    return HistoricalDraftAction(
        id=str(data["id"]),
        game_id=str(data["game_id"]),
        source=str(data["source"]),
        source_game_id=str(data["source_game_id"]),
        action_order=_required_int(data["action_order"]),
        action_kind=cast(DraftActionKind, str(data["action_kind"])),
        team_side=cast(DotaSide, str(data["team_side"])),
        team_source_id=_optional_text(data["team_source_id"]),
        hero_id=_required_int(data["hero_id"]),
    )


def _row_to_historical_dota_player_final_stats(
    row: object,
) -> HistoricalDotaPlayerFinalStats:
    data = cast("dict[str, object]", row)
    return HistoricalDotaPlayerFinalStats(
        id=str(data["id"]),
        game_id=str(data["game_id"]),
        source=str(data["source"]),
        source_game_id=str(data["source_game_id"]),
        account_id=str(data["account_id"]),
        player_slot=_optional_int(data["player_slot"]),
        team_side=cast(DotaSide, str(data["team_side"])),
        team_source_id=_optional_text(data["team_source_id"]),
        hero_id=_required_int(data["hero_id"]),
        kills=_optional_int(data["kills"]),
        deaths=_optional_int(data["deaths"]),
        assists=_optional_int(data["assists"]),
        net_worth=_optional_int(data["net_worth"]),
        last_hits=_optional_int(data["last_hits"]),
        denies=_optional_int(data["denies"]),
        gpm=_optional_int(data["gpm"]),
        xpm=_optional_int(data["xpm"]),
        level=_optional_int(data["level"]),
        hero_damage=_optional_int(data["hero_damage"]),
        tower_damage=_optional_int(data["tower_damage"]),
        hero_healing=_optional_int(data["hero_healing"]),
        final_item_ids=_item_ids_from_json(str(data["final_item_ids_json"])),
    )


def _row_to_historical_dota_advantage_point(
    row: object,
) -> HistoricalDotaAdvantagePoint:
    data = cast("dict[str, object]", row)
    return HistoricalDotaAdvantagePoint(
        id=str(data["id"]),
        game_id=str(data["game_id"]),
        source=str(data["source"]),
        source_game_id=str(data["source_game_id"]),
        metric=cast(AdvantageMetric, str(data["metric"])),
        source_index=_required_int(data["source_index"]),
        source_time_value=_optional_text(data["source_time_value"]),
        normalized_time_seconds=_optional_int(data["normalized_time_seconds"]),
        time_semantics_status=cast(
            TimeSemanticsStatus,
            str(data["time_semantics_status"]),
        ),
        value=_required_float(data["value"]),
    )


def _historical_match_values(match: HistoricalMatch) -> tuple[object, ...]:
    return (
        match.id,
        match.source,
        match.source_match_id,
        _datetime_to_text(match.started_at),
        _datetime_to_text(match.ended_at),
        match.team_a_name,
        match.team_b_name,
        match.team_a_source_id,
        match.team_b_source_id,
        match.winner_name,
        match.winner_source_id,
        match.winner_side,
        match.tournament_name,
        match.tournament_source_id,
        match.league_name,
        match.league_source_id,
        match.series_name,
        match.series_source_id,
        match.raw_stage_label,
        match.competitive_stage.value,
        match.normalized_round.value,
        match.best_of,
        match.status,
        _datetime_to_text(match.ingested_at),
    )


def _historical_dota_game_values(game: HistoricalDotaGame) -> tuple[object, ...]:
    return (
        game.id,
        game.source,
        game.source_game_id,
        game.parent_series_source_id,
        game.linked_historical_match_id,
        _datetime_to_text(game.started_at),
        _datetime_to_text(game.ended_at),
        game.team_a_name,
        game.team_b_name,
        game.team_a_source_id,
        game.team_b_source_id,
        game.winner_side,
        game.game_number,
        game.best_of,
        game.team_a_series_wins_before,
        game.team_b_series_wins_before,
        game.team_a_side,
        game.patch,
        int(game.draft_complete),
        game.tournament_name,
        game.tournament_source_id,
        game.league_name,
        game.league_source_id,
        game.raw_stage_label,
        _datetime_to_text(game.ingested_at),
    )


def _historical_draft_action_values(
    action: HistoricalDraftAction,
) -> tuple[object, ...]:
    return (
        action.id,
        action.game_id,
        action.source,
        action.source_game_id,
        action.action_order,
        action.action_kind,
        action.team_side,
        action.team_source_id,
        action.hero_id,
    )


def _historical_dota_player_final_stats_values(
    row: HistoricalDotaPlayerFinalStats,
) -> tuple[object, ...]:
    return (
        row.id,
        row.game_id,
        row.source,
        row.source_game_id,
        row.account_id,
        row.player_slot,
        row.team_side,
        row.team_source_id,
        row.hero_id,
        row.kills,
        row.deaths,
        row.assists,
        row.net_worth,
        row.last_hits,
        row.denies,
        row.gpm,
        row.xpm,
        row.level,
        row.hero_damage,
        row.tower_damage,
        row.hero_healing,
        json.dumps(list(row.final_item_ids), separators=(",", ":")),
    )


def _historical_dota_advantage_point_values(
    row: HistoricalDotaAdvantagePoint,
) -> tuple[object, ...]:
    return (
        row.id,
        row.game_id,
        row.source,
        row.source_game_id,
        row.metric,
        row.source_index,
        row.source_time_value,
        row.normalized_time_seconds,
        row.time_semantics_status,
        row.value,
    )


def _historical_matches_equivalent(
    existing: HistoricalMatch,
    incoming: HistoricalMatch,
) -> bool:
    return (
        existing.id == incoming.id
        and existing.source == incoming.source
        and existing.source_match_id == incoming.source_match_id
        and existing.started_at == incoming.started_at
        and existing.ended_at == incoming.ended_at
        and existing.team_a_name == incoming.team_a_name
        and existing.team_b_name == incoming.team_b_name
        and existing.team_a_source_id == incoming.team_a_source_id
        and existing.team_b_source_id == incoming.team_b_source_id
        and existing.winner_name == incoming.winner_name
        and existing.winner_source_id == incoming.winner_source_id
        and existing.winner_side == incoming.winner_side
        and existing.tournament_name == incoming.tournament_name
        and existing.tournament_source_id == incoming.tournament_source_id
        and existing.league_name == incoming.league_name
        and existing.league_source_id == incoming.league_source_id
        and existing.series_name == incoming.series_name
        and existing.series_source_id == incoming.series_source_id
        and existing.raw_stage_label == incoming.raw_stage_label
        and existing.competitive_stage == incoming.competitive_stage
        and existing.normalized_round == incoming.normalized_round
        and existing.best_of == incoming.best_of
        and existing.status == incoming.status
    )


def _historical_dota_games_equivalent(
    existing: HistoricalDotaGame,
    incoming: HistoricalDotaGame,
) -> bool:
    return (
        existing.id == incoming.id
        and existing.source == incoming.source
        and existing.source_game_id == incoming.source_game_id
        and existing.parent_series_source_id == incoming.parent_series_source_id
        and existing.linked_historical_match_id == incoming.linked_historical_match_id
        and existing.started_at == incoming.started_at
        and existing.ended_at == incoming.ended_at
        and existing.team_a_name == incoming.team_a_name
        and existing.team_b_name == incoming.team_b_name
        and existing.team_a_source_id == incoming.team_a_source_id
        and existing.team_b_source_id == incoming.team_b_source_id
        and existing.winner_side == incoming.winner_side
        and existing.game_number == incoming.game_number
        and existing.best_of == incoming.best_of
        and existing.team_a_series_wins_before == incoming.team_a_series_wins_before
        and existing.team_b_series_wins_before == incoming.team_b_series_wins_before
        and existing.team_a_side == incoming.team_a_side
        and existing.patch == incoming.patch
        and existing.draft_complete == incoming.draft_complete
        and existing.tournament_name == incoming.tournament_name
        and existing.tournament_source_id == incoming.tournament_source_id
        and existing.league_name == incoming.league_name
        and existing.league_source_id == incoming.league_source_id
        and existing.raw_stage_label == incoming.raw_stage_label
    )


def _raise_if_conflicting_draft_game(
    existing: HistoricalDotaGame,
    incoming: HistoricalDotaGame,
) -> None:
    checks = (
        ("started_at", existing.started_at, incoming.started_at),
        ("team_a_source_id", existing.team_a_source_id, incoming.team_a_source_id),
        ("team_b_source_id", existing.team_b_source_id, incoming.team_b_source_id),
    )
    for field_name, existing_value, incoming_value in checks:
        if (
            existing_value is not None
            and incoming_value is not None
            and existing_value != incoming_value
        ):
            raise ValueError(
                "Conflicting historical Dota game identity for "
                f"{incoming.source}:{incoming.source_game_id}: {field_name}."
            )
    if (
        existing.winner_side is not None
        and incoming.winner_side is not None
        and existing.winner_side != incoming.winner_side
    ):
        raise ValueError(
            "Conflicting historical Dota game winner for "
            f"{incoming.source}:{incoming.source_game_id}."
        )


def _item_ids_from_json(value: str) -> tuple[int, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    item_ids: list[int] = []
    for item in decoded:
        item_id = _optional_int(item)
        if item_id is not None and item_id > 0:
            item_ids.append(item_id)
    return tuple(item_ids)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _required_float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _required_int(value)


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
