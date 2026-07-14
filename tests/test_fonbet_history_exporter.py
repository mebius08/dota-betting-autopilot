from __future__ import annotations

import csv
from decimal import Decimal
import json
from pathlib import Path

from app.fonbet_history import export_personal_history, raw_response_path
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
