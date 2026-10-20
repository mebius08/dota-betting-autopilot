"""Check the running Dota 2 analytics web application with HTTPX."""

import argparse
from collections.abc import Sequence
import re

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 10.0
CHECKS = (("/", "text/html"), ("/api/matches", "application/json"))


def _base_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise argparse.ArgumentTypeError("Некорректный URL сервера.") from exc
    if url.scheme not in {"http", "https"} or not url.host:
        raise argparse.ArgumentTypeError("Укажите HTTP(S) URL с именем сервера.")
    return url


def _validate_response(response: httpx.Response, expected_type: str) -> str:
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}; ожидался HTTP 200")
    content_type = response.headers.get("content-type", "").partition(";")[0]
    if content_type.strip().lower() != expected_type:
        raise ValueError(
            f"ожидался Content-Type {expected_type}, "
            f"получен {content_type or 'не указан'}"
        )
    if expected_type == "text/html":
        if not re.search(r"<html(?:\s|>)", response.text, re.IGNORECASE):
            raise ValueError("ответ не содержит HTML-документ")
        return "HTTP 200, HTML"
    try:
        matches = response.json()
    except ValueError as exc:
        raise ValueError("ответ содержит некорректный JSON") from exc
    if not isinstance(matches, list) or any(
        not isinstance(match, dict) for match in matches
    ):
        raise ValueError("ожидался JSON-массив объектов матчей")
    return f"HTTP 200, JSON, матчей: {len(matches)}"


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        type=_base_url,
        default=DEFAULT_BASE_URL,
        help=f"URL приложения (по умолчанию {DEFAULT_BASE_URL}).",
    )
    args = parser.parse_args(argv)
    failed = False
    with httpx.Client(
        base_url=args.base_url, timeout=TIMEOUT_SECONDS, transport=transport
    ) as client:
        for path, expected_type in CHECKS:
            try:
                response = client.get(path, headers={"Accept": expected_type})
                result = _validate_response(response, expected_type)
            except (httpx.HTTPError, ValueError) as exc:
                detail = (
                    f"таймаут запроса ({TIMEOUT_SECONDS:g} с)"
                    if isinstance(exc, httpx.TimeoutException)
                    else str(exc) or type(exc).__name__
                )
                print(f"FAIL GET {path}: {detail}")
                failed = True
            else:
                print(f"OK GET {path}: {result}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
