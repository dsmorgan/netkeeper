"""Read what the page loads: the observation seam (ADR 0006, spec 9.3).

netkeeper drives its tab like a person -- it navigates, it scrolls -- and reads the
responses the page itself fetched while it did. This module is the reading half: an
:class:`Observation` listens to one tab's ``response`` events, keeps the bodies of the
responses whose method, origin, and path a :class:`ResponseMatch` names, and hands them
to the caller in the order they arrived. :meth:`netkeeper.linkedin.browser.BrowserRun.observe`
is the only thing that starts one, on the run's own tab.

**Passive, read-only, and nothing else.** A listener sees a response after the browser
has already received it. There is no way from here to hold, change, answer, or cancel
a request: this module never reaches for ``route``, a request's ``continue_``,
``fulfill``, or ``abort``, header setters, or an API request context, and
``tests/test_browser_safety.py`` fails the build if anything in the package does. What
it reads from the request side is what the page already sent: the method, the url, and
the request body (the pagination request's ``startIndex`` lives there). Nothing here
fetches anything; the requests are the page's own.

**Why a body could not be read** (#200). When a body read fails, the observation
keeps fixed, non-identifying facts about it in :class:`ReadDiagnostics`: whether a
service worker answered, the status, the content type and transfer encoding as
fixed categories, the content length, whether the request finished or failed (and a
fixed category for the failure), how long after the ``response`` event the read
started and failed, and how many other reads were in flight. It learns whether the
request finished or failed from the tab's ``requestfinished`` and ``requestfailed``
events, which it only listens to, like ``response``. :meth:`Observation.close` logs
one summary line of these per observation.

**Bodies never reach a log.** A response body is somebody's data: a name, a headline,
an email address. :class:`ObservedResponse` leaves both bodies out of its ``repr``, and
the only strings this module logs or raises are fixed phrases and counts, never a url
(a profile url names a person), a body, or an exception's own message (Playwright's can
quote the url).

**Bounded.** At most :attr:`ObservationLimits.max_pending` matching responses wait
unread at once, each body at most :attr:`ObservationLimits.max_body_bytes`; a body that
takes longer than :attr:`ObservationLimits.body_timeout_s` to arrive is recorded as
failed rather than waited on. Going past ``max_pending`` means a response was dropped,
and :meth:`Observation.next` raises :class:`ObservationFailed` rather than let the
caller mistake the gap for the end of anything.

Nothing here imports the ORM or opens a session (spec 9.10).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin

log = logging.getLogger(__name__)

#: The Playwright event a listener subscribes to. One name, so a test can pin it.
RESPONSE_EVENT: Final = "response"

#: The two request events an observation also listens to, only to learn whether a
#: request whose body could not be read had finished or failed (#200).
REQUEST_FINISHED_EVENT: Final = "requestfinished"
REQUEST_FAILED_EVENT: Final = "requestfailed"

#: How many ended requests an observation remembers at once; the oldest is forgotten.
MAX_REMEMBERED_ENDS: Final = 256


class ObservationFailed(RuntimeError):
    """The observation itself broke: a response was dropped, or the tab it listened to went away.

    Never raised for anything LinkedIn said -- a checkpoint or a login wall is an
    :class:`~netkeeper.linkedin.classify.Outcome` the caller classifies from what was
    observed. This is the mechanism failing, so the caller cannot trust that it saw
    every response, and a run that cannot trust that must not call itself complete.
    """


class RequestLike(Protocol):
    """The read-only slice of a Playwright ``Request`` an observation reads."""

    @property
    def method(self) -> str: ...

    @property
    def resource_type(self) -> str: ...

    @property
    def post_data(self) -> str | None: ...


class EndedRequestLike(Protocol):
    """The read-only slice of a Playwright ``Request`` a request event hands over."""

    @property
    def method(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def failure(self) -> str | None: ...


class ResponseLike(Protocol):
    """The read-only slice of a Playwright ``Response`` an observation reads."""

    @property
    def url(self) -> str: ...

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def request(self) -> RequestLike: ...

    async def body(self) -> bytes: ...


class ListenablePage(Protocol):
    """A tab that can be listened to, and stopped being listened to. Nothing more."""

    def on(self, event: str, handler: Callable[[Any], None]) -> None: ...

    def remove_listener(self, event: str, handler: Callable[[Any], None]) -> None: ...


@dataclass(frozen=True, slots=True)
class ResponseRule:
    """One request shape to keep: an HTTP method and an exact path, or a path prefix.

    The path is compared after dropping one trailing ``/`` from both sides, so
    ``/mynetwork/invite-connect/connections`` and ``.../connections/`` are the same
    page. The query string is never compared: LinkedIn decorates these urls with
    tracking parameters that change per request.

    ``prefix`` keeps every path under ``path`` instead (``/in/`` for any profile
    page, #190): a profile a slug redirects to has a path nobody knew before the
    navigation. A prefix must end with ``/``, so ``/in/`` never matches ``/inbox``.
    """

    method: str
    path: str
    prefix: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        if not self.path.startswith("/"):
            raise ValueError("a response rule's path starts with '/'")
        if self.prefix and not self.path.endswith("/"):
            raise ValueError("a prefix rule's path ends with '/'")

    def matches(self, method: str, path: str) -> bool:
        if method.upper() != self.method:
            return False
        if self.prefix:
            return path.startswith(self.path) and len(path) > len(self.path)
        return _trim(path) == _trim(self.path)


@dataclass(frozen=True, slots=True)
class ResponseMatch:
    """Which of the page's responses an observation keeps: one origin, a few rules.

    ``origin`` is compared by scheme, host, and port against the response's own
    url, after :func:`~netkeeper.linkedin.strict_origin.parse_strict_origin` has
    refused anything ambiguous, so a lookalike host never matches.
    """

    origin: str
    rules: tuple[ResponseRule, ...]

    def __post_init__(self) -> None:
        try:
            parsed = parse_strict_origin(self.origin)
        except NotAStrictOrigin as exc:
            raise ValueError(f"{self.origin!r} is not an origin an observation can match") from exc
        object.__setattr__(self, "origin", str(parsed))
        object.__setattr__(self, "rules", tuple(self.rules))
        if not self.rules:
            raise ValueError("an observation with no rules would keep nothing")

    def matches(self, method: str, url: str) -> bool:
        try:
            split = urlsplit(url)
            want = urlsplit(self.origin)
            if (split.scheme, split.hostname, split.port) != (
                want.scheme,
                want.hostname,
                want.port,
            ):
                return False
        except ValueError:
            return False
        return any(rule.matches(method, split.path) for rule in self.rules)


@dataclass(frozen=True, slots=True)
class ObservedResponse:
    """One response the page received, as the observation kept it.

    ``body`` is ``None`` when it could not be kept -- a redirect has none, a body
    past the size limit is not kept, a read that failed or timed out has none --
    and ``failure`` then says which, in a fixed phrase. ``location`` is a redirect's
    ``Location`` header. ``request_body`` is what the page sent (the pagination
    request's JSON), read from the request, never changed. ``cause`` is set only
    for :data:`FAILURE_UNREADABLE`: what the read raised, as its class name and a
    fixed category (:func:`unreadable_cause`, e.g. ``"Error (no resource)"``), never
    its message, and ``diagnostics`` the fixed facts about the failed read (#200).

    Both bodies are left out of ``repr``: a response body is a person's data, and a
    ``repr`` ends up in tracebacks and log lines.
    """

    method: str
    url: str = field(repr=False)
    status: int
    resource_type: str
    request_body: str | None = field(repr=False)
    body: bytes | None = field(repr=False)
    location: str | None = field(default=None, repr=False)
    failure: str | None = None
    cause: str | None = None
    diagnostics: ReadDiagnostics | None = None

    def text(self) -> str | None:
        """The body as text (UTF-8, replacing what does not decode), or ``None``."""
        return None if self.body is None else self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ReadDiagnostics:
    """Fixed, non-identifying facts about one body read that failed (#200).

    Numbers and fixed words only: never a url, a header's own value (except the
    content length, a number), a body, or an exception's message.

    ``content_type`` and ``transfer_encoding`` are categories
    (:data:`CONTENT_TYPES`, ``"chunked"``, ``"none"``, ``"other"``). ``request_end``
    is ``"finished"``, ``"failed"``, or ``"neither"`` (no request event had arrived
    when the read failed); ``request_failure`` is the failed request's error as a
    category of :data:`UNREADABLE_CATEGORIES`, and ``None`` unless it failed.
    ``read_after_ms`` is the time from the ``response`` event to the start of the
    read, ``failed_after_ms`` to its failure. ``reads_in_flight`` is how many other
    body reads of this observation were under way when this one started.
    """

    from_service_worker: bool | None
    status: int
    content_type: str
    content_length: int | None
    transfer_encoding: str
    request_end: str
    request_failure: str | None
    read_after_ms: int
    failed_after_ms: int
    reads_in_flight: int

    def describe(self) -> str:
        """One line of ``key=value`` pairs: fixed words and numbers only."""
        worker = {True: "yes", False: "no", None: "unknown"}[self.from_service_worker]
        end = self.request_end
        if self.request_failure is not None:
            end = f"{end} ({self.request_failure})"
        length = "none" if self.content_length is None else str(self.content_length)
        return (
            f"service_worker={worker} status={self.status}"
            f" content_type={self.content_type} content_length={length}"
            f" transfer_encoding={self.transfer_encoding} request={end}"
            f" read_after_ms={self.read_after_ms} failed_after_ms={self.failed_after_ms}"
            f" reads_in_flight={self.reads_in_flight}"
        )


@dataclass(frozen=True, slots=True)
class ObservationLimits:
    """How much an observation may hold. See the module docstring."""

    max_pending: int = 16
    max_body_bytes: int = 8 * 1024 * 1024
    body_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if self.max_pending < 1 or self.max_body_bytes < 1 or self.body_timeout_s <= 0:
            raise ValueError("observation limits must be positive")


#: The fixed phrases :attr:`ObservedResponse.failure` carries.
FAILURE_REDIRECT: Final = "redirect: no body"
FAILURE_TOO_LARGE: Final = "body larger than the observation limit"
FAILURE_TIMEOUT: Final = "body did not arrive in time"
FAILURE_UNREADABLE: Final = "body could not be read"

#: The fixed categories :func:`unreadable_cause` sorts a failed body read into, each
#: with the lowercase fragments of an exception message that place it there, checked
#: in this order. Only the category is ever kept or logged, never the message.
#:
#: ``"no data"`` and ``"no resource"`` are Chrome's two different answers (#200):
#: "No data found for resource with given identifier" is a request Chrome knows but
#: kept no body for -- what it says for a streamed answer the page's own client
#: aborted after reading it -- and "No resource with given identifier found" is a
#: request it no longer knows at all.
UNREADABLE_CATEGORIES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("evicted", ("evicted",)),
    ("no data", ("no data found",)),
    ("no resource", ("no resource",)),
    ("aborted", ("aborted", "canceled", "cancelled")),
    ("closed", ("target closed", "has been closed", "target page")),
    ("network error", ("net::err_", "failed")),
)

#: The category of a failed body read that matches none of :data:`UNREADABLE_CATEGORIES`.
UNREADABLE_OTHER: Final = "unclassified"

#: The content types :class:`ReadDiagnostics` names, by their media type; any other is
#: ``"other"``, and a response without one ``"none"``.
CONTENT_TYPES: Final[Mapping[str, str]] = {
    "text/x-component": "x-component",
    "application/octet-stream": "octet-stream",
    "application/json": "json",
    "text/html": "html",
    "text/plain": "plain",
}


class Observation:
    """One tab's matching responses, in arrival order. Start it with ``BrowserRun.observe``.

    The listener is synchronous and cheap: it reads the method and the url, and for
    a match starts one task that reads the body. Tasks are queued in the order their
    responses arrived, and :meth:`next` hands them back in that order, so a slow
    body never lets a later response overtake it.
    """

    def __init__(
        self,
        match: ResponseMatch,
        page: ListenablePage,
        limits: ObservationLimits | None = None,
    ) -> None:
        self.match = match
        self.limits = ObservationLimits() if limits is None else limits
        self._page = page
        self._pending: collections.deque[asyncio.Task[ObservedResponse]] = collections.deque()
        self._arrived = asyncio.Event()
        self._overflowed = False
        self._listening = False
        self._closed = False
        self.kept = 0
        #: How each matching request ended, by request, as a request event said (#200).
        self._ends: dict[object, tuple[str, str | None]] = {}
        self._reading = 0
        self.unreadable: list[ReadDiagnostics] = []

    @property
    def page(self) -> ListenablePage:
        """The tab this observation listens to. A different tab is not being listened to."""
        return self._page

    @property
    def overflowed(self) -> bool:
        """Whether a matching response was dropped because too many waited unread."""
        return self._overflowed

    def start(self) -> None:
        """Begin listening. Idempotent; refuses after :meth:`close`."""
        if self._closed:
            raise ObservationFailed("this observation is closed")
        if not self._listening:
            self._page.on(RESPONSE_EVENT, self._on_response)
            self._page.on(REQUEST_FINISHED_EVENT, self._on_request_finished)
            self._page.on(REQUEST_FAILED_EVENT, self._on_request_failed)
            self._listening = True

    def _on_response(self, response: ResponseLike) -> None:
        """The listener. Reads the method and url; keeps a match; never touches the request."""
        if self._closed:
            return
        try:
            method = response.request.method
            url = response.url
        except Exception as exc:  # a response that cannot describe itself is not ours to read
            log.debug("observation: skipped a response that could not be read (%s)", _kind(exc))
            return
        if not self.match.matches(method, url):
            return
        if len(self._pending) >= self.limits.max_pending:
            self._overflowed = True
            log.warning(
                "observation: %d responses were waiting unread; dropped one", len(self._pending)
            )
            return
        self.kept += 1
        loop = asyncio.get_running_loop()
        self._pending.append(loop.create_task(self._read(response, method, loop.time())))
        self._arrived.set()

    def _on_request_finished(self, request: EndedRequestLike) -> None:
        self._request_ended(request, "finished")

    def _on_request_failed(self, request: EndedRequestLike) -> None:
        self._request_ended(request, "failed")

    def _request_ended(self, request: EndedRequestLike, how: str) -> None:
        """A listener: remember how a matching request ended. Reads, never touches it."""
        if self._closed:
            return
        try:
            if not self.match.matches(request.method, request.url):
                return
            failure = request.failure if how == "failed" else None
        except Exception as exc:
            log.debug(
                "observation: skipped a request event that could not be read (%s)", _kind(exc)
            )
            return
        category = None if failure is None else _category(failure)
        if how == "failed" and category is None:
            category = UNREADABLE_OTHER
        self._ends[request] = (how, category)
        while len(self._ends) > MAX_REMEMBERED_ENDS:
            del self._ends[next(iter(self._ends))]

    async def _read(self, response: ResponseLike, method: str, arrived: float) -> ObservedResponse:
        request = response.request
        status = response.status
        resource_type = _safe(lambda: request.resource_type) or "other"
        request_body = _safe(lambda: request.post_data)
        if 300 <= status < 400:
            headers = _safe(lambda: response.headers) or {}
            location = headers.get("location") if isinstance(headers, Mapping) else None
            return ObservedResponse(
                method=method,
                url=response.url,
                status=status,
                resource_type=resource_type,
                request_body=request_body,
                body=None,
                location=location if isinstance(location, str) else None,
                failure=FAILURE_REDIRECT,
            )
        body: bytes | None = None
        failure: str | None = None
        cause: str | None = None
        diagnostics: ReadDiagnostics | None = None
        loop = asyncio.get_running_loop()
        started = loop.time()
        in_flight = self._reading
        self._reading += 1
        try:
            body = await asyncio.wait_for(response.body(), self.limits.body_timeout_s)
        except TimeoutError:
            failure = FAILURE_TIMEOUT
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            cause = unreadable_cause(exc)
            log.debug("observation: a response body could not be read (%s)", cause)
            failure = FAILURE_UNREADABLE
            diagnostics = self._diagnose(
                response,
                status,
                read_after_ms=_ms(started - arrived),
                failed_after_ms=_ms(loop.time() - arrived),
                reads_in_flight=in_flight,
            )
            self.unreadable.append(diagnostics)
        finally:
            self._reading -= 1
        self._ends.pop(request, None)
        if body is not None and len(body) > self.limits.max_body_bytes:
            body, failure = None, FAILURE_TOO_LARGE
        return ObservedResponse(
            method=method,
            url=response.url,
            status=status,
            resource_type=resource_type,
            request_body=request_body,
            body=body,
            failure=failure,
            cause=cause,
            diagnostics=diagnostics,
        )

    def _diagnose(
        self,
        response: ResponseLike,
        status: int,
        *,
        read_after_ms: int,
        failed_after_ms: int,
        reads_in_flight: int,
    ) -> ReadDiagnostics:
        """The fixed facts about a read that failed (#200). Reads only; fixed words out."""
        headers = _safe(lambda: response.headers)
        if not isinstance(headers, Mapping):
            headers = {}
        worker = _safe(lambda: getattr(response, "from_service_worker", None))
        how, request_failure = self._ends.get(response.request, ("neither", None))
        return ReadDiagnostics(
            from_service_worker=worker if isinstance(worker, bool) else None,
            status=status,
            content_type=_content_type(headers.get("content-type")),
            content_length=_content_length(headers.get("content-length")),
            transfer_encoding=_transfer_encoding(headers.get("transfer-encoding")),
            request_end=how,
            request_failure=request_failure,
            read_after_ms=read_after_ms,
            failed_after_ms=failed_after_ms,
            reads_in_flight=reads_in_flight,
        )

    def summary(self) -> str:
        """One line for the log: how many answers were kept, and how the unreadable ones
        ended, in fixed words and counts (#200)."""
        ends: dict[str, int] = {}
        for item in self.unreadable:
            key = (
                item.request_end
                if item.request_failure is None
                else (f"{item.request_end} ({item.request_failure})")
            )
            ends[key] = ends.get(key, 0) + 1
        chunked = sum(1 for item in self.unreadable if item.transfer_encoding == "chunked")
        worker = sum(1 for item in self.unreadable if item.from_service_worker)
        how = ", ".join(f"{key}: {count}" for key, count in sorted(ends.items()))
        return (
            f"answers_kept={self.kept} bodies_unreadable={len(self.unreadable)}"
            f" unreadable_requests=[{how}] unreadable_chunked={chunked}"
            f" unreadable_from_service_worker={worker}"
        )

    async def next(self, timeout_s: float) -> ObservedResponse | None:
        """The next response in arrival order, or ``None`` if none arrives within ``timeout_s``.

        Raises :class:`ObservationFailed` once a response has been dropped: from that
        point the caller has not seen everything the page loaded.
        """
        if self._overflowed:
            raise ObservationFailed(
                "a matching response was dropped because too many waited unread;"
                " this run cannot know it saw everything the page loaded"
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while not self._pending:
            # A fresh event rather than clearing the old one: one waiter, and the
            # listener sets whichever is current. (It also keeps `clear`, a page input
            # in Playwright, out of this module for tests/test_browser_safety.py.)
            self._arrived = asyncio.Event()
            remaining = deadline - loop.time()
            if remaining <= 0 or self._closed:
                return None
            try:
                await asyncio.wait_for(self._arrived.wait(), remaining)
            except TimeoutError:
                return None
            if self._overflowed:
                raise ObservationFailed(
                    "a matching response was dropped because too many waited unread"
                )
        task = self._pending[0]
        remaining = max(deadline - loop.time(), 0.0) + self.limits.body_timeout_s
        try:
            result = await asyncio.wait_for(asyncio.shield(task), remaining)
        except TimeoutError:
            return None
        self._pending.popleft()
        return result

    async def close(self) -> None:
        """Stop listening and drop whatever is still being read. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._listening:
            for event, handler in (
                (RESPONSE_EVENT, self._on_response),
                (REQUEST_FINISHED_EVENT, self._on_request_finished),
                (REQUEST_FAILED_EVENT, self._on_request_failed),
            ):
                try:
                    self._page.remove_listener(event, handler)
                except Exception as exc:
                    log.debug("observation: removing a listener failed (%s)", _kind(exc))
            self._listening = False
            if self.kept:
                log.info("observation: %s", self.summary())
        pending, self._pending = list(self._pending), collections.deque()
        for task in pending:
            task.cancel()
        for task in pending:
            # A cancelled or failed read is being discarded either way.
            with contextlib.suppress(BaseException):
                await task
        self._arrived.set()

    async def __aenter__(self) -> Observation:
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


def _trim(path: str) -> str:
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _safe[T](read: Callable[[], T]) -> T | None:
    """A read-only property that may raise on a response the browser already dropped."""
    try:
        return read()
    except Exception:
        return None


def unreadable_cause(exc: BaseException) -> str:
    """Why a body read failed, as ``"<class name> (<category>)"``: fixed words only.

    The category is matched from the exception's message against
    :data:`UNREADABLE_CATEGORIES`; the message itself is never returned, because
    Playwright's can quote the url, and a profile url names a person.
    """
    try:
        message = str(exc)
    except Exception:
        message = ""
    return f"{_kind(exc)} ({_category(message) or UNREADABLE_OTHER})"


def _category(message: str) -> str | None:
    """The :data:`UNREADABLE_CATEGORIES` name ``message`` falls in, or ``None``."""
    lowered = message.lower()
    return next(
        (name for name, fragments in UNREADABLE_CATEGORIES if any(f in lowered for f in fragments)),
        None,
    )


def _content_type(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "none"
    return CONTENT_TYPES.get(value.split(";", 1)[0].strip().lower(), "other")


def _content_length(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip().isdigit():
        return None
    return int(value.strip())


def _transfer_encoding(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "none"
    return "chunked" if value.strip().lower() == "chunked" else "other"


def _ms(seconds: float) -> int:
    return max(round(seconds * 1000), 0)


def _kind(exc: BaseException) -> str:
    """An exception's class name, never its message: Playwright's messages quote urls."""
    return type(exc).__name__
