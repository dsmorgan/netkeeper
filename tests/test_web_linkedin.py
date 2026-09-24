"""``/linkedin``: runs list and detail, resume, pins, budget, heat, status (P2-10).

The start/watch/stop path and the disarmed-serve property are in
``tests/test_runs_serve.py``; the two-user isolation of ``/linkedin/runs`` and
``/linkedin/pins`` is in ``tests/isolation``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import factories
import httpx
import pytest
from fastapi import FastAPI
from run_fakes import Clock, fake_provider
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from test_runs_serve import HEADERS, client_for, served

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User, UserKind
from netkeeper.services import budgets, enrich_plan, runs
from netkeeper.services.linkedin_accounts import ensure_account

NOW = datetime.now(UTC)


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _factory(app: FastAPI):  # type: ignore[no-untyped-def]
    return app.state.session_factory


async def test_runs_are_listed_newest_first_and_filtered(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = _local(session)
        for offset, kind in enumerate((SyncRunKind.CONNECTIONS_FULL, SyncRunKind.ENRICH)):
            run = runs.create_run(
                session,
                user,
                kind,
                trigger=SyncRunTrigger.MANUAL,
                now=NOW + timedelta(minutes=offset),
            )
            runs.finish_run(session, user, run.id, status=SyncRunStatus.COMPLETED, now=NOW)

    page = (await client.get("/api/v1/linkedin/runs")).json()
    enrich_only = (await client.get("/api/v1/linkedin/runs", params={"kind": "enrich"})).json()
    one = (await client.get(f"/api/v1/linkedin/runs/{page['items'][0]['id']}")).json()
    missing = await client.get("/api/v1/linkedin/runs/999")

    assert page["total"] == 2 and [r["kind"] for r in page["items"]] == [
        "enrich",
        "connections_full",
    ]
    assert enrich_only["total"] == 1
    assert one["kind"] == "enrich" and one["aging_refused"] is None
    assert missing.status_code == 404


async def test_pins_take_five_and_refuse_the_unvisitable(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = _local(session)
        contacts = [factories.make_contact(session, user).id for _ in range(6)]
        archived = factories.make_contact(session, user, archived_at=NOW).id

    for contact_id in contacts[:5]:
        pinned = await client.post(
            "/api/v1/linkedin/pins", json={"contact_id": contact_id}, headers=HEADERS
        )
        assert pinned.status_code == 200
    sixth = await client.post(
        "/api/v1/linkedin/pins", json={"contact_id": contacts[5]}, headers=HEADERS
    )
    unvisitable = await client.post(
        "/api/v1/linkedin/pins", json={"contact_id": archived}, headers=HEADERS
    )
    nobody = await client.post("/api/v1/linkedin/pins", json={"contact_id": 999}, headers=HEADERS)
    listed = (await client.get("/api/v1/linkedin/pins")).json()
    after_unpin = (
        await client.delete(f"/api/v1/linkedin/pins/{contacts[0]}", headers=HEADERS)
    ).json()

    assert (sixth.status_code, unvisitable.status_code, nobody.status_code) == (409, 409, 404)
    assert "at most 5" in sixth.json()["detail"]
    assert [pin["contact_id"] for pin in listed] == contacts[:5]
    assert listed[0]["first_name"] and listed[0]["last_name"]
    assert [pin["contact_id"] for pin in after_unpin] == contacts[1:5]


async def test_budget_heat_and_status_read_what_posture_reads(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    settings = Settings().linkedin
    with session_scope(_factory(running_app), write=True) as session:
        user = _local(session)
        account = ensure_account(session, user).id
        budgets.consume(
            session,
            user,
            account,
            budgets.ActionClass.PROFILE_VISITS,
            now=datetime.now(UTC),
            settings=settings.budget,
        )

    budget = (await client.get("/api/v1/linkedin/budget")).json()
    heat = (await client.get("/api/v1/linkedin/heat")).json()
    status = (await client.get("/api/v1/linkedin/status")).json()

    visits = {row["action"]: row for row in budget["budgets"]}["profile_visits"]
    assert visits["day"]["count"] == 1 and visits["week"]["count"] == 1
    today = budget["profile_visits_today"]
    assert today["spent_today"] == 1 and today["ramp"] == settings.budget.warmup_start
    assert (heat["score"], heat["tripped"], heat["threshold"]) == (0.0, False, 2.5)
    assert status == {
        "session_flag": None,
        "session_flagged_at": None,
        "heat_tripped": False,
        "armed": False,
        "running_run_id": None,
        "can_start_runs": False,  # this app was not started by `netkeeper serve`
    }


async def test_browser_instructions_come_from_config_and_touch_no_browser(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """GET /linkedin/browser: read-only, built from config, matches `browser launch` (P2-12)."""
    settings: Settings = running_app.state.settings
    browser = (await client.get("/api/v1/linkedin/browser")).json()

    assert browser["cdp_url"] == settings.linkedin.cdp_url
    assert browser["profile_dir"].endswith("chrome-profile")
    assert browser["launch_command"][1] == "  --remote-debugging-port=9222 \\"
    assert browser["remote_host_note"] is None  # the default cdp_url is loopback
    assert browser["check_command"] == "netkeeper preflight"


async def test_a_resume_takes_the_rest_of_the_plan_and_runs_it(
    bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST resume records a new run on the old plan's remainder and submits it (202)."""
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", "/nonexistent-dist")
    settings = Settings()
    provider, connector = fake_provider()
    async with served(bare_engine, settings, provider, Clock(NOW)) as app:
        with session_scope(_factory(app), write=True) as session:
            user = _local(session)
            ids = [factories.make_contact(session, user).id for _ in range(3)]
            old = runs.create_run(
                session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
            )
            enrich_plan.store_plan(session, user, old.id, ids)
            enrich_plan.mark_completed(session, user, old.id, ids[0])
            runs.finish_run(session, user, old.id, status=SyncRunStatus.ABORTED, now=NOW)
            old_id = old.id
        async with client_for(app) as client:
            resumed = await client.post(
                f"/api/v1/linkedin/runs/{old_id}/resume", json={"max_visits": 1}, headers=HEADERS
            )
            again = await client.post(
                f"/api/v1/linkedin/runs/{old_id}/resume", json={}, headers=HEADERS
            )
            await app.state.tasks.join()
            new = (await client.get(f"/api/v1/linkedin/runs/{resumed.json()['run_id']}")).json()
            old_after = (await client.get(f"/api/v1/linkedin/runs/{old_id}")).json()

    assert resumed.status_code == 202, resumed.text
    assert again.status_code == 409 and "already resumed" in again.json()["detail"]
    assert (new["resume_of_id"], new["max_visits"], new["planned"]) == (old_id, 1, 2)
    assert new["status"] != "running"  # the fake Chrome answered; the run ended
    assert connector.attaches == 1
    # L3 (#179 review): the old run's RunOut now says who took over its plan,
    # so a client can tell "already resumed" from "still resumable" on its own.
    assert new["resumed_by"] is None
    assert old_after["resumed_by"] == new["id"]


async def test_a_failed_enrichment_with_a_remaining_plan_can_be_resumed(
    bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L3 (#179 review): resume is not only for `aborted` — a run that could not
    run at all (browser unavailable, a pre-attach refusal) is `failed`, and its
    stored plan is exactly as resumable. `enrich_plan.start_resume` already
    allowed this (it checks running/completed/resumed_by, never the status
    name); the LinkedIn page only needed to stop hiding the button for it."""
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", "/nonexistent-dist")
    settings = Settings()
    provider, connector = fake_provider()
    async with served(bare_engine, settings, provider, Clock(NOW)) as app:
        with session_scope(_factory(app), write=True) as session:
            user = _local(session)
            ids = [factories.make_contact(session, user).id for _ in range(3)]
            old = runs.create_run(
                session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
            )
            enrich_plan.store_plan(session, user, old.id, ids)
            runs.finish_run(
                session,
                user,
                old.id,
                status=SyncRunStatus.FAILED,
                now=NOW,
                stop_reason="browser_unavailable",
                error="BrowserUnavailable: tab lost",
            )
            old_id = old.id
        async with client_for(app) as client:
            resumed = await client.post(
                f"/api/v1/linkedin/runs/{old_id}/resume", json={}, headers=HEADERS
            )
            await app.state.tasks.join()

    assert resumed.status_code == 202, resumed.text
    assert connector.attaches == 1


# --- #175 review ---------------------------------------------------------------------------


async def test_resume_is_refused_while_flagged_and_a_sync_has_nothing_to_resume(
    bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X3: the resume endpoint refuses a flagged session with 409 and records nothing.
    F5: resuming a connections sync is a 404, not a 500."""
    from netkeeper.linkedin.classify import Outcome
    from netkeeper.services.linkedin_session import flag_session

    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", "/nonexistent-dist")
    settings = Settings()
    provider, connector = fake_provider()
    async with served(bare_engine, settings, provider, Clock(NOW)) as app:
        with session_scope(_factory(app), write=True) as session:
            user = _local(session)
            ids = [factories.make_contact(session, user).id for _ in range(2)]
            enrich = runs.create_run(
                session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
            )
            enrich_plan.store_plan(session, user, enrich.id, ids)
            runs.finish_run(session, user, enrich.id, status=SyncRunStatus.ABORTED, now=NOW)
            sync = runs.create_run(
                session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
            )
            runs.finish_run(session, user, sync.id, status=SyncRunStatus.ABORTED, now=NOW)
            flag_session(session, user, Outcome.CHECKPOINT, url="/checkpoint/x")
            enrich_id, sync_id = enrich.id, sync.id
        async with client_for(app) as client:
            flagged = await client.post(
                f"/api/v1/linkedin/runs/{enrich_id}/resume", json={}, headers=HEADERS
            )
            not_enrichment = await client.post(
                f"/api/v1/linkedin/runs/{sync_id}/resume", json={}, headers=HEADERS
            )
            total = (await client.get("/api/v1/linkedin/runs")).json()["total"]

    assert flagged.status_code == 409 and "flagged" in flagged.json()["detail"]
    assert not_enrichment.status_code == 404
    assert "not an enrichment run" in not_enrichment.json()["detail"]
    assert total == 2 and connector.attaches == 0


async def test_a_rebinding_request_is_refused_before_it_reaches_a_route(
    running_app: FastAPI,
) -> None:
    """F8: a page on another name re-pointed at 127.0.0.1 sends its own name as Host and
    as Origin. It is refused for writes, arming included, and for reads."""
    evil = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app), base_url="http://evil.example:8000"
    )
    async with evil:
        arm = await evil.post(
            "/api/v1/linkedin/schedule/arm",
            json={"confirm": True},
            headers={**HEADERS, "Origin": "http://evil.example:8000"},
        )
        read = await evil.get("/api/v1/linkedin/runs")
        me = await evil.get("/api/v1/me")
    assert (arm.status_code, read.status_code, me.status_code) == (421, 421, 421)
    assert arm.json()["rule"] == "host"
    with session_scope(_factory(running_app)) as session:
        account = ensure_account(session, _local(session))
        assert account.scheduled_runs_armed_at is None


@pytest.mark.parametrize(
    "host", ["127.0.0.1:8000", "localhost:5173", "[::1]:8000", "LOCALHOST", "127.0.0.1"]
)
async def test_loopback_names_on_any_port_are_served(running_app: FastAPI, host: str) -> None:
    """The Vite dev proxy forwards the browser's Host unchanged (changeOrigin: false),
    so a dev request names the dev server's port; the name is what matters."""
    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/api/v1/me", headers={"host": host})
    assert response.status_code == 200
