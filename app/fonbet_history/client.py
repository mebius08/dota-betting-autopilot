from __future__ import annotations

from collections.abc import Mapping
import json
import math
import socket
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


FONBET_COUPON_INFO_ENDPOINT = (
    "https://clientsapi-lb54-w.bk6bba-resources.com/coupon/info"
)
FONBET_FSID_ENV = "FONBET_FSID"
FONBET_CLIENT_ID_ENV = "FONBET_CLIENT_ID"
FONBET_USER_AGENT = "dota-betting-autopilot-personal-history/1.0"
MAX_RESPONSE_BYTES = 5_000_000


class _Response(Protocol):
    def read(self, amount: int = -1) -> bytes:
        ...

    def __enter__(self) -> _Response:
        ...

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        ...


class UrlOpen(Protocol):
    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float = 10.0,
    ) -> _Response:
        ...


DEFAULT_URL_OPEN: UrlOpen = cast(UrlOpen, urlopen)


class FonbetHistoryError(RuntimeError):
    """Base error whose message is safe to show without credential values."""


class FonbetConfigurationError(FonbetHistoryError):
    pass


class FonbetRequestError(FonbetHistoryError):
    pass


class FonbetResponseError(FonbetHistoryError):
    pass


class FonbetCouponClient:
    """Small sequential HTTP client for the authenticated user's coupons."""

    def __init__(
        self,
        *,
        fsid: str,
        client_id: str,
        bet_type_name: str,
        sys_id: int,
        lang: str = "ru",
        timeout: float = 10.0,
        urlopen_func: UrlOpen = DEFAULT_URL_OPEN,
    ) -> None:
        if not fsid.strip():
            raise FonbetConfigurationError("FONBET_FSID must not be empty.")
        if not client_id.strip():
            raise FonbetConfigurationError("FONBET_CLIENT_ID must not be empty.")
        if not bet_type_name.strip():
            raise FonbetConfigurationError("FONBET_BET_TYPE_NAME must not be empty.")
        if not lang.strip():
            raise FonbetConfigurationError("FONBET_LANG must not be empty.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise FonbetConfigurationError(
                "HTTP timeout must be finite and greater than zero."
            )

        self._fsid = fsid.strip()
        self._client_id = client_id.strip()
        self._bet_type_name = bet_type_name.strip()
        self._sys_id = sys_id
        self._lang = lang.strip()
        self._timeout = timeout
        self._urlopen = urlopen_func

    def fetch_coupon_detail(self, coupon_id: str) -> Mapping[str, object]:
        request = self._build_request(coupon_id)
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                raw_body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise FonbetRequestError(
                    f"FONBET HTTP {exc.code}: authentication failed."
                ) from exc
            if exc.code == 429:
                raise FonbetRequestError("FONBET HTTP 429: rate limited.") from exc
            raise FonbetRequestError(f"FONBET HTTP {exc.code}.") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise FonbetRequestError("FONBET coupon request timed out.") from exc
        except (URLError, OSError) as exc:
            raise FonbetRequestError("FONBET coupon network request failed.") from exc

        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise FonbetResponseError("FONBET coupon response exceeded 5 MB.")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FonbetResponseError("FONBET returned invalid JSON.") from exc
        if not isinstance(payload, Mapping):
            raise FonbetResponseError("FONBET returned an unexpected JSON shape.")
        return cast(Mapping[str, object], payload)

    def sanitize_response(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Remove credential values if an upstream response unexpectedly echoes them."""

        sanitized = _redact_value(payload, (self._fsid, self._client_id))
        if not isinstance(sanitized, dict):
            raise FonbetResponseError("FONBET returned an unexpected JSON shape.")
        return sanitized

    def _build_request(self, coupon_id: str) -> Request:
        if not coupon_id.strip():
            raise FonbetRequestError("Coupon ID must not be empty.")
        body = json.dumps(
            {
                "regId": _json_identifier(coupon_id),
                "lang": self._lang,
                "betTypeName": self._bet_type_name,
                "fsid": self._fsid,
                "clientId": _json_identifier(self._client_id),
                "sysId": self._sys_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return Request(
            FONBET_COUPON_INFO_ENDPOINT,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": FONBET_USER_AGENT,
            },
            method="POST",
        )


def _json_identifier(value: str) -> int | str:
    stripped = value.strip()
    if stripped.isdecimal():
        return int(stripped)
    return stripped


def _redact_value(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, int | float) and not isinstance(value, bool):
        if any(secret and str(value) == secret for secret in secrets):
            return "[redacted]"
        return value
    if isinstance(value, Mapping):
        return {
            _redact_text(str(key), secrets): _redact_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    return value


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
