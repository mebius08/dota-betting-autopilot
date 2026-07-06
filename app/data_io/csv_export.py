from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.domain import Bet, BetCandidate, StreamerUtterance
from app.history import HistoricalMatch

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.storage import SQLiteRepository


BET_COLUMNS = [
    "id",
    "session_id",
    "match_id",
    "candidate_id",
    "mode",
    "market",
    "selection",
    "line",
    "odds",
    "stake_pct",
    "status",
    "result",
    "profit_units",
    "created_at",
    "settled_at",
]
CANDIDATE_COLUMNS = [
    "id",
    "session_id",
    "match_id",
    "market",
    "selection",
    "line",
    "odds",
    "phase",
    "market_score",
    "phase_score",
    "line_score",
    "streamer_score",
    "risk_score",
    "final_score",
    "decision",
    "explanation",
    "created_at",
]
UTTERANCE_COLUMNS = [
    "id",
    "session_id",
    "match_id",
    "source",
    "text",
    "detected_market",
    "detected_selection",
    "detected_team",
    "signal_type",
    "strength",
    "confidence",
    "hype_flag",
    "created_at",
]
HISTORY_COLUMNS = [
    "id",
    "source",
    "source_match_id",
    "started_at",
    "ended_at",
    "team_a_name",
    "team_b_name",
    "team_a_source_id",
    "team_b_source_id",
    "winner_name",
    "winner_source_id",
    "winner_side",
    "tournament_name",
    "tournament_source_id",
    "league_name",
    "league_source_id",
    "series_name",
    "series_source_id",
    "raw_stage_label",
    "competitive_stage",
    "normalized_round",
    "best_of",
    "status",
    "usable_for_match_winner_training",
    "ingested_at",
]


@dataclass(frozen=True)
class ExportResult:
    output_path: Path
    row_count: int


def export_bets_to_csv(
    repository: SQLiteRepository,
    output_path: str | Path,
) -> ExportResult:
    rows = [_bet_to_row(bet) for bet in repository.list_bets()]
    return _write_csv(output_path, BET_COLUMNS, rows)


def export_candidates_to_csv(
    repository: SQLiteRepository,
    output_path: str | Path,
) -> ExportResult:
    rows = [
        _candidate_to_row(candidate)
        for candidate in repository.list_bet_candidates()
    ]
    return _write_csv(output_path, CANDIDATE_COLUMNS, rows)


def export_utterances_to_csv(
    repository: SQLiteRepository,
    output_path: str | Path,
) -> ExportResult:
    rows = [
        _utterance_to_row(utterance)
        for utterance in repository.list_streamer_utterances()
    ]
    return _write_csv(output_path, UTTERANCE_COLUMNS, rows)


def export_history_to_csv(
    repository: SQLiteRepository,
    output_path: str | Path,
) -> ExportResult:
    rows = [
        _historical_match_to_row(match)
        for match in repository.list_historical_matches()
    ]
    return _write_csv(output_path, HISTORY_COLUMNS, rows)


def _write_csv(
    output_path: str | Path,
    columns: list[str],
    rows: Iterable[dict[str, object]],
) -> ExportResult:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row_list = list(rows)

    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(row_list)

    return ExportResult(output_path=path, row_count=len(row_list))


def _bet_to_row(bet: Bet) -> dict[str, object]:
    return {
        "id": bet.id,
        "session_id": bet.session_id,
        "match_id": bet.match_id,
        "candidate_id": bet.candidate_id,
        "mode": bet.mode,
        "market": bet.market,
        "selection": bet.selection,
        "line": _optional_value(bet.line),
        "odds": bet.odds,
        "stake_pct": bet.stake_pct,
        "status": bet.status,
        "result": bet.result,
        "profit_units": bet.profit_units,
        "created_at": _datetime_value(bet.created_at),
        "settled_at": _datetime_value(bet.settled_at),
    }


def _candidate_to_row(candidate: BetCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
        "session_id": candidate.session_id,
        "match_id": candidate.match_id,
        "market": candidate.market,
        "selection": candidate.selection,
        "line": _optional_value(candidate.line),
        "odds": candidate.odds,
        "phase": candidate.phase,
        "market_score": candidate.market_score,
        "phase_score": candidate.phase_score,
        "line_score": candidate.line_score,
        "streamer_score": candidate.streamer_score,
        "risk_score": candidate.risk_score,
        "final_score": candidate.final_score,
        "decision": candidate.decision,
        "explanation": candidate.explanation,
        "created_at": _datetime_value(candidate.created_at),
    }


def _utterance_to_row(utterance: StreamerUtterance) -> dict[str, object]:
    return {
        "id": utterance.id,
        "session_id": utterance.session_id,
        "match_id": _optional_value(utterance.match_id),
        "source": utterance.source,
        "text": utterance.text,
        "detected_market": _optional_value(utterance.detected_market),
        "detected_selection": _optional_value(utterance.detected_selection),
        "detected_team": _optional_value(utterance.detected_team),
        "signal_type": _optional_value(utterance.signal_type),
        "strength": utterance.strength,
        "confidence": utterance.confidence,
        "hype_flag": int(utterance.hype_flag),
        "created_at": _datetime_value(utterance.created_at),
    }


def _historical_match_to_row(match: HistoricalMatch) -> dict[str, object]:
    return {
        "id": match.id,
        "source": match.source,
        "source_match_id": match.source_match_id,
        "started_at": _datetime_value(match.started_at),
        "ended_at": _datetime_value(match.ended_at),
        "team_a_name": match.team_a_name,
        "team_b_name": match.team_b_name,
        "team_a_source_id": _optional_value(match.team_a_source_id),
        "team_b_source_id": _optional_value(match.team_b_source_id),
        "winner_name": _optional_value(match.winner_name),
        "winner_source_id": _optional_value(match.winner_source_id),
        "winner_side": _optional_value(match.winner_side),
        "tournament_name": _optional_value(match.tournament_name),
        "tournament_source_id": _optional_value(match.tournament_source_id),
        "league_name": _optional_value(match.league_name),
        "league_source_id": _optional_value(match.league_source_id),
        "series_name": _optional_value(match.series_name),
        "series_source_id": _optional_value(match.series_source_id),
        "raw_stage_label": _optional_value(match.raw_stage_label),
        "competitive_stage": match.competitive_stage.value,
        "normalized_round": match.normalized_round.value,
        "best_of": _optional_value(match.best_of),
        "status": match.status,
        "usable_for_match_winner_training": int(
            match.usable_for_match_winner_training
        ),
        "ingested_at": _datetime_value(match.ingested_at),
    }


def _datetime_value(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _optional_value(value: object | None) -> object:
    if value is None:
        return ""
    return value
