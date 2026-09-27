"""Gmail's installed-app OAuth flow (spec 11.5, ADR 0003; item P3-01).

The protocol is Google's "OAuth 2.0 for iOS & desktop apps": an authorization URL
with PKCE (``S256``) and a random ``state``, a loopback redirect, and a code
exchanged at the token endpoint for a refresh token. Scope
:data:`GMAIL_SCOPE` only (``gmail.modify``: read, compose, send, labels; no
delete). ``prompt=consent`` makes Google hand out a refresh token every time,
re-authorization included.

This module speaks HTTP and nothing else: no session, no Keychain, no model.
:mod:`netkeeper.services.mailboxes` stores what it returns. Standard library
only (``urllib``), so the flow needs no Google SDK; P3-02's client builds its credentials from
the same refresh token and client.

**Errors.** The status is read before the body. A network failure, a timeout,
an answer cut off part way, a 5xx, a ``408`` or a ``429`` is
:class:`OAuthUnavailable`, whatever the body says: nothing is known about the
grant. Otherwise a refresh or exchange Google refuses with ``invalid_grant``
raises :class:`InvalidGrant`: the grant is dead (revoked, or seven days old on
a consent screen still in Testing). Any other refusal is :class:`OAuthRefused`
with Google's error code (a wrong client secret is ``invalid_client``), or
``http_<status>`` when the body has none. Only the codes in
:data:`REAUTH_CODES` mean a person has to authorize again
(:func:`needs_reauthorization`); any other refusal (a proxy's ``407``, an HTML
``403``, ``invalid_request``) says nothing certain about the grant. No message
carries a token, a code, or the client secret.

**Endpoints.** Every call takes :class:`GoogleEndpoints`, defaulting to
:data:`GOOGLE` read at call time. Tests point it at a loopback fake
(``tests/gmail_fakes.py``) and replace :data:`GOOGLE` with endpoints that
refuse, so no test can reach Google.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import BaseHandler, ProxyHandler, Request, build_opener

log = logging.getLogger(__name__)

GMAIL_SCOPE: Final = "https://www.googleapis.com/auth/gmail.modify"

#: Every Google OAuth client id ends in this; anything else was pasted from the wrong field.
CLIENT_ID_SUFFIX: Final = ".apps.googleusercontent.com"

#: The longest client ID or secret accepted. Checked here, not by the request's
#: schema, whose error would echo the value back.
CLIENT_FIELD_MAX_LENGTH: Final = 300

#: What Google's token endpoint answers for a revoked or expired grant.
INVALID_GRANT: Final = "invalid_grant"

#: The token endpoint's error codes (RFC 6749 5.2) that mean the grant or the
#: client is dead, so only a person authorizing again fixes it. Any other refusal
#: is treated as transient: a mailbox is never marked on a guess.
REAUTH_CODES: Final = frozenset({INVALID_GRANT, "invalid_client", "unauthorized_client"})


@dataclass(frozen=True, slots=True)
class GoogleEndpoints:
    """Where the flow's three requests go.

    ``use_system_proxy`` False ignores ``HTTPS_PROXY`` and friends, which a test
    against a loopback fake wants and production never sets.
    """

    auth_uri: str
    token_uri: str
    profile_uri: str
    timeout_s: float = 20.0
    use_system_proxy: bool = True


#: Google's own endpoints. Never replaced; :data:`GOOGLE` is what calls use.
GOOGLE_ENDPOINTS: Final = GoogleEndpoints(
    auth_uri="https://accounts.google.com/o/oauth2/v2/auth",
    token_uri="https://oauth2.googleapis.com/token",  # noqa: S106 - a URL, not a secret
    profile_uri="https://gmail.googleapis.com/gmail/v1/users/me/profile",
)

#: Where calls go when they are given no endpoints. A test replaces it.
GOOGLE = GOOGLE_ENDPOINTS


def google() -> GoogleEndpoints:
    """:data:`GOOGLE` as it is now (read at call time, so a test can replace it)."""
    return GOOGLE


# --- errors ------------------------------------------------------------------------


class OAuthError(Exception):
    """Base for every failure here. ``code`` is short and safe to show and store."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ClientConfigError(OAuthError):
    """The OAuth client a person gave is not one this flow can use."""


class InvalidGrant(OAuthError):
    """Google refused the grant itself: a person has to authorize again."""


class OAuthRefused(OAuthError):
    """Google refused the request for another reason (``code`` is Google's error code)."""


class OAuthUnavailable(OAuthError):
    """Google could not be reached, timed out, or failed on its side. Try later."""


def needs_reauthorization(exc: OAuthError) -> bool:
    """Whether ``exc`` is Google saying the grant or client is dead (:data:`REAUTH_CODES`)."""
    return isinstance(exc, InvalidGrant | OAuthRefused) and exc.code in REAUTH_CODES


class ScopeNotGranted(OAuthError):
    """The person unticked Gmail access on the consent screen."""


class GmailApiRefused(OAuthError):
    """The token works but the Gmail API refused it: usually the API is not enabled."""


# --- the client --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """A Desktop-app OAuth client from the person's own Cloud project.

    Google says a desktop client's secret cannot be kept secret, but it is still
    stored in the Keychain beside the token and kept out of ``repr`` and logs.
    """

    client_id: str
    client_secret: str = field(repr=False)

    def to_json(self) -> str:
        return json.dumps({"client_id": self.client_id, "client_secret": self.client_secret})

    @classmethod
    def from_json(cls, text: str) -> OAuthClient:
        data = json.loads(text)
        return cls(client_id=str(data["client_id"]), client_secret=str(data["client_secret"]))


def validate_client(client_id: str, client_secret: str) -> OAuthClient:
    """An :class:`OAuthClient` from two pasted values, or :class:`ClientConfigError`."""
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id.endswith(CLIENT_ID_SUFFIX) or len(client_id) == len(CLIENT_ID_SUFFIX):
        raise ClientConfigError(
            f"a Google OAuth client ID ends in {CLIENT_ID_SUFFIX}; check you copied the"
            " client ID, not the project ID or the secret",
            code="bad_client_id",
        )
    if len(client_id) > CLIENT_FIELD_MAX_LENGTH:
        raise ClientConfigError(
            f"the client ID is over {CLIENT_FIELD_MAX_LENGTH} characters", code="bad_client_id"
        )
    if not client_secret or any(char.isspace() for char in client_secret):
        raise ClientConfigError("the client secret is empty or malformed", code="bad_secret")
    if len(client_secret) > CLIENT_FIELD_MAX_LENGTH:
        raise ClientConfigError(
            f"the client secret is over {CLIENT_FIELD_MAX_LENGTH} characters", code="bad_secret"
        )
    return OAuthClient(client_id=client_id, client_secret=client_secret)


def parse_client_file(text: str) -> OAuthClient:
    """The client in the JSON file the Cloud console downloads for a Desktop-app client.

    A ``web`` client is refused: its redirect URIs are a fixed list, and this
    flow redirects to a loopback port chosen at run time.
    """
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientConfigError("the client file is not JSON", code="bad_client_file") from exc
    if not isinstance(data, dict):
        raise ClientConfigError("the client file is not a JSON object", code="bad_client_file")
    if "web" in data and "installed" not in data:
        raise ClientConfigError(
            "this is a Web application client; create a Desktop app client instead"
            " (docs/gmail-setup.md, step 4)",
            code="web_client",
        )
    installed = data.get("installed")
    if not isinstance(installed, dict):
        raise ClientConfigError(
            "the client file has no 'installed' section; download the JSON of a Desktop app client",
            code="bad_client_file",
        )
    return validate_client(
        str(installed.get("client_id", "")), str(installed.get("client_secret", ""))
    )


# --- authorize ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Authorization:
    """One authorization in flight: the URL to open and what the callback must match."""

    url: str
    state: str
    verifier: str = field(repr=False)
    redirect_uri: str


def begin(
    client: OAuthClient,
    redirect_uri: str,
    *,
    login_hint: str | None = None,
    endpoints: GoogleEndpoints | None = None,
) -> Authorization:
    """The authorization URL for ``redirect_uri``, a loopback URL (checked here).

    ``login_hint`` preselects the account when re-authorizing a known mailbox.
    """
    _require_loopback(redirect_uri)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "state": state,
        "code_challenge": challenge.rstrip(b"=").decode(),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    if login_hint:
        params["login_hint"] = login_hint
    ends = endpoints or google()
    return Authorization(
        url=f"{ends.auth_uri}?{urlencode(params)}",
        state=state,
        verifier=verifier,
        redirect_uri=redirect_uri,
    )


@dataclass(frozen=True, slots=True)
class Grant:
    """What a code exchange gives: the refresh token to keep, an access token to use now."""

    refresh_token: str = field(repr=False)
    access_token: str = field(repr=False)


def exchange_code(
    client: OAuthClient,
    authorization: Authorization,
    code: str,
    *,
    endpoints: GoogleEndpoints | None = None,
) -> Grant:
    """Trade the callback's ``code`` for a :class:`Grant`.

    Refuses a grant without Gmail access (:class:`ScopeNotGranted`: Google lets a
    person untick it) or without a refresh token.
    """
    ends = endpoints or google()
    body = _post_form(
        ends,
        ends.token_uri,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": authorization.redirect_uri,
            "code_verifier": authorization.verifier,
        },
    )
    granted = str(body.get("scope", "")).split()
    if GMAIL_SCOPE not in granted:
        raise ScopeNotGranted(
            "Gmail access was not granted; authorize again and leave the Gmail box ticked",
            code="scope_not_granted",
        )
    refresh_token = body.get("refresh_token")
    access_token = body.get("access_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthRefused("Google returned no refresh token", code="no_refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthRefused("Google returned no access token", code="no_access_token")
    return Grant(refresh_token=refresh_token, access_token=access_token)


def refresh_access_token(
    client: OAuthClient, refresh_token: str, *, endpoints: GoogleEndpoints | None = None
) -> str:
    """A fresh access token for ``refresh_token``. :class:`InvalidGrant` when it is dead."""
    ends = endpoints or google()
    body = _post_form(
        ends,
        ends.token_uri,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        },
    )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthRefused("Google returned no access token", code="no_access_token")
    return access_token


def fetch_email(access_token: str, *, endpoints: GoogleEndpoints | None = None) -> str:
    """The authorized account's address, lower-cased, from Gmail's ``users.getProfile``.

    The call also proves the Gmail API is enabled in the person's project, the
    setup step most often missed: a ``403`` raises :class:`GmailApiRefused`.
    """
    ends = endpoints or google()
    request = Request(  # noqa: S310 - https in production, loopback in tests
        ends.profile_uri,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    try:
        body = _send(ends, request)
    except OAuthRefused as exc:
        if exc.code == "http_403":
            raise GmailApiRefused(
                "the Gmail API refused the token; enable the Gmail API in the Cloud project"
                " (docs/gmail-setup.md, step 2)",
                code="gmail_api_refused",
            ) from exc
        raise
    email = body.get("emailAddress")
    if not isinstance(email, str) or "@" not in email:
        raise OAuthRefused("Gmail returned no address for the account", code="no_email")
    return email.strip().lower()


# --- the CLI's redirect -------------------------------------------------------------

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>netkeeper</title>"
    b"<p>netkeeper has Google's answer. You can close this tab and go back to the terminal.</p>"
)


#: The longest the CLI's receiver gives one connection to send its request.
CONNECTION_TIMEOUT_S: Final = 10.0


class LoopbackReceiver:
    """A one-shot HTTP server on ``127.0.0.1`` and a free port, for ``netkeeper gmail login``.

    It answers Google's redirect to :attr:`redirect_uri` and hands back the
    query (``code`` and ``state``, or ``error``). Other paths (a browser's
    ``/favicon.ico``) get a 404 and are ignored. Bound on construction, so the
    port is known before the URL is shown; close it with :meth:`close` or ``with``.

    The server handles one connection at a time, so each connection gets at most
    ``connection_timeout_s`` (and never past :meth:`wait`'s deadline): a socket
    a browser opens and never uses (a preconnect) cannot hold the wait open.
    """

    def __init__(self, *, connection_timeout_s: float = CONNECTION_TIMEOUT_S) -> None:
        received: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            timeout = connection_timeout_s

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                if parts.path != "/":
                    self.send_error(404)
                    return
                query = parse_qs(parts.query)
                received.update({key: values[0] for key, values in query.items() if values})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(_DONE_PAGE)))
                self.end_headers()
                self.wfile.write(_DONE_PAGE)

            def log_message(self, format: str, *args: Any) -> None:
                """Silent: the request line carries the code."""

        self._received = received
        self._handler = Handler
        self._connection_timeout_s = connection_timeout_s
        self._server = HTTPServer(("127.0.0.1", 0), Handler)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    def wait(self, timeout_s: float) -> dict[str, str]:
        """The redirect's query once it arrives; :class:`TimeoutError` after ``timeout_s``.

        A hard bound: an idle connection is dropped at the deadline, not after it.
        """
        deadline = time.monotonic() + timeout_s
        while not self._received:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no answer from Google's page in time")
            self._server.timeout = left
            self._handler.timeout = min(self._connection_timeout_s, left)
            self._server.handle_request()
        return dict(self._received)

    def close(self) -> None:
        self._server.server_close()

    def __enter__(self) -> LoopbackReceiver:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- HTTP --------------------------------------------------------------------------


#: Statuses that say "not now" rather than anything about the grant.
_TRY_LATER: Final = frozenset({408, 429})


def _require_loopback(redirect_uri: str) -> None:
    parts = urlsplit(redirect_uri)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ClientConfigError(
            "the redirect must be a loopback http URL (the installed-app flow)",
            code="bad_redirect",
        )


def _post_form(ends: GoogleEndpoints, url: str, fields: dict[str, str]) -> dict[str, Any]:
    request = Request(  # noqa: S310 - https in production, loopback in tests
        url,
        data=urlencode(fields).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    return _send(ends, request)


def _send(ends: GoogleEndpoints, request: Request) -> dict[str, Any]:
    handlers: list[BaseHandler] = []
    if not ends.use_system_proxy:
        handlers.append(ProxyHandler({}))
    opener = build_opener(*handlers)
    target = urlsplit(request.full_url)
    where = f"{target.scheme}://{target.netloc}{target.path}"  # never the query
    try:
        with opener.open(request, timeout=ends.timeout_s) as response:
            raw = response.read()
    except HTTPError as exc:
        if exc.code >= 500 or exc.code in _TRY_LATER:
            # Before the body: a 5xx is never read as a dead grant, whatever it says.
            log.info("%s %s answered %d", request.get_method(), where, exc.code)
            raise OAuthUnavailable(f"Google answered {exc.code}", code="unavailable") from exc
        error = _error_code(exc)
        log.info("%s %s answered %d (%s)", request.get_method(), where, exc.code, error)
        if error == INVALID_GRANT:
            raise InvalidGrant(
                "Google refused the grant (revoked, or expired on a consent screen in"
                " Testing); authorize again",
                code=INVALID_GRANT,
            ) from exc
        code = error or f"http_{exc.code}"
        raise OAuthRefused(f"Google refused the request: {code}", code=code) from exc
    except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # HTTPException: the connection broke mid-answer (IncompleteRead, a bad status line).
        log.info("%s %s failed: %s", request.get_method(), where, type(exc).__name__)
        raise OAuthUnavailable("could not reach Google", code="unavailable") from exc
    try:
        body: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OAuthUnavailable(
            "Google answered with something not JSON", code="unavailable"
        ) from exc
    if not isinstance(body, dict):
        raise OAuthUnavailable("Google answered with something not an object", code="unavailable")
    return body


def _error_code(exc: HTTPError) -> str | None:
    """Google's ``error`` field, when the body has one that looks like a code."""
    try:
        body: Any = json.loads(exc.read())
    except (json.JSONDecodeError, OSError, ValueError, http.client.HTTPException):
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):  # the Gmail API's shape: {"error": {"status": ...}}
        return None
    if isinstance(error, str) and error.replace("_", "").isalnum() and len(error) <= 64:
        return error
    return None
