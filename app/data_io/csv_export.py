from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.domain import Bet, BetCandidate, StreamerUtterance

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


def _datetime_value(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _optional_value(value: object | None) -> object:
    if value is None:
        return ""
    return value
