"""Registered list endpoints. Add one :class:`ListEndpoint` here per list operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import factories
from sqlalchemy.orm import Session

from netkeeper.crm import tags as tag_service
from netkeeper.crm.interactions import add_interaction
from netkeeper.models import Contact, ContactSnapshot, InteractionKind, RuleField, User
from netkeeper.scoping import scoped
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


def _seed_tags(session: Session, user: User) -> int:
    tag_service.create_tag(session, user, "seeded")
    return 1


def _seed_autotag_rules(session: Session, user: User) -> int:
    tag = tag_service.create_tag(session, user, "seeded")
    tag_service.create_rule(session, user, tag.id, RuleField.TITLE, r"\bseeded\b")
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
    ListEndpoint(f"{API_PREFIX}/tags", _seed_tags, array_count),
    ListEndpoint(f"{API_PREFIX}/autotag-rules", _seed_autotag_rules, array_count),
]
