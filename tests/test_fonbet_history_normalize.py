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
    assert express["coupon_type"] == "Express"
    assert express["bet_mode"] == "Coupon"
    assert express["bet_count"] == 2
    assert express["leg_count"] == 2
    assert express["entry_odds"] == 2.673
    assert express["entry_odds_source"] == "coupon_k"
    assert express["calculation_time"] == "2024-03-09T20:30:00Z"
    assert express["profit_rub"] == 83.65
    assert express["event_name"] == (
        "Fixture Team G - Fixture Team H | Fixture Team I - Fixture Team J"
    )
    assert len(cast(list[object], express["legs"])) == 2

    freebet = records["fixture-freebet-005"]
    assert freebet["is_freebet"] is True
    assert freebet["coupon_type"] == "Odinar"
    assert freebet["bet_mode"] == "Freebet"
    assert freebet["bet_count"] == 1
    assert freebet["calculation_time"] == "2024-03-09T21:30:00Z"
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


def test_express_falls_back_to_legs_and_derives_complete_leg_product() -> None:
    summary: dict[str, object] = {
        "couponId": "fixture-derived-express",
        "registrationTime": 1710010800000,
        "betState": "Win",
        "betSum": 100,
        "winSum": 163,
    }
    detail: dict[str, object] = {
        "result": "couponInfo",
        "header": {
            "betTypeName": "sport",
            "calcTime": 1710016200,
            "state": "win",
        },
        "body": {
            "kind": "combo",
            "bets": [
                {"eventName": "Fixture event A", "factorValue": 1.25},
                {"eventName": "Fixture event B", "factorValue": 1.3},
            ],
        },
    }

    record = normalize_coupon(
        summary,
        detail,
        amount_divisor=Decimal("1"),
        detail_status="fixture",
    )

    assert record["coupon_type"] == "Express"
    assert record["bet_mode"] == "Coupon"
    assert record["bet_count"] == 2
    assert record["is_express"] is True
    assert record["entry_odds"] == 1.63
    assert record["entry_odds_source"] == "leg_product_rounded_2dp"


def test_express_does_not_derive_odds_from_incomplete_leg_factors() -> None:
    summary: dict[str, object] = {
        "couponId": "fixture-incomplete-express",
        "betState": "Lose",
        "betSum": 100,
        "winSum": 0,
    }
    detail: dict[str, object] = {
        "body": {
            "kind": "combo",
            "bets": [
                {"eventName": "Fixture event A", "factorValue": 1.4},
                {"eventName": "Fixture event B"},
            ],
        }
    }

    record = normalize_coupon(
        summary,
        detail,
        amount_divisor=Decimal("1"),
        detail_status="fixture",
    )

    assert record["bet_count"] == 2
    assert record["is_express"] is True
    assert record["entry_odds"] is None
    assert record["entry_odds_source"] is None


def _load_details() -> Mapping[str, Mapping[str, object]]:
    payload = json.loads((FIXTURE_DIR / "details.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(Mapping[str, Mapping[str, object]], payload)
