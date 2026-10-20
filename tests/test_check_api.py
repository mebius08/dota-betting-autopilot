import httpx
import pytest

from tools.check_api import main


def _success_response(path: str) -> httpx.Response:
    if path == "/":
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text="<!doctype html><html lang='ru'><body>Матчи</body></html>",
        )
    return httpx.Response(200, json=[])


@pytest.mark.parametrize("matches", [[], [{"id": "stored-match"}]])
@pytest.mark.parametrize("base_url", [None, "http://example.test:8123/"])
def test_success_and_empty_match_list(matches, base_url, capsys):
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/matches":
            return httpx.Response(200, json=matches)
        return _success_response(request.url.path)

    args = ["--base-url", base_url] if base_url else []
    result = main(args, transport=httpx.MockTransport(handle))

    assert result == 0
    expected_origin = (base_url or "http://127.0.0.1:8000").rstrip("/")
    assert [str(request.url) for request in requests] == [
        f"{expected_origin}/",
        f"{expected_origin}/api/matches",
    ]
    assert [request.headers["accept"] for request in requests] == [
        "text/html",
        "application/json",
    ]
    assert all(request.extensions["timeout"]["read"] == 10.0 for request in requests)
    output = capsys.readouterr().out
    assert "OK GET /: HTTP 200, HTML" in output
    assert f"OK GET /api/matches: HTTP 200, JSON, матчей: {len(matches)}" in output
    assert "FAIL" not in output


@pytest.mark.parametrize(
    ("path", "response", "message"),
    [
        ("/", httpx.Response(500), "HTTP 500"),
        ("/api/matches", httpx.Response(500), "HTTP 500"),
        ("/api/matches", httpx.Response(302), "HTTP 302"),
        ("/", httpx.Response(200, json={}), "Content-Type text/html"),
        (
            "/",
            httpx.Response(
                200, headers={"Content-Type": "text/html"}, text="plain text"
            ),
            "HTML-документ",
        ),
        (
            "/api/matches",
            httpx.Response(200, text="[]"),
            "Content-Type application/json",
        ),
        (
            "/api/matches",
            httpx.Response(
                200, headers={"Content-Type": "application/json"}, text="invalid"
            ),
            "некорректный JSON",
        ),
        ("/api/matches", httpx.Response(200, json={}), "JSON-массив объектов"),
        ("/api/matches", httpx.Response(200, json=[None]), "JSON-массив объектов"),
    ],
)
def test_invalid_response_fails_but_still_checks_both_routes(
    path, response, message, capsys
):
    visited = []

    def handle(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.path)
        return (
            response
            if request.url.path == path
            else _success_response(request.url.path)
        )

    assert main([], transport=httpx.MockTransport(handle)) == 1
    assert visited == ["/", "/api/matches"]
    output = capsys.readouterr().out
    assert f"FAIL GET {path}:" in output
    assert message in output
    assert "OK GET " in output


@pytest.mark.parametrize(
    ("path", "error", "detail", "expected"),
    [
        ("/", httpx.ConnectError, "server unavailable", "server unavailable"),
        ("/api/matches", httpx.ReadTimeout, "", "таймаут запроса (10 с)"),
    ],
)
def test_connection_errors_fail_without_traceback(
    path, error, detail, expected, capsys
):
    visited = []

    def handle(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.path)
        if request.url.path == path:
            raise error(detail, request=request)
        return _success_response(request.url.path)

    assert main([], transport=httpx.MockTransport(handle)) == 1
    assert visited == ["/", "/api/matches"]
    assert f"FAIL GET {path}: {expected}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "base_url", ["localhost:8000", "ftp://example.test", "http://"]
)
def test_invalid_base_url_exits_before_sending_requests(base_url, capsys):
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid base URL must not result in a request")

    with pytest.raises(SystemExit) as exc:
        main(["--base-url", base_url], transport=httpx.MockTransport(handle))
    assert exc.value.code == 2
    assert "--base-url" in capsys.readouterr().err
