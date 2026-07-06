from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from app.domain import BetResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.storage import SQLiteRepository


REQUIRED_COLUMNS = ("bet_id", "outcome", "profit_units")
SETTLEMENT_OUTCOMES = ("win", "loss", "push", "void")


@dataclass(frozen=True)
class SettlementRow:
    bet_id: str
    outcome: BetResult
    profit_units: float


@dataclass(frozen=True)
class SettlementImportResult:
    processed_rows: int
    updated_bets: int
    skipped_rows: int
    warnings: list[str]


def import_settlements_from_csv(
    repository: SQLiteRepository,
    csv_path: str | Path,
) -> SettlementImportResult:
    path = Path(csv_path)
    processed_rows = 0
    updated_bets = 0
    skipped_rows = 0
    warnings: list[str] = []

    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        _validate_header(reader.fieldnames)

        for row_number, row in enumerate(reader, start=2):
            processed_rows += 1
            warning = validate_settlement_row(row, row_number)
            if warning is not None:
                warnings.append(warning)
                skipped_rows += 1
                continue

            settlement = parse_settlement_row(row)
            if repository.get_bet(settlement.bet_id) is None:
                warnings.append(
                    f"row {row_number}: unknown bet_id: {settlement.bet_id}"
                )
                skipped_rows += 1
                continue

            try:
                repository.settle_bet(settlement.bet_id, settlement.outcome)
            except ValueError as exc:
                warnings.append(f"row {row_number}: {exc}")
                skipped_rows += 1
                continue

            updated_bets += 1

    return SettlementImportResult(
        processed_rows=processed_rows,
        updated_bets=updated_bets,
        skipped_rows=skipped_rows,
        warnings=warnings,
    )


def validate_settlement_row(
    row: Mapping[str | None, str | None],
    row_number: int = 0,
) -> str | None:
    prefix = f"row {row_number}: " if row_number else ""
    if None in row:
        return f"{prefix}malformed row"

    bet_id = _cell(row, "bet_id")
    if not bet_id:
        return f"{prefix}missing bet_id"

    outcome = _cell(row, "outcome")
    if not outcome or _normalize_outcome(outcome) not in SETTLEMENT_OUTCOMES:
        return f"{prefix}invalid outcome: {outcome}"

    profit_units = _cell(row, "profit_units")
    if not _is_valid_float(profit_units):
        return f"{prefix}invalid profit_units: {profit_units}"

    return None


def parse_settlement_row(
    row: Mapping[str | None, str | None],
) -> SettlementRow:
    outcome = cast(BetResult, _normalize_outcome(_cell(row, "outcome")))
    return SettlementRow(
        bet_id=_cell(row, "bet_id"),
        outcome=outcome,
        profit_units=float(_cell(row, "profit_units")),
    )


def _validate_header(fieldnames: Sequence[str] | None) -> None:
    if fieldnames is None:
        raise ValueError(
            "Settlement CSV header must include: bet_id, outcome, profit_units"
        )

    missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"Settlement CSV missing required columns: {missing_text}")


def _cell(row: Mapping[str | None, str | None], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    return value.strip()


def _normalize_outcome(value: str) -> str:
    return value.strip().lower()


def _is_valid_float(value: str) -> bool:
    try:
        parsed = float(value)
    except ValueError:
        return False

    return math.isfinite(parsed)
