from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
import socket
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STRATZ_GRAPHQL_ENDPOINT = "https://api.stratz.com/graphql"
STRATZ_TOKEN_ENV = "STRATZ_TOKEN"
STRATZ_USER_AGENT = "dota-betting-autopilot/1.0"
STRATZ_MISSING_TOKEN_MESSAGE = (
    "STRATZ token is not configured.\n"
    f"Set {STRATZ_TOKEN_ENV} before running the STRATZ feasibility probe."
)

STRATZ_SCHEMA_OVERVIEW_QUERY = """
query StratzSchemaOverview {
  __schema {
    queryType {
      name
    }
  }
}
"""

STRATZ_TYPE_INTROSPECTION_QUERY = """
query StratzTypeIntrospection($name: String!) {
  __type(name: $name) {
    name
    kind
    fields {
      name
      args {
        name
        type {
          ...StratzTypeRef
        }
      }
      type {
        ...StratzTypeRef
      }
    }
  }
}

fragment StratzTypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
          }
        }
      }
    }
  }
}
"""


class _Response(Protocol):
    def read(self) -> bytes:
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


class _UrlOpen(Protocol):
    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float = 10.0,
    ) -> _Response:
        ...


DEFAULT_URL_OPEN: _UrlOpen = cast(_UrlOpen, urlopen)


class StratzError(RuntimeError):
    pass


class StratzConfigurationError(StratzError):
    pass


class StratzRequestError(StratzError):
    pass


class StratzAuthenticationError(StratzRequestError):
    pass


class StratzRateLimitError(StratzRequestError):
    pass


class StratzResponseError(StratzError):
    pass


class StratzGraphQLError(StratzResponseError):
    pass


@dataclass(frozen=True)
class StratzGraphQLResponse:
    data: Mapping[str, object]


class StratzGraphQLClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        timeout: float = 10.0,
        endpoint: str = STRATZ_GRAPHQL_ENDPOINT,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        self.token = token if token is not None else os.environ.get(STRATZ_TOKEN_ENV)
        self.timeout = timeout
        self.endpoint = endpoint
        self.urlopen_func = urlopen_func

    def execute(
        self,
        query: str,
        variables: Mapping[str, object] | None = None,
    ) -> StratzGraphQLResponse:
        token = self._required_token()
        request = _graphql_request(
            endpoint=self.endpoint,
            token=token,
            query=query,
            variables=variables,
        )
        try:
            with self.urlopen_func(request, timeout=self.timeout) as response:
                raw_body = response.read()
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise StratzRequestError("STRATZ request timed out.") from exc
        except (URLError, OSError) as exc:
            raise StratzRequestError("STRATZ network request failed.") from exc

        payload = _decode_graphql_response(raw_body, token=token)
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            raise StratzGraphQLError(_graphql_errors_message(errors, token=token))
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise StratzResponseError("STRATZ returned no GraphQL data object.")
        return StratzGraphQLResponse(data=data)

    def _required_token(self) -> str:
        token = (self.token or "").strip()
        if not token:
            raise StratzConfigurationError(STRATZ_MISSING_TOKEN_MESSAGE)
        return token


def _graphql_request(
    *,
    endpoint: str,
    token: str,
    query: str,
    variables: Mapping[str, object] | None,
) -> Request:
    if not query.strip():
        raise ValueError("GraphQL query must not be empty")
    body = json.dumps(
        {
            "query": query,
            "variables": dict(variables or {}),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return Request(
        endpoint,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}",
            "Content-Type": "application/json",
            "User-Agent": STRATZ_USER_AGENT,
        },
        method="POST",
    )


def _http_error(exc: HTTPError) -> StratzRequestError:
    if exc.code in (401, 403):
        return StratzAuthenticationError(
            f"STRATZ HTTP {exc.code}: authentication failed."
        )
    if exc.code == 429:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        suffix = f" Retry-After: {retry_after}." if retry_after else ""
        return StratzRateLimitError(f"STRATZ HTTP 429: rate limited.{suffix}")
    return StratzRequestError(f"STRATZ HTTP {exc.code}.")


def _decode_graphql_response(
    raw_body: bytes,
    *,
    token: str,
) -> Mapping[str, object]:
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StratzResponseError("STRATZ returned invalid JSON.") from exc
    if not isinstance(decoded, Mapping):
        raise StratzResponseError("STRATZ returned unexpected JSON shape.")
    return decoded


def _graphql_errors_message(errors: list[object], *, token: str) -> str:
    messages: list[str] = []
    for item in errors[:3]:
        if isinstance(item, Mapping):
            message = item.get("message")
            if message is not None:
                messages.append(_redact_token(str(message), token=token))
                continue
        messages.append("unknown GraphQL error")
    suffix = "; ".join(messages)
    return f"STRATZ GraphQL errors: {suffix}"


def _redact_token(value: str, *, token: str) -> str:
    token = token.strip()
    if not token:
        return value
    return value.replace(token, "[redacted]")
