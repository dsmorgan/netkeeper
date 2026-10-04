"""``/campaigns``: create, enroll, list, status, pause and resume (#291, P3-13).

Activation is the review gate's, tested in ``test_campaign_review.py``; here only
that nothing under ``/campaigns`` itself activates a campaign.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import factories
import httpx
import pytest
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
from netkeeper.scoping import get_scoped, scoped

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
        "summary": "2 will start, 1 skipped (1 do-not-contact)",
        "excluded_summary": "2 will start, 1 skipped (1 do-not-contact)",
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
        user = _local(session)
        mailbox_id = make_mailbox(session, user).id
        campaign_id = factories.make_campaign(session, user, mailbox_id=mailbox_id).id  # active
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
    assert body["summary"] == "1 will start, none skipped"
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


@pytest.mark.parametrize("status", [MailboxStatus.REAUTH_REQUIRED, MailboxStatus.DISABLED])
async def test_resume_is_refused_while_the_mailbox_is_not_ok(
    running_app: FastAPI, client: httpx.AsyncClient, status: MailboxStatus
) -> None:
    """As activation is (#299): a mailbox that broke while the campaign was paused."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        mailbox = make_mailbox(session, user)
        campaign_id = factories.make_campaign(
            session, user, mailbox_id=mailbox.id, status=CampaignStatus.PAUSED
        ).id
        mailbox.status = status
        email = mailbox.email
    base = f"/api/v1/campaigns/{campaign_id}"

    refused = await client.post(f"{base}/resume", headers=CSRF)

    assert refused.status_code == 409
    assert refused.json()["detail"] == f"{email} is {status}, not ok"
    assert (await client.get(base)).json()["status"] == "paused"
    with session_scope(factory, write=True) as session:
        session.scalars(scoped(_local(session), Mailbox)).one().status = MailboxStatus.OK
    assert (await client.post(f"{base}/resume", headers=CSRF)).status_code == 200


async def test_resume_of_a_linkedin_only_campaign_needs_no_mailbox(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        campaign_id = factories.make_campaign(
            session,
            _local(session),
            channels=(TemplateChannel.LINKEDIN,),
            status=CampaignStatus.PAUSED,
        ).id

    resumed = await client.post(f"/api/v1/campaigns/{campaign_id}/resume", headers=CSRF)

    assert resumed.status_code == 200, resumed.text


# --- the scheduled start and step timing (#338) -------------------------------------------


def _active_campaign(app: FastAPI, *, starts_at: datetime) -> tuple[int, int, list[int]]:
    """An active campaign starting at ``starts_at``, with two enrollments waiting for step 1."""
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        mailbox_id = make_mailbox(session, user).id
        campaign = factories.make_campaign(
            session,
            user,
            channels=(TemplateChannel.EMAIL, TemplateChannel.EMAIL),
            mailbox_id=mailbox_id,
            starts_at=starts_at,
        )
        enrollments = [
            factories.make_enrollment(
                session, campaign, factories.make_contact(session, user), next_action_at=starts_at
            ).id
            for _ in range(2)
        ]
        return campaign.id, campaign.steps[1].id, enrollments


async def test_start_options_give_the_default_the_suggestion_and_a_warning_only(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)
    base = f"/api/v1/campaigns/{created['id']}/start-options"

    options = (await client.get(base)).json()
    zone = ZoneInfo(options["timezone"])
    default = datetime.fromisoformat(options["default_start"]).astimezone(zone)
    assert (default.weekday(), default.hour, default.minute) == (1, 9, 0)
    assert options["suggestion"] == "Most effective: Tue–Thu mornings."  # noqa: RUF001
    assert "`serve` is running and this Mac is awake" in options["reminder"]
    assert options["sending_hours"].startswith("Sending hours: Mon to Fri, 09:00 to 17:00.")
    assert options["warning"] is None

    saturday_night = datetime(2099, 1, 3, 22, 0, tzinfo=zone)
    checked = (await client.get(base, params={"at": saturday_night.isoformat()})).json()
    assert checked["warning"].startswith("That is outside the suggested slots")
    tuesday_morning = datetime(2099, 1, 6, 10, 0, tzinfo=zone)
    checked = (await client.get(base, params={"at": tuesday_morning.isoformat()})).json()
    assert checked["warning"] is None
    naive = await client.get(base, params={"at": "2099-01-06T10:00:00"})
    assert naive.status_code == 422
    assert (await client.get("/api/v1/campaigns/999/start-options")).status_code == 404


async def test_the_start_moves_until_the_campaign_sends(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    later = datetime(2099, 1, 6, 9, 0, tzinfo=UTC)
    campaign_id, _, enrollments = _active_campaign(running_app, starts_at=later)
    base = f"/api/v1/campaigns/{campaign_id}"
    before = (await client.get(base)).json()
    assert before["starts_at"] == "2099-01-06T09:00:00Z" and before["start_editable"] is True

    sooner = datetime(2099, 1, 5, 22, 0, tzinfo=UTC)
    moved = await client.put(f"{base}/start", json={"starts_at": sooner.isoformat()}, headers=CSRF)
    assert moved.status_code == 200, moved.text
    assert moved.json()["starts_at"] == "2099-01-05T22:00:00Z"
    assert moved.json()["next_action_at"] == "2099-01-05T22:00:00Z"

    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        enrollment = get_scoped(session, _local(session), Enrollment, enrollments[0])
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1)
    assert (await client.get(base)).json()["start_editable"] is False
    refused = await client.put(f"{base}/start", json={"starts_at": later.isoformat()}, headers=CSRF)
    assert refused.status_code == 409 and "already sent" in refused.json()["detail"]


async def test_the_start_of_a_draft_cannot_move(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    created = await _create(client, _seed(running_app))
    response = await client.put(
        f"/api/v1/campaigns/{created['id']}/start",
        json={"starts_at": "2099-01-06T09:00:00Z"},
        headers=CSRF,
    )
    assert response.status_code == 409
    assert created["starts_at"] is None and created["start_editable"] is False


async def test_a_steps_timing_is_set_and_shown(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    created = await _create(client, _seed(running_app))
    step = created["steps"][1]
    assert step["send_time"] is None
    url = f"/api/v1/campaigns/{created['id']}/steps/{step['id']}/schedule"

    updated = await client.put(url, json={"delay_days": 3, "send_time": "22:00"}, headers=CSRF)
    assert updated.status_code == 200, updated.text
    [_, second, _] = updated.json()["steps"]
    assert (second["delay_days"], second["send_time"]) == (3, "22:00")

    cleared = await client.put(url, json={"delay_days": 5, "send_time": None}, headers=CSRF)
    assert [(s["delay_days"], s["send_time"]) for s in cleared.json()["steps"]][1] == (5, None)

    for bad in ({"delay_days": 3, "send_time": "9pm"}, {"delay_days": -1}):
        assert (await client.put(url, json=bad, headers=CSRF)).status_code == 422
    other = f"/api/v1/campaigns/{created['id']}/steps/99999/schedule"
    assert (await client.put(other, json={"delay_days": 1}, headers=CSRF)).status_code == 404


async def test_a_changed_step_time_moves_an_active_campaigns_waiting_enrollments(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    start = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)
    campaign_id, step_id, enrollments = _active_campaign(running_app, starts_at=start)
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        enrollment = get_scoped(session, _local(session), Enrollment, enrollments[0])
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=start)
        enrollment.current_step = 1
        enrollment.next_action_at = start + timedelta(days=7)

    url = f"/api/v1/campaigns/{campaign_id}/steps/{step_id}/schedule"
    response = await client.put(url, json={"delay_days": 4, "send_time": "22:00"}, headers=CSRF)
    assert response.status_code == 200, response.text
    with session_scope(factory) as session:
        moved = get_scoped(session, _local(session), Enrollment, enrollments[0])
        untouched = get_scoped(session, _local(session), Enrollment, enrollments[1])
        assert moved is not None and untouched is not None
        zone = ZoneInfo(_local(session).timezone)
        local = moved.next_action_at
        assert local is not None
        # Saturday 22:00 is outside the default sending hours: Monday 09:00 (#338).
        assert local.astimezone(zone).replace(tzinfo=None) == datetime(2026, 10, 5, 9, 0)
        assert untouched.next_action_at == start


async def test_a_completed_campaigns_steps_no_longer_change(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        campaign = factories.make_campaign(
            session, _local(session), status=CampaignStatus.COMPLETED
        )
        ids = (campaign.id, campaign.steps[0].id)
    url = f"/api/v1/campaigns/{ids[0]}/steps/{ids[1]}/schedule"
    assert (await client.put(url, json={"delay_days": 1}, headers=CSRF)).status_code == 409


# --- the lifecycle (#345) -------------------------------------------------------------


async def test_end_archive_and_unarchive(running_app: FastAPI, client: httpx.AsyncClient) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        mailbox_id = make_mailbox(session, user).id
        campaign = factories.make_campaign(session, user, mailbox_id=mailbox_id)  # active
        contact = factories.make_contact(session, user)
        factories.make_enrollment(session, campaign, contact, next_action_at=datetime.now(UTC))
        campaign_id = campaign.id
    base = f"/api/v1/campaigns/{campaign_id}"

    async def on_the_dashboard() -> list[int]:
        page = (await client.get("/api/v1/dashboard/next-fires")).json()
        return [fire["campaign_id"] for fire in page["items"]]

    assert await on_the_dashboard() == [campaign_id]
    running = (await client.get(base)).json()
    assert (running["concluded"], running["deletable"]) == (False, False)
    refused = await client.post(f"{base}/archive", headers=CSRF)
    assert refused.status_code == 409
    assert "end it first" in refused.json()["detail"]

    ended = await client.post(f"{base}/end", headers=CSRF)
    assert ended.status_code == 200, ended.text
    assert (ended.json()["status"], ended.json()["concluded"]) == ("completed", True)
    assert await on_the_dashboard() == []
    assert ended.json()["enrollments"] == {"active": 1}
    assert (await client.post(f"{base}/end", headers=CSRF)).status_code == 409
    assert (await client.post(f"{base}/resume", headers=CSRF)).status_code == 409

    archived = await client.post(f"{base}/archive", headers=CSRF)
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"
    assert [c["id"] for c in (await client.get("/api/v1/campaigns")).json()] == []
    assert await on_the_dashboard() == []
    listed = await client.get("/api/v1/campaigns", params={"archived": "true"})
    assert [c["id"] for c in listed.json()] == [campaign_id]
    # Still there by its id, with its results.
    assert (await client.get(base)).status_code == 200
    assert (await client.get(f"{base}/results")).status_code == 200

    unarchived = await client.post(f"{base}/unarchive", headers=CSRF)
    assert unarchived.status_code == 200, unarchived.text
    assert unarchived.json()["status"] == "completed"
    assert [c["id"] for c in (await client.get("/api/v1/campaigns")).json()] == [campaign_id]
    assert (await client.post(f"{base}/unarchive", headers=CSRF)).status_code == 409


async def test_delete_a_draft_and_refuse_one_that_sent(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    seed = _seed(running_app)
    created = await _create(client, seed)
    base = f"/api/v1/campaigns/{created['id']}"
    await client.post(f"{base}/enroll", json={}, headers=CSRF)
    assert (await client.get(base)).json()["deletable"] is True

    plan = await client.get(f"{base}/delete-plan")
    assert plan.status_code == 200, plan.text
    assert plan.json() == {
        "campaign_id": created["id"],
        "name": "Reconnect",
        "deletable": True,
        "refusal": None,
        "steps": 3,
        "enrollments": 2,
        "leftover_drafts": [],
        "unverifies": None,
    }
    assert (await client.post(f"{base}/archive", headers=CSRF)).status_code == 409

    deleted = await client.delete(base, headers=CSRF)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deletable"] is True
    assert (await client.get(base)).status_code == 404
    assert (await client.delete(base, headers=CSRF)).status_code == 404

    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        sent = factories.make_campaign(session, user, mailbox_id=seed["mailbox_id"])
        enrollment = factories.make_enrollment(session, sent, factories.make_contact(session, user))
        factories.make_message(session, enrollment)
        sent_id = sent.id
    refused = await client.delete(f"/api/v1/campaigns/{sent_id}", headers=CSRF)
    assert refused.status_code == 409
    assert "never activated" in refused.json()["detail"]
    plan = (await client.get(f"/api/v1/campaigns/{sent_id}/delete-plan")).json()
    assert (plan["deletable"], plan["refusal"]) == (False, refused.json()["detail"])


async def test_the_lifecycle_never_reaches_another_users_campaign(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session, UserKind.HOSTED)
        draft = factories.make_campaign(session, other, status=CampaignStatus.DRAFT).id
        ended = factories.make_campaign(session, other, status=CampaignStatus.COMPLETED).id
        archived = factories.make_campaign(session, other, status=CampaignStatus.ARCHIVED).id
        active = factories.make_campaign(session, other).id
    for method, path in (
        ("POST", f"{active}/end"),
        ("POST", f"{ended}/archive"),
        ("POST", f"{archived}/unarchive"),
        ("GET", f"{draft}/delete-plan"),
        ("DELETE", f"{draft}"),
    ):
        response = await client.request(method, f"/api/v1/campaigns/{path}", headers=CSRF)
        assert response.status_code == 404, (path, response.text)
    assert (await client.get("/api/v1/campaigns", params={"archived": "true"})).json() == []
    with session_scope(factory) as session:
        statuses = {
            c.id: c.status
            for c in session.scalars(
                scoped(session.get_one(User, other.id), Campaign).where(
                    Campaign.id.in_((draft, ended, archived, active))
                )
            )
        }
    assert statuses == {
        draft: CampaignStatus.DRAFT,
        ended: CampaignStatus.COMPLETED,
        archived: CampaignStatus.ARCHIVED,
        active: CampaignStatus.ACTIVE,
    }
