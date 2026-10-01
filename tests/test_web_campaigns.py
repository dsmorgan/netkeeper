"""``/campaigns``: create, enroll, list, status, pause and resume (#291, P3-13).

Activation is the review gate's, tested in ``test_campaign_review.py``; here only
that nothing under ``/campaigns`` itself activates a campaign.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    Enrollment,
    EnrollmentStatus,
    ListKind,
    Mailbox,
    MailboxStatus,
    Template,
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
        "removed": 0,
        "pending": 2,
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


# --- after review (#292) --------------------------------------------------------------


def _pending_contacts(app: FastAPI, campaign_id: int) -> list[int]:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory) as session:
        return sorted(
            session.scalars(
                scoped(_local(session), Enrollment)
                .with_only_columns(Enrollment.contact_id)
                .where(
                    Enrollment.campaign_id == campaign_id,
                    Enrollment.status == EnrollmentStatus.PENDING,
                )
            )
        )


async def test_a_new_list_on_a_draft_replaces_the_audience(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """List A enrolls two; switching to list B (one other contact) leaves only B's."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    seed = _seed(running_app)
    with session_scope(factory, write=True) as session:
        user = _local(session)
        list_a = list_service.create_list(session, user, "A", ListKind.STATIC)
        list_service.add_members(session, user, list_a.id, seed["contacts"][:2])
        newcomer = factories.make_contact(session, user, emails=["b0@contacts.example"]).id
        list_b = list_service.create_list(session, user, "B", ListKind.STATIC)
        list_service.add_members(session, user, list_b.id, [newcomer])
        a_id, b_id = list_a.id, list_b.id
    created = await _create(client, seed, list_id=a_id)
    base = f"/api/v1/campaigns/{created['id']}"
    first = (await client.post(f"{base}/enroll", json={}, headers=CSRF)).json()
    assert (first["enrolled"], first["pending"]) == (2, 2)
    before = (await client.get(f"{base}/review")).json()

    switched = await client.post(f"{base}/enroll", json={"list_id": b_id}, headers=CSRF)

    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert (body["enrolled"], body["removed"], body["pending"]) == (1, 2, 1)
    assert _pending_contacts(running_app, created["id"]) == [newcomer]
    after = (await client.get(f"{base}/review")).json()
    assert body["summary"] == after["guard_summary"]
    assert body["summary"].startswith("1 in audience")
    assert after["audience_fingerprint"] != before["audience_fingerprint"]
    status = (await client.get(base)).json()
    assert status["source_list_id"] == b_id
    assert status["enrollments"] == {"pending": 1}


async def test_another_users_or_an_unknown_list_is_not_found(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    seed = _seed(running_app)
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session, UserKind.HOSTED)
        theirs = list_service.create_list(session, other, "theirs", ListKind.STATIC).id
    for list_id in (theirs, 999_999):
        response = await client.post(
            "/api/v1/campaigns", json=_body(seed, name=f"C{list_id}", list_id=list_id), headers=CSRF
        )
        assert response.status_code == 404, response.text
    created = await _create(client, seed)
    base = f"/api/v1/campaigns/{created['id']}"
    for list_id in (theirs, 999_999):
        response = await client.post(f"{base}/enroll", json={"list_id": list_id}, headers=CSRF)
        assert response.status_code == 404, response.text
    assert (await client.get(base)).json()["source_list_id"] == seed["list_id"]
    assert _pending_contacts(running_app, created["id"]) == []


async def test_create_refuses_a_superseded_template(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    seed = _seed(running_app)
    with session_scope(factory, write=True) as session:
        user = _local(session)
        session.add(
            Template(
                user_id=user.id,
                name="email",
                channel=TemplateChannel.EMAIL,
                subject="Catching up",
                body="Hi {{ first_name }}!",
                lint_json=[],
                version=2,
                previous_id=seed["email"],
            )
        )

    response = await client.post(
        "/api/v1/campaigns",
        json=_body(seed, steps=[{"template_id": seed["email"]}]),
        headers=CSRF,
    )

    assert response.status_code == 422
    assert "older version" in response.json()["detail"]


async def test_enroll_refuses_a_list_and_a_filter_together(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)
    base = f"/api/v1/campaigns/{created['id']}"

    response = await client.post(
        f"{base}/enroll",
        json={"list_id": seed["list_id"], "filter": {"where": {"op": "has_li_url"}}},
        headers=CSRF,
    )

    assert response.status_code == 422
    assert "not both" in response.json()["detail"]
    status = (await client.get(base)).json()
    assert (status["source_list_id"], status["filter"]) == (seed["list_id"], None)
    assert _pending_contacts(running_app, created["id"]) == []


async def test_enrollments_list_searches_and_pages(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)
    base = f"/api/v1/campaigns/{created['id']}"
    assert (await client.post(f"{base}/enroll", json={}, headers=CSRF)).status_code == 200

    listed = (await client.get(f"{base}/enrollments")).json()

    assert listed["total"] == 2
    assert [(e["contact_id"], e["status"], e["email"]) for e in listed["items"]] == [
        (seed["contacts"][0], "pending", "p0@contacts.example"),
        (seed["contacts"][1], "pending", "p1@contacts.example"),
    ]
    found = (await client.get(f"{base}/enrollments", params={"q": "P1@CONTACTS"})).json()
    assert [e["contact_id"] for e in found["items"]] == [seed["contacts"][1]]
    assert found["total"] == 1
    paged = (await client.get(f"{base}/enrollments", params={"limit": 1, "offset": 1})).json()
    assert (paged["total"], len(paged["items"])) == (2, 1)
    none = (await client.get(f"{base}/enrollments", params={"status": "active"})).json()
    assert none == {"items": [], "total": 0}
    assert (await client.get("/api/v1/campaigns/999/enrollments")).status_code == 404


async def test_steps_carry_the_id_test_send_takes(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)

    ids = [s["id"] for s in created["steps"]]

    assert len(set(ids)) == 3
    assert all(isinstance(i, int) for i in ids)


async def test_list_answers_the_next_fire_of_an_active_campaign(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user)  # active
        for hour in (15, 9):
            factories.make_enrollment(
                session,
                campaign,
                factories.make_contact(session, user),
                next_action_at=datetime(2030, 1, 2, hour, tzinfo=UTC),
            )
        campaign_id = campaign.id

    listed = {c["id"]: c for c in (await client.get("/api/v1/campaigns")).json()}

    assert listed[campaign_id]["next_action_at"] == "2030-01-02T09:00:00Z"
    await client.post(f"/api/v1/campaigns/{campaign_id}/pause", headers=CSRF)
    listed = {c["id"]: c for c in (await client.get("/api/v1/campaigns")).json()}
    assert listed[campaign_id]["next_action_at"] is None


async def test_create_refuses_a_mailbox_that_cannot_send(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    factory: sessionmaker[Session] = running_app.state.session_factory
    for status in (MailboxStatus.REAUTH_REQUIRED, MailboxStatus.DISABLED):
        with session_scope(factory, write=True) as session:
            mailbox = session.scalars(
                scoped(_local(session), Mailbox).where(Mailbox.id == seed["mailbox_id"])
            ).one()
            mailbox.status = status

        response = await client.post(
            "/api/v1/campaigns", json=_body(seed, name=f"On {status}"), headers=CSRF
        )

        assert response.status_code == 409, response.text
        assert str(status) in response.json()["detail"]
    assert (await client.get("/api/v1/campaigns")).json() == []


async def test_enrollment_search_treats_wildcards_literally(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        odd = [
            factories.make_contact(session, user, last_name=name, emails=[f"w{n}@odd.example"]).id
            for n, name in enumerate(("100% Kettle", "Under_score", "Back\\slash"))
        ]
        # Matched by its address alone, so the email subquery's escape is what keeps a
        # `_` meaning itself there (#299).
        by_email = factories.make_contact(
            session, user, last_name="Plain", emails=["snake_case@odd.example"]
        ).id
    base = f"/api/v1/campaigns/{created['id']}"
    enrolled = await client.post(
        f"{base}/enroll", json={"contact_ids": [*odd, by_email]}, headers=CSRF
    )
    assert enrolled.json()["pending"] == 6

    async def found(q: str) -> list[int]:
        body = (await client.get(f"{base}/enrollments", params={"q": q})).json()
        return [e["contact_id"] for e in body["items"]]

    assert await found("%") == [odd[0]]
    assert sorted(await found("_")) == sorted([odd[1], by_email])
    assert await found("e_c") == [by_email]
    assert await found("\\") == [odd[2]]
    assert await found("0%") == [odd[0]]


async def test_the_next_send_counts_only_active_enrollments(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user)  # active
        factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            next_action_at=datetime(2030, 1, 2, 15, tzinfo=UTC),
        )
        for hour, status in ((6, EnrollmentStatus.PAUSED), (7, EnrollmentStatus.REPLIED)):
            factories.make_enrollment(
                session,
                campaign,
                factories.make_contact(session, user),
                status=status,
                next_action_at=datetime(2030, 1, 2, hour, tzinfo=UTC),
            )
        campaign_id = campaign.id

    listed = {c["id"]: c for c in (await client.get("/api/v1/campaigns")).json()}
    detail = (await client.get(f"/api/v1/campaigns/{campaign_id}")).json()

    assert listed[campaign_id]["next_action_at"] == "2030-01-02T15:00:00Z"
    assert detail["next_action_at"] == "2030-01-02T15:00:00Z"  # the detail page's too (#299)
