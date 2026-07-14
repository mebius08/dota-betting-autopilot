from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
from typing import cast

from app.fonbet_history.client import FonbetHistoryError


SUMMARY_LIST_KEYS = ("coupons", "couponList", "items", "records")
SUMMARY_WRAPPER_KEYS = ("data", "result", "response")
DETAIL_WRAPPER_KEYS = ("coupon", "couponInfo", "data", "response")
LEG_LIST_KEYS = ("bets", "betList", "events", "stakes", "selections", "items")
LEG_FIELDS = {
    "eventName",
    "stakeName",
    "factorValue",
    "score",
    "resultScore",
    "sportName",
    "live",
    "eventStartTime",
    "result",
}
SETTLED_STATES = {"win", "lose", "sold", "cashout", "cash-out", "cash_out"}

NORMALIZED_COLUMNS = [
    "coupon_id",
    "registration_time",
    "calculation_time",
    "coupon_type",
    "bet_mode",
    "bet_count",
    "state",
    "stake_rub",
    "return_rub",
    "profit_rub",
    "entry_odds",
    "event_name",
    "selection",
    "entry_score",
    "result_score",
    "is_live",
    "event_start_time",
    "sport_name",
    "is_express",
    "is_freebet",
    "is_cashout",
    "cash_stake_rub",
    "freebet_nominal_rub",
    "accounting_method",
    "source_bet_sum",
    "source_win_sum",
    "source_coupon_k",
    "source_coupon_original_k",
    "amount_divisor",
    "leg_count",
    "detail_status",
    "legs_json",
]


class FonbetDataError(FonbetHistoryError):
    pass


def load_coupon_summaries(path: str | Path) -> list[Mapping[str, object]]:
    summary_path = Path(path)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FonbetDataError(f"Summary JSON not found: {summary_path}") from exc
    except OSError as exc:
        raise FonbetDataError(f"Could not read summary JSON: {summary_path}") from exc
    except json.JSONDecodeError as exc:
        raise FonbetDataError("Summary file contains invalid JSON.") from exc

    rows = _extract_summary_rows(payload)
    seen_ids: set[str] = set()
    for row in rows:
        coupon_id = coupon_id_from_summary(row)
        if coupon_id in seen_ids:
            raise FonbetDataError("Summary JSON contains duplicate coupon IDs.")
        seen_ids.add(coupon_id)
    return rows


def coupon_id_from_summary(summary: Mapping[str, object]) -> str:
    value = summary.get("couponId")
    if isinstance(value, bool) or value is None:
        raise FonbetDataError("Each summary record must contain couponId.")
    if isinstance(value, int | str):
        coupon_id = str(value).strip()
        if coupon_id:
            return coupon_id
    raise FonbetDataError("Each summary record must contain a usable couponId.")


def normalize_coupon(
    summary: Mapping[str, object],
    detail: Mapping[str, object] | None,
    *,
    amount_divisor: Decimal,
    detail_status: str,
) -> dict[str, object]:
    if amount_divisor <= 0 or not amount_divisor.is_finite():
        raise FonbetDataError("Amount divisor must be finite and greater than zero.")

    coupon_id = coupon_id_from_summary(summary)
    detail_root = _detail_root(detail) if detail is not None else None
    legs = _normalized_legs(detail)
    state = _optional_text(summary.get("betState"))
    coupon_type = _optional_text(summary.get("couponType"))
    bet_mode = _optional_text(summary.get("betMode"))
    bet_count = _optional_int(summary.get("betCount"))
    is_freebet = _is_freebet(summary, coupon_type=coupon_type, bet_mode=bet_mode)
    is_express = _is_express(coupon_type=coupon_type, bet_count=bet_count)
    is_cashout = (state or "").casefold() in {
        "sold",
        "cashout",
        "cash-out",
        "cash_out",
    }

    stake_rub = _amount(summary.get("betSum"), amount_divisor)
    return_rub = _amount(summary.get("winSum"), amount_divisor)
    cash_stake_rub = 0 if is_freebet and stake_rub is not None else stake_rub
    freebet_nominal_rub = stake_rub if is_freebet else None
    profit_rub, accounting_method = _profit(
        state=state,
        return_rub=return_rub,
        cash_stake_rub=cash_stake_rub,
        is_freebet=is_freebet,
    )

    calculation_value = _first_value(
        (summary, detail_root),
        ("calculationTime", "calcTime", "settlementTime"),
    )
    entry_odds = _optional_number(
        _first_value(
            (summary,),
            ("couponK", "couponOriginalK"),
        )
    )
    if entry_odds is None and len(legs) == 1:
        entry_odds = cast(int | float | None, legs[0]["entry_odds"])

    return {
        "coupon_id": coupon_id,
        "registration_time": _timestamp(summary.get("registrationTime")),
        "calculation_time": _timestamp(calculation_value),
        "coupon_type": coupon_type,
        "bet_mode": bet_mode,
        "bet_count": bet_count,
        "state": state,
        "stake_rub": stake_rub,
        "return_rub": return_rub,
        "profit_rub": profit_rub,
        "entry_odds": entry_odds,
        "event_name": _join_leg_values(legs, "event_name"),
        "selection": _join_leg_values(legs, "selection"),
        "entry_score": _join_leg_values(legs, "entry_score"),
        "result_score": _join_leg_values(legs, "result_score"),
        "is_live": _aggregate_live(legs),
        "event_start_time": _join_leg_values(legs, "event_start_time"),
        "sport_name": _join_leg_values(legs, "sport_name"),
        "is_express": is_express,
        "is_freebet": is_freebet,
        "is_cashout": is_cashout,
        "cash_stake_rub": cash_stake_rub,
        "freebet_nominal_rub": freebet_nominal_rub,
        "accounting_method": accounting_method,
        "source_bet_sum": summary.get("betSum"),
        "source_win_sum": summary.get("winSum"),
        "source_coupon_k": summary.get("couponK"),
        "source_coupon_original_k": summary.get("couponOriginalK"),
        "amount_divisor": _decimal_to_number(amount_divisor),
        "leg_count": len(legs),
        "detail_status": detail_status,
        "legs": legs,
    }


def csv_row(record: Mapping[str, object]) -> dict[str, object]:
    row: dict[str, object] = {}
    for column in NORMALIZED_COLUMNS:
        if column == "legs_json":
            row[column] = json.dumps(
                record.get("legs", []),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            continue
        value = record.get(column)
        if value is None:
            row[column] = ""
        elif isinstance(value, bool):
            row[column] = str(value).lower()
        else:
            row[column] = value
    return row


def _extract_summary_rows(payload: object) -> list[Mapping[str, object]]:
    if isinstance(payload, list):
        return _mapping_rows(payload)
    if not isinstance(payload, Mapping):
        raise FonbetDataError("Summary JSON must contain an object or array.")
    if "couponId" in payload:
        return [cast(Mapping[str, object], payload)]
    for key in SUMMARY_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return _mapping_rows(value)
    for key in SUMMARY_WRAPPER_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping | list):
            try:
                return _extract_summary_rows(value)
            except FonbetDataError:
                continue
    raise FonbetDataError("Could not find coupon records in summary JSON.")


def _mapping_rows(values: Sequence[object]) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise FonbetDataError("Summary coupon records must be JSON objects.")
        rows.append(cast(Mapping[str, object], value))
    return rows


def _detail_root(detail: Mapping[str, object]) -> Mapping[str, object]:
    current = detail
    for _ in range(4):
        nested = next(
            (
                current.get(key)
                for key in DETAIL_WRAPPER_KEYS
                if isinstance(current.get(key), Mapping)
            ),
            None,
        )
        if not isinstance(nested, Mapping):
            break
        current = cast(Mapping[str, object], nested)
    return current


def _normalized_legs(
    detail: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    if detail is None:
        return []
    mappings = _leg_mappings(detail)
    return [
        {
            "event_name": _optional_text(item.get("eventName")),
            "selection": _optional_text(item.get("stakeName")),
            "entry_odds": _optional_number(item.get("factorValue")),
            "entry_score": _optional_text(item.get("score")),
            "result_score": _optional_text(item.get("resultScore")),
            "is_live": _optional_bool(item.get("live")),
            "event_start_time": _timestamp(item.get("eventStartTime")),
            "sport_name": _optional_text(item.get("sportName")),
            "result": _optional_text(item.get("result")),
        }
        for item in mappings
    ]


def _leg_mappings(detail: Mapping[str, object]) -> list[Mapping[str, object]]:
    for mapping in _walk_mappings(detail):
        for key in LEG_LIST_KEYS:
            value = mapping.get(key)
            if not isinstance(value, list):
                continue
            rows = [
                cast(Mapping[str, object], item)
                for item in value
                if isinstance(item, Mapping) and LEG_FIELDS.intersection(item.keys())
            ]
            if rows:
                return rows
    direct = [
        mapping
        for mapping in _walk_mappings(detail)
        if ("eventName" in mapping or "stakeName" in mapping)
    ]
    return direct[:1] if direct and direct[0] is detail else direct


def _walk_mappings(value: object) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, object], value)
        found.append(mapping)
        for nested in mapping.values():
            found.extend(_walk_mappings(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_walk_mappings(nested))
    return found


def _first_value(
    mappings: Sequence[Mapping[str, object] | None],
    keys: Sequence[str],
) -> object | None:
    for mapping in mappings:
        if mapping is None:
            continue
        for key in keys:
            value = mapping.get(key)
            if value is not None:
                return value
    return None


def _amount(value: object, divisor: Decimal) -> int | float | None:
    decimal = _decimal(value)
    if decimal is None:
        return None
    return _decimal_to_number(decimal / divisor)


def _profit(
    *,
    state: str | None,
    return_rub: int | float | None,
    cash_stake_rub: int | float | None,
    is_freebet: bool,
) -> tuple[int | float | None, str]:
    if (state or "").casefold() not in SETTLED_STATES:
        return None, "unsettled_or_unsupported_state"
    if return_rub is None or cash_stake_rub is None:
        return None, "missing_amount"
    profit = Decimal(str(return_rub)) - Decimal(str(cash_stake_rub))
    method = "freebet_zero_cash_stake" if is_freebet else "cash_return_minus_stake"
    return _decimal_to_number(profit), method


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | float | str):
        raise FonbetDataError("Amount fields must be numeric.")
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise FonbetDataError("Amount fields must be numeric.") from exc
    if not parsed.is_finite():
        raise FonbetDataError("Amount fields must be finite.")
    return parsed


def _decimal_to_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    if value == integral:
        return int(integral)
    return float(value)


def _optional_number(value: object) -> int | float | None:
    decimal = _decimal(value)
    return None if decimal is None else _decimal_to_number(decimal)


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError as exc:
        raise FonbetDataError("betCount must be an integer.") from exc


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _timestamp(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        numeric = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric = float(text)
        except ValueError:
            return _iso_timestamp(text)
    else:
        return str(value)
    if not math.isfinite(numeric):
        return str(value)
    seconds = numeric / 1000 if abs(numeric) >= 100_000_000_000 else numeric
    try:
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return str(value)
    return parsed.isoformat().replace("+00:00", "Z")


def _iso_timestamp(value: str) -> str:
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_freebet(
    summary: Mapping[str, object],
    *,
    coupon_type: str | None,
    bet_mode: str | None,
) -> bool:
    for value in (coupon_type, bet_mode):
        if value is not None and "free" in value.casefold():
            return True
    for key in ("freeBet", "isFreeBet"):
        if _optional_bool(summary.get(key)) is True:
            return True
    return any(summary.get(key) is not None for key in ("freeBetId", "freeBetSum"))


def _is_express(*, coupon_type: str | None, bet_count: int | None) -> bool:
    return bool(
        (coupon_type is not None and "express" in coupon_type.casefold())
        or (bet_count is not None and bet_count > 1)
    )


def _join_leg_values(legs: Sequence[Mapping[str, object]], key: str) -> str | None:
    values: list[str] = []
    for leg in legs:
        value = leg.get(key)
        if value is None:
            continue
        text = str(value)
        if text not in values:
            values.append(text)
    return " | ".join(values) if values else None


def _aggregate_live(legs: Sequence[Mapping[str, object]]) -> bool | None:
    values = [leg.get("is_live") for leg in legs if leg.get("is_live") is not None]
    if not values:
        return None
    return any(value is True for value in values)
