"""``/tasks``: the enqueue-and-return diagnostic (spec section 5).

``POST /tasks/ping`` is the smallest route of the shape every browser-touching
route will have: submit a task, answer ``202`` before the work runs, and let the
task report over the event bus. ``GET /tasks/{task_id}`` reads the outcome.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from netkeeper.web.deps import CurrentUser, Tasks
from netkeeper.web.schemas import TaskAccepted, TaskOut

router = APIRouter(prefix="/tasks", tags=["tasks"])

PING_STEPS = 2
PING_STEP_DELAY_S = 0.05


@router.post("/ping", operation_id="ping_task", status_code=202)
async def ping_task(user: CurrentUser, tasks: Tasks) -> TaskAccepted:
    """Submit a tiny task that reports progress twice, and return its id at once."""

    async def run() -> None:
        for step in range(1, PING_STEPS + 1):
            await asyncio.sleep(PING_STEP_DELAY_S)
            tasks.progress({"step": step, "of": PING_STEPS})

    info = tasks.submit("ping", run, user_id=user.id)
    return TaskAccepted(task_id=info.id)


@router.get("/{task_id}", operation_id="get_task", responses={404: {"description": "No such task"}})
async def get_task(task_id: str, user: CurrentUser, tasks: Tasks) -> TaskOut:
    info = tasks.get(task_id)
    if info is None or info.user_id != user.id:
        raise HTTPException(status_code=404, detail="no such task")
    return TaskOut.model_validate(info)
