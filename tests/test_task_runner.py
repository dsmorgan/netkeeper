import asyncio
import logging

import pytest

from netkeeper.services.events import Event, EventBus, Subscription
from netkeeper.services.tasks import DEFAULT_HISTORY, TaskRunner


async def _drain(bus: EventBus, subscription: Subscription) -> list[Event]:
    bus.unsubscribe(subscription)
    return [event async for event in subscription]


async def _noop() -> None:
    pass


async def test_submit_returns_at_once_and_the_task_succeeds() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    runner = TaskRunner(bus)
    ran = asyncio.Event()

    async def work() -> None:
        ran.set()

    info = runner.submit("demo", work, user_id=1)
    assert not ran.is_set()  # nothing ran inside submit
    assert info.status == "pending"
    assert len(info.id) == 32
    assert info.error is None
    assert info.finished_at is None
    assert info.created_at.tzinfo is not None

    await runner.join()
    assert ran.is_set()
    assert info.status == "pending"  # a snapshot; the runner hands out new ones
    done = runner.get(info.id)
    assert done is not None
    assert done.status == "succeeded"
    assert done.error is None
    assert done.finished_at is not None
    assert done.finished_at.tzinfo is not None

    events = await _drain(bus, subscription)
    assert [event.type for event in events] == ["task.started", "task.finished"]
    for event in events:
        assert event.data == {"task_id": info.id, "name": "demo"}
        assert event.user_id == 1


async def test_failure_is_recorded_logged_and_contained(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    runner = TaskRunner(bus)

    async def boom() -> None:
        raise RuntimeError("kaboom")

    info = runner.submit("boom", boom, user_id=1)
    with caplog.at_level(logging.ERROR, logger="netkeeper.services.tasks"):
        await runner.join()  # the exception does not escape

    done = runner.get(info.id)
    assert done is not None
    assert done.status == "failed"
    assert done.error == "RuntimeError: kaboom"
    assert done.finished_at is not None

    events = await _drain(bus, subscription)
    assert [event.type for event in events] == ["task.started", "task.failed"]
    assert events[1].data == {"task_id": info.id, "name": "boom", "error": "RuntimeError: kaboom"}
    assert any(f"task {info.id} (boom) failed" in record.getMessage() for record in caplog.records)


async def test_progress_carries_the_task_identity() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    runner = TaskRunner(bus)

    async def work() -> None:
        runner.progress({"step": 1})
        await asyncio.sleep(0)
        runner.progress({"step": 2})

    info = runner.submit("steps", work, user_id=3)
    await runner.join()

    events = await _drain(bus, subscription)
    assert [event.type for event in events] == [
        "task.started",
        "task.progress",
        "task.progress",
        "task.finished",
    ]
    progress = [event for event in events if event.type == "task.progress"]
    assert [event.data["step"] for event in progress] == [1, 2]
    assert all(event.data["task_id"] == info.id for event in progress)
    assert all(event.user_id == 3 for event in events)


def test_progress_outside_a_task_is_an_error() -> None:
    runner = TaskRunner(EventBus())
    with pytest.raises(RuntimeError, match="inside a submitted task"):
        runner.progress({"step": 1})


async def test_history_keeps_only_the_newest_finished_tasks() -> None:
    assert DEFAULT_HISTORY == 200
    runner = TaskRunner(EventBus(), history=3)
    ids = [runner.submit(f"t{n}", _noop, user_id=1).id for n in range(5)]
    await runner.join()

    assert [runner.get(task_id) is None for task_id in ids] == [True, True, False, False, False]
    assert runner.get("nope") is None


async def test_running_tasks_are_never_pruned() -> None:
    runner = TaskRunner(EventBus(), history=1)
    gate = asyncio.Event()

    async def wait() -> None:
        await gate.wait()

    slow = runner.submit("slow", wait, user_id=1)
    quick = [runner.submit("quick", _noop, user_id=1).id for _ in range(3)]
    await asyncio.sleep(0.01)  # the quick ones finish on the next loop iteration

    assert [runner.get(task_id) is None for task_id in quick] == [True, True, False]
    still = runner.get(slow.id)
    assert still is not None
    assert still.status == "running"

    gate.set()
    await runner.join()
    finished = runner.get(slow.id)
    assert finished is not None
    assert finished.status == "succeeded"


async def test_cancel_all_marks_running_tasks_failed() -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    runner = TaskRunner(bus)
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    info = runner.submit("forever", forever, user_id=1)
    await started.wait()
    await runner.cancel_all()

    done = runner.get(info.id)
    assert done is not None
    assert done.status == "failed"
    assert done.error == "cancelled"
    assert done.finished_at is not None
    events = await _drain(bus, subscription)
    assert [event.type for event in events] == ["task.started", "task.failed"]
    assert events[1].data["error"] == "cancelled"

    await runner.cancel_all()  # nothing running: fine
