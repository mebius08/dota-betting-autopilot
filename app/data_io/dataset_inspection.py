from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from app.evaluation import build_evaluation_dataset
from app.ml import build_ml_training_status

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


DatasetReadiness = Literal[
    "not_enough_data",
    "smoke_training_ready",
    "evaluation_ready",
]

EVALUATION_MIN_RECORDS = 10
TRAINING_STATUS_MIN_ROWS = 30
OUTCOMES = ("win", "loss", "push", "void")


@dataclass(frozen=True)
class DatasetInspectionReport:
    database_path: Path
    sessions: int
    matches: int
    odds_snapshots: int
    bet_candidates: int
    paper_bets: int
    open_bets: int
    settled_bets: int
    outcomes: dict[str, int]
    streamer_utterances: int
    usable_ml_records: int
    readiness: DatasetReadiness


def inspect_dataset(
    repository: SQLiteRepository,
    db_path: str | Path,
) -> DatasetInspectionReport:
    bets = repository.list_bets()
    evaluation_dataset = build_evaluation_dataset(repository)
    ml_status = build_ml_training_status(
        repository,
        min_rows=TRAINING_STATUS_MIN_ROWS,
    )
    outcomes = {outcome: 0 for outcome in OUTCOMES}
    for bet in bets:
        if bet.result in outcomes:
            outcomes[bet.result] += 1

    return DatasetInspectionReport(
        database_path=Path(db_path),
        sessions=len(repository.list_sessions()),
        matches=len(repository.list_matches()),
        odds_snapshots=len(repository.list_odds_snapshots()),
        bet_candidates=len(repository.list_bet_candidates()),
        paper_bets=len(bets),
        open_bets=len(repository.list_open_bets()),
        settled_bets=sum(1 for bet in bets if bet.status == "settled"),
        outcomes=outcomes,
        streamer_utterances=len(repository.list_streamer_utterances()),
        usable_ml_records=evaluation_dataset.usable_records,
        readiness=_readiness_from_counts(
            usable_ml_records=evaluation_dataset.usable_records,
            positive_rows=ml_status.positive_rows,
            negative_rows=ml_status.negative_rows,
        ),
    )


def format_dataset_inspection_report(report: DatasetInspectionReport) -> str:
    lines = [
        f"Database: {report.database_path.as_posix()}",
        "",
        f"Sessions: {report.sessions}",
        f"Matches: {report.matches}",
        f"Odds snapshots: {report.odds_snapshots}",
        f"Bet candidates: {report.bet_candidates}",
        f"Paper bets: {report.paper_bets}",
        "",
        f"Open bets: {report.open_bets}",
        f"Settled bets: {report.settled_bets}",
        "",
        "Outcomes:",
    ]
    for outcome in OUTCOMES:
        lines.append(f"  {outcome}: {report.outcomes[outcome]}")

    lines.extend(
        [
            "",
            f"Streamer utterances: {report.streamer_utterances}",
            "",
            f"Usable ML records: {report.usable_ml_records}",
            "",
            f"Readiness: {report.readiness}",
        ]
    )
    return "\n".join(lines)


def _readiness_from_counts(
    *,
    usable_ml_records: int,
    positive_rows: int,
    negative_rows: int,
) -> DatasetReadiness:
    has_both_classes = positive_rows > 0 and negative_rows > 0
    if usable_ml_records >= EVALUATION_MIN_RECORDS and has_both_classes:
        return "evaluation_ready"
    if usable_ml_records > 0 and has_both_classes:
        return "smoke_training_ready"
    return "not_enough_data"
