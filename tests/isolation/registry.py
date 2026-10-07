"""Registered list endpoints. Add one :class:`ListEndpoint` here per list operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
from sqlalchemy.orm import Session

from netkeeper.campaigns import templates as template_service
from netkeeper.config import Settings
from netkeeper.crm import do_not_send
from netkeeper.crm import import_runs as import_service
from netkeeper.crm import lists as list_service
from netkeeper.crm import positions as position_service
from netkeeper.crm import tags as tag_service
from netkeeper.crm.filters import parse_filter
from netkeeper.crm.interactions import add_interaction
from netkeeper.models import (
    Campaign,
    Contact,
    ContactList,
    ContactSnapshot,
    ContactSource,
    DoNotSendReason,
    ImportRun,
    InteractionKind,
    JsonValue,
    ListKind,
    MessageDirection,
    MessageStatus,
    RuleField,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    Template,
    TemplateChannel,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import scoped
from netkeeper.services import campaigns as campaign_service
from netkeeper.services import enrich_plan, run_contacts, runs
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.web.app import API_PREFIX

Body = dict[str, Any]


@dataclass(frozen=True)
class ListEndpoint:
    """A list endpoint under the isolation test.

    ``path`` is the full path as the OpenAPI schema spells it (``/api/v1/contacts``,
    ``/api/v1/contacts/{contact_id}/interactions``). ``seed`` creates rows for the
    given user, through the services, and returns how many items the list should
    then show that user; it runs once per user on a fresh database. ``count``
    extracts the item count from the decoded JSON body (:func:`paged_count` and
    :func:`array_count` cover the two response shapes).

    A path with parameters needs ``path_params``: it runs for every user after
    the seeding, in the same session, and returns the values to format the path
    with, as strings. It also runs for the third, unseeded user, so it must
    produce a resource of that user's own (creating an empty one if there is
    none) or leave the placeholder pointing at nothing. That user must then get
    a ``404`` or an empty list; a ``404`` alone would not tell an endpoint that
    filters by user from one that only checks the parent exists.

    A list behind a ``POST`` (a query whose filter does not fit a query string)
    sets ``method`` and ``body``: the JSON to send, as a dict or a callable that
    runs like ``path_params`` for each user. The harness adds the CSRF header.
    """

    path: str
    seed: Callable[[Session, User], int]
    count: Callable[[Any], int]
    path_params: Callable[[Session, User], dict[str, str]] | None = None
    method: str = "GET"
    body: Body | Callable[[Session, User], Body] | None = None


def paged_count(body: Any) -> int:
    """Item count of a paged response: an object with an ``items`` array."""
    items = body["items"]
    assert isinstance(items, list), f"items is not an array: {items!r}"
    return len(items)


def array_count(body: Any) -> int:
    """Item count of a plain array response."""
    assert isinstance(body, list), f"body is not an array: {body!r}"
    return len(body)


# --- seeds ------------------------------------------------------------------

SEED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def own_contact(session: Session, user: User) -> dict[str, str]:
    """``contact_id`` of the user's first contact; a fresh, empty one when they have none."""
    contact = session.scalars(scoped(user, Contact).order_by(Contact.id)).first()
    if contact is None:
        contact = factories.make_contact(session, user)
    return {"contact_id": str(contact.id)}


def seed_contacts(session: Session, user: User) -> int:
    """Two live contacts of ``user``, one with an address and one with a number.

    Public because the bulk isolation test in ``test_isolation.py`` seeds the
    same way: bulk is not a list operation, so it is not in ``REGISTRY``, but it
    gets the same two-user treatment.
    """
    factories.make_contact(session, user, emails=[f"seed-{user.id}@example.test"])
    factories.make_contact(session, user, phones=["+15550100"])
    return 2


def _seed_duplicates(session: Session, user: User) -> int:
    """Two contacts of ``user`` with one name and one address: each the other's duplicate (#363).

    Every user gets the same name and address, so an unscoped match would list
    the other users' copies too.
    """
    for _ in range(2):
        factories.make_contact(
            session,
            user,
            first_name="Twin",
            last_name="Seed",
            li_urn=None,
            li_public_id=None,
            emails=["twin.seed@example.test"],
        )
    return 1


def _seed_interactions(session: Session, user: User) -> int:
    contact = factories.make_contact(session, user)
    add_interaction(session, user, contact.id, InteractionKind.NOTE, SEED_AT, "met at a meetup")
    add_interaction(session, user, contact.id, InteractionKind.EMAIL_OUT, SEED_AT, "followed up")
    return 2


def _seed_timeline(session: Session, user: User) -> int:
    contact = factories.make_contact(session, user)
    add_interaction(session, user, contact.id, InteractionKind.NOTE, SEED_AT, "met at a meetup")
    session.add(
        ContactSnapshot(
            user_id=user.id, contact_id=contact.id, headline="Before", observed_at=SEED_AT
        )
    )
    session.flush()
    return 2


# A file with its own mapping, so the seed does not depend on preset detection.
IMPORT_CSV = (
    "First Name,Last Name,Company\n"
    "Hortensia,Blennerhassett,Tarnish and Sons\n"
    "Peregrine,Wollstonecraft,Vellum Press\n"
)
EMPTY_IMPORT_CSV = "First Name,Last Name,Company\n"
IMPORT_MAPPING = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Company": "current_company",
}


def own_import_run(session: Session, user: User) -> dict[str, str]:
    """``run_id`` of the user's first import run; a fresh, empty one when they have none."""
    run = session.scalars(scoped(user, ImportRun).order_by(ImportRun.id)).first()
    if run is None:
        run = import_service.create_run(
            session,
            user,
            filename="empty.csv",
            content=EMPTY_IMPORT_CSV,
            mapping=IMPORT_MAPPING,
        )
    return {"run_id": str(run.id)}


def _seed_import_runs(session: Session, user: User) -> int:
    import_service.create_run(
        session, user, filename="people.csv", content=IMPORT_CSV, mapping=IMPORT_MAPPING
    )
    return 1


def _seed_import_rows(session: Session, user: User) -> int:
    run = import_service.create_run(
        session, user, filename="people.csv", content=IMPORT_CSV, mapping=IMPORT_MAPPING
    )
    return run.total_rows


def _seed_import_presets(session: Session, user: User) -> int:
    import_service.save_preset(session, user, f"mine-{user.id}", IMPORT_MAPPING)
    import_service.save_preset(session, user, f"also-mine-{user.id}", IMPORT_MAPPING)
    return 2


def saved_presets_count(body: Any) -> int:
    """The user's own presets; the built-in ones are the same for everybody (#78)."""
    saved = body["saved"]
    assert isinstance(saved, list), f"saved is not an array: {saved!r}"
    return len(saved)


def _seed_tags(session: Session, user: User) -> int:
    tag_service.create_tag(session, user, "seeded")
    return 1


def _seed_contact_tags(session: Session, user: User) -> int:
    """One tagged contact, the first the user has, so ``own_contact`` points at it."""
    contact = factories.make_contact(session, user)
    tag = tag_service.create_tag(session, user, "seeded")
    tag_service.tag_contact(session, user, contact.id, tag.id)
    return 1


def _seed_autotag_rules(session: Session, user: User) -> int:
    tag = tag_service.create_tag(session, user, "seeded")
    tag_service.create_rule(session, user, tag.id, RuleField.TITLE, r"\bseeded\b")
    return 1


def _seed_exports(session: Session, user: User) -> int:
    factories.make_contact(session, user, emails=["seeded@example.test"])
    factories.make_contact(session, user)
    return 2


def own_list(session: Session, user: User) -> dict[str, str]:
    """``list_id`` of the user's first list; a fresh, empty static one when they have none."""
    row = session.scalars(scoped(user, ContactList).order_by(ContactList.id)).first()
    if row is None:
        row = list_service.create_list(session, user, "empty", ListKind.STATIC)
    return {"list_id": str(row.id)}


def _seed_lists(session: Session, user: User) -> int:
    list_service.create_list(session, user, "seeded", ListKind.STATIC)
    return 1


def _seed_list_members(session: Session, user: User) -> int:
    contact = factories.make_contact(session, user)
    row = list_service.create_list(session, user, "seeded", ListKind.STATIC)
    list_service.add_members(session, user, row.id, [contact.id])
    return 1


def _seed_list_query(session: Session, user: User) -> int:
    """Two reachable contacts of ``user``, and a smart list of ``user``'s that holds them.

    For the ``list_member`` predicate (P1-27): the filter names a list id, so
    the harness's crossed call hands one user the other's list id, and nothing
    of the owner's may come back.

    The list is *smart* on purpose. A static list would cross weakly: its
    membership rows name the owner's contacts, which the caller's own scoping
    already keeps out, so the answer is empty either way. A smart list is
    inlined — its stored tree is compiled against whoever is asking — so a
    lookup that stopped being scoped would answer the caller with their own
    contacts under the owner's definition, which is both a leak of that
    definition and an oracle for which list ids exist. Here that shows up as a
    crossed call returning rows.
    """
    for index in range(2):
        factories.make_contact(session, user, emails=[f"list-{user.id}-{index}@example.test"])
    list_service.create_list(
        session,
        user,
        "isolation",
        ListKind.SMART,
        filter=parse_filter({"where": {"op": "has_email"}}),
    )
    return 2


def list_member_body(session: Session, user: User) -> Body:
    """``POST /contacts/query`` filtered to ``user``'s own first list."""
    return {
        "filter": {
            "where": {"op": "list_member", "list_id": int(own_list(session, user)["list_id"])}
        }
    }


def _seed_views(session: Session, user: User) -> int:
    list_service.create_view(session, user, "seeded", ["first_name", "last_name"])
    return 1


def _seed_mailboxes(session: Session, user: User) -> int:
    """One connected mailbox and one disconnected (still listed, #244)."""
    old = mailbox_service.connect(
        session, user, f"old{user.id}@example.com", f"rt-old-{user.id}", daily_cap=80
    )
    mailbox_service.disconnect(session, user, old)
    mailbox_service.connect(
        session, user, f"user{user.id}@example.com", f"rt-{user.id}", daily_cap=80
    )
    return 2


def _seed_poll_status(session: Session, user: User) -> int:
    """One armed mailbox polled for replies, and one completed enrichment (#401).

    Each user's mailbox, its reply poll, and its last LinkedIn run are their own:
    :func:`poll_status_count` counts the mailboxes and every check with a last run.
    The mailbox is armed and polled, so the Gmail replies check has a last run too.
    """
    mailbox = mailbox_service.connect(
        session, user, f"poll{user.id}@example.com", f"rt-poll-{user.id}", daily_cap=80
    )
    mailbox.armed_at = SEED_AT
    mailbox.replies_polled_at = SEED_AT
    run = runs.create_run(
        session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=SEED_AT
    )
    runs.finish_run(session, user, run.id, status=SyncRunStatus.COMPLETED, now=SEED_AT)
    session.flush()
    return 3  # the mailbox, the Gmail replies check's last run, the enrichment's


def poll_status_count(body: Any) -> int:
    """``GET /poll-status``: its mailboxes, and its checks that have a last run.

    The checks themselves are the same list for every user, so their count alone
    would say nothing about isolation; what each one last saw is the user's own."""
    mailboxes = body["mailboxes"]
    items = body["items"]
    assert isinstance(mailboxes, list) and isinstance(items, list)
    return len(mailboxes) + sum(1 for item in items if item["last_at"] is not None)


def _seed_templates(session: Session, user: User) -> int:
    """Two templates, one of them in its second version: the older version is not listed.

    The second version is written directly, as a campaign's use of the first would
    make it (P3-04 wires that up), so the list's "nothing replaced it" subquery is
    under the crossed call too.
    """
    body = "Hi {{ first_name }}"
    first = template_service.create_template(
        session,
        user,
        name="reconnect",
        channel=TemplateChannel.EMAIL,
        subject="Hello",
        body=body,
    )
    session.add(
        Template(
            user_id=user.id,
            name=first.name,
            channel=first.channel,
            subject=first.subject,
            body=body + "!",
            version=2,
            previous_id=first.id,
        )
    )
    template_service.create_template(
        session,
        user,
        name="nudge",
        channel=TemplateChannel.LINKEDIN,
        subject=None,
        body=body,
    )
    session.flush()
    return 2


def _seed_triage_suggestions(session: Session, user: User) -> int:
    """A contact with message history, which is what the bulk suggestion offers to mark met.

    One suggestion, however many contacts it covers: the endpoint lists the
    actions worth offering, and one that matches nobody is left out, so the
    unseeded user is served an empty array.
    """
    contact = factories.make_contact(session, user)
    add_interaction(
        session,
        user,
        contact.id,
        InteractionKind.LI_IN,
        SEED_AT,
        "a message",
        source=ContactSource.ARCHIVE,
    )
    return 1


def _suggestion_key(_session: Session, _user: User) -> dict[str, str]:
    """The message-history batch, whose key is the same for everybody.

    A user with no message history is served an empty page rather than a 404,
    which is what makes this a real isolation test: the seeded user sees their
    own contact, and the unseeded one sees nobody.
    """
    return {"key": "met_with_messages"}


def _seed_positions(session: Session, user: User) -> int:
    position_service.add_position(session, user, company="Seeded Works", title="Engineer")
    return 1


def _seed_runs(session: Session, user: User) -> int:
    """Two finished LinkedIn runs of ``user`` (P2-10)."""
    first = runs.create_run(
        session, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=SEED_AT
    )
    runs.finish_run(session, user, first.id, status=SyncRunStatus.COMPLETED, now=SEED_AT)
    second = runs.create_run(
        session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=SEED_AT
    )
    runs.finish_run(session, user, second.id, status=SyncRunStatus.ABORTED, now=SEED_AT)
    return 2


def _seed_run_contacts(session: Session, user: User) -> int:
    """Two contacts ``user``'s newest run touched (#324)."""
    run = runs.create_run(
        session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=SEED_AT
    )
    runs.finish_run(session, user, run.id, status=SyncRunStatus.COMPLETED, now=SEED_AT)
    touched = [(factories.make_contact(session, user).id, "applied") for _ in range(2)]
    run_contacts.record(session, user, run.linkedin_account_id, run.id, touched)
    return 2


def _seed_run_diagnostics(session: Session, user: User) -> int:
    """An aborted enrichment of ``user``: two unreadable visits on its contacts, and the
    visit that stopped it at once (#405)."""
    run = runs.create_run(
        session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=SEED_AT
    )
    contacts = [factories.make_contact(session, user).id for _ in range(3)]
    visits: list[JsonValue] = [
        {"visit": number + 1, "contact_id": contacts[number], "reason": "x"} for number in (0, 1)
    ]
    stopped: JsonValue = {"visit": 3, "contact_id": contacts[2], "reason": "profile_status"}
    runs.finish_run(
        session,
        user,
        run.id,
        status=SyncRunStatus.ABORTED,
        now=SEED_AT,
        counts={"unreadable_visits": visits, "stopped_by": stopped},
    )
    return 3


def diagnostics_count(body: Any) -> int:
    """Item count of a run's diagnostics: every visit and answer it names."""
    return len(body["unreadable_visits"]) + len(body["lost_answers"]) + len(body["stopped_by"])


def _own_run(session: Session, user: User) -> dict[str, str]:
    """``run_id`` of the user's newest run; the placeholder points nowhere when they
    have none, which the endpoint answers ``404``."""
    run = session.scalars(scoped(user, SyncRun).order_by(SyncRun.id.desc())).first()
    return {"run_id": "0" if run is None else str(run.id)}


def _seed_pins(session: Session, user: User) -> int:
    """Two contacts of ``user`` pinned to the front of the next enrichment."""
    account = ensure_account(session, user)
    for _ in range(2):
        enrich_plan.pin(session, user, account.id, factories.make_contact(session, user).id)
    return 2


def _seed_next_fires(session: Session, user: User) -> int:
    """Two due enrollments of ``user`` in one active email campaign (P3-12)."""
    campaign = factories.make_campaign(session, user)
    for _ in range(2):
        factories.make_enrollment(
            session, campaign, factories.make_contact(session, user), next_action_at=SEED_AT
        )
    return 2


def _seed_linkedin_ready(session: Session, user: User) -> int:
    """Two due enrollments of ``user`` in one active LinkedIn campaign (P4-09), and one
    whose prefill typed nothing, waiting for Try again (``try_again``, #445)."""
    campaign = factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))
    for _ in range(2):
        factories.make_enrollment(
            session, campaign, factories.make_contact(session, user), next_action_at=SEED_AT
        )
    factories.make_enrollment(
        session,
        campaign,
        factories.make_contact(session, user),
        next_action_at=None,
        not_sent_count=1,
        not_sent_error="not_typed: the browser was busy",
    )
    return 2


def _seed_linkedin_waiting(session: Session, user: User) -> int:
    """A prefilled and a stale LinkedIn message of ``user``, and a sent one that waits for
    nobody (P4-09)."""
    campaign = factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))
    for status in (MessageStatus.PREFILLED, MessageStatus.STALE, MessageStatus.SENT):
        enrollment = factories.make_enrollment(
            session, campaign, factories.make_contact(session, user)
        )
        factories.make_message(session, enrollment, status=status, prefilled_at=SEED_AT)
    return 2


def _seed_campaigns(session: Session, user: User) -> int:
    """Two draft campaigns of ``user`` from the campaign service (P3-13)."""
    mailbox = mailbox_service.connect(
        session, user, f"camp{user.id}@example.com", f"rt-camp-{user.id}", daily_cap=80
    )
    template = template_service.create_template(
        session,
        user,
        name="campaign step",
        channel=TemplateChannel.EMAIL,
        subject="Hello",
        body="Hi {{ first_name }}",
    )
    for name in ("first", "second"):
        campaign_service.create_campaign(
            session,
            user,
            name=name,
            steps=[campaign_service.StepSpec(template_id=template.id)],
            settings=Settings(),
            mailbox_id=mailbox.id,
        )
    return 2


def _seed_changed_jobs(session: Session, user: User) -> int:
    """One contact of ``user`` whom enrichment saw change job two days ago (P3-12, #286)."""
    contact = factories.make_contact(session, user, current_title="Seeded", current_company="New")
    session.add(
        ContactSnapshot(
            user_id=user.id,
            contact_id=contact.id,
            current_title="Seeded",
            current_company="Seed Co",
            position_changed=True,
            source=ContactSource.SYNC,
            observed_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    session.flush()
    return 1


def _seed_enrollments(session: Session, user: User) -> int:
    """One draft campaign of ``user`` with two contacts enrolled (P3-11)."""
    _seed_campaigns(session, user)
    campaign = session.scalars(scoped(user, Campaign).order_by(Campaign.id)).first()
    assert campaign is not None
    contacts = [
        factories.make_contact(session, user, emails=[f"enr{n}-{user.id}@example.test"]).id
        for n in range(2)
    ]
    campaign_service.enroll(session, user, campaign.id, now=utcnow(), contact_ids=contacts)
    return 2


def _seed_inbox(session: Session, user: User) -> int:
    """A reply and a bounce in one of ``user``'s campaigns, and a sent message that is
    neither (P3-11b)."""
    campaign = factories.make_campaign(session, user)
    enrollment = factories.make_enrollment(session, campaign, factories.make_contact(session, user))
    factories.make_message(session, enrollment)
    factories.make_message(session, enrollment, status=MessageStatus.BOUNCED, bounced_at=SEED_AT)
    factories.make_message(
        session,
        enrollment,
        direction=MessageDirection.IN,
        status=MessageStatus.RECEIVED,
        subject="Re: Hello",
        snippet="Good to hear from you",
        sent_at=SEED_AT,
    )
    return 2


def _seed_do_not_send(session: Session, user: User) -> int:
    """Two of ``user``'s addresses on the do-not-send list (#238): one by hand, one bounced."""
    do_not_send.add_by_hand(session, user, f"hand-{user.id}@example.test")
    do_not_send.add(session, user, f"bounced-{user.id}@example.test", DoNotSendReason.BOUNCED)
    return 2


def _own_campaign(session: Session, user: User) -> dict[str, str]:
    """``campaign_id`` of the user's first campaign; the placeholder points nowhere when
    they have none, which the endpoint answers ``404``."""
    campaign = session.scalars(scoped(user, Campaign).order_by(Campaign.id)).first()
    return {"campaign_id": "0" if campaign is None else str(campaign.id)}


REGISTRY: list[ListEndpoint] = [
    ListEndpoint(f"{API_PREFIX}/contacts", seed_contacts, paged_count),
    ListEndpoint(
        f"{API_PREFIX}/contacts/query",
        seed_contacts,
        paged_count,
        method="POST",
        body={"filter": {"where": {"op": "has_li_url"}}, "sort": [{"field": "last_name"}]},
    ),
    ListEndpoint(
        f"{API_PREFIX}/contacts/query",
        _seed_list_query,
        paged_count,
        method="POST",
        body=list_member_body,
    ),
    ListEndpoint(
        f"{API_PREFIX}/contacts/{{contact_id}}/interactions",
        _seed_interactions,
        paged_count,
        path_params=own_contact,
    ),
    ListEndpoint(
        f"{API_PREFIX}/contacts/{{contact_id}}/timeline",
        _seed_timeline,
        paged_count,
        path_params=own_contact,
    ),
    ListEndpoint(
        f"{API_PREFIX}/contacts/{{contact_id}}/duplicates",
        _seed_duplicates,
        array_count,
        path_params=own_contact,
    ),
    ListEndpoint(
        f"{API_PREFIX}/contacts/{{contact_id}}/tags",
        _seed_contact_tags,
        array_count,
        path_params=own_contact,
    ),
    ListEndpoint(f"{API_PREFIX}/imports", _seed_import_runs, paged_count),
    ListEndpoint(f"{API_PREFIX}/imports/presets", _seed_import_presets, saved_presets_count),
    ListEndpoint(
        f"{API_PREFIX}/imports/{{run_id}}/rows",
        _seed_import_rows,
        paged_count,
        path_params=own_import_run,
    ),
    ListEndpoint(f"{API_PREFIX}/tags", _seed_tags, array_count),
    ListEndpoint(f"{API_PREFIX}/autotag-rules", _seed_autotag_rules, array_count),
    ListEndpoint(f"{API_PREFIX}/exports", _seed_exports, array_count),
    ListEndpoint(f"{API_PREFIX}/lists", _seed_lists, array_count),
    ListEndpoint(
        f"{API_PREFIX}/lists/{{list_id}}/members",
        _seed_list_members,
        paged_count,
        path_params=own_list,
    ),
    ListEndpoint(f"{API_PREFIX}/views", _seed_views, array_count),
    ListEndpoint(f"{API_PREFIX}/templates", _seed_templates, array_count),
    ListEndpoint(f"{API_PREFIX}/triage/suggestions", _seed_triage_suggestions, array_count),
    ListEndpoint(
        f"{API_PREFIX}/triage/suggestions/{{key}}/contacts",
        _seed_triage_suggestions,
        paged_count,
        path_params=_suggestion_key,
    ),
    ListEndpoint(f"{API_PREFIX}/me/positions", _seed_positions, array_count),
    ListEndpoint(f"{API_PREFIX}/linkedin/runs", _seed_runs, paged_count),
    ListEndpoint(
        f"{API_PREFIX}/linkedin/runs/{{run_id}}/contacts",
        _seed_run_contacts,
        paged_count,
        path_params=_own_run,
    ),
    ListEndpoint(
        f"{API_PREFIX}/linkedin/runs/{{run_id}}/diagnostics",
        _seed_run_diagnostics,
        diagnostics_count,
        path_params=_own_run,
    ),
    ListEndpoint(f"{API_PREFIX}/linkedin/pins", _seed_pins, array_count),
    ListEndpoint(f"{API_PREFIX}/mailboxes", _seed_mailboxes, array_count),
    ListEndpoint(f"{API_PREFIX}/campaigns", _seed_campaigns, array_count),
    ListEndpoint(
        f"{API_PREFIX}/campaigns/{{campaign_id}}/enrollments",
        _seed_enrollments,
        paged_count,
        path_params=_own_campaign,
    ),
    ListEndpoint(f"{API_PREFIX}/inbox", _seed_inbox, paged_count),
    ListEndpoint(f"{API_PREFIX}/do-not-send", _seed_do_not_send, array_count),
    ListEndpoint(f"{API_PREFIX}/dashboard/next-fires", _seed_next_fires, paged_count),
    ListEndpoint(f"{API_PREFIX}/campaigns/linkedin/ready", _seed_linkedin_ready, paged_count),
    ListEndpoint(f"{API_PREFIX}/campaigns/linkedin/waiting", _seed_linkedin_waiting, paged_count),
    ListEndpoint(f"{API_PREFIX}/dashboard/changed-jobs", _seed_changed_jobs, paged_count),
    ListEndpoint(f"{API_PREFIX}/poll-status", _seed_poll_status, poll_status_count),
]
