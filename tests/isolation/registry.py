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
from netkeeper.crm import import_runs as import_service
from netkeeper.crm import lists as list_service
from netkeeper.crm import positions as position_service
from netkeeper.crm import tags as tag_service
from netkeeper.crm.filters import parse_filter
from netkeeper.crm.interactions import add_interaction
from netkeeper.models import (
    Contact,
    ContactList,
    ContactSnapshot,
    ContactSource,
    ImportRun,
    InteractionKind,
    ListKind,
    RuleField,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    Template,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services import campaigns as campaign_service
from netkeeper.services import enrich_plan, runs
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
        me_keys=(),
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
        me_keys=(),
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
        me_keys=(),
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
    """One contact of ``user`` who started a job two days ago (P3-12)."""
    started = datetime.now(UTC).date() - timedelta(days=2)
    position = {"title": "Seeded", "company": "Seed Co", "started_on": started}
    factories.make_contact(session, user, positions=[position])
    return 1


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
    ListEndpoint(f"{API_PREFIX}/linkedin/pins", _seed_pins, array_count),
    ListEndpoint(f"{API_PREFIX}/mailboxes", _seed_mailboxes, array_count),
    ListEndpoint(f"{API_PREFIX}/campaigns", _seed_campaigns, array_count),
    ListEndpoint(f"{API_PREFIX}/dashboard/next-fires", _seed_next_fires, paged_count),
    ListEndpoint(f"{API_PREFIX}/dashboard/changed-jobs", _seed_changed_jobs, paged_count),
]
