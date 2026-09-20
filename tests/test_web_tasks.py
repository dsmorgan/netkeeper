import asyncio

import httpx
from fastapi import FastAPI

from netkeeper.services.events import EventBus
from netkeeper.services.tasks import TaskRunner

CSRF = {"X-Netkeeper-Client": "1"}


async def test_ping_answers_202_before_the_work_runs(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    bus: EventBus = running_app.state.bus
    runner: TaskRunner = running_app.state.tasks
    subscription = bus.subscribe()

    accepted = await client.post("/api/v1/tasks/ping", headers=CSRF)
    assert accepted.status_code == 202
    task_id = accepted.json()["task_id"]
    assert len(task_id) == 32

    early = await client.get(f"/api/v1/tasks/{task_id}")
    assert early.status_code == 200
    assert early.json()["status"] in {"pending", "running"}

    await asyncio.wait_for(runner.join(), timeout=5)

    done = await client.get(f"/api/v1/tasks/{task_id}")
    assert done.status_code == 200
    body = done.json()
    assert body["id"] == task_id
    assert body["name"] == "ping"
    assert body["status"] == "succeeded"
    assert body["error"] is None
    assert body["finished_at"] is not None
    assert body["created_at"].endswith("Z") or "+00:00" in body["created_at"]

    bus.unsubscribe(subscription)
    events = [event async for event in subscription]
    assert [event.type for event in events] == [
        "task.started",
        "task.progress",
        "task.progress",
        "task.finished",
    ]
    assert {event.data["task_id"] for event in events} == {task_id}
    assert [event.data["step"] for event in events if event.type == "task.progress"] == [1, 2]
    assert {event.user_id for event in events} == {1}


async def test_unknown_task_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/tasks/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such task"}
