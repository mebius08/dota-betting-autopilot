from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import json
from pathlib import Path
from typing import cast

from app.fonbet_history import (
    build_single_event_sequences,
    load_coupon_summaries,
    normalize_coupon,
    normalize_coupon_legs,
)


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


def test_normalize_single_coupon_leg_uses_exact_source_fields() -> None:
    detail: dict[str, object] = {
        "body": {
            "bets": [
                {
                    "eventId": 101,
                    "factorId": 202,
                    "segmentId": 303,
                    "sportId": 404,
                    "eventName": "Fixture single event",
                    "stakeName": "Fixture single selection",
                    "factorValue": 1.75,
                    "score": "0:0",
                    "resultScore": "2:0",
                    "eventStartTime": 1710001800000,
                    "live": False,
                }
            ]
        }
    }

    assert normalize_coupon_legs("fixture-single", detail) == [
        {
            "coupon_id": "fixture-single",
            "leg_index": 1,
            "event_id": 101,
            "factor_id": 202,
            "segment_id": 303,
            "sport_id": 404,
            "event_name": "Fixture single event",
            "selection": "Fixture single selection",
            "entry_odds": 1.75,
            "entry_score": "0:0",
            "result_score": "2:0",
            "event_start_time": "2024-03-09T16:30:00Z",
            "is_live": False,
        }
    ]


def test_normalize_express_coupon_exports_every_real_leg() -> None:
    detail: dict[str, object] = {
        "body": {
            "bets": [
                {"eventName": "Fixture express A", "factorValue": 1.5},
                {"eventName": "Fixture express B", "factorValue": 1.8},
            ]
        }
    }

    legs = normalize_coupon_legs("fixture-express", detail)

    assert [leg["leg_index"] for leg in legs] == [1, 2]
    assert [leg["event_name"] for leg in legs] == [
        "Fixture express A",
        "Fixture express B",
    ]


def test_normalize_coupon_leg_keeps_missing_identifiers_null() -> None:
    detail: dict[str, object] = {
        "eventName": "Fixture missing identifiers",
        "stakeName": "Fixture selection",
    }

    leg = normalize_coupon_legs("fixture-missing-identifiers", detail)[0]

    assert leg["event_id"] is None
    assert leg["factor_id"] is None
    assert leg["segment_id"] is None
    assert leg["sport_id"] is None


def test_single_event_sequences_order_chronologically_with_coupon_tiebreaker(
) -> None:
    coupons = [
        _sequence_coupon("fixture-c", "2026-01-03T10:00:00Z"),
        _sequence_coupon("fixture-b", "2026-01-02T10:00:00Z"),
        _sequence_coupon("fixture-a", "2026-01-02T10:00:00Z"),
    ]
    legs = [
        _sequence_leg("fixture-c", 101, "Fixture A"),
        _sequence_leg("fixture-b", 101, "Fixture A"),
        _sequence_leg("fixture-a", 101, "Fixture A"),
    ]

    records = build_single_event_sequences(coupons, legs)

    assert [record["coupon_id"] for record in records] == [
        "fixture-a",
        "fixture-b",
        "fixture-c",
    ]


def test_single_event_sequences_track_first_repeats_and_side_switches() -> None:
    coupons = [
        _sequence_coupon("fixture-first", "2026-01-01T10:00:00Z"),
        _sequence_coupon("fixture-repeat", "2026-01-02T10:00:00Z"),
        _sequence_coupon("fixture-switch", "2026-01-03T10:00:00Z"),
    ]
    legs = [
        _sequence_leg("fixture-first", 101, "Fixture A"),
        _sequence_leg("fixture-repeat", 101, "Fixture A"),
        _sequence_leg("fixture-switch", 101, "Fixture B"),
    ]

    first, repeated, switched = build_single_event_sequences(coupons, legs)

    assert first["sequence_index"] == 1
    assert first["prior_entry_count"] == 0
    assert first["previous_coupon_id"] is None
    assert first["previous_selection"] is None
    assert first["side_switch"] is False
    assert repeated["sequence_index"] == 2
    assert repeated["prior_entry_count"] == 1
    assert repeated["previous_coupon_id"] == "fixture-first"
    assert repeated["previous_selection"] == "Fixture A"
    assert repeated["side_switch"] is False
    assert switched["sequence_index"] == 3
    assert switched["prior_entry_count"] == 2
    assert switched["previous_coupon_id"] == "fixture-repeat"
    assert switched["previous_selection"] == "Fixture A"
    assert switched["side_switch"] is True


def test_single_event_sequences_keep_exact_event_ids_separate() -> None:
    coupons = [
        _sequence_coupon("fixture-event-a", "2026-01-01T10:00:00Z"),
        _sequence_coupon("fixture-event-b", "2026-01-02T10:00:00Z"),
        {
            **_sequence_coupon("fixture-express", "2026-01-03T10:00:00Z"),
            "is_express": True,
            "leg_count": 2,
        },
    ]
    legs = [
        _sequence_leg("fixture-event-a", 101, "Fixture A"),
        _sequence_leg("fixture-event-b", 202, "Fixture B"),
        _sequence_leg("fixture-express", 101, "Fixture A"),
        _sequence_leg("fixture-express", 202, "Fixture B"),
    ]

    records = build_single_event_sequences(coupons, legs)

    assert [record["event_id"] for record in records] == [101, 202]
    assert [record["sequence_index"] for record in records] == [1, 1]
    assert all(record["previous_coupon_id"] is None for record in records)


def _sequence_coupon(coupon_id: str, registration_time: str) -> dict[str, object]:
    return {
        "coupon_id": coupon_id,
        "registration_time": registration_time,
        "is_express": False,
        "leg_count": 1,
        "entry_odds": 1.8,
        "cash_stake_rub": 100,
        "return_rub": 180,
        "profit_rub": 80,
        "is_cashout": False,
        "state": "Win",
    }


def _sequence_leg(
    coupon_id: str,
    event_id: int,
    selection: str,
) -> dict[str, object]:
    return {
        "coupon_id": coupon_id,
        "event_id": event_id,
        "selection": selection,
        "entry_score": "0:0",
        "result_score": "2:0",
        "is_live": False,
    }


def _load_details() -> Mapping[str, Mapping[str, object]]:
    payload = json.loads((FIXTURE_DIR / "details.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(Mapping[str, Mapping[str, object]], payload)
