"""A second, read-only copy of the page's answers, streamed as they arrive (#200, ADR 0006).

Chrome keeps no body for an answer the page's own client aborted after reading it:
a streamed ``pagination`` answer the page has everything it needs from, cancelled
while the stream is still open. The page moves on with the data; ``response.body()``
(``Network.getResponseBody``) then fails with "No data found", and the run loses that
page of the list (#197, #200). Part B of #200 reproduced exactly that on an isolated
Chrome, and found one passive way to keep the bytes: ``Network.streamResourceContent``,
which has Chrome forward an answer's data to a DevTools client as it arrives, before
anything aborts it.

A :class:`BodyTap` is that client's bookkeeping. It holds no browser: it receives
CDP event parameters (plain mappings) from the one read-only CDP session
:meth:`netkeeper.linkedin.browser.BrowserRun.observe` opens for it, and calls back
through the one function it is given to ask Chrome to stream an answer it matched.
Nothing here sends, holds, changes, answers, or cancels a request: the session it
listens on enables the Network domain (events only) and asks for a matched answer's
data. What it keeps is used only when the page's own copy could not be read, and
only after the caller checks it against what the page asked for next
(:mod:`netkeeper.linkedin.page_connections`).

**Bounded.** At most :data:`MAX_STREAMS` answers are held at once (the oldest is
dropped), each at most the observation's body limit; an answer that grows past it is
kept as too large, which is no copy at all. Bodies never reach a log.

Nothing here imports the ORM or opens a session (spec 9.10).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from netkeeper.linkedin.observe import ResponseMatch

log = logging.getLogger(__name__)

#: The CDP events a tap listens to, in the order Chrome sends them for one answer.
REQUEST_EVENT: Final = "Network.requestWillBeSent"
RESPONSE_EVENT: Final = "Network.responseReceived"
DATA_EVENT: Final = "Network.dataReceived"
FINISHED_EVENT: Final = "Network.loadingFinished"
FAILED_EVENT: Final = "Network.loadingFailed"

#: The one way a failed answer may still have a whole copy (#202 review): the page's
#: own client cancelled it after reading it. Any other failure (a connection reset, a
#: length mismatch, a failure Chrome did not mark as a cancel) may have cut it short.
ABORTED_BY_PAGE: Final = "net::ERR_ABORTED"

#: The most answers a tap holds at once; the oldest is dropped past it.
MAX_STREAMS: Final = 32

#: Asks Chrome to stream one answer (``Network.streamResourceContent``) and returns
#: its parameters: ``bufferedData``, what had already arrived, base64-encoded.
StreamStart = Callable[[str], Awaitable[Mapping[str, Any]]]

_Key = tuple[str, str, str | None]


@dataclass(slots=True)
class _Stream:
    """One matched answer as the tap sees it."""

    key: _Key
    data: bytearray = field(default_factory=bytearray)
    too_large: bool = False
    #: Whether Chrome agreed to stream it; ``False`` when it refused (an answer that
    #: had already finished), ``None`` until it answered.
    streaming: bool | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    how: str | None = None
    #: ``loadingFailed``'s ``canceled`` and ``errorText``; a fixed Chrome error code,
    #: never logged.
    canceled: bool = False
    error: str | None = None

    @property
    def whole(self) -> bool:
        """Whether the stream ended in a way that leaves the copy whole: it finished,
        or the page's own client cancelled it (``net::ERR_ABORTED``)."""
        if self.how == "finished":
            return True
        return self.how == "failed" and self.canceled and self.error == ABORTED_BY_PAGE


class BodyTap:
    """The streamed copies of the answers ``match`` names, by request (see the module).

    Feed it the CDP events in :meth:`handlers`; a caller whose own read of an answer
    failed asks :meth:`take` for the streamed copy of the same request.
    """

    def __init__(
        self,
        match: ResponseMatch,
        *,
        stream: StreamStart,
        detach: Callable[[], Awaitable[None]] | None = None,
        max_body_bytes: int,
    ) -> None:
        self.match = match
        self._stream = stream
        self._detach = detach
        self._max_body_bytes = max_body_bytes
        self._streams: collections.OrderedDict[str, _Stream] = collections.OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        #: How many streamed copies were handed to a caller whose read had failed.
        self.handed = 0

    def handlers(self) -> tuple[tuple[str, Callable[[Mapping[str, Any]], None]], ...]:
        """Each CDP event a tap listens to, with its handler."""
        return (
            (REQUEST_EVENT, self.on_request),
            (RESPONSE_EVENT, self.on_response),
            (DATA_EVENT, self.on_data),
            (FINISHED_EVENT, self.on_finished),
            (FAILED_EVENT, self.on_failed),
        )

    # --- the listeners: read the event, never touch the request ----------------------

    def on_request(self, params: Mapping[str, Any]) -> None:
        if self._closed:
            return
        try:
            request_id = str(params["requestId"])
            request = params["request"]
            method = str(request["method"]).upper()
            url = str(request["url"])
            post_data = request.get("postData")
        except (KeyError, TypeError, AttributeError):
            return
        if not self.match.matches(method, url):
            return
        body = post_data if isinstance(post_data, str) else None
        self._streams[request_id] = _Stream(key=(method, url, body))
        self._streams.move_to_end(request_id)
        while len(self._streams) > MAX_STREAMS:
            self._streams.popitem(last=False)

    def on_response(self, params: Mapping[str, Any]) -> None:
        entry, request_id = self._entry(params)
        if entry is None or request_id is None or entry.streaming is not None:
            return
        task = asyncio.get_running_loop().create_task(self._start(request_id, entry))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start(self, request_id: str, entry: _Stream) -> None:
        try:
            answer = await self._stream(request_id)
            buffered = _decode(answer.get("bufferedData"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Chrome refuses an answer that has already finished loading; its own
            # copy is then whole, and nothing here is needed.
            log.debug("body tap: an answer was not streamed (%s)", type(exc).__name__)
            entry.streaming = False
        else:
            entry.data[:0] = buffered
            entry.streaming = True
            self._check_size(entry)
        finally:
            entry.started.set()

    def on_data(self, params: Mapping[str, Any]) -> None:
        entry, _ = self._entry(params)
        if entry is None or entry.too_large or entry.streaming is False:
            return
        data = params.get("data")
        if isinstance(data, str) and data:
            entry.data += _decode(data)
            self._check_size(entry)

    def on_finished(self, params: Mapping[str, Any]) -> None:
        self._end(params, "finished")

    def on_failed(self, params: Mapping[str, Any]) -> None:
        entry, _ = self._entry(params)
        if entry is not None:
            entry.canceled = params.get("canceled") is True
            error = params.get("errorText")
            entry.error = error if isinstance(error, str) else None
        self._end(params, "failed")

    def _end(self, params: Mapping[str, Any], how: str) -> None:
        entry, _ = self._entry(params)
        if entry is None:
            return
        entry.how = how
        entry.ended.set()

    def _entry(self, params: Mapping[str, Any]) -> tuple[_Stream | None, str | None]:
        if self._closed:
            return None, None
        try:
            request_id = str(params["requestId"])
        except (KeyError, TypeError):
            return None, None
        return self._streams.get(request_id), request_id

    def _check_size(self, entry: _Stream) -> None:
        if len(entry.data) > self._max_body_bytes:
            entry.too_large = True
            entry.data = bytearray()

    # --- the caller's side --------------------------------------------------------------

    async def take(
        self, method: str, url: str, post_data: str | None, *, wait_s: float
    ) -> bytes | None:
        """The streamed copy of the oldest held answer to this request, or ``None``.

        Waits up to ``wait_s`` for Chrome to say whether it streamed the answer and
        for the answer to end. The copy is handed over once and forgotten. ``None``
        when the tap never matched the request, Chrome did not stream it, the answer
        had not ended in time, it ended by any failure but the page's own cancel
        (``canceled`` with ``net::ERR_ABORTED``), or it grew past the body limit.
        """
        request_id = self._oldest((method.upper(), url, post_data))
        if request_id is None:
            return None
        entry = self._streams.pop(request_id)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait_s):
                await entry.started.wait()
                await entry.ended.wait()
        if not (entry.streaming and entry.ended.is_set() and entry.whole) or entry.too_large:
            return None
        self.handed += 1
        return bytes(entry.data)

    def discard(self, method: str, url: str, post_data: str | None) -> None:
        """Forget the oldest held answer to this request: the caller read its own copy."""
        request_id = self._oldest((method.upper(), url, post_data))
        if request_id is not None:
            del self._streams[request_id]

    def _oldest(self, key: _Key) -> str | None:
        return next((rid for rid, entry in self._streams.items() if entry.key == key), None)

    async def close(self) -> None:
        """Stop, drop every held copy, and detach the session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._streams = collections.OrderedDict()
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        if self._detach is not None:
            try:
                await self._detach()
            except Exception as exc:
                log.debug("body tap: detaching failed (%s)", type(exc).__name__)


def _decode(data: object) -> bytes:
    if not isinstance(data, str):
        return b""
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return b""
