"""The CSRF guard for the local API (spec section 14.2).

The API binds to loopback with no login, so the threat is a page on some other
site making your browser send a state-changing request to it. Two rules, checked
by :func:`csrf_violation`, stop that: the request must carry
``X-Netkeeper-Client: 1`` (a custom header a cross-site form cannot set), and when
the browser says where the request came from (``Origin``, ``Sec-Fetch-Site``) it
must be this origin. Reads are never blocked.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

CLIENT_HEADER = "x-netkeeper-client"
CLIENT_HEADER_VALUE = "1"
GUARDED_PREFIX = "/api/"
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

RULE_CLIENT_HEADER = "client-header"
RULE_ORIGIN = "origin"
RULE_SEC_FETCH_SITE = "sec-fetch-site"

_MESSAGES = {
    RULE_CLIENT_HEADER: "state-changing requests to the API need the header X-Netkeeper-Client: 1",
    RULE_ORIGIN: "the Origin header does not match this server's origin",
    RULE_SEC_FETCH_SITE: "Sec-Fetch-Site says the request is not same-origin",
}
_SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def csrf_violation(
    method: str, path: str, headers: Mapping[str, str], *, scheme: str
) -> str | None:
    """Return the name of the rule the request breaks, or None when it may proceed.

    ``headers`` has lower-cased names. ``scheme`` is the request's own scheme; its own
    host and port are read from the ``Host`` header.
    """
    if method.upper() not in STATE_CHANGING_METHODS or not path.startswith(GUARDED_PREFIX):
        return None
    if headers.get(CLIENT_HEADER) != CLIENT_HEADER_VALUE:
        return RULE_CLIENT_HEADER
    site = headers.get("sec-fetch-site")
    origin = headers.get("origin")
    if origin is not None:
        if site in _SAME_ORIGIN_SITES or _same_origin(origin, scheme, headers.get("host", "")):
            return None
        return RULE_ORIGIN
    if site is not None and site not in _SAME_ORIGIN_SITES:
        return RULE_SEC_FETCH_SITE
    return None


def rejection_message(rule: str) -> str:
    """The human-readable reason for a rule name from :func:`csrf_violation`."""
    return _MESSAGES[rule]


def _same_origin(origin: str, scheme: str, host: str) -> bool:
    return _normalize(origin) == _normalize(f"{scheme}://{host}")


def _normalize(url: str) -> tuple[str, str, int | None] | None:
    """``(scheme, host, port)`` with the default port filled in, or None if unparseable."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not hostname:
        return None  # covers "Origin: null" and garbage
    scheme = parts.scheme.lower()
    return scheme, hostname.lower(), port if port is not None else _DEFAULT_PORTS.get(scheme)


class CSRFMiddleware:
    """Pure ASGI middleware applying :func:`csrf_violation` to every HTTP request.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so streaming responses (the SSE
    stream) pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope["headers"]
        }
        rule = csrf_violation(
            scope["method"], scope["path"], headers, scheme=scope.get("scheme", "http")
        )
        if rule is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": rejection_message(rule), "rule": rule}, status_code=403)
        await response(scope, receive, send)
