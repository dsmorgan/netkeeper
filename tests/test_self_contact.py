"""The self contact (#342, #320): your own details, held as a contact, never in your network.

One test per surface a contact can be acted on through: each gives the self contact
everything that would make it show up there (a name, a company, an email, a
LinkedIn identity, a job change, a tag) and checks that it does not.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.templates import REMOVED_FIELD_BLOCK, removed_field_campaigns
from netkeeper.config import LegacyMe, Settings
from netkeeper.crm import contacts as crm_contacts
from netkeeper.crm import identity, triage
from netkeeper.crm.apply import _mark_seen, age_unseen, known_urns
from netkeeper.crm.duplicates import possible_duplicates
from netkeeper.crm.filters import FilterTree
from netkeeper.crm.interactions import NotFound as InteractionNotFound
from netkeeper.crm.interactions import add_interaction, delete_interaction, update_interaction
from netkeeper.crm.lists import create_list, list_members
from netkeeper.crm.self_contact import (
    InvalidSelfValue,
    ensure_self_contact,
    get_self_contact,
    update_self_contact,
)
from netkeeper.crm.tags import contact_counts, create_rule, create_tag, run_rules
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    Contact,
    ContactEmail,
    ContactSnapshot,
    ContactSource,
    ContactTag,
    Interaction,
    InteractionKind,
    LinkKind,
    ListKind,
    ListMember,
    RuleField,
    TagSource,
    User,
)
from netkeeper.scoping import get_scoped, scoped, scoped_contacts, scoped_contacts_count
from netkeeper.services import campaigns as campaign_service
from netkeeper.services import dashboard, enrich_plan
from netkeeper.services.campaign_engine import enroll as engine_enroll
from netkeeper.services.campaign_guards import Reason
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.web.app import create_app

CSRF = {"X-Netkeeper-Client": "1"}
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SELF_EMAIL = "selfie@example.test"
SELF_URN = "urn:li:fsd_profile/SELF000001"
SELF_SLUG = "selfie-owner"


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _make_attractive(session: Session, contact: Contact) -> Contact:
    """Everything that would put a contact on a surface: name, company, email, identity."""
    contact.first_name = contact.preferred_name = "Selfie"
    contact.last_name = "Owner"
    contact.current_company = "Example Co"
    contact.li_urn, contact.li_public_id = SELF_URN, SELF_SLUG
    contact.emails.append(ContactEmail(user_id=contact.user_id, email=SELF_EMAIL, is_primary=True))
    session.flush()
    return contact


@pytest.fixture
def you(writer: Session, user: User) -> Contact:
    return _make_attractive(writer, ensure_self_contact(writer, user))


# --- the record -------------------------------------------------------------------------


def test_one_self_contact_per_user_by_the_database(writer: Session, user: User) -> None:
    ensure_self_contact(writer, user)
    other = factories.make_user(writer)
    ensure_self_contact(writer, other)  # another user has their own
    with pytest.raises(IntegrityError), writer.begin_nested():
        writer.add(Contact(user_id=user.id, is_self=True))
        writer.flush()


def test_ensure_creates_once_seeded_from_an_old_me_section(writer: Session, user: User) -> None:
    legacy = LegacyMe(name="Ada B. Lovelace", website="https://ada.example", city="London")
    you = ensure_self_contact(writer, user, legacy=legacy)
    assert (you.first_name, you.last_name, you.location) == ("Ada", "B. Lovelace", "London")
    assert [(link.url, link.kind) for link in you.links] == [
        ("https://ada.example", LinkKind.WEBSITE)
    ]
    again = ensure_self_contact(writer, user, legacy=LegacyMe(name="Someone Else"))
    assert again.id == you.id and again.first_name == "Ada"  # the config is read once


def test_creating_it_needs_a_writer(session: Session) -> None:
    user = factories.make_user(session)
    with pytest.raises(RuntimeError, match="write=True"):
        ensure_self_contact(session, user)


def test_update_sets_and_clears_fields_and_refuses_others(writer: Session, user: User) -> None:
    you = update_self_contact(writer, user, {"first_name": " Ada ", "location": "Paris"})
    assert (you.first_name, you.preferred_name, you.location) == ("Ada", "Ada", "Paris")
    update_self_contact(writer, user, {"location": "  "})
    assert you.location is None
    with pytest.raises(InvalidSelfValue, match="no field li_urn"):
        update_self_contact(writer, user, {"li_urn": "x"})
    with pytest.raises(InvalidSelfValue, match="longer than 200"):
        update_self_contact(writer, user, {"first_name": "a" * 201})


async def test_the_app_creates_it_at_start_seeded_from_the_config(bare_engine: Engine) -> None:
    settings = Settings(legacy_me=LegacyMe(name="Ada Lovelace", city="London"))
    app = create_app(settings, engine=bare_engine)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            shown = (await client.get("/api/v1/settings/self-contact")).json()
    assert shown == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "current_company": "",
        "current_title": "",
        "location": "London",
        "exists": True,
    }


async def test_the_settings_api_reads_and_replaces_your_details(
    client: httpx.AsyncClient,
) -> None:
    url = "/api/v1/settings/self-contact"
    assert (await client.get(url)).json()["first_name"] == ""
    body = {"first_name": "Ada", "current_company": "Example Co"}
    assert (await client.put(url, json=body)).status_code == 403  # the CSRF header
    saved = await client.put(url, json=body, headers=CSRF)
    assert saved.status_code == 200, saved.text
    assert (await client.get(url)).json() == {
        "first_name": "Ada",
        "last_name": "",
        "current_company": "Example Co",
        "current_title": "",
        "location": "",
        "exists": True,
    }
    too_long = await client.put(url, json={"first_name": "a" * 201}, headers=CSRF)
    assert too_long.status_code == 422


# --- every surface leaves it out: the API --------------------------------------------------


def _factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


def _setup(app: FastAPI) -> tuple[int, int]:
    """The app's self contact, made attractive, and one ordinary lookalike contact."""
    with session_scope(_factory(app), write=True) as session:
        user = session.scalars(select(User)).one()
        you = get_self_contact(session, user)
        assert you is not None
        _make_attractive(session, you)
        other = factories.make_contact(
            session,
            user,
            first_name="Selfie",
            last_name="Owner",
            current_company="Example Co",
            emails=[SELF_EMAIL],
            # No LinkedIn identity: two different URNs never match as duplicates, so the
            # hint test would pass for that reason alone and not because of the scope.
            li_urn=None,
            li_public_id=None,
        )
        return you.id, other.id


async def test_lists_and_search_leave_it_out(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    you, other = _setup(running_app)
    listed = (await client.get("/api/v1/contacts")).json()
    assert [c["id"] for c in listed["items"]] == [other] and listed["total"] == 1
    for q in ("Selfie", SELF_EMAIL, SELF_SLUG):
        found = (await client.get("/api/v1/contacts", params={"q": q})).json()
        assert you not in [c["id"] for c in found["items"]], q
    queried = await client.post("/api/v1/contacts/query", json={}, headers=CSRF)
    assert [c["id"] for c in queried.json()["items"]] == [other]


async def test_the_contact_itself_answers_404_like_another_users(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    you, _ = _setup(running_app)
    base = f"/api/v1/contacts/{you}"
    assert (await client.get(base)).status_code == 404
    assert (await client.patch(base, json={"notes": "x"}, headers=CSRF)).status_code == 404
    assert (await client.post(f"{base}/archive", headers=CSRF)).status_code == 404
    interaction = {"kind": "note", "at": NOW.isoformat()}
    assert (
        await client.post(f"{base}/interactions", json=interaction, headers=CSRF)
    ).status_code == 404
    merge_fields = await client.get("/api/v1/templates/merge-fields", params={"contact_id": you})
    assert merge_fields.status_code == 404


async def test_merges_and_duplicate_hints_leave_it_out(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    you, other = _setup(running_app)
    for path in ("merge", "merge/preview"):
        into_other = f"/api/v1/contacts/{other}/{path}"
        assert (
            await client.post(into_other, json={"loser_id": you}, headers=CSRF)
        ).status_code == 404
        into_you = f"/api/v1/contacts/{you}/{path}"
        assert (
            await client.post(into_you, json={"loser_id": other}, headers=CSRF)
        ).status_code == 404
    hints = await client.get(f"/api/v1/contacts/{other}/duplicates")
    assert hints.status_code == 200, hints.text
    # Same name, company and email, and still no hint.
    assert you not in [d["contact_id"] for d in hints.json()]
    with session_scope(_factory(running_app)) as session:
        user = session.scalars(select(User)).one()
        _, duplicates = possible_duplicates(session, user, other)
        assert you not in [d.contact.id for d in duplicates]


async def test_exports_leave_it_out(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    _setup(running_app)
    for preset in ("full", "nine-column", "campaign-audience"):
        response = await client.get("/api/v1/exports", params={"preset": preset, "format": "csv"})
        assert response.status_code == 200, response.text
        assert response.text.count(SELF_EMAIL) == 1, preset  # the lookalike's row only


async def test_static_lists_refuse_it(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    you, other = _setup(running_app)
    created = await client.post(
        "/api/v1/lists", json={"name": "First 100", "kind": "static"}, headers=CSRF
    )
    members = f"/api/v1/lists/{created.json()['id']}/members"
    refused = await client.post(members, json={"contact_ids": [other, you]}, headers=CSRF)
    assert refused.status_code == 404
    assert (await client.get(members)).json()["items"] == []


async def test_stats_leave_it_out(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    _setup(running_app)
    stats = (await client.get("/api/v1/contacts/stats")).json()
    assert stats["total"] == 1 and stats["with_email"] == 1


async def test_enrichment_pins_refuse_it(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    you, _ = _setup(running_app)
    pinned = await client.post("/api/v1/linkedin/pins", json={"contact_id": you}, headers=CSRF)
    assert pinned.status_code == 404


# --- every surface leaves it out: the services --------------------------------------------


def test_triage_never_serves_it(writer: Session, user: User, you: Contact) -> None:
    assert triage.next_contact(writer, user) is None
    assert triage.progress(writer, user).total == 0
    other = factories.make_contact(writer, user)
    card = triage.next_contact(writer, user)
    assert card is not None and card.id == other.id


def test_the_scope_helper_leaves_only_it_out(writer: Session, user: User, you: Contact) -> None:
    other = factories.make_contact(writer, user)
    assert [c.id for c in writer.scalars(scoped_contacts(user))] == [other.id]
    assert writer.scalar(scoped_contacts_count(user)) == 1
    assert {c.id for c in writer.scalars(scoped(user, Contact))} == {you.id, other.id}
    assert crm_contacts.contact_stats(writer, user).total == 1


def test_linkedin_sync_and_import_matching_never_reach_it(
    writer: Session, user: User, you: Contact
) -> None:
    """Identity resolution (spec 8.2) by URN, slug, email, and name and company."""
    assert SELF_URN not in known_urns(writer, user)
    rows = [
        identity.IncomingContact(source=ContactSource.SYNC, li_urn=SELF_URN),
        identity.IncomingContact(source=ContactSource.SYNC, li_public_id=SELF_SLUG),
        identity.IncomingContact(
            source=ContactSource.CSV, emails=(identity.IncomingEmail(SELF_EMAIL),)
        ),
        identity.IncomingContact(
            source=ContactSource.CSV,
            first_name="Selfie",
            last_name="Owner",
            current_company="Example Co",
        ),
    ]
    for row in rows:
        assert isinstance(identity.resolve(writer, user, row), identity.New), row


def test_enrichment_never_plans_it(writer: Session, user: User, you: Contact) -> None:
    account = ensure_account(writer, user)
    other = factories.make_contact(writer, user)
    planned = enrich_plan.prioritize(writer, user, account.id, now=NOW, limit=50, stale_days=180)
    assert [contact_id for contact_id, _ in planned] == [other.id]


def test_campaign_audiences_and_the_enrollment_guards_leave_it_out(
    writer: Session, user: User, you: Contact
) -> None:
    campaign = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    other = factories.make_contact(writer, user, emails=["other@example.test"])
    outcome = campaign_service.enroll(writer, user, campaign.id, now=NOW, filter=FilterTree())
    assert (outcome.enrolled, outcome.excluded) == (1, 0)  # the filter never names it
    # Named by id, the guards exclude it with their own reason.
    result = engine_enroll(writer, user, campaign.id, [you.id, other.id], now=NOW)
    [verdict] = [v for v in result.verdicts if v.contact_id == you.id]
    assert verdict.reasons == (Reason.SELF,)
    assert you.id not in result.enrolled


def test_dashboard_and_tag_counts_leave_it_out(writer: Session, user: User, you: Contact) -> None:
    writer.add(
        ContactSnapshot(
            user_id=user.id,
            contact_id=you.id,
            current_title="Old",
            position_changed=True,
            source=ContactSource.SYNC,
            observed_at=NOW - timedelta(days=1),
        )
    )
    tag = create_tag(writer, user, name="Friends")
    writer.add(
        ContactTag(user_id=user.id, contact_id=you.id, tag_id=tag.id, source=TagSource.MANUAL)
    )
    writer.flush()
    jobs, total = dashboard.changed_jobs(writer, user, now=NOW, limit=10)
    assert (jobs, total) == ([], 0)
    assert contact_counts(writer, user, [tag.id]) == {}


# --- more surfaces (#396 review) -----------------------------------------------------------


def test_a_bulk_action_by_ids_never_writes_it(writer: Session, user: User, you: Contact) -> None:
    other = factories.make_contact(writer, user)
    selection = crm_contacts.Selection(ids=(you.id, other.id))
    assert crm_contacts.count_selection(writer, user, selection) == 1
    written = crm_contacts.bulk_update(
        writer, user, selection, "archive", expected_count=1, now=NOW
    )
    assert written == 1
    writer.refresh(you)
    writer.refresh(other)
    assert you.archived_at is None and other.archived_at is not None


def test_an_auto_tag_rule_run_never_tags_it(writer: Session, user: User, you: Contact) -> None:
    other = factories.make_contact(writer, user, current_company="Example Co")
    tag = create_tag(writer, user, name="Example")
    create_rule(writer, user, tag.id, RuleField.COMPANY, "Example")
    run_rules(writer, user)
    tagged = set(writer.scalars(scoped(user, ContactTag).with_only_columns(ContactTag.contact_id)))
    assert tagged == {other.id}


def test_aging_after_a_full_sync_never_counts_or_ages_it(
    writer: Session, user: User, you: Contact
) -> None:
    """Its URN is never in a sync: aged with the network, it would count as a miss."""
    seen = {factories.make_contact(writer, user).li_urn for _ in range(10)}
    counts = age_unseen(
        writer,
        user,
        frozenset(urn for urn in seen if urn is not None),
        observed_at=NOW,
        disconnect_after_misses=1,
        created_by_sync=frozenset(),
    )
    assert (counts.missed, counts.disconnected, counts.refused) == (0, 0, None)
    writer.refresh(you)
    assert (you.li_missing_count, you.li_disconnected_at) == (0, None)


def test_a_sync_sighting_never_touches_it(writer: Session, user: User, you: Contact) -> None:
    you.li_missing_count = 1
    you.li_disconnected_at = NOW
    writer.flush()
    assert _mark_seen(writer, user, urns={SELF_URN}, public_ids={SELF_SLUG}) == 0
    writer.refresh(you)
    assert (you.li_missing_count, you.li_disconnected_at) == (1, NOW)


def test_a_static_lists_members_leave_it_out(writer: Session, user: User, you: Contact) -> None:
    """Even a membership row written before it was the self contact, or by hand."""
    other = factories.make_contact(writer, user)
    listed = create_list(writer, user, name="First 100", kind=ListKind.STATIC)
    for contact in (you, other):
        writer.add(ListMember(user_id=user.id, list_id=listed.id, contact_id=contact.id))
    writer.flush()
    members, total = list_members(writer, user, listed.id, limit=50)
    assert [m.id for m in members] == [other.id] and total == 1


def test_its_interactions_cannot_be_edited_or_deleted(
    writer: Session, user: User, you: Contact
) -> None:
    """Scoped through the contact: an interaction on the self contact is not found."""
    row = Interaction(
        user_id=user.id, contact_id=you.id, kind=InteractionKind.NOTE, at=NOW, summary="x"
    )
    writer.add(row)
    writer.flush()
    with pytest.raises(InteractionNotFound):
        update_interaction(writer, user, row.id, summary="y")
    with pytest.raises(InteractionNotFound):
        delete_interaction(writer, user, row.id)
    other = factories.make_contact(writer, user)
    mine = add_interaction(writer, user, other.id, kind=InteractionKind.NOTE, at=NOW)
    assert update_interaction(writer, user, mine.id, summary="y").summary == "y"


def test_the_enroll_outcome_names_yourself_among_the_excluded(
    writer: Session, user: User, you: Contact
) -> None:
    campaign = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    other = factories.make_contact(writer, user, emails=["other@example.test"])
    outcome = campaign_service.enroll(
        writer, user, campaign.id, now=NOW, contact_ids=[you.id, other.id]
    )
    assert (outcome.enrolled, outcome.excluded) == (1, 1)
    assert outcome.excluded_summary == "1 will start, 1 skipped (1 yourself)"


# --- an active campaign using a removed field (S2) ----------------------------------------


def _use_me(session: Session, user: User, name: str) -> int:
    campaign = factories.make_campaign(session, user, name=name)
    template = campaign.steps[0].template
    assert template is not None
    template.body = "Hi {{ first_name }}, {{ me.signature }}"
    session.flush()
    return campaign.id


def test_removed_field_campaigns_lists_active_and_paused_ones_only(
    writer: Session, user: User
) -> None:
    active = _use_me(writer, user, "Active")
    paused = _use_me(writer, user, "Paused")
    draft = _use_me(writer, user, "Draft")
    clean = factories.make_campaign(writer, user, name="Clean")
    for campaign_id, status in ((paused, CampaignStatus.PAUSED), (draft, CampaignStatus.DRAFT)):
        row = get_scoped(writer, user, Campaign, campaign_id)
        assert row is not None
        row.status = status
    writer.flush()
    found = removed_field_campaigns(writer, user)
    assert [(c.campaign_id, c.step_positions) for c in found] == [(active, (1,)), (paused, (1,))]
    assert clean.id not in [c.campaign_id for c in found]


async def test_the_app_start_warns_about_them(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    async with app.router.lifespan_context(app):
        with session_scope(_factory(app), write=True) as session:
            _use_me(session, session.scalars(select(User)).one(), "First 100")
    with caplog.at_level(logging.WARNING, logger="netkeeper.web.app"):
        async with app.router.lifespan_context(app):
            pass
    [warning] = [
        r.getMessage()
        for r in caplog.records
        if r.name == "netkeeper.web.app" and "removed me.* fields" in r.getMessage()
    ]
    assert "'First 100'" in warning and "removed me.* fields" in warning
    assert "Publish a new template version" in warning


async def test_the_campaign_page_shows_why_an_enrollment_is_blocked(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = session.scalars(select(User)).one()
        campaign = factories.make_campaign(session, user)
        contact = factories.make_contact(session, user)
        enrollment = factories.make_enrollment(session, campaign, contact)
        enrollment.not_sent_error = REMOVED_FIELD_BLOCK
        campaign_id = campaign.id
    page = (await client.get(f"/api/v1/campaigns/{campaign_id}/enrollments")).json()
    assert [row["not_sent_error"] for row in page["items"]] == [REMOVED_FIELD_BLOCK]
