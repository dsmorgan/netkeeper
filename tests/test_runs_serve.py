"""``netkeeper serve`` never contacts LinkedIn on its own until a person arms it (P2-10).

The maintainer runs ``netkeeper serve`` against their real, logged-in Chrome
every day. Scheduled LinkedIn runs therefore ship disarmed, and this module
drives the real ``serve`` startup path -- :func:`netkeeper.web.app.create_app`'s
lifespan with the extractor ``netkeeper serve`` hands it, the real scheduler,
the real worker, on a fake Chrome and a fake clock -- across every due time a
week holds (the first-setup full sync, the daily incremental, enrichment every
three hours, a catch-up after downtime) and asserts the browser is never
attached. The anti-coincidence test runs the same drive armed and sees it attach.

Every path the review asked about is walked here: the lifespan, first setup,
catch-up after a restart, retry parking, a restart with a run left ``running``
(nothing replays it), the SSE stream and every ``GET`` (read only).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from browser_fakes import FakeConnector
from fastapi import FastAPI
from run_fakes import Clock, ConnectionsContext, Gate, fake_provider, worker_extractor
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_web_events import SSEClient

from netkeeper.config import Settings
from netkeeper.db import make_session_factory, session_scope
from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import SyncRun, SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.scoping import install_scope_guard, scoped
from netkeeper.services import heat, route_breaker, runs, scheduler
from netkeeper.services.linkedin_accounts import (
    arm_scheduled_runs,
    ensure_account,
    scheduled_runs_armed,
)
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.scheduled_runs import ServeExtractor, serve_registry
from netkeeper.services.settings_kv import delete_setting
from netkeeper.web.app import create_app
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

#: These tests start runs by hand at whatever time the suite runs (#213).
pytestmark = pytest.mark.usefixtures("inside_active_hours")

#: Wednesday 2026-09-23, 02:00 in New York: before the active window opens, so
#: the first day's due times are all snapped to 08:30 and then land in it.
START = datetime(2026, 9, 23, 6, 0, tzinfo=UTC)
STEP = timedelta(hours=1)
HEADERS = {CLIENT_HEADER: CLIENT_HEADER_VALUE}


@pytest.fixture
def settings() -> Settings:
    return Settings()


@asynccontextmanager
async def serving(
    engine: Engine, settings: Settings, extractor: ServeExtractor
) -> AsyncIterator[FastAPI]:
    """The app ``netkeeper serve`` runs, inside its lifespan, on ``engine``."""
    app = create_app(settings, engine=engine, extractor=extractor)
    async with app.router.lifespan_context(app):
        yield app


def served(
    engine: Engine, settings: Settings, provider: AttachBrowserProvider, clock: Clock
) -> Any:
    """:func:`serving` with the worker ``netkeeper serve`` builds, on ``provider``."""
    return serving(engine, settings, worker_extractor(provider, settings, clock=clock))


async def heartbeat(app: FastAPI) -> None:
    """One tick of the scheduler ``serve`` started: its real heartbeat job, run now."""
    job = app.state.scheduler.get_job(scheduler.HEARTBEAT_JOB_ID)
    assert job is not None
    await job.func()


async def drive(app: FastAPI, clock: Clock, until: datetime) -> None:
    while clock.at < until:
        clock.at += STEP
        await heartbeat(app)
        await app.state.tasks.join()


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


def _rows(engine: Engine) -> list[SyncRun]:
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory) as session:
        found: list[SyncRun] = []
        for user in session.scalars(select(User)):
            found.extend(session.scalars(scoped(user, SyncRun)))
        for run in found:
            session.expunge(run)
        return found


def _local(session: Session) -> User:
    user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
    return user


@pytest.fixture
def no_frontend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))


# --- THE property ------------------------------------------------------------------


async def _a_week_of_serve(
    engine: Engine, settings: Settings, *, arm: bool
) -> tuple[FakeConnector, ConnectionsContext, Clock]:
    """Serve for four days, stop for two (downtime), serve for three more, polling
    every twenty minutes, with every GET the page makes once a day."""
    context = ConnectionsContext()
    provider, connector = fake_provider(context)
    clock = Clock(START)
    async with served(engine, settings, provider, clock) as app:
        if arm:
            async with client_for(app) as client:
                armed = await client.post(
                    "/api/v1/linkedin/schedule/arm", json={"confirm": True}, headers=HEADERS
                )
                assert armed.status_code == 200 and armed.json()["armed"] is True
        for _ in range(4):
            await drive(app, clock, clock.at + timedelta(days=1))
            await _every_get(app)
    clock.at += timedelta(days=2)  # the process is down; everything due lapses
    async with served(engine, settings, provider, clock) as app:
        await drive(app, clock, clock.at + timedelta(days=3))
        await _every_get(app)
    return connector, context, clock


async def _every_get(app: FastAPI) -> None:
    async with client_for(app) as client:
        for path in (
            "/api/v1/linkedin/status",
            "/api/v1/linkedin/runs",
            "/api/v1/linkedin/schedule",
            "/api/v1/linkedin/budget",
            "/api/v1/linkedin/heat",
            "/api/v1/linkedin/pins",
            "/api/v1/me",
        ):
            response = await client.get(path)
            assert response.status_code == 200, (path, response.text)


@pytest.mark.slow
async def test_a_disarmed_serve_never_touches_the_browser_across_a_week(
    bare_engine: Engine,
    settings: Settings,
    no_frontend: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The week, then each of the three checks attacked with the ones in front of it gone.

    1. The drive itself: every due fire the real heartbeat reached reports
       ``skipped_reason == "disarmed"`` -- the scheduler's gate, not a later check,
       stopped it -- and nothing logs an error.
    2. As if the gate had let a fire through: ``serve``'s own handlers are called
       directly. ``create_run`` refuses, so no run is recorded.
    3. As if ``create_run`` had recorded one too: a scheduled row is written by hand
       and handed to the worker. It refuses before it attaches.

    Each check, removed on its own, turns this test red (#175 review, F1).
    """
    fires: list[scheduler.FireResult] = []
    real_poll = scheduler.poll_and_fire

    async def spy(*args: Any, **kwargs: Any) -> scheduler.FireResult | None:
        result = await real_poll(*args, **kwargs)
        if result is not None:
            fires.append(result)
        return result

    monkeypatch.setattr(scheduler, "poll_and_fire", spy)
    caplog.set_level(logging.WARNING)

    connector, context, clock = await _a_week_of_serve(bare_engine, settings, arm=False)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []
    assert fires, "the heartbeat never reached a due fire"
    assert {(f.fired, f.skipped_reason) for f in fires} == {(False, "disarmed")}
    assert {f.kind for f in fires} == set(scheduler.SERVED_SCHEDULES)
    assert connector.attaches == 0
    assert context.pages == [] and context.fetches == []
    assert _rows(bare_engine) == []  # not even a refused scheduled run was recorded
    factory = make_session_factory(bare_engine)
    with session_scope(factory) as session:
        user = _local(session)
        account = ensure_account(session, user).id
        assert not scheduled_runs_armed(session, user, account)
        full = scheduler.stored_due(session, user, account, scheduler.JobKind.CONNECTIONS_FULL)
        enrich = scheduler.stored_due(session, user, account, scheduler.JobKind.ENRICH)
        inbox = scheduler.stored_due(session, user, account, scheduler.JobKind.INBOX)
    assert full is not None and full > clock.at - timedelta(days=1)
    assert enrich is not None and enrich > START + timedelta(days=8)
    assert inbox is None  # no page source yet (P4-01), not scheduled

    # 2 and 3: past the gate, then past create_run too.
    provider, connector = fake_provider()
    async with served(bare_engine, settings, provider, clock) as app:
        factory = app.state.session_factory
        with session_scope(factory) as session:
            user = _local(session)
            user_id, account = user.id, ensure_account(session, user).id
        registry = serve_registry(factory, app.state.executor, app.state.tasks, clock=clock)
        for kind, handler in registry.items():
            outcome = await handler(
                scheduler.JobContext(
                    user_id=user_id, account_id=account, kind=kind, due=clock.at, catch_up=False
                )
            )
            # The inbox poll (P4-08) has nobody to watch here, so it stops even earlier.
            expected = (
                scheduler.JobOutcome.NOTHING_TO_WATCH
                if kind is scheduler.JobKind.INBOX
                else scheduler.JobOutcome.DISARMED_AFTER_GATE
            )
            assert outcome is expected
        await app.state.tasks.join()
        assert _rows(bare_engine) == []
        with session_scope(factory, write=True) as session:
            forged = SyncRun(
                user_id=user_id,
                linkedin_account_id=account,
                kind=SyncRunKind.CONNECTIONS_FULL,
                trigger=SyncRunTrigger.SCHEDULED,
                started_at=clock.at,
            )
            session.add(forged)
            session.flush()
            forged_id = forged.id
        await app.state.executor.execute(forged_id, user_id)
    assert connector.attaches == 0
    ((row),) = _rows(bare_engine)
    assert (row.status, row.stop_reason) == (SyncRunStatus.FAILED, "disarmed")


@pytest.mark.slow
async def test_the_same_week_armed_does_attach(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """Anti-coincidence: the drive above reaches the browser once a person arms it."""
    connector, context, _ = await _a_week_of_serve(bare_engine, settings, arm=True)

    assert connector.attaches > 0
    assert context.fetches
    rows = _rows(bare_engine)
    assert rows and {run.trigger for run in rows} == {SyncRunTrigger.SCHEDULED}
    assert SyncRunKind.CONNECTIONS_FULL in {run.kind for run in rows}


async def test_first_setup_waits_for_arming_then_runs_within_the_hour(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """#164's full sync on day 0 does not fire on a disarmed install. Armed later,
    the full sync (never run) is offered again within FIRST_SETUP_RETRY."""
    provider, connector = fake_provider()
    clock = Clock(START + timedelta(hours=8))  # 10:00 in New York: in the window
    async with served(bare_engine, settings, provider, clock) as app:
        await drive(app, clock, clock.at + timedelta(hours=3))
        assert connector.attaches == 0
        async with client_for(app) as client:
            await client.post(
                "/api/v1/linkedin/schedule/arm", json={"confirm": True}, headers=HEADERS
            )
        await drive(app, clock, clock.at + scheduler.FIRST_SETUP_RETRY + STEP)
    assert connector.attaches >= 1
    kinds = [run.kind for run in _rows(bare_engine)]
    assert kinds[0] is SyncRunKind.CONNECTIONS_FULL


async def test_a_run_left_running_is_failed_at_start_and_never_replayed(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """A task queue with nothing persisted replays nothing; the row says what happened."""
    provider, connector = fake_provider()
    clock = Clock(START)
    async with served(bare_engine, settings, provider, clock):
        pass
    factory = make_session_factory(bare_engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        user = _local(session)
        left = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=START
        ).id

    async with served(bare_engine, settings, provider, clock) as app:
        await drive(app, clock, clock.at + timedelta(hours=1))

    assert connector.attaches == 0
    (run,) = _rows(bare_engine)
    assert (run.id, run.status, run.stop_reason, run.error) == (
        left,
        SyncRunStatus.FAILED,
        "interrupted",
        runs.INTERRUPTED,
    )


async def test_a_run_whose_browser_lock_is_held_is_left_running_at_start(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """A `netkeeper linkedin sync` in a terminal holds the lock; serve starting must not
    mark its run failed under it."""
    provider, _ = fake_provider()
    clock = Clock(START)
    async with served(bare_engine, settings, provider, clock):
        pass
    factory = make_session_factory(bare_engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        user = _local(session)
        account = ensure_account(session, user).id
        runs.create_run(session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=START)
    claim = activity_lock.try_claim(activity_lock.account_key(account))
    assert claim is not None
    try:
        async with served(bare_engine, settings, provider, clock):
            pass
    finally:
        claim.release()
    (run,) = _rows(bare_engine)
    assert run.status is SyncRunStatus.RUNNING


async def test_an_app_without_the_extractor_starts_no_scheduler_and_no_run(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    assert running_app.state.scheduler is None
    response = await client.post(
        "/api/v1/linkedin/runs", json={"kind": "connections_incremental"}, headers=HEADERS
    )
    assert response.status_code == 503
    assert (await client.get("/api/v1/linkedin/runs")).json()["total"] == 0


async def test_the_event_stream_starts_nothing(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """Subscribing (and reconnecting) to ``/events`` is a read: no run, no attach."""
    provider, connector = fake_provider()
    clock = Clock(START)
    async with served(bare_engine, settings, provider, clock) as app:
        for _ in range(3):  # a reconnect is the same request again
            stream = SSEClient(app)
            stream.start()
            async with asyncio.timeout(2):
                await stream.started.wait()
            assert stream.status == 200
            await stream.close()
        await drive(app, clock, clock.at + timedelta(hours=2))
    assert connector.attaches == 0 and _rows(bare_engine) == []


# --- arming: a person's act, never a default -----------------------------------------


async def test_arming_takes_confirm_and_the_client_header(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    provider, _ = fake_provider()
    async with (
        served(bare_engine, settings, provider, Clock(START)) as app,
        client_for(app) as client,
    ):
        no_header = await client.post("/api/v1/linkedin/schedule/arm", json={"confirm": True})
        unconfirmed = await client.post(
            "/api/v1/linkedin/schedule/arm", json={"confirm": False}, headers=HEADERS
        )
        state = (await client.get("/api/v1/linkedin/schedule")).json()
        assert (no_header.status_code, unconfirmed.status_code) == (403, 422)
        assert state["armed"] is False and state["scheduler_running"] is True
        assert [job["kind"] for job in state["jobs"]] == [
            "connections_incremental",
            "connections_full",
            "enrich",
        ]
        armed = await client.post(
            "/api/v1/linkedin/schedule/arm", json={"confirm": True}, headers=HEADERS
        )
        assert armed.json()["armed"] is True
        disarmed = await client.post("/api/v1/linkedin/schedule/disarm", headers=HEADERS)
        assert disarmed.json()["armed"] is False


async def test_arming_through_the_api_seeds_a_served_kind_with_no_due_time(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """#327: a served kind with no due time (added after the schedule was established)
    gets one when a person arms, without waiting for a restart."""
    provider, _ = fake_provider()
    async with (
        served(bare_engine, settings, provider, Clock(START)) as app,
        client_for(app) as client,
    ):
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            user = _local(session)
            account = ensure_account(session, user).id
            assert delete_setting(session, user, f"scheduler.job.{account}.enrich")
        before = (await client.get("/api/v1/linkedin/schedule")).json()
        armed = await client.post(
            "/api/v1/linkedin/schedule/arm", json={"confirm": True}, headers=HEADERS
        )
    due = {job["kind"]: job["next_due"] for job in before["jobs"]}
    after = {job["kind"]: job["next_due"] for job in armed.json()["jobs"]}
    assert due["enrich"] is None and after["enrich"] is not None
    assert {k: v for k, v in after.items() if k != "enrich"} == {
        k: v for k, v in due.items() if k != "enrich"
    }


# --- start, watch, stop: P2-10's done-when ---------------------------------------------


async def test_the_ui_can_start_a_run_watch_it_and_stop_it(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """A manual run, allowed while disarmed: 202 at once, progress on the bus, cancel.

    The wait between pages is held shut until the cancel is in, so the cancel lands
    inside the wait (spec 9.9's sliced cooldown), after the first page was written.
    """
    context = ConnectionsContext(_many(120), first=40)  # the first unit needs no scroll
    provider, connector = fake_provider(context)
    gate = Gate()
    clock = Clock(START + timedelta(hours=8))
    extractor = worker_extractor(provider, settings, clock=clock, sleep=gate)
    async with serving(bare_engine, settings, extractor) as app:
        subscription = app.state.bus.subscribe()
        async with client_for(app) as client:
            started = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "connections_full"}, headers=HEADERS
            )
            assert started.status_code == 202, started.text
            run_id = started.json()["run_id"]
            assert started.json()["task_id"]
            again = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "enrich"}, headers=HEADERS
            )
            assert again.status_code == 409  # one run per account at a time

            seen: list[str] = []
            async with asyncio.timeout(5):
                async for event in subscription:
                    seen.append(event.type)
                    if event.type == "run.progress":
                        assert event.data["run_id"] == run_id and event.data["pages"] == 1
                        break
            watching = (await client.get(f"/api/v1/linkedin/runs/{run_id}")).json()
            assert watching["status"] == "running" and watching["progress"]["pages"] == 1
            # #169 F: the run holds its own account's lock file, and every other
            # netkeeper process on this data directory would be told it is busy.
            with session_scope(app.state.session_factory) as session:
                account = ensure_account(session, _local(session)).id
            assert activity_lock.inspect(activity_lock.account_key(account)).held

            cancelled = await client.post(f"/api/v1/linkedin/runs/{run_id}/cancel", headers=HEADERS)
            assert cancelled.status_code == 200 and cancelled.json()["cancel_requested_at"]
            gate.open()
            await app.state.tasks.join()
            async with asyncio.timeout(5):
                async for event in subscription:
                    seen.append(event.type)
                    if event.type == "run.finished":
                        break
            done = (await client.get(f"/api/v1/linkedin/runs/{run_id}")).json()
            too_late = await client.post(f"/api/v1/linkedin/runs/{run_id}/cancel", headers=HEADERS)
        app.state.bus.unsubscribe(subscription)

    assert "run.started" in seen and seen[-1] == "run.finished"
    assert (done["status"], done["stop_reason"], done["trigger"]) == (
        "aborted",
        "cancelled",
        "manual",
    )
    assert done["counts"]["pages"] == 1 and done["counts"]["aging"] is None
    assert too_late.status_code == 409
    # One page load and no pagination: the run was cancelled in the wait between units.
    assert connector.attaches == 1 and context.fetches == []
    assert [method for method, _, _ in context.requests] == ["GET"]


def _many(count: int) -> list[Any]:
    from voyager_pages import Person

    return [Person(1000 + i, f"Given{i}", f"Family{i}", f"Role {i}") for i in range(count)]


async def test_a_start_is_refused_while_flagged_or_hot_and_records_nothing(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    provider, connector = fake_provider()
    async with served(bare_engine, settings, provider, Clock(START)) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            flag_session(session, _local(session), Outcome.CHECKPOINT, url="/checkpoint/x")
        async with client_for(app) as client:
            flagged = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "connections_full"}, headers=HEADERS
            )
        with session_scope(factory, write=True) as session:
            from netkeeper.services.linkedin_session import clear_session_flag

            user = _local(session)
            clear_session_flag(session, user)
            account = ensure_account(session, user).id
            while not heat.should_skip(
                session, user, account, now=datetime.now(UTC), settings=settings.linkedin.heat
            ):
                heat.raise_heat(
                    session, user, account, now=datetime.now(UTC), settings=settings.linkedin.heat
                )
        async with client_for(app) as client:
            hot = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "connections_full"}, headers=HEADERS
            )
    assert (flagged.status_code, hot.status_code) == (409, 409)
    assert "flagged" in flagged.json()["detail"] and "heat" in hot.json()["detail"]
    assert connector.attaches == 0 and _rows(bare_engine) == []


async def test_a_start_without_the_client_header_is_refused(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    provider, connector = fake_provider()
    async with (
        served(bare_engine, settings, provider, Clock(START)) as app,
        client_for(app) as client,
    ):
        response = await client.post("/api/v1/linkedin/runs", json={"kind": "enrich"})
    assert response.status_code == 403
    assert connector.attaches == 0 and _rows(bare_engine) == []


# --- tab loss: the scheduler parks a retry 20 to 50 minutes out (spec 9.9) --------------


async def test_an_unreachable_browser_parks_a_retry_twenty_to_fifty_minutes_out(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    provider, connector = fake_provider(error=BrowserUnavailable("Chrome is not running"))
    clock = Clock(START + timedelta(hours=8))
    async with served(bare_engine, settings, provider, clock) as app:
        factory = app.state.session_factory
        with session_scope(factory, write=True) as session:
            arm_scheduled_runs(session, _local(session), now=clock.at)
        while connector.attaches == 0:
            clock.at += STEP
            await heartbeat(app)
            await app.state.tasks.join()
        failed_at = clock.at
        with session_scope(factory) as session:
            user = _local(session)
            account = ensure_account(session, user).id
            due = scheduler.stored_due(session, user, account, scheduler.JobKind.CONNECTIONS_FULL)
    (run,) = _rows(bare_engine)
    assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "browser_unavailable")
    assert due is not None
    assert failed_at + timedelta(minutes=20) <= due <= failed_at + timedelta(minutes=50)


# --- the worker is the third check -------------------------------------------------------


async def test_the_worker_refuses_a_scheduled_run_on_a_disarmed_account(
    session_factory: Any, settings: Settings
) -> None:
    """Even a scheduled run someone recorded by going around ``create_run`` never attaches."""
    import factories

    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider()
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user)
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.SCHEDULED,
            started_at=START,
        )
        session.add(run)
        session.flush()
        run_id, user_id = run.id, user.id
    worker = BrowserWorker(provider, session_factory, settings.linkedin)

    outcome = await worker.execute(run_id, user_id)

    assert outcome is runs.RunOutcome.DONE
    assert connector.attaches == 0
    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        stored = runs.get_run(session, owner, run_id)
        assert (stored.status, stored.stop_reason) == (SyncRunStatus.FAILED, "disarmed")


async def test_the_worker_refuses_a_scheduled_connections_run_with_the_breaker_tripped(
    session_factory: Any, settings: Settings
) -> None:
    """#191 review F6: a second, independent check for the route-changed breaker,
    matching the arming design above -- even a scheduled run someone recorded by
    going around the scheduler's own gate (``services.scheduler.poll_and_fire``)
    never attaches."""
    import factories

    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider()
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user)
        arm_scheduled_runs(session, user, now=START)
        for _ in range(route_breaker.THRESHOLD):
            route_breaker.record(
                session, user, account.id, route_changed=True, succeeded=False, now=START
            )
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.SCHEDULED,
            started_at=START,
        )
        session.add(run)
        session.flush()
        run_id, user_id = run.id, user.id
    worker = BrowserWorker(provider, session_factory, settings.linkedin)

    outcome = await worker.execute(run_id, user_id)

    assert outcome is runs.RunOutcome.DONE
    assert connector.attaches == 0
    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        stored = runs.get_run(session, owner, run_id)
        assert (stored.status, stored.stop_reason) == (
            SyncRunStatus.FAILED,
            "route_changed_breaker",
        )


async def test_the_worker_refuses_a_scheduled_connections_run_with_the_answer_lost_limit_tripped(
    session_factory: Any, settings: Settings
) -> None:
    """#199: the same second, independent check for the answer-lost limit."""
    import factories

    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider()
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user)
        arm_scheduled_runs(session, user, now=START)
        for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
            route_breaker.record_answer_lost(
                session,
                user,
                account.id,
                kind=SyncRunKind.CONNECTIONS_FULL,
                answer_lost=True,
                clean_end=False,
                now=START,
            )
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.CONNECTIONS_INCREMENTAL,
            trigger=SyncRunTrigger.SCHEDULED,
            started_at=START,
        )
        session.add(run)
        session.flush()
        run_id, user_id = run.id, user.id
    worker = BrowserWorker(provider, session_factory, settings.linkedin)

    outcome = await worker.execute(run_id, user_id)

    assert outcome is runs.RunOutcome.DONE
    assert connector.attaches == 0
    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        stored = runs.get_run(session, owner, run_id)
        assert (stored.status, stored.stop_reason) == (
            SyncRunStatus.FAILED,
            "answer_lost_breaker",
        )


async def test_the_worker_refuses_a_scheduled_run_with_a_corrupt_answer_lost_row(
    session_factory: Any, settings: Settings
) -> None:
    """#199 review, L4: the worker asks ``answer_lost_tripped``, which fails closed."""
    import factories

    from netkeeper.services.settings_kv import set_setting
    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider()
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user)
        arm_scheduled_runs(session, user, now=START)
        set_setting(
            session,
            user,
            f"linkedin.answer_lost_breaker.connections_full.{account.id}",
            "not an object",
        )
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.SCHEDULED,
            started_at=START,
        )
        session.add(run)
        session.flush()
        run_id, user_id = run.id, user.id
    worker = BrowserWorker(provider, session_factory, settings.linkedin)

    outcome = await worker.execute(run_id, user_id)

    assert outcome is runs.RunOutcome.DONE
    assert connector.attaches == 0
    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        stored = runs.get_run(session, owner, run_id)
        assert stored.stop_reason == "answer_lost_breaker"


async def test_the_worker_attaches_a_manual_run_with_the_answer_lost_limit_tripped(
    session_factory: Any, settings: Settings
) -> None:
    """Manual runs are never refused for it: a run by hand is how a person checks."""
    import factories

    from netkeeper.linkedin.browser import BrowserUnavailable
    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider(error=BrowserUnavailable("Chrome is not running"))
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user)
        for _ in range(route_breaker.ANSWER_LOST_THRESHOLD):
            route_breaker.record_answer_lost(
                session,
                user,
                account.id,
                kind=SyncRunKind.CONNECTIONS_FULL,
                answer_lost=True,
                clean_end=False,
                now=START,
            )
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.CONNECTIONS_FULL,
            trigger=SyncRunTrigger.MANUAL,
            started_at=START,
        )
        session.add(run)
        session.flush()
        run_id, user_id = run.id, user.id
    worker = BrowserWorker(provider, session_factory, settings.linkedin)

    outcome = await worker.execute(run_id, user_id)

    assert connector.attaches == 1
    assert outcome is runs.RunOutcome.RETRY_LATER


async def test_the_worker_does_not_refuse_a_tripped_breaker_for_enrichment(
    session_factory: Any, settings: Settings
) -> None:
    """#191 review F6's decision, at the worker too: enrichment does not share the
    connections breaker's counter, so a tripped breaker never stops a scheduled
    enrichment run from attaching."""
    import factories

    from netkeeper.linkedin.browser import BrowserUnavailable
    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider(error=BrowserUnavailable("Chrome is not running"))
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user)
        arm_scheduled_runs(session, user, now=START)
        for _ in range(route_breaker.THRESHOLD):
            route_breaker.record(
                session, user, account.id, route_changed=True, succeeded=False, now=START
            )
        run = SyncRun(
            user_id=user.id,
            linkedin_account_id=account.id,
            kind=SyncRunKind.ENRICH,
            trigger=SyncRunTrigger.SCHEDULED,
            started_at=START,
        )
        session.add(run)
        session.flush()
        run_id, user_id = run.id, user.id
    worker = BrowserWorker(provider, session_factory, settings.linkedin)

    outcome = await worker.execute(run_id, user_id)

    # It reached the attach (and failed there, for the unrelated reason this fake
    # provider is rigged with) rather than being refused by the breaker check.
    assert connector.attaches == 1
    assert outcome is runs.RunOutcome.RETRY_LATER
    with session_scope(session_factory) as session:
        owner = session.get(User, user_id)
        assert owner is not None
        stored = runs.get_run(session, owner, run_id)
        assert stored.stop_reason == "browser_unavailable"


def test_the_real_serve_builds_its_extractor_on_the_attach_provider(
    session_factory: Any, settings: Settings
) -> None:
    """``netkeeper serve``'s app gets the worker on the one attach provider, building it
    attaches to nothing, and the legacy-lock co-claim goes with the local user's
    account whatever its id (#175 review, F10)."""
    import factories

    from netkeeper.linkedin.activity_lock import account_key
    from netkeeper.models import UserKind
    from netkeeper.worker import BrowserWorker, serve_extractor

    with session_scope(session_factory, write=True) as session:
        hosted = factories.make_user(session, kind=UserKind.HOSTED)
        ensure_account(session, hosted)  # takes id 1
        local = factories.make_user(session)
        local_account = ensure_account(session, local).id
    assert local_account != 1

    executor = serve_extractor(settings).executor(session_factory, None)  # type: ignore[arg-type]

    assert isinstance(executor, BrowserWorker)
    assert isinstance(executor.provider, AttachBrowserProvider)
    assert executor.provider.cdp_url == settings.linkedin.cdp_url
    assert executor.provider.locks.legacy_partner == account_key(local_account)


async def test_shutting_down_mid_run_records_it_interrupted(
    bare_engine: Engine, settings: Settings, no_frontend: None
) -> None:
    """The lifespan cancels running tasks; the run keeps what it wrote and says why it
    stopped, and the next start has nothing left running to sweep up."""
    provider, _ = fake_provider(ConnectionsContext(_many(120), first=40))
    gate = Gate()  # never opened: the run is parked in its wait between pages
    extractor = worker_extractor(provider, settings, clock=Clock(START), sleep=gate)
    async with serving(bare_engine, settings, extractor) as app:
        subscription = app.state.bus.subscribe()
        async with client_for(app) as client:
            started = await client.post(
                "/api/v1/linkedin/runs", json={"kind": "connections_full"}, headers=HEADERS
            )
        async with asyncio.timeout(5):
            async for event in subscription:
                if event.type == "run.progress":
                    break
    (run,) = _rows(bare_engine)
    assert run.id == started.json()["run_id"]
    assert (run.status, run.stop_reason, run.error) == (
        SyncRunStatus.ABORTED,
        "interrupted",
        runs.INTERRUPTED,
    )
    assert run.progress_json is not None and run.progress_json["pages"] == 1


async def test_a_live_sync_reads_what_the_connections_page_loads() -> None:
    """#187: the worker hands the connections runner one PageConnections per run, on
    LinkedIn's own connections page, with no fallback behind it; building one loads
    nothing. An incremental sync requires the list newest first; a full sync does not."""
    from netkeeper.linkedin.connections import SyncMode
    from netkeeper.linkedin.page_connections import PageConnections
    from netkeeper.worker import connections_source

    provider, connector = fake_provider()
    async with provider.run("account-9") as run:
        first = connections_source(run, mode=SyncMode.INCREMENTAL)
        second = connections_source(run, mode=SyncMode.FULL)
    assert isinstance(first, PageConnections) and isinstance(second, PageConnections)
    assert first is not second
    assert first.page_url == "https://www.linkedin.com/mynetwork/invite-connect/connections/"
    assert first._require_newest_first and not second._require_newest_first
    assert connector.attaches == 1  # building the sources loaded no page
    assert all(page.goto_calls == [] for page in connector.browsers[0].context_list[0].pages)


# --- #175 review F2, F3: the landing check, and the worker's own refusal -------------------


def _manual_run(factory: Any, kind: SyncRunKind) -> tuple[int, int]:
    import factories

    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        run = runs.create_run(
            session, user, kind, trigger=SyncRunTrigger.MANUAL, now=datetime.now(UTC)
        )
        return run.id, user.id


async def test_a_connections_page_that_lands_on_a_checkpoint_fetches_nothing(
    session_factory: Any, settings: Settings
) -> None:
    """The page load is classified before anything is read: a checkpoint stops the run
    as one, raises the session flag, nothing is evaluated in the page, and the page is
    never scrolled, so it asks for nothing."""
    from run_fakes import CheckpointContext

    from netkeeper.services.linkedin_session import session_flag
    from netkeeper.worker import BrowserWorker

    context = CheckpointContext()
    provider, connector = fake_provider(context)
    run_id, user_id = _manual_run(session_factory, SyncRunKind.CONNECTIONS_FULL)

    await BrowserWorker(provider, session_factory, settings.linkedin).execute(run_id, user_id)

    assert connector.attaches == 1
    assert all(page.evaluate_calls == [] for page in context.pages)
    assert all(page.mouse.wheels == [] for page in context.pages)
    assert context.fetches == []
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.get_run(session, user, run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, "checkpoint")
        flag = session_flag(session, user)
        assert flag is not None and flag.outcome is Outcome.CHECKPOINT


async def test_the_worker_refuses_a_flagged_session_before_it_attaches(
    session_factory: Any, settings: Settings
) -> None:
    """A run recorded before the flag was raised still never reaches the browser."""
    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider()
    run_id, user_id = _manual_run(session_factory, SyncRunKind.ENRICH)
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        flag_session(session, user, Outcome.LOGGED_OUT, url="/authwall")

    outcome = await BrowserWorker(provider, session_factory, settings.linkedin).execute(
        run_id, user_id
    )

    assert outcome is runs.RunOutcome.DONE and connector.attaches == 0
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.get_run(session, user, run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "session_flagged")


async def test_the_worker_refuses_heat_before_it_attaches(
    session_factory: Any, settings: Settings
) -> None:
    from netkeeper.worker import BrowserWorker

    provider, connector = fake_provider()
    run_id, user_id = _manual_run(session_factory, SyncRunKind.CONNECTIONS_INCREMENTAL)
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        account = ensure_account(session, user).id
        now = datetime.now(UTC)
        while not heat.should_skip(
            session, user, account, now=now, settings=settings.linkedin.heat
        ):
            heat.raise_heat(session, user, account, now=now, settings=settings.linkedin.heat)

    await BrowserWorker(provider, session_factory, settings.linkedin).execute(run_id, user_id)

    assert connector.attaches == 0
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        assert runs.get_run(session, user, run_id).stop_reason == "heat_skip"


# --- #197: a lost answer through the real worker, and the double end ----------------------


def _the_run(factory: Any, user_id: int, run_id: int) -> SyncRun:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        run = runs.get_run(session, user, run_id)
        session.expunge(run)
        return run


async def test_a_lost_answer_the_page_moves_past_is_an_aborted_run_the_breaker_ignores(
    session_factory: Any, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Supervised run 5 on #31, with the page moving past the answer it lost: the run
    reads on (#200), then ends aborted as answer_lost, naming the start, and the
    breaker does not move."""
    from flagship_site import Lost

    from netkeeper.worker import BrowserWorker

    caplog.set_level(logging.INFO, logger="netkeeper")
    context = ConnectionsContext(_many(90), lost={40: Lost("move_on")})
    provider, _ = fake_provider(context)
    run_id, user_id = _manual_run(session_factory, SyncRunKind.CONNECTIONS_FULL)

    outcome = await BrowserWorker(
        provider, session_factory, settings.linkedin, sleep=_no_wait
    ).execute(run_id, user_id)

    assert outcome is runs.RunOutcome.DONE
    run = _the_run(session_factory, user_id, run_id)
    assert (run.status, run.stop_reason, run.error) == (SyncRunStatus.ABORTED, "answer_lost", None)
    assert run.notes is not None and "start 40 (Exception (no resource))" in run.notes
    assert runs.lost_answers(run) == 1
    assert "fake-lost-slug" not in run.notes
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        assert route_breaker.state(session, user, run.linkedin_account_id).count == 0
    assert "already ended" not in caplog.text


async def test_a_run_that_fails_inside_its_runner_is_recorded_once(
    session_factory: Any, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """#197's double end: the runner records the failure and re-raises; the worker
    must not try to record it again ("run 5 already ended failed; not recording
    failed")."""
    from flagship_site import Answer

    from netkeeper.worker import BrowserWorker

    huge = Answer(status=200, body=b"0" * (8 * 1024 * 1024 + 1))
    provider, _ = fake_provider(ConnectionsContext(_many(90), answers={20: huge}))
    run_id, user_id = _manual_run(session_factory, SyncRunKind.CONNECTIONS_FULL)

    await BrowserWorker(provider, session_factory, settings.linkedin, sleep=_no_wait).execute(
        run_id, user_id
    )

    run = _the_run(session_factory, user_id, run_id)
    assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "error")
    assert run.error is not None and run.error.startswith("ObservationFailed")
    assert "already ended" not in caplog.text


async def _no_wait(seconds: float) -> None:
    await asyncio.sleep(0)


def test_a_live_worker_reads_profiles_through_the_live_source() -> None:
    """#210's seam: the offline tests hand the worker a short landing wait, but the
    worker ``serve`` and the CLI build still reads through ``profile_source``, whose
    landing wait is PageProfiles' live 20 s."""
    import inspect

    from netkeeper.linkedin.page_profiles import LANDING_WAIT_S
    from netkeeper.worker import BrowserWorker, profile_source

    assert inspect.signature(BrowserWorker).parameters["profiles"].default is profile_source
    assert inspect.signature(profile_source).parameters.keys() == {"run", "sleep"}
    assert LANDING_WAIT_S == 20.0
