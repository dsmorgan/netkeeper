"""The task runner: routes enqueue work here and return (spec sections 5 and 14.1).

Nothing long-running executes inside a request handler. :meth:`TaskRunner.submit`
schedules a coroutine on the loop and returns a :class:`TaskInfo` at once; the
task's lifecycle goes out on the :class:`EventBus` as ``task.started``,
``task.progress``, ``task.finished``, and ``task.failed`` so the UI can follow it
over SSE.

This is the skeleton: one in-process runner, no persistence, no activity lock.
The browser activity lock (one per LinkedIn account) arrives with the extractor.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from netkeeper.models.base import utcnow
from netkeeper.services.events import Event, EventBus

log = logging.getLogger(__name__)

TaskStatus = Literal["pending", "running", "succeeded", "failed"]
DEFAULT_HISTORY = 200

# Set inside each task's context so progress() knows who is calling. Child tasks
# spawned by a task inherit it.
_current_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "netkeeper_task_id", default=None
)


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """A snapshot of one task. :meth:`TaskRunner.get` returns the current one."""

    id: str
    name: str
    user_id: int
    status: TaskStatus
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class TaskRunner:
    """Runs submitted coroutines as asyncio tasks and remembers how they went.

    The most recently finished ``history`` tasks stay readable through :meth:`get`;
    tasks that are still pending or running are never forgotten. Not thread-safe: call
    from the event loop thread.
    """

    def __init__(self, bus: EventBus, *, history: int = DEFAULT_HISTORY) -> None:
        self._bus = bus
        self._history = history
        self._tasks: dict[str, TaskInfo] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._finished: deque[str] = deque()  # ids in completion order, oldest first

    def submit(
        self, name: str, factory: Callable[[], Awaitable[None]], *, user_id: int
    ) -> TaskInfo:
        """Schedule ``factory()`` and return its pending :class:`TaskInfo` at once.

        Needs a running event loop: call it from an ``async def`` route, not from a
        sync one (those run in a worker thread).
        """
        info = TaskInfo(
            id=uuid.uuid4().hex,
            name=name,
            user_id=user_id,
            status="pending",
            error=None,
            created_at=utcnow(),
            finished_at=None,
        )
        self._tasks[info.id] = info
        self._running[info.id] = asyncio.create_task(
            self._run(info.id, factory), name=f"netkeeper-task-{name}-{info.id[:8]}"
        )
        log.info("task %s (%s) submitted for user %d", info.id, name, user_id)
        return info

    def get(self, task_id: str) -> TaskInfo | None:
        """The current snapshot of a task, or None if unknown or pruned."""
        return self._tasks.get(task_id)

    def progress(self, data: Mapping[str, object]) -> None:
        """Publish ``task.progress`` for the task this is called from.

        Raises :class:`RuntimeError` outside a submitted task.
        """
        task_id = _current_task_id.get()
        if task_id is None:
            raise RuntimeError("TaskRunner.progress() must be called from inside a submitted task")
        self._publish("task.progress", self._tasks[task_id], dict(data))

    async def join(self) -> None:
        """Wait for every running task to finish (tests and shutdown flushes)."""
        running = list(self._running.values())
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def cancel_all(self) -> None:
        """Cancel every running task and wait for them to wind down (shutdown)."""
        running = list(self._running.values())
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def _run(self, task_id: str, factory: Callable[[], Awaitable[None]]) -> None:
        _current_task_id.set(task_id)
        info = self._update(task_id, status="running", error=None, finished_at=None)
        self._publish("task.started", info)
        try:
            await factory()
        except asyncio.CancelledError:
            info = self._update(task_id, status="failed", error="cancelled", finished_at=utcnow())
            log.warning("task %s (%s) cancelled", task_id, info.name)
            self._publish("task.failed", info, {"error": "cancelled"})
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("task %s (%s) failed", task_id, info.name)
            info = self._update(task_id, status="failed", error=error, finished_at=utcnow())
            self._publish("task.failed", info, {"error": error})
        else:
            info = self._update(task_id, status="succeeded", error=None, finished_at=utcnow())
            log.info("task %s (%s) finished", task_id, info.name)
            self._publish("task.finished", info)
        finally:
            self._running.pop(task_id, None)
            self._finished.append(task_id)
            self._prune()

    def _update(
        self, task_id: str, *, status: TaskStatus, error: str | None, finished_at: datetime | None
    ) -> TaskInfo:
        info = replace(self._tasks[task_id], status=status, error=error, finished_at=finished_at)
        self._tasks[task_id] = info
        return info

    def _publish(self, type_: str, info: TaskInfo, extra: dict[str, object] | None = None) -> None:
        data: dict[str, object] = {"task_id": info.id, "name": info.name}
        if extra:
            data.update(extra)
        self._bus.publish(Event(type=type_, data=data, user_id=info.user_id))

    def _prune(self) -> None:
        while len(self._finished) > self._history:
            del self._tasks[self._finished.popleft()]
