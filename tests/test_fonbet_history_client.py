from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.fonbet_history.client import (
    FonbetCouponClient,
    FonbetRequestError,
)


def test_client_posts_supplied_contract_without_leaking_credentials() -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        seen["url"] = request.full_url
        seen["method"] = request.method
        seen["timeout"] = timeout
        seen["payload"] = json.loads((request.data or b"").decode("utf-8"))
        return _FakeResponse(
            b'{"eventName":"Fixture event","echo":"fixture-fsid-credential",'
            b'"nested":{"client":424242}}'
        )

    client = FonbetCouponClient(
        fsid="fixture-fsid-credential",
        client_id="424242",
        bet_type_name="fixture-type",
        sys_id=7,
        timeout=2.5,
        urlopen_func=fake_urlopen,
    )

    response = client.fetch_coupon_detail("fixture-coupon")
    sanitized = client.sanitize_response(response)

    assert seen["method"] == "POST"
    assert seen["timeout"] == 2.5
    assert seen["payload"] == {
        "regId": "fixture-coupon",
        "lang": "ru",
        "betTypeName": "fixture-type",
        "fsid": "fixture-fsid-credential",
        "clientId": 424242,
        "sysId": 7,
    }
    serialized = json.dumps(sanitized)
    assert "fixture-fsid-credential" not in serialized
    assert "424242" not in serialized
    assert serialized.count("[redacted]") == 2


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_client_http_errors_never_include_credentials(status_code: int) -> None:
    def fake_urlopen(request: Request, timeout: float) -> _FakeResponse:
        raise HTTPError(
            request.full_url,
            status_code,
            "fixture-fsid-credential",
            hdrs=None,
            fp=None,
        )

    client = FonbetCouponClient(
        fsid="fixture-fsid-credential",
        client_id="424242",
        bet_type_name="fixture-type",
        sys_id=7,
        urlopen_func=fake_urlopen,
    )

    with pytest.raises(FonbetRequestError) as exc_info:
        client.fetch_coupon_detail("fixture-coupon")

    message = str(exc_info.value)
    assert str(status_code) in message
    assert "fixture-fsid-credential" not in message
    assert "424242" not in message


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False
