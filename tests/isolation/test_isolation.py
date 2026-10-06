"""The two-user isolation test (spec section 5, ADR 0005): one case per registered endpoint.

The registry check that fails an unregistered list endpoint is test_coverage.py, so
it keeps running while this module is skipped for an empty registry.
"""

from __future__ import annotations

from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import import_runs as import_service
from netkeeper.crm import lists as list_service
from netkeeper.crm import tags as tag_service
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import (
    Campaign,
    Contact,
    ContactTag,
    InteractionKind,
    ListKind,
    ListMember,
    MessageStatus,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import scoped, scoped_count
from netkeeper.services.linkedin_session import flag_session
from netkeeper.web.app import API_PREFIX
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE

from .harness import acting_as, assert_isolated
from .registry import (
    IMPORT_CSV,
    IMPORT_MAPPING,
    REGISTRY,
    ListEndpoint,
    _seed_linkedin_ready,
    _seed_linkedin_waiting,
    seed_contacts,
)

if not REGISTRY:
    pytest.skip("REGISTRY is empty: no list endpoints exist yet", allow_module_level=True)


@pytest.mark.parametrize("endpoint", REGISTRY, ids=[endpoint.path for endpoint in REGISTRY])
async def test_registered_list_endpoint_is_isolated(
    endpoint: ListEndpoint, running_app: FastAPI
) -> None:
    await assert_isolated(running_app, endpoint)


# --- bulk actions -----------------------------------------------------------

BULK_SELECTION: dict[str, Any] = {"filter": {"where": None}}


async def test_a_bulk_action_is_isolated(running_app: FastAPI) -> None:
    """Two users, one filter that says "everyone": neither may count or touch the other's.

    Bulk is not a list operation, so it is not in ``REGISTRY``; the risk is the
    same one and it gets the same two-user treatment. The count is the read half
    and the action is the write half, and a token minted by one user must not
    execute for the other.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        seeded = {a.id: seed_contacts(session, a), b.id: seed_contacts(session, b)}
        factories.make_contact(session, b)  # B has one more, so a leak shows as a count
        seeded[b.id] += 1
        a_id, b_id = a.id, b.id

    tokens: dict[int, str] = {}
    for user_id, want in seeded.items():
        body = await _post(
            running_app,
            user_id,
            "/contacts/bulk/count",
            {
                "selection": BULK_SELECTION,
                "action": "archive",
            },
        )
        assert body["count"] == want, f"user {user_id} counted {body['count']}, not {want}"
        tokens[user_id] = body["token"]

    # A's token, presented by B, is refused before anything is counted or written.
    crossed = await _post(
        running_app,
        b_id,
        "/contacts/bulk",
        {"selection": BULK_SELECTION, "action": "archive", "token": tokens[a_id]},
        want=422,
    )
    assert crossed["reason"] == "user"

    # Each user's own action touches exactly their own rows.
    for user_id, want in seeded.items():
        applied = await _post(
            running_app,
            user_id,
            "/contacts/bulk",
            {"selection": BULK_SELECTION, "action": "archive", "token": tokens[user_id]},
        )
        assert applied == {"affected": want}


async def _post(
    app: FastAPI, user_id: int, path: str, body: dict[str, Any], *, want: int = 200
) -> dict[str, Any]:
    with acting_as(app, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post(
                f"{API_PREFIX}{path}",
                json=body,
                headers={CLIENT_HEADER: CLIENT_HEADER_VALUE},
            )
    assert response.status_code == want, (
        f"{path} as {user_id}: {response.status_code} {response.text}"
    )
    parsed: dict[str, Any] = response.json()
    return parsed


# --- adding a contact by hand (#303) -------------------------------------------


async def test_adding_a_contact_is_isolated(running_app: FastAPI) -> None:
    """A create is not a list operation, so ``POST /contacts`` is not in ``REGISTRY``;
    it reads the address book to deduplicate and writes tags and lists, so it gets the
    two-user treatment here.

    B adding the person A already has is no duplicate of A's contact, never names
    A's contact id, and B cannot put the new contact on A's tag or A's list.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        a_contact = factories.make_contact(
            session, a, li_public_id="same-person-fake", emails=["same@example.test"]
        )
        a_tag = tag_service.create_tag(session, a, "a-only")
        a_list = list_service.create_list(session, a, "A only", ListKind.STATIC)
        a_id, b_id = a.id, b.id
        a_contact_id, a_tag_id, a_list_id = a_contact.id, a_tag.id, a_list.id

    same = {
        "first_name": "Same",
        "last_name": "Person",
        "email": "SAME@example.test",
        "li_url": "https://www.linkedin.com/in/same-person-fake",
    }
    # A gets the duplicate; B, adding the very same person, gets a contact of B's own.
    refused = await _post(running_app, a_id, "/contacts", same, want=409)
    assert refused["contact_id"] == a_contact_id
    created = await _post(running_app, b_id, "/contacts", same, want=201)
    assert created["id"] != a_contact_id
    # And B's is now B's duplicate, not A's.
    again = await _post(running_app, b_id, "/contacts", same, want=409)
    assert again["contact_ids"] == [created["id"]]

    # A's tag and list do not exist for B.
    for extra in ({"tag_ids": [a_tag_id]}, {"list_id": a_list_id}):
        await _post(running_app, b_id, "/contacts", {"first_name": "Other", **extra}, want=422)

    with session_scope(factory) as session:
        a_now, b_now = session.get(User, a_id), session.get(User, b_id)
        assert a_now is not None and b_now is not None
        assert session.scalar(scoped_count(a_now, Contact)) == 1
        assert session.scalar(scoped_count(b_now, Contact)) == 1
        assert session.scalar(scoped_count(a_now, ContactTag)) == 0
        assert session.scalar(scoped_count(a_now, ListMember)) == 0


# --- deleting a draft import run ---------------------------------------------


async def test_deleting_a_draft_import_run_is_isolated(running_app: FastAPI) -> None:
    """Delete is destructive, not a list operation, so it is not in ``REGISTRY``; it gets
    the same two-user treatment the bulk actions above get.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        a_run = import_service.create_run(
            session, a, filename="a.csv", content=IMPORT_CSV, mapping=IMPORT_MAPPING
        )
        b_run = import_service.create_run(
            session, b, filename="b.csv", content=IMPORT_CSV, mapping=IMPORT_MAPPING
        )
        a_id, b_id, a_run_id, b_run_id = a.id, b.id, a_run.id, b_run.id

    # B's id does not reach A's draft: refused before anything is touched.
    await _delete(running_app, b_id, a_run_id, want=404)
    with session_scope(factory) as session:
        a_now, b_now = session.get(User, a_id), session.get(User, b_id)
        assert a_now is not None and b_now is not None
        assert import_service.list_runs(session, a_now)[1] == 1
        assert import_service.list_runs(session, b_now)[1] == 1

    # Each deletes their own.
    await _delete(running_app, a_id, a_run_id, want=204)
    await _delete(running_app, b_id, b_run_id, want=204)
    with session_scope(factory) as session:
        a_now, b_now = session.get(User, a_id), session.get(User, b_id)
        assert a_now is not None and b_now is not None
        assert import_service.list_runs(session, a_now)[1] == 0
        assert import_service.list_runs(session, b_now)[1] == 0


async def _delete(app: FastAPI, user_id: int, run_id: int, *, want: int) -> None:
    with acting_as(app, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.delete(
                f"{API_PREFIX}/imports/{run_id}", headers={CLIENT_HEADER: CLIENT_HEADER_VALUE}
            )
    assert response.status_code == want, (
        f"delete run {run_id} as {user_id}: {response.status_code} {response.text}"
    )


# --- posture -----------------------------------------------------------------


async def _get(app: FastAPI, user_id: int, path: str) -> dict[str, Any]:
    with acting_as(app, user_id):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.get(f"{API_PREFIX}{path}")
    assert response.status_code == 200, (
        f"{path} as {user_id}: {response.status_code} {response.text}"
    )
    parsed: dict[str, Any] = response.json()
    return parsed


async def test_posture_is_isolated(running_app: FastAPI) -> None:
    """Not a list operation (a single report), so ``GET /posture`` is not in
    ``REGISTRY`` either; it reads a user's own session flag, heat, and budget
    counters, so it gets the same two-user treatment as the rows above.
    """
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        flag_session(session, a, Outcome.CHECKPOINT, url="https://example.invalid/checkpoint/x")
        a_id, b_id = a.id, b.id

    a_report = await _get(running_app, a_id, "/posture")
    b_report = await _get(running_app, b_id, "/posture")

    a_flag = next(row for row in a_report["protections"] if row["name"] == "session flag")
    b_flag = next(row for row in b_report["protections"] if row["name"] == "session flag")
    # A's checkpoint warns on A's own report and never reaches B's.
    assert a_flag["warnings"] != []
    assert b_flag["warnings"] == []
    assert a_report["ok"] is False
    # B's report is not "not clear" for a reason that traces back to A's flag —
    # the one unavoidable warning here (no browser probe, spec 9.1) is the
    # only thing keeping it False, not anything of A's.
    assert all("checkpoint" not in warning for warning in b_report["warnings"])


async def test_inbound_this_week_is_isolated(running_app: FastAPI) -> None:
    """``GET /dashboard/inbound`` is a count, not a list, so it is not in ``REGISTRY``;
    it counts one user's interactions, so it gets the two-user treatment here."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        contact = factories.make_contact(session, a)
        add_interaction(session, a, contact.id, InteractionKind.EMAIL_IN, utcnow(), "a message in")
        a_id, b_id = a.id, b.id

    assert (await _get(running_app, a_id, "/dashboard/inbound"))["count"] == 1
    assert (await _get(running_app, b_id, "/dashboard/inbound"))["count"] == 0


async def test_one_campaigns_linkedin_queue_is_isolated(running_app: FastAPI) -> None:
    """#383: ``GET /campaigns/linkedin/ready?campaign_id=`` with another user's campaign
    answers nothing, its per-step counts included."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        _seed_linkedin_ready(session, a)
        _seed_linkedin_ready(session, b)
        campaign_id = session.scalars(scoped(a, Campaign)).one().id
        a_id, b_id = a.id, b.id

    path = f"/campaigns/linkedin/ready?campaign_id={campaign_id}"
    mine = await _get(running_app, a_id, path)
    assert (mine["total"], mine["by_step"]) == (2, {"1": 2})
    theirs = await _get(running_app, b_id, path)
    assert theirs == {"items": [], "total": 0, "by_step": {}}


async def test_one_campaigns_linkedin_waiting_list_is_isolated(running_app: FastAPI) -> None:
    """#383: ``GET /campaigns/linkedin/waiting?campaign_id=`` with another user's campaign
    answers nothing."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        _seed_linkedin_waiting(session, a)
        _seed_linkedin_waiting(session, b)
        campaign_id = session.scalars(scoped(a, Campaign)).one().id
        a_id, b_id = a.id, b.id

    path = f"/campaigns/linkedin/waiting?campaign_id={campaign_id}"
    mine = await _get(running_app, a_id, path)
    assert mine["total"] == 2
    assert {item["campaign_id"] for item in mine["items"]} == {campaign_id}
    assert await _get(running_app, b_id, path) == {"items": [], "total": 0}


async def test_a_partly_typed_row_is_listed_only_for_its_owner(running_app: FastAPI) -> None:
    """#383 (B1): ``GET /campaigns/linkedin/waiting`` lists a user's own partly typed
    message and never another user's."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        a = User(kind=UserKind.HOSTED, display_name="A")
        b = User(kind=UserKind.HOSTED, display_name="B")
        session.add_all([a, b])
        session.flush()
        for user in (a, b):
            campaign = factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))
            enrollment = factories.make_enrollment(
                session,
                campaign,
                factories.make_contact(session, user),
                next_action_at=None,
            )
            factories.make_message(
                session,
                enrollment,
                status=MessageStatus.FAILED,
                error="partially_typed: fixed words",
                sent_at=None,
            )
        a_id, b_id = a.id, b.id

    mine = await _get(running_app, a_id, "/campaigns/linkedin/waiting")
    assert [(item["status"], item["partly_typed"]) for item in mine["items"]] == [("failed", True)]
    theirs = await _get(running_app, b_id, "/campaigns/linkedin/waiting")
    assert theirs["total"] == 1
    assert {item["campaign_id"] for item in mine["items"]}.isdisjoint(
        {item["campaign_id"] for item in theirs["items"]}
    )
