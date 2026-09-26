"""A fake of Google's OAuth token endpoint and Gmail's profile call, on a loopback port.

Nothing here reaches Google. :class:`FakeGoogle` serves ``/token`` (the
authorization-code and refresh-token grants, with PKCE checked) and
``/profile`` on ``127.0.0.1`` and a free port, in a thread. A test plays the
person's part with :meth:`FakeGoogle.consent`, which reads the authorization
URL netkeeper built the way Google's page would, and answers the redirect URL
Google would send the browser to.

The fixture ``fake_google`` (``tests/conftest.py``) starts one and points
``netkeeper.campaigns.gmail_oauth.GOOGLE`` at it.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httplib2
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError

from netkeeper.campaigns.gmail_oauth import GMAIL_SCOPE, GoogleEndpoints

CLIENT_ID = "1234-fake.apps.googleusercontent.com"
CLIENT_SECRET = "fake-client-secret"
FAKE_EMAIL = "sender@example.com"


@dataclass
class _Code:
    email: str
    scope: str
    challenge: str
    redirect_uri: str
    client_id: str


@dataclass
class FakeGoogle:
    """The fake's state. Tests change it between calls to script Google's answers."""

    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    codes: dict[str, _Code] = field(default_factory=dict)
    refresh_tokens: dict[str, str] = field(default_factory=dict)  # token -> email
    access_tokens: dict[str, str] = field(default_factory=dict)  # token -> email
    revoked: set[str] = field(default_factory=set)
    profile_status: int = 200
    token_status: int | None = None  # force every /token answer to this status
    # Force every /token answer to this status and body (a str is sent as HTML).
    token_answer: tuple[int, dict[str, Any] | str] | None = None
    truncate: bool = False  # promise more body than is sent, then hang up (IncompleteRead)
    requests: list[tuple[str, str]] = field(default_factory=list)  # (path, grant_type or "")
    _counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    # --- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                fake._handle(self, "POST")

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def base(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def endpoints(self) -> GoogleEndpoints:
        return GoogleEndpoints(
            auth_uri=f"{self.base}/auth",
            token_uri=f"{self.base}/token",
            profile_uri=f"{self.base}/profile",
            timeout_s=5.0,
            use_system_proxy=False,
        )

    # --- the person's part -----------------------------------------------------------

    def consent(
        self, authorization_url: str, *, email: str = FAKE_EMAIL, scope: str = GMAIL_SCOPE
    ) -> str:
        """Allow access on "Google's page": the redirect URL, with a code and the state."""
        query = {
            key: values[0] for key, values in parse_qs(urlsplit(authorization_url).query).items()
        }
        assert query["code_challenge_method"] == "S256"
        code = f"code-{next(self._counter)}"
        self.codes[code] = _Code(
            email=email,
            scope=scope,
            challenge=query["code_challenge"],
            redirect_uri=query["redirect_uri"],
            client_id=query["client_id"],
        )
        return f"{query['redirect_uri']}?{urlencode({'state': query['state'], 'code': code})}"

    def deny(self, authorization_url: str) -> str:
        """Press Cancel on "Google's page"."""
        query = {
            key: values[0] for key, values in parse_qs(urlsplit(authorization_url).query).items()
        }
        answer = urlencode({"state": query["state"], "error": "access_denied"})
        return f"{query['redirect_uri']}?{answer}"

    def issue_refresh_token(self, email: str = FAKE_EMAIL) -> str:
        token = f"rt-{next(self._counter)}"
        self.refresh_tokens[token] = email
        return token

    def revoke_all(self) -> None:
        self.revoked.update(self.refresh_tokens)

    # --- serving ---------------------------------------------------------------------

    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(handler.path).path
        status: int
        body: dict[str, Any] | str
        if method == "POST" and path == "/token":
            length = int(handler.headers.get("Content-Length", "0"))
            form = {k: v[0] for k, v in parse_qs(handler.rfile.read(length).decode()).items()}
            self.requests.append((path, form.get("grant_type", "")))
            status, body = self.token_answer or self._token(form)
        elif method == "GET" and path == "/profile":
            self.requests.append((path, ""))
            status, body = self._profile(handler.headers.get("Authorization", ""))
        else:
            status, body = 404, {"error": "not_found"}
        html = isinstance(body, str)
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html" if html else "application/json")
        promised = len(raw) + 100 if self.truncate else len(raw)
        handler.send_header("Content-Length", str(promised))
        handler.end_headers()
        handler.wfile.write(raw)

    def _token(self, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if self.token_status is not None:
            return self.token_status, {"error": "backend_error"}
        if (
            form.get("client_id") != self.client_id
            or form.get("client_secret") != self.client_secret
        ):
            return 401, {
                "error": "invalid_client",
                "error_description": "The OAuth client was not found.",
            }
        grant = form.get("grant_type")
        if grant == "authorization_code":
            code = self.codes.pop(form.get("code", ""), None)
            if code is None or code.redirect_uri != form.get("redirect_uri"):
                return 400, {"error": "invalid_grant", "error_description": "Bad Request"}
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != code.challenge:
                return 400, {
                    "error": "invalid_grant",
                    "error_description": "Invalid code verifier.",
                }
            refresh = self.issue_refresh_token(code.email)
            access = self._access(code.email)
            return 200, {
                "access_token": access,
                "refresh_token": refresh,
                "scope": code.scope,
                "expires_in": 3599,
                "token_type": "Bearer",
            }
        if grant == "refresh_token":
            token = form.get("refresh_token", "")
            if token not in self.refresh_tokens or token in self.revoked:
                return 400, {
                    "error": "invalid_grant",
                    "error_description": "Token has been expired or revoked.",
                }
            return 200, {
                "access_token": self._access(self.refresh_tokens[token]),
                "expires_in": 3599,
                "scope": GMAIL_SCOPE,
                "token_type": "Bearer",
            }
        return 400, {"error": "unsupported_grant_type"}

    def _profile(self, authorization: str) -> tuple[int, dict[str, Any]]:
        if self.profile_status != 200:
            return self.profile_status, {
                "error": {"code": self.profile_status, "status": "PERMISSION_DENIED"}
            }
        email = self.access_tokens.get(authorization.removeprefix("Bearer "))
        if email is None:
            return 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}
        return 200, {"emailAddress": email, "messagesTotal": 1, "threadsTotal": 1}

    def _access(self, email: str) -> str:
        token = f"at-{next(self._counter)}"
        self.access_tokens[token] = email
        return token


class MemoryKeyring(KeyringBackend):
    """An in-memory ``keyring`` backend, installed for every test (``tests/conftest.py``).

    ``broken`` makes every call raise, as a locked or missing Keychain would.
    """

    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.entries: dict[tuple[str, str], str] = {}
        self.broken = False

    def get_password(self, service: str, username: str) -> str | None:
        self._check()
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._check()
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._check()
        if self.entries.pop((service, username), None) is None:
            raise PasswordDeleteError(username)

    def _check(self) -> None:
        if self.broken:
            raise KeyringError("the fake Keychain is locked")


@dataclass
class Sent:
    """One request :class:`RecordingHttp` saw."""

    method: str
    uri: str
    body: Any
    headers: dict[str, str]

    @property
    def path(self) -> str:
        return urlsplit(self.uri).path

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.uri).query)

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class RecordingHttp:
    """The transport under ``GmailClient``: records every request, answers from a script.

    Spec 16's "fake service object that records calls and replays canned
    responses", at the HTTP layer so the real client's request building and
    error mapping run. Each answer is ``(status, body)`` or an exception to
    raise. Running out of answers fails the test, so nothing falls through to
    a network.
    """

    answers: list[tuple[int, Any] | BaseException] = field(default_factory=list)
    requests: list[Sent] = field(default_factory=list)
    timeout: float | None = None

    def answer(self, status: int, body: Any = None) -> RecordingHttp:
        self.answers.append((status, {} if body is None else body))
        return self

    def fail(self, exc: BaseException) -> RecordingHttp:
        self.answers.append(exc)
        return self

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        **_: Any,
    ) -> tuple[Any, bytes]:
        self.requests.append(Sent(method, uri, body, dict(headers or {})))
        assert self.answers, f"no answer scripted for {method} {uri}"
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, payload = answer
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return httplib2.Response({"status": str(status), "content-type": "application/json"}), raw


def gmail_error(status: int, reason: str, message: str = "a refusal") -> dict[str, Any]:
    """A Gmail API error body."""
    return {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"domain": "global", "reason": reason, "message": message}],
        }
    }
