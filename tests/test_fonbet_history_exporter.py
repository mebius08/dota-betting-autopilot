from __future__ import annotations

from collections import Counter
import csv
from decimal import Decimal
import json
from pathlib import Path

from app.fonbet_history import (
    ENTRY_DECISION_COLUMNS,
    ENTRY_OUTCOME_COLUMNS,
    export_personal_history,
    raw_response_path,
)
from app.fonbet_history.client import FonbetRequestError


def test_export_resumes_sequentially_and_keeps_partial_successes(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "coupons": [
                    _summary("fixture-resume", "Win", 10000, 18000),
                    _summary("fixture-fetch", "Lose", 5000, 0),
                    _summary("fixture-failure", "Sold", 6000, 4000),
                ]
            }
        ),
        encoding="utf-8",
    )
    data_dir = tmp_path / "local-data" / "fonbet-history"
    resumed_path = raw_response_path(data_dir, "fixture-resume")
    resumed_path.parent.mkdir(parents=True)
    resumed_path.write_text(
        json.dumps(_detail("Resumed fixture")),
        encoding="utf-8",
    )
    client = _FakeClient()
    sleeps: list[float] = []

    result = export_personal_history(
        summary_path=summary_path,
        local_data_dir=data_dir,
        amount_divisor=Decimal("100"),
        client=client,
        max_fetches=10,
        delay_seconds=0.25,
        sleep_func=sleeps.append,
    )

    assert client.calls == ["fixture-fetch", "fixture-failure"]
    assert sleeps == [0.25]
    assert result.summary_count == 3
    assert result.resumed_count == 1
    assert result.fetched_count == 1
    assert result.failure_count == 1
    assert raw_response_path(data_dir, "fixture-fetch").exists()
    assert not raw_response_path(data_dir, "fixture-failure").exists()

    payload = json.loads(result.normalized_json_path.read_text(encoding="utf-8"))
    records = {row["coupon_id"]: row for row in payload["records"]}
    assert records["fixture-resume"]["detail_status"] == "resumed"
    assert records["fixture-fetch"]["detail_status"] == "fetched"
    assert records["fixture-failure"]["detail_status"] == "fetch_failed"
    assert payload["failures"] == [
        {
            "coupon_id": "fixture-failure",
            "stage": "fetch",
            "message": "FONBET HTTP 503.",
        }
    ]
    with result.normalized_csv_path.open(encoding="utf-8", newline="") as file:
        csv_rows = list(csv.DictReader(file))
    assert len(csv_rows) == 3
    assert csv_rows[0]["coupon_id"] == "fixture-resume"


def test_offline_resume_requires_no_credentials(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps({"coupons": [_summary("fixture-resume", "Win", 100, 180)]}),
        encoding="utf-8",
    )
    data_dir = tmp_path / "local-data" / "fonbet-history"
    raw_path = raw_response_path(data_dir, "fixture-resume")
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(json.dumps(_detail("Offline fixture")), encoding="utf-8")

    result = export_personal_history(
        summary_path=summary_path,
        local_data_dir=data_dir,
        amount_divisor=Decimal("1"),
        client=None,
    )

    assert result.fetched_count == 0
    assert result.resumed_count == 1
    assert result.failure_count == 0


def test_leg_export_has_unique_keys_and_matches_coupon_leg_counts(
    tmp_path: Path,
) -> None:
    single = _summary("fixture-single", "Win", 100, 180)
    express = _summary("fixture-express", "Win", 100, 270)
    express["couponType"] = "Express"
    express["betCount"] = 2
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps({"coupons": [single, express]}),
        encoding="utf-8",
    )
    data_dir = tmp_path / "local-data" / "fonbet-history"
    single_raw = raw_response_path(data_dir, "fixture-single")
    single_raw.parent.mkdir(parents=True)
    single_raw.write_text(json.dumps(_detail("Fixture single")), encoding="utf-8")
    raw_response_path(data_dir, "fixture-express").write_text(
        json.dumps(
            {
                "body": {
                    "kind": "combo",
                    "bets": [
                        _detail("Fixture express A"),
                        _detail("Fixture express B"),
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    result = export_personal_history(
        summary_path=summary_path,
        local_data_dir=data_dir,
        amount_divisor=Decimal("1"),
        client=None,
    )

    coupon_payload = json.loads(
        result.normalized_json_path.read_text(encoding="utf-8")
    )
    legs_path = result.normalized_json_path.with_name("legs.json")
    leg_payload = json.loads(legs_path.read_text(encoding="utf-8"))
    leg_rows = leg_payload["records"]
    keys = [(row["coupon_id"], row["leg_index"]) for row in leg_rows]
    expected_counts = {
        row["coupon_id"]: row["leg_count"] for row in coupon_payload["records"]
    }

    assert len(keys) == len(set(keys))
    assert Counter(row["coupon_id"] for row in leg_rows) == expected_counts
    with legs_path.with_suffix(".csv").open(encoding="utf-8", newline="") as file:
        csv_rows = list(csv.DictReader(file))
    assert len(csv_rows) == len(leg_rows) == 3

    sequences_path = result.normalized_json_path.with_name(
        "single_event_sequences.json"
    )
    sequence_payload = json.loads(sequences_path.read_text(encoding="utf-8"))
    assert sequence_payload["records"] == [
        {
            "cash_stake_rub": 100,
            "coupon_id": "fixture-single",
            "entry_odds": 1.8,
            "entry_score": "0:0",
            "event_id": 101,
            "is_cashout": False,
            "is_live": False,
            "previous_coupon_id": None,
            "previous_selection": None,
            "previous_selection_side": None,
            "prior_entry_count": 0,
            "profit_rub": 80,
            "registration_time": "2026-01-01T00:00:00Z",
            "result_score": "2:0",
            "return_rub": 180,
            "selection": "Fixture selection",
            "selection_side": None,
            "sequence_index": 1,
            "side_switch": None,
            "state": "Win",
        }
    ]
    with sequences_path.with_suffix(".csv").open(
        encoding="utf-8", newline=""
    ) as file:
        sequence_csv_rows = list(csv.DictReader(file))
    assert len(sequence_csv_rows) == 1
    assert sequence_csv_rows[0]["coupon_id"] == "fixture-single"
    assert sequence_csv_rows[0]["selection_side"] == ""
    assert sequence_csv_rows[0]["previous_selection_side"] == ""
    assert sequence_csv_rows[0]["side_switch"] == ""

    decisions_path = result.normalized_json_path.with_name(
        "entry_decisions.json"
    )
    outcomes_path = result.normalized_json_path.with_name("entry_outcomes.json")
    decision_rows = json.loads(decisions_path.read_text(encoding="utf-8"))[
        "records"
    ]
    outcome_rows = json.loads(outcomes_path.read_text(encoding="utf-8"))[
        "records"
    ]
    assert len(decision_rows) == len(outcome_rows) == 1
    assert set(decision_rows[0]) == set(ENTRY_DECISION_COLUMNS)
    assert set(outcome_rows[0]) == set(ENTRY_OUTCOME_COLUMNS)
    assert decision_rows[0]["coupon_id"] == outcome_rows[0]["coupon_id"]
    assert decision_rows[0]["seconds_since_previous_entry"] is None
    assert decision_rows[0]["prior_cash_stake_rub"] == 0
    with decisions_path.with_suffix(".csv").open(
        encoding="utf-8", newline=""
    ) as file:
        assert next(csv.reader(file)) == ENTRY_DECISION_COLUMNS
    with outcomes_path.with_suffix(".csv").open(
        encoding="utf-8", newline=""
    ) as file:
        assert next(csv.reader(file)) == ENTRY_OUTCOME_COLUMNS


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_coupon_detail(self, coupon_id: str) -> dict[str, object]:
        self.calls.append(coupon_id)
        if coupon_id == "fixture-failure":
            raise FonbetRequestError("FONBET HTTP 503.")
        return _detail("Fetched fixture")

    def sanitize_response(self, payload: dict[str, object]) -> dict[str, object]:
        return payload


def _summary(
    coupon_id: str,
    state: str,
    stake: int,
    returned: int,
) -> dict[str, object]:
    return {
        "couponId": coupon_id,
        "registrationTime": "2026-01-01T00:00:00Z",
        "betState": state,
        "betSum": stake,
        "winSum": returned,
        "couponType": "Ordinar",
        "couponK": 1.8,
        "couponOriginalK": 1.8,
        "betCount": 1,
        "betMode": "Normal",
    }


def _detail(event_name: str) -> dict[str, object]:
    return {
        "eventId": 101,
        "eventName": event_name,
        "stakeName": "Fixture selection",
        "factorValue": 1.8,
        "score": "0:0",
        "resultScore": "2:0",
        "sportName": "Dota 2",
        "live": False,
        "eventStartTime": "2026-01-01T01:00:00Z",
        "result": "Win",
    }
