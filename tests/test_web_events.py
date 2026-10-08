"""The SSE stream, driven at the ASGI level.

httpx's ASGI transport and Starlette's TestClient both buffer the whole body, so
an endless stream would never return. Calling the app directly lets the test read
the first frames and then disconnect.
"""

import asyncio
import gc
import json
import signal
import sys
import threading
import traceback
from collections.abc import Callable, MutableMapping
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool
from time_limit import Stopwatch, scaled

from netkeeper.services.events import Event, EventBus

Message = MutableMapping[str, Any]
#: How long a step of the stream may take; it takes milliseconds. The test runs with
#: garbage collection held (``wall_clock``), since one full collection of an xdist
#: worker's heap took 2-3 s on CI and used to land in this wait (#472).
WAIT_S = scaled(2.0)


class SSEClient:
    """One ``GET /api/v1/events`` call: parsed frames in, a disconnect out."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._buffer = b""
        self.started = asyncio.Event()
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None
        self.watch = Stopwatch()  # restarted by start()

    def start(self) -> None:
        self.watch = Stopwatch()
        scope: Message = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"127.0.0.1"), (b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))

    async def _receive(self) -> Message:
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self.started.set()
        elif message["type"] == "http.response.body":
            self._chunks.put_nowait(message.get("body", b""))

    async def next_event(self) -> tuple[str, dict[str, Any]]:
        """The next named event as ``(name, parsed data)``; comments (pings) are skipped."""
        try:
            async with asyncio.timeout(WAIT_S):
                while True:
                    frame, sep, rest = self._buffer.partition(b"\r\n\r\n")
                    if sep:
                        self._buffer = rest
                        parsed = _parse_frame(frame)
                        if parsed is not None:
                            return parsed
                        continue
                    self._buffer += await self._chunks.get()
        except TimeoutError:
            self.report()
            raise

    def report(self, file: Any = sys.stderr) -> None:
        """Print why the stream stalled: the app task's state, and every task and thread stack.

        Each part is printed on its own, and one that raises (sse-starlette's state is
        private, so an upgrade can move it) is reported and skipped, so the test still
        fails with its TimeoutError.
        """
        parts: list[tuple[str, Callable[[], None]]] = [
            ("app task", lambda: self._print_task(file)),
            ("timing", lambda: print("since the request started:", self.watch, file=file)),
            ("thread limiter", lambda: _print_thread_limiter(file)),
            ("sse-starlette", lambda: print("sse-starlette:", _sse_state(), file=file)),
            ("tasks", lambda: _print_tasks(file)),
            ("threads", lambda: _print_threads(file)),
        ]
        for name, part in parts:
            try:
                part()
            except Exception as exc:  # a diagnostic must never replace the failure
                print(f"({name}: could not print, {exc!r})", file=file)

    def _print_task(self, file: Any) -> None:
        task = self._task
        print("app task:", task, "status:", self.status, file=file)
        if task is not None and task.done() and not task.cancelled():
            exc = task.exception()
            print("app task exception:", repr(exc), file=file)
            if exc is not None:
                traceback.print_exception(exc, file=file)

    async def close(self) -> None:
        self._disconnect.set()
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout=WAIT_S)


def _print_await_chain(awaitable: Any, file: Any) -> None:
    """Each frame a task is suspended in, outermost first, and what the innermost awaits.

    ``Task.print_stack`` shows only the task's outermost coroutine frame.
    """
    while awaitable is not None:
        if type(awaitable).__name__ == "async_generator_asend":
            # An ``asend`` awaitable hides its generator; find it among its referents.
            agens = [r for r in gc.get_referents(awaitable) if hasattr(r, "ag_frame")]
            if agens:
                awaitable = agens[0]
        frame = (
            getattr(awaitable, "cr_frame", None)
            or getattr(awaitable, "gi_frame", None)
            or getattr(awaitable, "ag_frame", None)
        )
        if frame is None:
            print("  awaiting:", repr(awaitable), file=file)
            return
        code = frame.f_code
        print(f"  {code.co_filename}:{frame.f_lineno} in {code.co_name}", file=file)
        awaitable = (
            getattr(awaitable, "cr_await", None)
            or getattr(awaitable, "gi_yieldfrom", None)
            or getattr(awaitable, "ag_await", None)
        )


def _print_thread_limiter(file: Any) -> None:
    """anyio's default thread limiter: a request stuck waiting for a thread shows here."""
    import anyio.to_thread

    print(
        "thread limiter:", anyio.to_thread.current_default_thread_limiter().statistics(), file=file
    )


def _print_tasks(file: Any) -> None:
    for task in asyncio.all_tasks():
        print("task:", task.get_name(), file=file)
        _print_await_chain(task.get_coro(), file)


def _print_threads(file: Any) -> None:
    print("threads:", [t.name for t in threading.enumerate()], file=file)
    for tid, frame in sys._current_frames().items():
        print("thread stack", tid, file=file)
        traceback.print_stack(frame, file=file)


def _sse_state() -> dict[str, Any]:
    """sse-starlette's process-wide shutdown state, which ends every stream once set."""
    from sse_starlette import sse

    state = getattr(sse._thread_state, "shutdown_state", None)
    return {
        "should_exit": sse.AppStatus.should_exit,
        "watcher_started": None if state is None else state.watcher_started,
        "events": None if state is None else len(state.events),
        "sigterm": repr(signal.getsignal(signal.SIGTERM)),
    }


def _parse_frame(frame: bytes) -> tuple[str, dict[str, Any]] | None:
    name: str | None = None
    data: list[str] = []
    for line in frame.decode().split("\r\n"):
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)
    if name is None:
        return None
    parsed: dict[str, Any] = json.loads("\n".join(data))
    return name, parsed


@pytest.mark.wall_clock
async def test_published_events_arrive_on_the_stream(
    running_app: FastAPI, bare_engine: Engine
) -> None:
    bus: EventBus = running_app.state.bus
    stream = SSEClient(running_app)
    stream.start()
    try:
        await asyncio.wait_for(stream.started.wait(), timeout=WAIT_S)
    except TimeoutError:
        stream.report()
        raise

    assert stream.status == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert bus.subscriber_count == 1
    # The request session closed before streaming began (Depends scope="function"),
    # so the stream holds no SQLite connection or read transaction open.
    assert isinstance(bare_engine.pool, QueuePool)
    assert bare_engine.pool.checkedout() == 0

    bus.publish(Event(type="demo.other", data={"n": 0}, user_id=999))  # not this user's
    bus.publish(Event(type="demo.hello", data={"n": 1}, user_id=1))
    name, payload = await stream.next_event()
    assert name == "demo.hello"
    assert payload["type"] == "demo.hello"
    assert payload["data"] == {"n": 1}
    assert payload["user_id"] == 1
    assert payload["ts"].endswith("+00:00")

    bus.publish(Event(type="demo.everyone", user_id=None))
    assert (await stream.next_event())[0] == "demo.everyone"

    await stream.close()
    assert bus.subscriber_count == 0
