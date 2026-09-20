"""``/events``: the SSE stream the UI subscribes to once (spec 14.1).

Every :class:`~netkeeper.services.events.Event` on the bus becomes one SSE message:
the event name is ``event.type`` and the data is the event's JSON. A ping comment
goes out every 15 seconds so proxies and the browser keep the connection open.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask

from netkeeper.services.events import EventBus, Subscription
from netkeeper.web.deps import Bus, CurrentUser

router = APIRouter(tags=["events"])

PING_INTERVAL_S = 15


@router.get(
    "/events",
    operation_id="stream_events",
    response_class=EventSourceResponse,
    summary="Server-sent events: task progress, run status, and health",
    responses={
        200: {
            "description": "An SSE stream; each message is named by the event type.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def stream_events(user: CurrentUser, bus: Bus) -> EventSourceResponse:
    # Subscribe before the response starts so nothing published in between is lost.
    subscription = bus.subscribe()
    return EventSourceResponse(
        _frames(bus, subscription, user.id),
        ping=PING_INTERVAL_S,
        background=BackgroundTask(bus.unsubscribe, subscription),
    )


async def _frames(
    bus: EventBus, subscription: Subscription, user_id: int
) -> AsyncIterator[ServerSentEvent]:
    try:
        async for event in subscription:
            if event.user_id is not None and event.user_id != user_id:
                continue
            payload = json.dumps(event.to_dict(), separators=(",", ":"), ensure_ascii=False)
            yield ServerSentEvent(data=payload, event=event.type)
    finally:
        bus.unsubscribe(subscription)
