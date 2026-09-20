"""The in-process event bus behind the SSE stream (spec section 14.1).

Pure asyncio, no framework types. Publishers call :meth:`EventBus.publish` from
the event loop; every subscriber reads its own bounded queue, so a slow consumer
loses its oldest events instead of stalling the publisher or the other subscribers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Self

from netkeeper.models.base import utcnow

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 256


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, in the shape the UI receives over SSE.

    ``user_id`` is the user the event belongs to; None means everyone (for example
    process-wide health). ``ts`` must be timezone-aware.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=utcnow)
    user_id: int | None = None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("Event.ts must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        """The JSON-ready wire shape."""
        return {
            "type": self.type,
            "data": self.data,
            "ts": self.ts.isoformat(),
            "user_id": self.user_id,
        }


class Subscription:
    """One subscriber's view of the bus: an async iterator over its own queue.

    Created by :meth:`EventBus.subscribe`. Iteration ends after
    :meth:`EventBus.unsubscribe`, once whatever was already queued has been read.
    ``dropped`` counts events lost to overflow.
    """

    def __init__(self, maxsize: int) -> None:
        # Unbounded queue, bound enforced in _offer, so the end-of-stream sentinel
        # never displaces an event.
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._maxsize = maxsize
        self._closed = False
        self._exhausted = False
        self.dropped = 0

    def _offer(self, event: Event) -> None:
        if self._closed:
            return
        if self._queue.qsize() >= self._maxsize:
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(event)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)  # sentinel: end of stream after the backlog

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        if self._exhausted:
            raise StopAsyncIteration
        event = await self._queue.get()
        if event is None:
            self._exhausted = True
            raise StopAsyncIteration
        return event


class EventBus:
    """Fan-out of :class:`Event` to any number of subscribers.

    Not thread-safe: publish and subscribe from the event loop thread.
    """

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to every current subscriber without blocking."""
        log.debug(
            "event %s (user %s) to %d subscribers",
            event.type,
            event.user_id,
            len(self._subscribers),
        )
        for subscription in list(self._subscribers):
            subscription._offer(event)

    def subscribe(self) -> Subscription:
        """Start receiving events published from now on."""
        subscription = Subscription(self._queue_size)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Stop delivering to ``subscription``; its iterator ends after the backlog. Idempotent."""
        self._subscribers.discard(subscription)
        subscription._close()
