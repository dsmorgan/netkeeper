"""Interactions, the timeline, and notes (spec 8.1, 10.1; item P1-10).

An ``interactions`` row is one thing that happened between you and a contact.
The outbound kinds (:data:`OUTBOUND_KINDS`: you reaching out) feed
``contacts.last_contacted_at``, the denormalized copy the ``last_contacted``
filter and sort read (spec 10.4). This module is what keeps that column honest:
adding an outbound interaction raises it to ``max(existing, at)``; editing or
deleting one recomputes it from the outbound rows that remain, always as a
query over ``interactions``, never as a decrement, so the column can be repaired
from the rows at any time (:func:`recompute_last_contacted`).

:func:`timeline` interleaves interactions and ``contact_snapshots`` newest
first for the contact detail page (spec 10.1) and the triage evidence panel
(spec 10.2). Notes are Markdown on ``contacts.notes``, written through
:func:`netkeeper.crm.provenance.set_manual_field` because they are a field the
person owns.

Transactions belong to the caller. Nothing here commits. Every writer reads
before it writes, so it needs a writer session (``session_scope(factory,
write=True)``, or a non-GET request's session); :func:`timeline` is read-only.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import ColumnElement, CursorResult, func, or_
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from netkeeper.crm.provenance import set_manual_field
from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactSnapshot,
    ContactSource,
    Interaction,
    InteractionKind,
    Message,
    User,
)
from netkeeper.scoping import (
    get_scoped,
    get_scoped_contact,
    not_self,
    scoped,
    scoped_contacts_update,
)

log = logging.getLogger(__name__)

OUTBOUND_KINDS: Final[frozenset[InteractionKind]] = frozenset(
    {
        InteractionKind.EMAIL_OUT,
        InteractionKind.LI_OUT,
        InteractionKind.CALL,
        InteractionKind.MEETING,
    }
)
"""The kinds that count as you contacting someone, and so move ``last_contacted_at``.

A note is a record, not a contact; an inbound message is them reaching you; a
profile view is neither. A call or a meeting takes two, and either counts as
having been in touch.
"""


INVITATION_SUMMARY: Final = "LinkedIn invitation"
"""What marks an ``li_in``/``li_out`` interaction as an invitation, not a message.

The schema has one kind for both directions of LinkedIn traffic and no column
for the distinction, so the archive importer writes the marker at the head of
the summary (``"LinkedIn invitation"``, or ``"LinkedIn invitation: <note>"``
when the invitation carried one) and this is where everything that has to tell
them apart reads it from. It matters to triage: "we wrote to each other" is
evidence you have met someone, and "they clicked Connect" is not.

A discriminator in the text is not one a schema can enforce, and a message whose
body happens to start with these two words reads as an invitation. The cost is
bounded — a batch scoped slightly wrong, previewable and undoable in one step —
and the alternative, a pair of new interaction kinds, rewrites every row the
importer has ever written. :func:`is_invitation` is the one reader.
"""


def summary_is_invitation(summary: str | None) -> bool:
    """:func:`is_invitation` for a summary already loaded: the same whole-token marker.

    The archive import and the inbox poll's dedupe both use it: an invitation is not a
    message, so neither matches one against the other's messages.
    """
    return summary is not None and (
        summary == INVITATION_SUMMARY or summary.startswith(f"{INVITATION_SUMMARY}:")
    )


def is_invitation() -> ColumnElement[bool]:
    """SQL: this interaction is an invitation rather than a message.

    The marker is a whole token: the summary is exactly ``"LinkedIn
    invitation"``, or it is that followed by ``":"`` and the note. A message
    that opens "LinkedIn invitation requests are piling up" is prose about
    invitations and stays message history, where it belongs.

    ``substr`` rather than ``LIKE``: ``LIKE`` ignores case on SQLite and honors
    it on PostgreSQL, and the marker is written in one spelling, so an exact
    comparison is both the intent and the portable rendering. A NULL summary
    compares NULL and so is not an invitation.
    """
    return or_(
        Interaction.summary == INVITATION_SUMMARY,
        has_invitation_note(),
    )


def has_invitation_note() -> ColumnElement[bool]:
    """SQL: this interaction is an invitation that carried a note someone wrote.

    Matched on ``"LinkedIn invitation:"`` and not on the space after it. The
    space is what the importer writes, but a later pass over stored summaries
    may not keep it — P1-26 strips HTML from archive rows and trims each line,
    which turns ``"LinkedIn invitation: <p>hello</p>"`` into
    ``"LinkedIn invitation:\nhello"`` — and a note that stops being one because
    its body opened with a block tag would leave this batch silently.
    """
    marker = f"{INVITATION_SUMMARY}:"
    return func.substr(Interaction.summary, 1, len(marker)) == marker


class NotFound(LookupError):
    """The contact or interaction is not one of the user's. Never says which user has it."""


class NoSuchMessage(ValueError):
    """``message_id`` is not one of the user's campaign messages to this interaction's contact.

    An interaction records a message to its own contact (P3-04 gave the column
    its foreign key): pointing it at another contact's message would let the
    campaign guards read one person's history as another's (spec 11.9).
    """


class _Missing(enum.Enum):
    MISSING = "missing"


MISSING: Final = _Missing.MISSING
"""The default for an :func:`update_interaction` field that is not being changed.

A sentinel rather than ``None`` because ``summary`` and ``message_id`` may be set
to ``None`` on purpose.
"""

type Missing = Literal[_Missing.MISSING]
TimelineKind = Literal["interaction", "snapshot"]


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One item of a contact's timeline: an interaction or a snapshot, and when it happened.

    ``at`` is the interaction's ``at`` or the snapshot's ``observed_at``; ``row``
    is the ORM object with every field.
    """

    kind: TimelineKind
    at: datetime
    row: Interaction | ContactSnapshot


def is_outbound(kind: InteractionKind) -> bool:
    return kind in OUTBOUND_KINDS


# --- interactions -----------------------------------------------------------


def add_interaction(
    session: Session,
    user: User,
    contact_id: int,
    kind: InteractionKind,
    at: datetime,
    summary: str | None = None,
    message_id: int | None = None,
    *,
    source: ContactSource = ContactSource.MANUAL,
    external_id: str | None = None,
) -> Interaction:
    """Record ``kind`` at ``at`` on one of ``user``'s contacts and return the flushed row.

    An outbound kind raises the contact's ``last_contacted_at`` to ``at`` when
    that is later than the current value; an earlier outbound row, backfilled
    from an archive, leaves a newer value alone. ``source`` is ``manual`` for a
    person's own entry; an importer passes its own. ``external_id`` is the row's id
    outside netkeeper (a LinkedIn message URN, P4-08), unique per user; the caller
    checks it is new. :class:`NotFound` when the
    contact is not ``user``'s; :class:`NoSuchMessage` for a ``message_id`` that is not
    a message to that contact; ``ValueError`` for a naive ``at``; ``RuntimeError``
    when ``session`` is not a writer.
    """
    _require_writer(session)
    _require_aware(at)
    contact = _owned_contact(session, user, contact_id)
    if message_id is not None:
        _check_message(session, user, contact.id, message_id)
    row = Interaction(
        user_id=user.id,
        contact_id=contact.id,
        kind=InteractionKind(kind),
        at=at,
        summary=summary,
        message_id=message_id,
        source=ContactSource(source),
        external_id=external_id,
    )
    session.add(row)
    session.flush()
    if is_outbound(row.kind) and (
        contact.last_contacted_at is None or at > contact.last_contacted_at
    ):
        contact.last_contacted_at = at
    log.debug("added %s interaction %d to contact %d", row.kind.value, row.id, contact.id)
    return row


def get_interaction(session: Session, user: User, interaction_id: int) -> Interaction:
    """The interaction with ``interaction_id`` if it is ``user``'s and on a contact in the
    network, else :class:`NotFound`. One on the self contact (#342) is not found either."""
    row = session.scalars(
        scoped(user, Interaction)
        .join(Contact, Contact.id == Interaction.contact_id)
        .where(Interaction.id == interaction_id, Contact.user_id == user.id, not_self())
    ).one_or_none()
    if row is None:
        raise NotFound(f"interaction {interaction_id} is not one of user {user.id}'s")
    return row


def list_interactions(
    session: Session, user: User, contact_id: int, *, limit: int, offset: int = 0
) -> tuple[list[Interaction], int]:
    """One page of a contact's interactions, newest first, and the total count.

    :class:`NotFound` when the contact is not ``user``'s; ``ValueError`` for a
    ``limit`` under 1 or a negative ``offset``.
    """
    _check_page(limit, offset)
    contact = _owned_contact(session, user, contact_id)
    base = scoped(user, Interaction).where(Interaction.contact_id == contact.id)
    total = session.scalar(base.with_only_columns(func.count())) or 0
    rows = session.scalars(
        base.order_by(Interaction.at.desc(), Interaction.id.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), total


def update_interaction(
    session: Session,
    user: User,
    interaction_id: int,
    *,
    kind: InteractionKind | Missing = MISSING,
    at: datetime | Missing = MISSING,
    summary: str | Missing | None = MISSING,
    message_id: int | Missing | None = MISSING,
) -> Interaction:
    """Change the given fields of one of ``user``'s interactions and return it.

    A field left at :data:`MISSING` is untouched. When the row was outbound
    before the change or is outbound after it, the contact's
    ``last_contacted_at`` is recomputed from its outbound rows: moving the
    newest outbound interaction back in time, or turning it into a note, must
    lower the column, and only a query over the remaining rows knows by how
    much. :class:`NotFound`, :class:`NoSuchMessage`, ``ValueError`` for a naive
    ``at``, ``RuntimeError`` for a session that is not a writer.
    """
    _require_writer(session)
    if at is not MISSING:
        _require_aware(at)
    row = get_interaction(session, user, interaction_id)
    if message_id is not MISSING and message_id is not None:
        _check_message(session, user, row.contact_id, message_id)
    was_outbound = is_outbound(row.kind)
    if kind is not MISSING:
        row.kind = InteractionKind(kind)
    if at is not MISSING:
        row.at = at
    if summary is not MISSING:
        row.summary = summary
    if message_id is not MISSING:
        row.message_id = message_id
    session.flush()
    if was_outbound or is_outbound(row.kind):
        _refresh_last_contacted(session, user, row.contact)
    return row


def delete_interaction(session: Session, user: User, interaction_id: int) -> None:
    """Delete one of ``user``'s interactions.

    Deleting an outbound row recomputes the contact's ``last_contacted_at`` from
    the outbound rows that remain. :class:`NotFound`; ``RuntimeError`` for a
    session that is not a writer.
    """
    _require_writer(session)
    row = get_interaction(session, user, interaction_id)
    contact = row.contact
    outbound = is_outbound(row.kind)
    session.delete(row)
    session.flush()
    if outbound:
        _refresh_last_contacted(session, user, contact)


def recompute_last_contacted(
    session: Session, user: User, contact_ids: list[int] | None = None
) -> int:
    """Set ``last_contacted_at`` from the outbound rows for ``contact_ids``, or every contact.

    The repair for a column that drifted (a writer that bypassed this module, a
    restored backup). One correlated ``UPDATE``, so it is the same statement on
    SQLite and PostgreSQL and touches 10,000 contacts in one round trip. Returns
    the number of contacts written. Loaded ``Contact`` objects in ``session``
    have their ``last_contacted_at`` expired so the next read sees the new value.
    """
    _require_writer(session)
    newest = (
        _newest_outbound_at(user)
        .where(Interaction.contact_id == Contact.id)
        .correlate(Contact)
        .scalar_subquery()
    )
    statement = scoped_contacts_update(user).values(last_contacted_at=newest)
    if contact_ids is not None:
        statement = statement.where(Contact.id.in_(contact_ids))
    # "auto" would issue its own unscoped SELECT to find the rows (see
    # netkeeper.crm.filters.compile_update); the identity map is expired by hand below.
    # Session.execute() is typed as the plain Result; DML gets a CursorResult.
    result = cast(
        CursorResult[Any],
        session.execute(statement.execution_options(synchronize_session=False)),
    )
    wanted = None if contact_ids is None else set(contact_ids)
    for obj in list(session.identity_map.values()):
        if isinstance(obj, Contact) and (wanted is None or obj.id in wanted):
            session.expire(obj, ["last_contacted_at"])
    count: int = result.rowcount
    log.info("recomputed last_contacted_at for %d contacts of user %d", count, user.id)
    return count


def _refresh_last_contacted(session: Session, user: User, contact: Contact) -> None:
    """``contact.last_contacted_at`` from its outbound rows as they are now."""
    contact.last_contacted_at = session.scalar(
        _newest_outbound_at(user).where(Interaction.contact_id == contact.id)
    )


def _newest_outbound_at(user: User) -> Select[tuple[datetime]]:
    """``max(at)`` over ``user``'s outbound interactions; the caller narrows it to a contact."""
    return (
        scoped(user, Interaction)
        .with_only_columns(func.max(Interaction.at))
        .where(Interaction.kind.in_(OUTBOUND_KINDS))
    )


# --- timeline ---------------------------------------------------------------


def timeline(
    session: Session,
    user: User,
    contact_id: int,
    *,
    limit: int,
    before: datetime | None = None,
) -> list[TimelineEntry]:
    """A contact's interactions and snapshots interleaved, newest first, one page at a time.

    ``before`` is the cursor: only entries strictly older than it are returned,
    so the next page is ``before=<the last entry's at>``. A page never splits a
    group of entries that share one ``at``: when the entry at the ``limit``
    boundary has the same time as the ones after it, the page grows to include
    them all, because a cursor that is only a timestamp could otherwise skip
    them. So a page can be longer than ``limit``, and a page shorter than
    ``limit`` is the last one. :class:`NotFound` when the contact is not
    ``user``'s; ``ValueError`` for a ``limit`` under 1 or a naive ``before``.
    """
    _check_page(limit, 0)
    if before is not None:
        _require_aware(before)
    contact = _owned_contact(session, user, contact_id)
    # One more than the page from each source: enough to know whether a source
    # still has entries past the boundary (see the proof in _page).
    fetched = limit + 1
    interactions = scoped(user, Interaction).where(Interaction.contact_id == contact.id)
    snapshots = scoped(user, ContactSnapshot).where(ContactSnapshot.contact_id == contact.id)
    if before is not None:
        interactions = interactions.where(Interaction.at < before)
        snapshots = snapshots.where(ContactSnapshot.observed_at < before)
    merged = _merge(
        session.scalars(
            interactions.order_by(Interaction.at.desc(), Interaction.id.desc()).limit(fetched)
        ).all(),
        session.scalars(
            snapshots.order_by(ContactSnapshot.observed_at.desc(), ContactSnapshot.id.desc()).limit(
                fetched
            )
        ).all(),
    )
    if len(merged) <= limit:
        return merged  # neither source hit its limit, so this is everything
    page = merged[:limit]
    boundary = page[-1].at
    if merged[limit].at < boundary:
        return page
    # Entries tied at the boundary may extend past what was fetched: get them all.
    tied = _merge(
        session.scalars(interactions.where(Interaction.at == boundary)).all(),
        session.scalars(snapshots.where(ContactSnapshot.observed_at == boundary)).all(),
    )
    return [entry for entry in page if entry.at > boundary] + tied


def _merge(
    interactions: Sequence[Interaction], snapshots: Sequence[ContactSnapshot]
) -> list[TimelineEntry]:
    """Interleave newest first; at one time an interaction precedes a snapshot, then newer ids."""
    entries = [TimelineEntry("interaction", row.at, row) for row in interactions]
    entries += [TimelineEntry("snapshot", row.observed_at, row) for row in snapshots]
    entries.sort(key=lambda entry: (entry.at, entry.kind == "interaction", entry.row.id))
    entries.reverse()
    return entries


# --- notes ------------------------------------------------------------------


def set_notes(session: Session, user: User, contact_id: int, notes: str | None) -> Contact:
    """Write ``notes`` (Markdown, stored as given) on one of ``user``'s contacts.

    Through :func:`~netkeeper.crm.provenance.set_manual_field`: notes are a
    field the person owns, so no import ever touches them. :class:`NotFound`;
    ``RuntimeError`` for a session that is not a writer.
    """
    _require_writer(session)
    contact = _owned_contact(session, user, contact_id)
    set_manual_field(contact, "notes", notes)
    session.flush()
    return contact


# --- helpers ----------------------------------------------------------------


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "interaction writes need a writer session; use session_scope(factory, write=True)"
        )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")


def _check_page(limit: int, offset: int) -> None:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if offset < 0:
        raise ValueError("offset must not be negative")


def _check_message(session: Session, user: User, contact_id: int, message_id: int) -> None:
    message = get_scoped(session, user, Message, message_id)
    if message is None or message.contact_id != contact_id:
        raise NoSuchMessage(f"message {message_id} is not a message to contact {contact_id}")


def _owned_contact(session: Session, user: User, contact_id: int) -> Contact:
    contact = get_scoped_contact(session, user, contact_id)
    if contact is None:
        raise NotFound(f"contact {contact_id} is not one of user {user.id}'s")
    return contact
