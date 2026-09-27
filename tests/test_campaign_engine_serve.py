"""The campaign engine inside ``netkeeper serve`` (P3-06): its lifespan, and #259's deadlock."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import factories
import httpx
import pytest
from campaign_fakes import NOW, SETTINGS, FakeSender, make_mailbox
from fastapi import FastAPI
from sqlalchemy import Connection, Engine, event, select

from netkeeper.config import Settings
from netkeeper.db import SQLITE_BUSY_TIMEOUT_MS, session_scope
from netkeeper.models import MessageStatus, StepMode, User, UserKind
from netkeeper.services.campaign_engine import CampaignEngine
from netkeeper.services.scheduled_runs import ServeExtractor
from netkeeper.web.app import create_app

CLIENT = {"X-Netkeeper-Client": "1"}


async def test_the_engine_ticks_only_under_serve(
    running_app: FastAPI,
) -> None:
    """Like the mailbox poll: only ``serve`` runs background work."""
    assert running_app.state.campaign_engine is None


async def test_serve_starts_and_stops_the_engine_with_its_sender(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conftest

    conftest._copy_template(_migrated_template, tmp_path / "bare")
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    sender = FakeSender()
    served = create_app(Settings(), engine=bare_engine, extractor=extractor, campaign_sender=sender)
    async with served.router.lifespan_context(served):
        campaigns = served.state.campaign_engine
        assert isinstance(campaigns, CampaignEngine)
        assert campaigns.sender is sender
        assert campaigns._interval_s == 60.0
        assert campaigns._task is not None and not campaigns._task.done()
    assert campaigns._task is None


def _due_enrollment_and_a_contact(app: FastAPI) -> int:
    """A campaign with one step due now for the local user, and a contact to pin."""
    with session_scope(app.state.session_factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        mailbox = make_mailbox(session, user)
        campaign = factories.make_campaign(session, user, mailbox_id=mailbox.id)
        campaign.steps[0].mode = StepMode.SEND
        contact = factories.make_contact(session, user, emails=["due@example.test"])
        factories.make_enrollment(session, campaign, contact, next_action_at=NOW)
        return factories.make_contact(session, user).id


async def test_a_request_holding_the_write_lock_during_a_tick_does_not_deadlock(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """#259, with the repro from #255: a POST opens its write transaction in a worker
    thread and needs the event loop to reach its commit. A tick that took the write lock
    on the loop's own thread would block the loop in SQLite's busy handler, and neither
    could move until the busy timeout failed the tick with "database is locked".
    The tick runs in a worker thread, so it waits for the request's commit instead."""
    to_pin = _due_enrollment_and_a_contact(running_app)
    sender = FakeSender()
    campaigns = CampaignEngine(
        running_app.state.session_factory, SETTINGS, sender, clock=lambda: NOW
    )
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    opened = asyncio.Event()

    def on_begin(_connection: Connection) -> None:
        # The request's transaction opens in a worker thread; tell a loop task.
        if threading.get_ident() != loop_thread:
            loop.call_soon_threadsafe(opened.set)

    async def tick_once_the_request_holds_the_lock() -> list[object]:
        await opened.wait()
        return list(await campaigns.tick_once())

    engine: Engine = running_app.state.engine
    event.listen(engine, "begin", on_begin)
    try:
        started = time.monotonic()
        ticking = asyncio.create_task(tick_once_the_request_holds_the_lock())
        response = await client.post(
            "/api/v1/linkedin/pins", json={"contact_id": to_pin}, headers=CLIENT
        )
        [result] = await asyncio.wait_for(ticking, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000 * 2)
        elapsed = time.monotonic() - started
    finally:
        event.remove(engine, "begin", on_begin)

    assert response.status_code == 200, response.text
    assert opened.is_set()
    assert len(result.fired) == 1  # type: ignore[attr-defined]
    [(_, outcome)] = result.fired  # type: ignore[attr-defined]
    assert outcome.outcome.value == MessageStatus.SENT.value
    # Waiting on the request's commit, not on the busy timeout.
    assert elapsed < SQLITE_BUSY_TIMEOUT_MS / 1000 / 2
