"""The SSE stream, driven at the ASGI level.

httpx's ASGI transport and Starlette's TestClient both buffer the whole body, so
an endless stream would never return. Calling the app directly lets the test read
the first frames and then disconnect.
"""

import asyncio
import json
from collections.abc import MutableMapping
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool

from netkeeper.services.events import Event, EventBus

Message = MutableMapping[str, Any]
WAIT_S = 2.0


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

    def start(self) -> None:
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
            "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")],
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

    async def close(self) -> None:
        self._disconnect.set()
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout=WAIT_S)


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


async def test_published_events_arrive_on_the_stream(
    running_app: FastAPI, bare_engine: Engine
) -> None:
    bus: EventBus = running_app.state.bus
    stream = SSEClient(running_app)
    stream.start()
    await asyncio.wait_for(stream.started.wait(), timeout=WAIT_S)

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
