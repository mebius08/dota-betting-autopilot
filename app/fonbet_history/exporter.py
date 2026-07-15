from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Protocol, cast

from app.fonbet_history.client import (
    FonbetConfigurationError,
    FonbetHistoryError,
)
from app.fonbet_history.normalize import (
    ENTRY_DECISION_COLUMNS,
    ENTRY_OUTCOME_COLUMNS,
    LEG_COLUMNS,
    NORMALIZED_COLUMNS,
    SINGLE_EVENT_SEQUENCE_COLUMNS,
    FonbetDataError,
    build_entry_exports,
    build_single_event_sequences,
    coupon_id_from_summary,
    csv_row,
    entry_decision_csv_row,
    entry_outcome_csv_row,
    leg_csv_row,
    load_coupon_summaries,
    normalize_coupon,
    normalize_coupon_legs,
    single_event_sequence_csv_row,
)


class CouponDetailClient(Protocol):
    def fetch_coupon_detail(self, coupon_id: str) -> Mapping[str, object]:
        ...

    def sanitize_response(self, payload: Mapping[str, object]) -> dict[str, object]:
        ...


@dataclass(frozen=True)
class ExportFailure:
    coupon_id: str
    stage: str
    message: str


@dataclass(frozen=True)
class ExportResult:
    summary_count: int
    fetched_count: int
    resumed_count: int
    deferred_count: int
    failure_count: int
    normalized_json_path: Path
    normalized_csv_path: Path


CASHOUT_HOLD_AUDIT_COLUMNS = [
    "coupon_id",
    "selection",
    "entry_odds",
    "result_score",
    "cash_stake_rub",
    "actual_return_rub",
    "actual_profit_rub",
    "hold_result",
    "hold_return_rub",
    "hold_profit_rub",
    "cashout_vs_hold_delta_rub",
    "derivation_status",
]

_SIMPLE_FINAL_SCORE_PATTERN = re.compile(r"([0-9]+):([0-9]+)")
_SUPPORTED_HOLD_SELECTIONS = {"Поб 1", "Поб 2", "Ничья"}


def export_personal_history(
    *,
    summary_path: str | Path,
    local_data_dir: str | Path,
    amount_divisor: Decimal,
    client: CouponDetailClient | None,
    max_fetches: int = 100,
    delay_seconds: float = 1.0,
    sleep_func: Callable[[float], None] = time.sleep,
) -> ExportResult:
    if max_fetches < 0:
        raise FonbetConfigurationError("Maximum fetches must not be negative.")
    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise FonbetConfigurationError(
            "Request delay must be finite and not negative."
        )
    if amount_divisor <= 0 or not amount_divisor.is_finite():
        raise FonbetConfigurationError(
            "Amount divisor must be finite and greater than zero."
        )

    data_dir = validate_local_data_dir(local_data_dir)
    summaries = load_coupon_summaries(summary_path)
    paths = [
        raw_response_path(data_dir, coupon_id_from_summary(summary))
        for summary in summaries
    ]
    if max_fetches > 0 and any(not path.exists() for path in paths) and client is None:
        raise FonbetConfigurationError(
            "Network fetch requires FONBET_FSID and FONBET_CLIENT_ID, plus "
            "FONBET_BET_TYPE_NAME and FONBET_SYS_ID request context. Set the "
            "environment variables or pass the matching runtime arguments."
        )

    records: list[dict[str, object]] = []
    leg_records: list[dict[str, object]] = []
    failures: list[ExportFailure] = []
    fetched_count = 0
    resumed_count = 0
    deferred_count = 0
    network_attempts = 0

    for summary, raw_path in zip(summaries, paths, strict=True):
        coupon_id = coupon_id_from_summary(summary)
        detail: Mapping[str, object] | None = None
        detail_status = "missing"

        if raw_path.exists():
            resumed_count += 1
            try:
                detail = _read_raw_response(raw_path)
                detail_status = "resumed"
            except FonbetDataError as exc:
                detail_status = "invalid_raw"
                failures.append(
                    ExportFailure(coupon_id, "read_raw", str(exc))
                )
        elif network_attempts >= max_fetches:
            detail_status = "deferred_by_limit"
            deferred_count += 1
        else:
            if network_attempts > 0 and delay_seconds > 0:
                sleep_func(delay_seconds)
            network_attempts += 1
            try:
                assert client is not None
                response = client.fetch_coupon_detail(coupon_id)
                sanitized = client.sanitize_response(response)
                _write_json_atomic(raw_path, sanitized)
                detail = sanitized
                detail_status = "fetched"
                fetched_count += 1
            except FonbetHistoryError as exc:
                detail_status = "fetch_failed"
                failures.append(
                    ExportFailure(coupon_id, "fetch", str(exc))
                )
            except OSError:
                detail_status = "store_failed"
                failures.append(
                    ExportFailure(
                        coupon_id,
                        "store_raw",
                        "Could not store the raw coupon response.",
                    )
                )

        try:
            record = normalize_coupon(
                summary,
                detail,
                amount_divisor=amount_divisor,
                detail_status=detail_status,
            )
            records.append(record)
            leg_records.extend(normalize_coupon_legs(coupon_id, detail))
        except FonbetDataError as exc:
            failures.append(
                ExportFailure(coupon_id, "normalize", str(exc))
            )

    normalized_dir = data_dir / "normalized"
    json_path = normalized_dir / "coupons.json"
    csv_path = normalized_dir / "coupons.csv"
    json_payload = {
        "schema_version": 1,
        "amount_conversion": {
            "source_fields": ["betSum", "winSum"],
            "divisor": _decimal_json_value(amount_divisor),
            "formula": "rub = source_amount / divisor",
        },
        "accounting": {
            "cash": "profit_rub = return_rub - cash_stake_rub",
            "freebet": (
                "cash_stake_rub = 0; freebet nominal is retained separately"
            ),
        },
        "entry_odds": {
            "source_priority": (
                "couponK from summary/summary.extra/detail.header, then a "
                "single leg factor"
            ),
            "express_fallback": (
                "product of every positive leg factor, rounded to 2 decimal "
                "places with ROUND_HALF_UP; unavailable if any factor is missing"
            ),
        },
        "records": records,
        "failures": [
            {
                "coupon_id": failure.coupon_id,
                "stage": failure.stage,
                "message": failure.message,
            }
            for failure in failures
        ],
    }
    _validate_leg_records(records, leg_records)
    sequence_records = build_single_event_sequences(records, leg_records)
    entry_decisions, entry_outcomes = build_entry_exports(records, leg_records)
    verified_cash_win_count = verify_settled_cash_win_payout_rounding(records)
    cashout_hold_audit = build_cashout_hold_audit(records)
    _write_json_atomic(json_path, json_payload)
    _write_csv_atomic(csv_path, records)
    _write_json_atomic(
        normalized_dir / "legs.json",
        {"schema_version": 1, "records": leg_records},
    )
    _write_leg_csv_atomic(normalized_dir / "legs.csv", leg_records)
    _write_json_atomic(
        normalized_dir / "single_event_sequences.json",
        {"schema_version": 1, "records": sequence_records},
    )
    _write_single_event_sequence_csv_atomic(
        normalized_dir / "single_event_sequences.csv",
        sequence_records,
    )
    _write_json_atomic(
        normalized_dir / "entry_decisions.json",
        {"schema_version": 1, "records": entry_decisions},
    )
    _write_entry_decision_csv_atomic(
        normalized_dir / "entry_decisions.csv",
        entry_decisions,
    )
    _write_json_atomic(
        normalized_dir / "entry_outcomes.json",
        {"schema_version": 1, "records": entry_outcomes},
    )
    _write_entry_outcome_csv_atomic(
        normalized_dir / "entry_outcomes.csv",
        entry_outcomes,
    )
    _write_json_atomic(
        normalized_dir / "cashout_hold_audit.json",
        {
            "schema_version": 1,
            "payout_rounding": {
                "formula": (
                    "ROUND_HALF_UP(cash_stake_rub * entry_odds) to whole RUB"
                ),
                "settled_cash_win_rows_verified": verified_cash_win_count,
            },
            "records": cashout_hold_audit,
        },
    )
    _write_cashout_hold_audit_csv_atomic(
        normalized_dir / "cashout_hold_audit.csv",
        cashout_hold_audit,
    )

    return ExportResult(
        summary_count=len(summaries),
        fetched_count=fetched_count,
        resumed_count=resumed_count,
        deferred_count=deferred_count,
        failure_count=len(failures),
        normalized_json_path=json_path,
        normalized_csv_path=csv_path,
    )


def validate_local_data_dir(path: str | Path) -> Path:
    data_dir = Path(path)
    if "local-data" not in {part.casefold() for part in data_dir.parts}:
        raise FonbetConfigurationError(
            "Personal FONBET output must be inside a local-data directory."
        )
    return data_dir


def raw_response_path(local_data_dir: str | Path, coupon_id: str) -> Path:
    digest = hashlib.sha256(coupon_id.encode("utf-8")).hexdigest()
    return Path(local_data_dir) / "raw" / f"coupon-{digest}.json"


def verify_settled_cash_win_payout_rounding(
    records: Sequence[Mapping[str, object]],
) -> int:
    verified_count = 0
    for record in records:
        if record.get("state") != "Win":
            continue
        cash_stake = _finite_decimal(record.get("cash_stake_rub"))
        entry_odds = _finite_decimal(record.get("entry_odds"))
        actual_return = _finite_decimal(record.get("return_rub"))
        if (
            cash_stake is None
            or cash_stake <= 0
            or entry_odds is None
            or entry_odds <= 0
            or actual_return is None
        ):
            continue
        verified_count += 1
        expected_return = _winning_cash_return(cash_stake, entry_odds)
        if actual_return != expected_return:
            coupon_id = record.get("coupon_id")
            raise FonbetDataError(
                "Settled cash Win payout does not match whole-RUB "
                f"ROUND_HALF_UP for coupon {coupon_id}."
            )
    return verified_count


def build_cashout_hold_audit(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    audit_records: list[dict[str, object]] = []
    for record in records:
        cash_stake = _finite_decimal(record.get("cash_stake_rub"))
        if (
            record.get("state") != "Sold"
            or record.get("is_express") is not False
            or record.get("is_freebet") is not False
            or cash_stake is None
            or cash_stake <= 0
        ):
            continue

        selection = record.get("selection")
        result_score = record.get("result_score")
        audit_record: dict[str, object] = {
            "coupon_id": record.get("coupon_id"),
            "selection": selection,
            "entry_odds": record.get("entry_odds"),
            "result_score": result_score,
            "cash_stake_rub": record.get("cash_stake_rub"),
            "actual_return_rub": record.get("return_rub"),
            "actual_profit_rub": record.get("profit_rub"),
            "hold_result": None,
            "hold_return_rub": None,
            "hold_profit_rub": None,
            "cashout_vs_hold_delta_rub": None,
            "derivation_status": "unsupported_selection",
        }
        if (
            not isinstance(selection, str)
            or selection not in _SUPPORTED_HOLD_SELECTIONS
        ):
            audit_records.append(audit_record)
            continue
        if not isinstance(result_score, str):
            audit_record["derivation_status"] = "unsupported_result_score"
            audit_records.append(audit_record)
            continue
        score_match = _SIMPLE_FINAL_SCORE_PATTERN.fullmatch(result_score)
        if score_match is None:
            audit_record["derivation_status"] = "unsupported_result_score"
            audit_records.append(audit_record)
            continue

        score_1, score_2 = (int(value) for value in score_match.groups())
        hold_won = (
            (selection == "Поб 1" and score_1 > score_2)
            or (selection == "Поб 2" and score_2 > score_1)
            or (selection == "Ничья" and score_1 == score_2)
        )
        audit_record["hold_result"] = "Win" if hold_won else "Lose"
        if hold_won:
            entry_odds = _finite_decimal(record.get("entry_odds"))
            if entry_odds is None or entry_odds <= 0:
                audit_record["derivation_status"] = "unsupported_entry_odds"
                audit_records.append(audit_record)
                continue
            hold_return = _winning_cash_return(cash_stake, entry_odds)
        else:
            hold_return = Decimal(0)

        hold_profit = hold_return - cash_stake
        audit_record["hold_return_rub"] = _decimal_json_value(hold_return)
        audit_record["hold_profit_rub"] = _decimal_json_value(hold_profit)
        actual_return = _finite_decimal(record.get("return_rub"))
        if actual_return is None:
            audit_record["derivation_status"] = "missing_actual_return"
            audit_records.append(audit_record)
            continue
        audit_record["cashout_vs_hold_delta_rub"] = _decimal_json_value(
            actual_return - hold_return
        )
        audit_record["derivation_status"] = "derived"
        audit_records.append(audit_record)
    return audit_records


def _read_raw_response(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FonbetDataError(
            "Existing raw coupon response is unreadable; delete it to retry."
        ) from exc
    if not isinstance(value, Mapping):
        raise FonbetDataError(
            "Existing raw coupon response has an unexpected JSON shape; "
            "delete it to retry."
        )
    return cast(Mapping[str, object], value)


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv_atomic(
    path: Path,
    records: Sequence[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=NORMALIZED_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(csv_row(record) for record in records)
    temporary.replace(path)


def _write_leg_csv_atomic(
    path: Path,
    records: Sequence[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=LEG_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(leg_csv_row(record) for record in records)
    temporary.replace(path)


def _write_single_event_sequence_csv_atomic(
    path: Path,
    records: Sequence[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=SINGLE_EVENT_SEQUENCE_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(
            single_event_sequence_csv_row(record) for record in records
        )
    temporary.replace(path)


def _write_entry_decision_csv_atomic(
    path: Path,
    records: Sequence[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=ENTRY_DECISION_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(entry_decision_csv_row(record) for record in records)
    temporary.replace(path)


def _write_entry_outcome_csv_atomic(
    path: Path,
    records: Sequence[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=ENTRY_OUTCOME_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(entry_outcome_csv_row(record) for record in records)
    temporary.replace(path)


def _write_cashout_hold_audit_csv_atomic(
    path: Path,
    records: Sequence[Mapping[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=CASHOUT_HOLD_AUDIT_COLUMNS,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(records)
    temporary.replace(path)


def _validate_leg_records(
    coupon_records: Sequence[Mapping[str, object]],
    leg_records: Sequence[Mapping[str, object]],
) -> None:
    keys = [
        (record.get("coupon_id"), record.get("leg_index"))
        for record in leg_records
    ]
    if len(keys) != len(set(keys)):
        raise FonbetDataError("Normalized leg keys must be unique.")

    actual_counts: dict[object, int] = {}
    for record in leg_records:
        coupon_id = record.get("coupon_id")
        actual_counts[coupon_id] = actual_counts.get(coupon_id, 0) + 1
    for coupon in coupon_records:
        coupon_id = coupon.get("coupon_id")
        if actual_counts.get(coupon_id, 0) != coupon.get("leg_count"):
            raise FonbetDataError(
                "Normalized leg counts must match coupon leg counts."
            )


def _decimal_json_value(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    return float(value)


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal_value.is_finite():
        return None
    return decimal_value


def _winning_cash_return(cash_stake: Decimal, entry_odds: Decimal) -> Decimal:
    return (cash_stake * entry_odds).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
