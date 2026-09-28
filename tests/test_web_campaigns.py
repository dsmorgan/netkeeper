"""``/campaigns``: create, enroll, list, status, pause and resume (#291, P3-13).

Activation is the review gate's, tested in ``test_campaign_review.py``; here only
that nothing under ``/campaigns`` itself activates a campaign.
"""

from __future__ import annotations

from typing import Any

import factories
import httpx
from campaign_fakes import make_mailbox
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import templates as template_service
from netkeeper.crm import lists as list_service
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    ListKind,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.scoping import scoped

CSRF = {"X-Netkeeper-Client": "1"}


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _seed(app: FastAPI) -> dict[str, Any]:
    """A mailbox, an email and a LinkedIn template, and a list of three contacts, one of
    them do-not-contact."""
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        mailbox = make_mailbox(session, user)

        def template(name: str, channel: TemplateChannel) -> int:
            return template_service.create_template(
                session,
                user,
                name=name,
                channel=channel,
                subject="Catching up" if channel is TemplateChannel.EMAIL else None,
                body="Hi {{ first_name }}",
                me_keys=(),
            ).id

        email, linkedin = (
            template("email", TemplateChannel.EMAIL),
            template("note", TemplateChannel.LINKEDIN),
        )
        contacts = [
            factories.make_contact(
                session, user, emails=[f"p{n}@contacts.example"], do_not_contact=n == 2
            ).id
            for n in range(3)
        ]
        people = list_service.create_list(session, user, "people", ListKind.STATIC)
        list_service.add_members(session, user, people.id, contacts)
        return {
            "mailbox_id": mailbox.id,
            "email": email,
            "linkedin": linkedin,
            "list_id": people.id,
            "contacts": contacts,
        }


def _body(seed: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Reconnect",
        "mailbox_id": seed["mailbox_id"],
        "steps": [
            {"template_id": seed["email"]},
            {"template_id": seed["email"], "mode": "send"},
            {"template_id": seed["linkedin"]},
        ],
        "list_id": seed["list_id"],
    }
    body.update(overrides)
    return body


async def _create(client: httpx.AsyncClient, seed: dict[str, Any], **overrides: Any) -> Any:
    response = await client.post("/api/v1/campaigns", json=_body(seed, **overrides), headers=CSRF)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_answers_the_draft_with_spec_defaults(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)

    created = await _create(client, seed)

    assert created["status"] == "draft"
    assert created["source_list_id"] == seed["list_id"]
    assert [
        (s["position"], s["channel"], s["mode"], s["delay_days"], s["condition"], s["same_thread"])
        for s in created["steps"]
    ] == [
        (1, "email", "draft", 0, "always", False),
        (2, "email", "send", 7, "no_reply", True),
        (3, "linkedin", "prefill", 7, "no_reply", False),
    ]
    assert created["contacted_within_days_guard"] == 30
    assert {m["requirement"] for m in created["missing"]} >= {"reviewing", "audience"}


async def test_create_refusals(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    seed = _seed(running_app)
    await _create(client, seed)

    async def post(body: dict[str, Any]) -> httpx.Response:
        return await client.post("/api/v1/campaigns", json=body, headers=CSRF)

    assert (await post(_body(seed))).status_code == 409  # the name is taken
    assert (await post(_body(seed, name="B", mailbox_id=None))).status_code == 422
    assert (await post(_body(seed, name="C", steps=[{"template_id": 999}]))).status_code == 404
    assert (await post(_body(seed, name="D", mailbox_id=999))).status_code == 404
    auto = [{"template_id": seed["linkedin"], "mode": "auto_send"}]
    assert (await post(_body(seed, name="E", steps=auto))).status_code == 422
    wrong = [{"template_id": seed["email"], "mode": "prefill"}]
    assert (await post(_body(seed, name="F", steps=wrong))).status_code == 422
    thread = [{"template_id": seed["email"], "same_thread": True}]
    assert (await post(_body(seed, name="G", steps=thread))).status_code == 422
    both = _body(seed, name="H", filter={"where": {"op": "has_li_url"}})
    assert (await post(both)).status_code == 422


async def test_enroll_list_and_status(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)
    base = f"/api/v1/campaigns/{created['id']}"

    enrolled = await client.post(f"{base}/enroll", json={}, headers=CSRF)

    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json() == {
        "campaign_id": created["id"],
        "enrolled": 2,
        "already": 0,
        "excluded": 1,
        "summary": "3 in audience, 1 excluded: 1 do-not-contact",
    }
    listed = (await client.get("/api/v1/campaigns")).json()
    assert [(c["id"], c["status"], c["steps"], c["enrollments"]) for c in listed] == [
        (created["id"], "draft", 3, {"pending": 2})
    ]
    status = (await client.get(base)).json()
    assert status["enrollments"] == {"pending": 2}
    assert "audience" not in {m["requirement"] for m in status["missing"]}
    assert (await client.get("/api/v1/campaigns/999")).status_code == 404
    assert (
        await client.post("/api/v1/campaigns/999/enroll", json={}, headers=CSRF)
    ).status_code == 404


async def test_enroll_from_a_filter_sets_the_source_on_a_draft_only(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed, list_id=None)
    base = f"/api/v1/campaigns/{created['id']}"
    assert (await client.post(f"{base}/enroll", json={}, headers=CSRF)).status_code == 422

    by_filter = {"filter": {"where": {"op": "has_li_url"}}}
    response = await client.post(f"{base}/enroll", json=by_filter, headers=CSRF)

    assert response.status_code == 200, response.text
    assert response.json()["enrolled"] == 2
    assert (await client.get(base)).json()["filter"] is not None
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        row = session.scalars(
            scoped(_local(session), Campaign).where(Campaign.id == created["id"])
        ).one()
        row.status = CampaignStatus.REVIEWING
    again = await client.post(f"{base}/enroll", json=by_filter, headers=CSRF)
    assert again.status_code == 409
    assert "audience source is fixed" in again.json()["detail"]


async def test_pause_and_resume(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        campaign_id = factories.make_campaign(session, _local(session)).id  # active
    base = f"/api/v1/campaigns/{campaign_id}"

    paused = await client.post(f"{base}/pause", headers=CSRF)
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    assert (await client.post(f"{base}/pause", headers=CSRF)).status_code == 409

    resumed = await client.post(f"{base}/resume", headers=CSRF)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"
    assert (await client.post(f"{base}/resume", headers=CSRF)).status_code == 409
    assert (await client.post("/api/v1/campaigns/999/pause", headers=CSRF)).status_code == 404


async def test_nothing_under_campaigns_activates_a_draft(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)
    base = f"/api/v1/campaigns/{created['id']}"
    await client.post(f"{base}/enroll", json={}, headers=CSRF)

    assert (await client.post(f"{base}/resume", headers=CSRF)).status_code == 409
    assert (await client.post(f"{base}/pause", headers=CSRF)).status_code == 409
    assert (await client.get(base)).json()["status"] == "draft"
