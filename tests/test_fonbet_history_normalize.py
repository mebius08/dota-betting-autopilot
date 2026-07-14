from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import json
from pathlib import Path
from typing import cast

from app.fonbet_history import load_coupon_summaries, normalize_coupon


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fonbet_history"


def test_normalize_sanitized_coupon_states_and_accounting() -> None:
    summaries = load_coupon_summaries(FIXTURE_DIR / "summary.json")
    details = _load_details()

    records = {
        str(summary["couponId"]): normalize_coupon(
            summary,
            details[str(summary["couponId"])],
            amount_divisor=Decimal("100"),
            detail_status="fixture",
        )
        for summary in summaries
    }

    win = records["fixture-win-001"]
    assert win["state"] == "Win"
    assert win["stake_rub"] == 100
    assert win["return_rub"] == 185
    assert win["profit_rub"] == 85
    assert win["event_name"] == "Fixture Team A - Fixture Team B"
    assert win["entry_score"] == "0:0"
    assert win["result_score"] == "2:0"

    lose = records["fixture-lose-002"]
    assert lose["state"] == "Lose"
    assert lose["profit_rub"] == -200

    sold = records["fixture-sold-003"]
    assert sold["state"] == "Sold"
    assert sold["is_cashout"] is True
    assert sold["profit_rub"] == -30
    assert sold["is_live"] is True

    express = records["fixture-express-004"]
    assert express["is_express"] is True
    assert express["bet_count"] == 2
    assert express["leg_count"] == 2
    assert express["profit_rub"] == 83.65
    assert express["event_name"] == (
        "Fixture Team G - Fixture Team H | Fixture Team I - Fixture Team J"
    )
    assert len(cast(list[object], express["legs"])) == 2

    freebet = records["fixture-freebet-005"]
    assert freebet["is_freebet"] is True
    assert freebet["stake_rub"] == 100
    assert freebet["cash_stake_rub"] == 0
    assert freebet["freebet_nominal_rub"] == 100
    assert freebet["return_rub"] == 75
    assert freebet["profit_rub"] == 75
    assert freebet["accounting_method"] == "freebet_zero_cash_stake"
    assert freebet["source_bet_sum"] == 10000
    assert freebet["source_win_sum"] == 7500


def test_amount_divisor_is_explicit_and_preserved() -> None:
    summary = load_coupon_summaries(FIXTURE_DIR / "summary.json")[0]
    detail = _load_details()["fixture-win-001"]

    minor_units = normalize_coupon(
        summary,
        detail,
        amount_divisor=Decimal("100"),
        detail_status="fixture",
    )
    major_units = normalize_coupon(
        summary,
        detail,
        amount_divisor=Decimal("1"),
        detail_status="fixture",
    )

    assert minor_units["stake_rub"] == 100
    assert minor_units["amount_divisor"] == 100
    assert major_units["stake_rub"] == 10000
    assert major_units["amount_divisor"] == 1


def _load_details() -> Mapping[str, Mapping[str, object]]:
    payload = json.loads((FIXTURE_DIR / "details.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(Mapping[str, Mapping[str, object]], payload)
