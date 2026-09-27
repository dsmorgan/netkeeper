"""The Gmail API client the campaign engine uses (spec 11.5, 11.7; ADR 0003; item P3-02).

:class:`Gmail` is the interface: drafts, send, messages and threads (metadata
only, never a body), labels, history, and message search. :class:`GmailClient`
implements it over ``googleapiclient``; :class:`netkeeper.campaigns.gmail_fake.FakeGmail`
implements it in memory for every engine test. Code that sends mail takes a
:class:`Gmail`, never a concrete class.

**Purpose logging.** Every method takes a keyword-only ``purpose``, such as
``"send step 2 for enrollment 14"``, and logs it with the method before the call
(ADR 0003: every call is logged with its purpose). :func:`check_purpose` refuses
an empty or multi-line purpose, and one with an ``@`` in it, so an address
cannot reach the log through it. Nothing else a call carries is logged: not the
message, not a search query (which names addresses), not a token.
``googleapiclient`` and ``google_auth_httplib2`` log request URLs, and a page
request's body, at ``DEBUG``; their loggers are held at ``WARNING`` here.

**Errors.** Every failure is one of five :class:`GmailError` types:
:class:`GmailRateLimited` (429, or a 403 whose reason is a rate or quota limit,
or whose ``status`` is ``RESOURCE_EXHAUSTED``, whatever reason it names),
:class:`GmailNotFound` (404: a gone draft, an unknown thread, or a history id
older than Gmail keeps), :class:`GmailAuthError` (the grant is dead, or the API
refused the token), :class:`GmailTransient` (network, timeout, 5xx; try later),
and :class:`GmailRejected` (any other 4xx: the request itself is wrong, and
trying again will not help). ``code`` is short and safe to store; Google's own
message text is never kept, since it can quote an address.

A :class:`GmailTransient` from a call that writes (send, create a draft,
create a label, change labels) has ``outcome_unknown`` set: the request may
have reached Gmail before the connection failed, so the caller must look (a
search for ``rfc822msgid:``) before trying again, or it may send twice.
Nothing here retries a request by itself, and neither does the transport:
``httplib2`` silently sends a request a second time when the first attempt's
connection drops before an answer (``BadStatusLine``, ``RemoteDisconnected``),
whatever the method. :class:`WriteOnceHttp` is ``httplib2`` with that turned off
for every method that is not idempotent, so a ``POST`` is written to the wire
at most once, and always on a fresh connection. It follows no redirect. An
answer cut short (``IncompleteRead``) is a :class:`GmailTransient` too, with the
outcome unknown for a write. A name that does not resolve or a refused
connection sent nothing, so its outcome is known even for a write.

**Auth.** :class:`GmailClient` never refreshes a token itself: it is given a
``refresh`` function (``services.mailboxes.open_gmail`` passes P3-01's
:func:`~netkeeper.campaigns.gmail_oauth.refresh_access_token`), calls it for the
first token and again on a 401. A refresh Google refuses with a code in
:data:`~netkeeper.campaigns.gmail_oauth.REAUTH_CODES` (``invalid_grant``,
``invalid_client``, ``unauthorized_client``) raises :class:`GmailAuthError` and
calls ``on_auth_failure``, which ``open_gmail`` points at the mailbox's
``reauth_required`` path. Any other refusal of the refresh (a proxy's ``407``,
an HTML ``403``, ``invalid_request``) says nothing certain about the grant: it
is a :class:`GmailTransient`, and nothing is marked.

**Blocking.** Every call blocks on the network. Call it off the event loop, and
never with a database session open. A client is not thread-safe (``httplib2``
is not): one per job.
"""

from __future__ import annotations

import base64
import html
import http.client
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Final, Protocol

import google.auth.credentials
import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from netkeeper.campaigns import gmail_oauth

log = logging.getLogger(__name__)

# Both log request URLs (a search's ``q`` names addresses) and page bodies at DEBUG.
for _noisy in ("googleapiclient", "google_auth_httplib2"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

#: The headers a message is read with. The body never is (spec 11.7, step 4).
METADATA_HEADERS: Final = (
    "From",
    "To",
    "Cc",
    "Subject",
    "Date",
    "Message-ID",
    "In-Reply-To",
    "References",
    "Auto-Submitted",
)

#: Longest purpose a call may log.
PURPOSE_MAX_LENGTH: Final = 120

#: A 403 with one of these reasons is a limit, not a refusal of the token.
RATE_LIMIT_REASONS: Final = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"}
)

#: A 403 with this ``error.status`` is a limit too, whatever its ``errors[].reason``
#: says. Newer answers can carry only the status, with no legacy reason.
RATE_LIMIT_STATUS: Final = "RESOURCE_EXHAUSTED"

#: ``messages.list`` answers at most this many per page.
_PAGE_MAX: Final = 500

#: Seconds before a request with no answer gives up.
DEFAULT_TIMEOUT_S: Final = 30.0

#: The methods ``httplib2`` may send again after a dropped connection: the
#: idempotent ones (RFC 9110, 9.2.2). Every Gmail write is a ``POST``.
RESENDABLE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


# --- errors ------------------------------------------------------------------------


class GmailError(Exception):
    """Base for every Gmail failure. ``code`` is short and safe to show and store."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class GmailRateLimited(GmailError):
    """Gmail said slow down (a rate limit or a quota). Try again later."""


class GmailNotFound(GmailError):
    """What the call named is not there: a sent or discarded draft, an unknown id,
    or a ``startHistoryId`` older than Gmail keeps (re-baseline from threads)."""


class GmailAuthError(GmailError):
    """The grant is dead or the API refused the token: a person has to authorize again."""


class GmailTransient(GmailError):
    """Network, timeout or a failure on Gmail's side. Try again later.

    ``outcome_unknown`` is True for a call that writes: it may have happened.
    """

    def __init__(self, message: str, *, code: str, outcome_unknown: bool = False) -> None:
        super().__init__(message, code=code)
        self.outcome_unknown = outcome_unknown


class GmailRejected(GmailError):
    """Gmail refused the request as it stands (a bad argument, no recipient)."""


class GmailConflict(GmailRejected):
    """What the call would create exists already (a label name)."""


# --- values ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Profile:
    email: str
    history_id: int


@dataclass(frozen=True, slots=True)
class Label:
    """``type`` is ``system`` (``INBOX``, ``SENT``, ``DRAFT``...) or ``user``."""

    id: str
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class MessageRef:
    id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class Message:
    """A message's metadata: the :data:`METADATA_HEADERS`, labels and snippet, no body.

    ``internal_date`` is when Gmail received it (for a sent message, when it was sent).
    ``snippet`` is plain text: Gmail sends it HTML-escaped (``don&#39;t``), and
    :func:`snippet_text` has undone that, so a phrase can be matched against it.
    """

    id: str
    thread_id: str
    label_ids: frozenset[str]
    history_id: int
    internal_date: datetime
    snippet: str
    headers: tuple[tuple[str, str], ...] = field(repr=False)

    def header(self, name: str) -> str | None:
        """The first header called ``name``, compared without case."""
        wanted = name.lower()
        return next((value for key, value in self.headers if key.lower() == wanted), None)


@dataclass(frozen=True, slots=True)
class Thread:
    """A thread's messages, oldest first."""

    id: str
    history_id: int
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class Draft:
    id: str
    message: MessageRef


@dataclass(frozen=True, slots=True)
class History:
    """What :meth:`Gmail.history` found: messages added since the start, in history
    order, and the ``history_id`` to start from next time."""

    history_id: int
    messages_added: tuple[MessageRef, ...]


# --- the interface -----------------------------------------------------------------


class Gmail(Protocol):
    """Everything the engine may ask of a mailbox. Every call needs a ``purpose``."""

    def profile(self, *, purpose: str) -> Profile: ...

    def send(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> MessageRef:
        """``messages.send``. A follow-up joins ``thread_id`` only when its
        ``In-Reply-To`` or ``References`` names a message in that thread and its
        subject matches; otherwise Gmail starts a new thread, without an error."""
        ...

    def create_draft(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> Draft:
        """``drafts.create``, threaded by the same rule as :meth:`send`."""
        ...

    def get_draft(self, draft_id: str, *, purpose: str) -> Draft:
        """``drafts.get``: :class:`GmailNotFound` once it is sent or discarded."""
        ...

    def list_drafts(self, *, purpose: str) -> list[Draft]:
        """``drafts.list``, every page: each draft the mailbox holds, with its message."""
        ...

    def get_message(self, message_id: str, *, purpose: str) -> Message: ...

    def get_thread(self, thread_id: str, *, purpose: str) -> Thread: ...

    def search(self, query: str, *, max_results: int = 100, purpose: str) -> list[MessageRef]:
        """``messages.list`` with Gmail's search syntax, newest first. Spam and
        trash are left out, as Gmail leaves them out, unless the query says
        ``in:anywhere``. ``from:`` and ``to:`` match whole words."""
        ...

    def list_labels(self, *, purpose: str) -> list[Label]: ...

    def create_label(self, name: str, *, purpose: str) -> Label:
        """:class:`GmailConflict` when a label of that name exists."""
        ...

    def modify_labels(
        self,
        message_id: str,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
        purpose: str,
    ) -> None: ...

    def history(
        self, start_history_id: int, *, label_id: str | None = None, purpose: str
    ) -> History:
        """``history.list`` of ``messageAdded`` since ``start_history_id``, every page.
        :class:`GmailNotFound` when the start is older than Gmail keeps.

        A message deleted since it was added is still listed (a draft sent or
        discarded, a reply the person deleted), and reading it is
        :class:`GmailNotFound`: skip it, never fail the poll on it."""
        ...


def check_purpose(purpose: str) -> str:
    """``purpose``, stripped, or :class:`ValueError` when it is not fit to log."""
    stripped = purpose.strip()
    if not stripped:
        raise ValueError("every Gmail call needs a purpose")
    if len(stripped) > PURPOSE_MAX_LENGTH or not stripped.isprintable():
        raise ValueError(f"a purpose is one line of at most {PURPOSE_MAX_LENGTH} characters")
    if "@" in stripped:
        raise ValueError("a purpose never names an address; name the enrollment or step")
    return stripped


def log_call(method: str, mailbox_id: int, purpose: str) -> None:
    """The one log line every call writes, from the client and the fake alike."""
    log.info("gmail %s for mailbox %d: %s", method, mailbox_id, purpose)


def ensure_label(gmail: Gmail, name: str, *, purpose: str) -> Label:
    """The user label called ``name``, created when it is missing.

    Labels compare without case, as Gmail compares them. Safe to race: a
    create that meets a label made in between reads it back.
    """
    wanted = name.lower()
    for label in gmail.list_labels(purpose=purpose):
        if label.name.lower() == wanted:
            return label
    try:
        return gmail.create_label(name, purpose=purpose)
    except GmailConflict:
        for label in gmail.list_labels(purpose=purpose):
            if label.name.lower() == wanted:
                return label
        raise


def snippet_text(snippet: str) -> str:
    """A snippet as Gmail's API sends it (HTML-escaped), as the text it stands for."""
    return html.unescape(snippet)


def encode_raw(message: EmailMessage) -> str:
    """The message as Gmail's ``raw`` field: RFC 2822 with CRLF, base64url."""
    return base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode("ascii")


# --- the transport -----------------------------------------------------------------


class SentWithoutAnswer(Exception):
    """A write reached the connection, and the connection failed before an answer.

    Raised in place of the ``OSError`` or ``HTTPException`` that ``httplib2``
    would have caught and answered by sending the request again. Gmail may have
    acted on it.
    """


class WriteOnceMixin(http.client.HTTPConnection):
    """What makes an ``httplib2`` connection never let a write be sent twice.

    For a method outside :data:`RESENDABLE_METHODS` it drops a kept-alive
    socket and connects afresh first, so a connection the server closed while
    idle cannot fail a write, and it turns any failure after that into
    :class:`SentWithoutAnswer`, which ``httplib2``'s retry loop does not catch.
    """

    _nk_method = "GET"
    _nk_fresh = False  # connected, and nothing sent on the socket yet

    def connect(self) -> None:
        super().connect()
        self._nk_fresh = True

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> None:
        self._nk_method = method.upper()
        try:
            if self._nk_method in RESENDABLE_METHODS:
                super().request(method, url, *args, **kwargs)
                return
            if self.sock is None or not self._nk_fresh:
                self.close()
                self.connect()  # outside the inner try: a failure to connect sent nothing
            try:
                super().request(method, url, *args, **kwargs)
            except (OSError, http.client.HTTPException) as exc:
                raise self._no_answer(exc) from exc
        finally:
            self._nk_fresh = False

    def getresponse(self) -> http.client.HTTPResponse:
        try:
            return super().getresponse()
        except (OSError, http.client.HTTPException) as exc:
            if self._nk_method in RESENDABLE_METHODS:
                raise
            raise self._no_answer(exc) from exc

    def _no_answer(self, exc: BaseException) -> SentWithoutAnswer:
        self.close()
        return SentWithoutAnswer(
            f"the connection failed during a {self._nk_method} ({type(exc).__name__})"
        )


class WriteOnceConnection(WriteOnceMixin, httplib2.HTTPSConnectionWithTimeout):  # type: ignore[misc]
    """``httplib2``'s HTTPS connection with :class:`WriteOnceMixin`."""


class WriteOnceHttp(httplib2.Http):  # type: ignore[misc]
    """``httplib2.Http`` whose HTTPS connections are ``connection_type``:
    :class:`WriteOnceConnection`, unless a test passes another class with
    :class:`WriteOnceMixin`."""

    def __init__(
        self,
        *,
        timeout: float | None = DEFAULT_TIMEOUT_S,
        connection_type: type[WriteOnceMixin] = WriteOnceConnection,
    ) -> None:
        super().__init__(timeout=timeout)
        # A redirect would resend a write's body (308) or leave this transport for
        # plain httplib2 (a redirect to http:). Gmail's API never redirects, so a
        # 3xx is answered as it is and the client reads it as a refusal.
        self.follow_redirects = False
        self._nk_connection_type = connection_type

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        redirections: int = httplib2.DEFAULT_MAX_REDIRECTS,
        connection_type: Any = None,
    ) -> Any:
        if connection_type is None and uri.lower().startswith("https:"):
            connection_type = self._nk_connection_type
        return super().request(uri, method, body, headers, redirections, connection_type)


# --- the real client ---------------------------------------------------------------


class _Credentials(google.auth.credentials.Credentials):
    """A bearer token that ``refresh`` renews: google-auth's shape over P3-01's refresh."""

    def __init__(self, refresh: Callable[[], str]) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self._renew = refresh

    def refresh(self, request: Any) -> None:
        self.token = self._renew()


class GmailClient:
    """:class:`Gmail` over ``googleapiclient``, for one mailbox.

    ``refresh`` returns a fresh access token (see the module docstring).
    ``on_auth_failure`` is called with the :class:`GmailAuthError` before it is
    raised; a failure inside it is logged and does not hide the auth error.
    ``http`` is the transport under the auth layer: a test passes a recording
    fake, production leaves it to :class:`WriteOnceHttp`.
    """

    def __init__(
        self,
        refresh: Callable[[], str],
        *,
        mailbox_id: int,
        on_auth_failure: Callable[[GmailAuthError], None] | None = None,
        http: Any = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.mailbox_id = mailbox_id
        self._on_auth_failure = on_auth_failure
        transport = http if http is not None else WriteOnceHttp(timeout=timeout_s)
        self._service: Any = build(
            "gmail",
            "v1",
            http=AuthorizedHttp(_Credentials(refresh), http=transport),
            static_discovery=True,
            cache_discovery=False,
        )
        self._users: Any = self._service.users()

    # --- calls -----------------------------------------------------------------------

    def profile(self, *, purpose: str) -> Profile:
        body = self._run("users.getProfile", purpose, self._users.getProfile(userId="me"))
        return Profile(email=str(body["emailAddress"]).lower(), history_id=int(body["historyId"]))

    def send(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> MessageRef:
        payload: dict[str, Any] = {"raw": encode_raw(message)}
        if thread_id is not None:
            payload["threadId"] = thread_id
        request = self._users.messages().send(userId="me", body=payload)
        return _ref(self._run("messages.send", purpose, request, writes=True))

    def create_draft(
        self, message: EmailMessage, *, thread_id: str | None = None, purpose: str
    ) -> Draft:
        payload: dict[str, Any] = {"raw": encode_raw(message)}
        if thread_id is not None:
            payload["threadId"] = thread_id
        request = self._users.drafts().create(userId="me", body={"message": payload})
        return _draft(self._run("drafts.create", purpose, request, writes=True))

    def get_draft(self, draft_id: str, *, purpose: str) -> Draft:
        request = self._users.drafts().get(userId="me", id=draft_id, format="minimal")
        return _draft(self._run("drafts.get", purpose, request))

    def list_drafts(self, *, purpose: str) -> list[Draft]:
        found: list[Draft] = []
        token: str | None = None
        while True:
            request = self._users.drafts().list(userId="me", maxResults=_PAGE_MAX, pageToken=token)
            body = self._run("drafts.list", purpose, request)
            found.extend(_draft(item) for item in body.get("drafts", []))
            token = body.get("nextPageToken")
            if not token:
                return found

    def get_message(self, message_id: str, *, purpose: str) -> Message:
        request = self._users.messages().get(
            userId="me", id=message_id, format="metadata", metadataHeaders=list(METADATA_HEADERS)
        )
        return _message(self._run("messages.get", purpose, request))

    def get_thread(self, thread_id: str, *, purpose: str) -> Thread:
        request = self._users.threads().get(
            userId="me", id=thread_id, format="metadata", metadataHeaders=list(METADATA_HEADERS)
        )
        body = self._run("threads.get", purpose, request)
        return Thread(
            id=str(body["id"]),
            history_id=int(body["historyId"]),
            messages=tuple(_message(item) for item in body.get("messages", [])),
        )

    def search(self, query: str, *, max_results: int = 100, purpose: str) -> list[MessageRef]:
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        found: list[MessageRef] = []
        token: str | None = None
        while len(found) < max_results:
            request = self._users.messages().list(
                userId="me",
                q=query,
                maxResults=min(max_results - len(found), _PAGE_MAX),
                pageToken=token,
            )
            body = self._run("messages.list", purpose, request)
            found.extend(_ref(item) for item in body.get("messages", []))
            token = body.get("nextPageToken")
            if not token:
                break
        return found[:max_results]

    def list_labels(self, *, purpose: str) -> list[Label]:
        body = self._run("labels.list", purpose, self._users.labels().list(userId="me"))
        return [_label(item) for item in body.get("labels", [])]

    def create_label(self, name: str, *, purpose: str) -> Label:
        request = self._users.labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        return _label(self._run("labels.create", purpose, request, writes=True))

    def modify_labels(
        self,
        message_id: str,
        *,
        add: Sequence[str] = (),
        remove: Sequence[str] = (),
        purpose: str,
    ) -> None:
        request = self._users.messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": list(add), "removeLabelIds": list(remove)},
        )
        self._run("messages.modify", purpose, request, writes=True)

    def history(
        self, start_history_id: int, *, label_id: str | None = None, purpose: str
    ) -> History:
        added: list[MessageRef] = []
        seen: set[str] = set()
        token: str | None = None
        latest = start_history_id
        while True:
            request = self._users.history().list(
                userId="me",
                startHistoryId=str(start_history_id),
                historyTypes=["messageAdded"],
                labelId=label_id,
                pageToken=token,
            )
            body = self._run("history.list", purpose, request)
            latest = int(body.get("historyId", latest))
            for record in body.get("history", []):
                for item in record.get("messagesAdded", []):
                    ref = _ref(item["message"])
                    if ref.id not in seen:
                        seen.add(ref.id)
                        added.append(ref)
            token = body.get("nextPageToken")
            if not token:
                return History(history_id=latest, messages_added=tuple(added))

    # --- plumbing --------------------------------------------------------------------

    def _run(self, method: str, purpose: str, request: Any, *, writes: bool = False) -> Any:
        log_call(method, self.mailbox_id, check_purpose(purpose))
        try:
            return request.execute(num_retries=0)
        except HttpError as exc:
            error = _from_http(exc, writes=writes)
        except gmail_oauth.OAuthUnavailable as exc:
            # The token could not be renewed, so nothing was sent.
            error = GmailTransient("could not renew the access token", code=exc.code)
        except gmail_oauth.OAuthError as exc:
            if gmail_oauth.needs_reauthorization(exc):
                error = GmailAuthError("the mailbox's grant was refused", code=exc.code)
            else:
                # Refused, but not in a way that says the grant is dead. Nothing was sent.
                error = GmailTransient("Google refused to renew the access token", code=exc.code)
        except (httplib2.ServerNotFoundError, ConnectionRefusedError) as exc:
            # The name did not resolve, or the connection was refused: nothing left.
            error = GmailTransient(
                f"could not reach Gmail ({type(exc).__name__})", code="unavailable"
            )
        except (
            OSError,
            http.client.HTTPException,  # an answer cut short (IncompleteRead) among them
            httplib2.HttpLib2Error,
            SentWithoutAnswer,
        ) as exc:
            error = GmailTransient(
                f"could not reach Gmail ({type(exc).__name__})",
                code="unavailable",
                outcome_unknown=writes,
            )
        log.info(
            "gmail %s for mailbox %d failed: %s (%s)",
            method,
            self.mailbox_id,
            type(error).__name__,
            error.code,
        )
        if isinstance(error, GmailAuthError) and self._on_auth_failure is not None:
            try:
                self._on_auth_failure(error)
            except Exception:
                log.exception("mailbox %d: recording the auth failure failed", self.mailbox_id)
        raise error


def _from_http(exc: HttpError, *, writes: bool) -> GmailError:
    status = int(exc.resp.status)
    reason, api_status = _error_codes(exc.content)
    code = reason or f"http_{status}"
    limited = reason in RATE_LIMIT_REASONS or api_status == RATE_LIMIT_STATUS
    if status == 429 or (status == 403 and limited):
        return GmailRateLimited(f"Gmail answered {status}: {code}", code=code)
    if status == 401 or status == 403:
        return GmailAuthError(f"Gmail refused the token: {code}", code=code)
    if status == 404:
        return GmailNotFound(f"Gmail found nothing: {code}", code=code)
    if status == 409:
        return GmailConflict(f"Gmail answered 409: {code}", code=code)
    if status >= 500 or status == 408:
        return GmailTransient(f"Gmail answered {status}", code=code, outcome_unknown=writes)
    return GmailRejected(f"Gmail refused the request: {code}", code=code)


def _error_codes(content: bytes) -> tuple[str | None, str | None]:
    """The first ``errors[].reason`` and the ``status`` of a Gmail error body, each
    when it looks like a code."""
    try:
        body: Any = json.loads(content)
    except (ValueError, TypeError):
        return None, None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None
    errors = error.get("errors")
    first = errors[0] if isinstance(errors, list) and errors else None
    reason = first.get("reason") if isinstance(first, dict) else None
    status = error.get("status")
    return _as_code(reason), _as_code(status)


def _as_code(value: object) -> str | None:
    if isinstance(value, str) and value.replace("_", "").isalnum() and len(value) <= 64:
        return value
    return None


def _ref(body: Mapping[str, Any]) -> MessageRef:
    return MessageRef(id=str(body["id"]), thread_id=str(body["threadId"]))


def _draft(body: Mapping[str, Any]) -> Draft:
    return Draft(id=str(body["id"]), message=_ref(body["message"]))


def _label(body: Mapping[str, Any]) -> Label:
    return Label(
        id=str(body["id"]), name=str(body["name"]), type=str(body.get("type", "user")).lower()
    )


def _message(body: Mapping[str, Any]) -> Message:
    headers: Iterable[Mapping[str, Any]] = body.get("payload", {}).get("headers", [])
    return Message(
        id=str(body["id"]),
        thread_id=str(body["threadId"]),
        label_ids=frozenset(body.get("labelIds", [])),
        history_id=int(body["historyId"]),
        internal_date=datetime.fromtimestamp(int(body["internalDate"]) / 1000, tz=UTC),
        snippet=snippet_text(str(body.get("snippet", ""))),
        headers=tuple((str(item["name"]), str(item["value"])) for item in headers),
    )
