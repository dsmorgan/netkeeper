"""Triage: the queue, its evidence, decisions, undo, and the bulk suggestion (spec 10.2).

Triage is the screen where you go through the people LinkedIn says you are
connected to and answer one question about each: have you actually met them?
Fifty contacts in ten minutes with the keyboard alone (P1-14), which sets the
shape of everything here.

The queue
---------
The untriaged contacts, in ``id`` order, which is the order they entered the
address book. Nothing in the order depends on a decision, so a contact that is
put back by undo lands exactly where it was. :func:`next_contact` takes
``after_id`` as its cursor, so the ``→`` key ("show me the next one, I am not
deciding this one") moves forward without writing anything.

Evidence in the same response
-----------------------------
Every answer that hands out a contact hands out its evidence with it
(:class:`Card`): the LinkedIn message history, the timeline of interactions and
job snapshots, the companies shared with the rest of the address book, the
notes, and the tags. A triage screen therefore needs one request per contact,
not one for the contact and four for the panel. :func:`decide` returns the next
card with the decision, so the steady state of a run is a single request per
contact, and the client can hold the one after that already rendered.

The evidence load costs a fixed number of queries whatever a contact carries;
``tests/test_web_triage.py`` counts them and fails if that stops being true.

Undo
----
Undo restores the previous state exactly, and :mod:`netkeeper.models.triage` is
how: every decision records the fields it changed as they were and as it left
them. Undo compares the contact with what the decision left, refuses when they
differ (:class:`UndoConflict`) rather than overwriting an edit that came from
somewhere else, then writes the recorded ``before`` back and marks the decision
spent. So the second undo reaches the decision before, a bulk apply undoes as
one batch, and a contact that was decided twice walks back one step at a time.

The writes go through :func:`netkeeper.crm.provenance.set_manual_field`, for a
decision and for undoing one: ``met`` and ``preferred_name`` are fields the
person owns, and an edit made here outranks every later sync and import
(spec 10.5).

Transactions belong to the caller. Nothing here commits. Every writer reads
before it writes, so it needs a writer session (``session_scope(factory,
write=True)``, or a non-GET request's session).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, Select, case, func, select
from sqlalchemy.orm import Session

from netkeeper.crm.interactions import OUTBOUND_KINDS, TimelineEntry, timeline
from netkeeper.crm.provenance import set_manual_field
from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactMet,
    Interaction,
    InteractionKind,
    TriageDecision,
    TriageDecisionKind,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped, scoped_count

log = logging.getLogger(__name__)

MESSAGE_KINDS: Final[frozenset[InteractionKind]] = frozenset(
    {
        InteractionKind.LI_IN,
        InteractionKind.LI_OUT,
        InteractionKind.EMAIL_IN,
        InteractionKind.EMAIL_OUT,
    }
)
"""The interaction kinds that are a message from one of you to the other.

The archive importer (P1-03) and the inbox poll write LinkedIn messages and
invitation notes as ``li_in`` and ``li_out``; the campaign engine will write
``email_in`` and ``email_out``. A note, a call, a meeting, or a profile view is
not message history: none of them is evidence that the two of you have written
to each other, which is what the bulk suggestion turns into "met".
"""

DECIDABLE: Final[frozenset[ContactMet]] = frozenset(
    {ContactMet.MET, ContactMet.NOT_MET, ContactMet.SKIP}
)
"""What the ``m``, ``n``, and ``s`` keys set. ``unknown`` is not a decision; undo is."""

DEFAULT_QUEUE_STATES: Final[tuple[ContactMet, ...]] = (ContactMet.UNKNOWN,)
"""The queue is the untriaged. Pass ``(ContactMet.SKIP,)`` to revisit the skipped (spec 10.2)."""

TIMELINE_LIMIT: Final[int] = 20
"""Timeline entries on a card. A page of evidence, not the whole history."""

RECENT_MESSAGES: Final[int] = 5
"""Message interactions quoted on a card, newest first; the counts cover the rest."""

SUGGESTION_MET_WITH_MESSAGES: Final[str] = "met_with_messages"
"""The one bulk suggestion of v1: "mark everyone with message history as met"."""

# Contacts per statement when a bulk apply walks its matches.
_CHUNK: Final[int] = 500

# The contact columns a decision may change, and how each one is stored in
# ``before_state`` and ``after_state``. Text, so one JSON map holds them all.
_MET_FIELD: Final[str] = "met"
_TRIAGED_AT_FIELD: Final[str] = "triaged_at"
_PREFERRED_NAME_FIELD: Final[str] = "preferred_name"
_RECORDED_FIELDS: Final[frozenset[str]] = frozenset(
    {_MET_FIELD, _TRIAGED_AT_FIELD, _PREFERRED_NAME_FIELD}
)


# --- errors -----------------------------------------------------------------


class NotFound(LookupError):
    """The contact is not one of the user's. Never says which user has it."""


class InvalidDecision(ValueError):
    """A met value that is not one of the three keys, or an empty preferred name."""


class NothingToUndo(LookupError):
    """The user's undo stack is empty: every decision has been undone already."""


class UndoConflict(RuntimeError):
    """The contact no longer holds what the decision left, so undo would overwrite an edit.

    ``field``, ``expected``, and ``found`` say what diverged, all as the decision
    log stores them. ``force`` on :func:`undo` writes the recorded previous state
    anyway.
    """

    def __init__(
        self, decision_id: int, contact_id: int, field: str, expected: str | None, found: str | None
    ) -> None:
        self.decision_id = decision_id
        self.contact_id = contact_id
        self.field = field
        self.expected = expected
        self.found = found
        super().__init__(
            f"contact {contact_id} has {field}={found!r} where the decision left {expected!r}; "
            "something changed it after the decision, so undo would overwrite that change"
        )


class CountChanged(RuntimeError):
    """The bulk suggestion matches a different number of contacts than the preview showed."""

    def __init__(self, expected: int, found: int) -> None:
        self.expected = expected
        self.found = found
        super().__init__(
            f"the suggestion now matches {found} contacts, not the {expected} you were shown; "
            "take the preview again"
        )


# --- what a card carries ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class SharedCompany:
    """A company this contact is at, or was at, and who else in the address book is there.

    The person's own career is not in the database (there is no table of your
    positions), so "shared" means shared with the rest of your contacts, which is
    the evidence that actually helps in triage: you know four people at a
    company, three of whom you have already marked met, so you were probably in
    the same room. ``company`` is the contact's own spelling; the overlap is
    counted without regard to case against other live contacts' current company.
    """

    company: str
    contact_count: int
    met_count: int


@dataclass(frozen=True, slots=True)
class MessageEvidence:
    """The message history with one contact: the shape of it, and the newest few.

    ``recent`` is newest first and at most :data:`RECENT_MESSAGES` long; ``total``
    counts them all, so a panel can say "37 messages, last one in March".
    """

    total: int
    inbound: int
    outbound: int
    first_at: datetime | None
    last_at: datetime | None
    recent: list[Interaction] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the triage panel shows beside the contact (spec 10.2)."""

    messages: MessageEvidence
    timeline: list[TimelineEntry]
    shared_companies: list[SharedCompany]


@dataclass(frozen=True, slots=True)
class Card:
    """One contact and its evidence: what a client needs to show a triage screen."""

    contact: Contact
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class Progress:
    """Triaged against total, and how much of the current queue is left (spec 10.2)."""

    total: int
    triaged: int
    remaining: int
    by_state: dict[ContactMet, int]


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A bulk action offered to the user, with the count it would apply to.

    Never applied on its own: :func:`apply_suggestion` is a separate call, and
    what it does is one undoable batch.
    """

    key: str
    title: str
    description: str
    count: int


@dataclass(frozen=True, slots=True)
class Applied:
    """The result of applying a bulk suggestion: how many, and the batch undo takes back."""

    key: str
    applied: int
    batch_id: str


@dataclass(frozen=True, slots=True)
class Undone:
    """What one undo put back.

    ``contact`` is the contact to show next when a single decision was undone;
    a batch has no single contact, so it is ``None`` and ``decisions`` says how
    many rows were restored. ``forced`` lists the contacts whose state had moved
    on and was overwritten anyway.
    """

    kind: TriageDecisionKind
    decisions: int
    batch_id: str | None
    contact: Contact | None
    forced: list[int] = field(default_factory=list)


# --- the queue --------------------------------------------------------------


def next_contact(
    session: Session,
    user: User,
    *,
    states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES,
    after_id: int | None = None,
) -> Contact | None:
    """The next contact to triage, or ``None`` when the queue is empty.

    The queue is the user's live contacts whose ``met`` is one of ``states``, in
    ``id`` order. ``after_id`` is the cursor: the first contact past it, which is
    how "next without deciding" moves on. Membership depends only on ``met`` and
    the order only on ``id``, so undoing a decision puts a contact back exactly
    where it stood.
    """
    statement = _queue(user, states).order_by(Contact.id).limit(1)
    if after_id is not None:
        statement = statement.where(Contact.id > after_id)
    return session.scalars(statement).first()


def next_card(
    session: Session,
    user: User,
    *,
    states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES,
    after_id: int | None = None,
) -> Card | None:
    """:func:`next_contact` with its evidence loaded, ready to render."""
    contact = next_contact(session, user, states=states, after_id=after_id)
    return None if contact is None else load_card(session, user, contact)


def load_card(session: Session, user: User, contact: Contact) -> Card:
    """The evidence panel for one contact of ``user`` (spec 10.2).

    A fixed number of queries: the message counts, the newest messages, the
    timeline, the contact's positions, and the address-book overlap. None of them
    grows with how much history the contact has.
    """
    return Card(contact=contact, evidence=_evidence(session, user, contact))


def progress(
    session: Session, user: User, *, states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES
) -> Progress:
    """How far triage has got, from one grouped count over the live contacts."""
    statement = (
        scoped(user, Contact)
        .with_only_columns(Contact.met, func.count())
        .where(Contact.archived_at.is_(None), Contact.merged_into_id.is_(None))
        .group_by(Contact.met)
    )
    by_state = dict.fromkeys(ContactMet, 0)
    for met, count in session.execute(statement).all():
        by_state[ContactMet(met)] = count
    total = sum(by_state.values())
    wanted = _states(states)
    return Progress(
        total=total,
        triaged=total - by_state[ContactMet.UNKNOWN],
        remaining=sum(by_state[state] for state in wanted),
        by_state=by_state,
    )


# --- decisions --------------------------------------------------------------


def decide(
    session: Session,
    user: User,
    contact_id: int,
    met: ContactMet,
    *,
    at: datetime | None = None,
) -> TriageDecision:
    """Record the ``m``, ``n``, or ``s`` key on one of ``user``'s contacts.

    Writes ``met`` and ``triaged_at``, and logs what both were, so undo can put
    them back exactly. Deciding a contact that was decided before is allowed and
    logs another row, so undo walks back one decision at a time.
    :class:`NotFound` when the contact is not ``user``'s; :class:`InvalidDecision`
    for ``unknown`` (undo is how a contact goes back to untriaged);
    ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    decided = ContactMet(met)
    if decided not in DECIDABLE:
        raise InvalidDecision(
            f"{decided.value} is not a triage decision; the keys set "
            + ", ".join(sorted(state.value for state in DECIDABLE))
        )
    contact = _owned_contact(session, user, contact_id)
    moment = at if at is not None else utcnow()
    _require_aware(moment)
    before = _snapshot(contact, (_MET_FIELD, _TRIAGED_AT_FIELD))
    set_manual_field(contact, _MET_FIELD, decided)
    contact.triaged_at = moment
    return _log(
        session,
        user,
        contact,
        TriageDecisionKind.DECIDE,
        before=before,
        at=moment,
    )


def set_preferred_name(
    session: Session, user: User, contact_id: int, preferred_name: str
) -> TriageDecision:
    """The ``p`` key: what you call this person, as your own edit.

    Through :func:`~netkeeper.crm.provenance.set_manual_field`, so no later sync,
    archive, or CSV import overwrites it (spec 10.5). An empty name means "use
    the first name", which is what the column does with it; the log records what
    the column ended up with, so undo restores that. :class:`NotFound`;
    ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    contact = _owned_contact(session, user, contact_id)
    before = _snapshot(contact, (_PREFERRED_NAME_FIELD,))
    set_manual_field(contact, _PREFERRED_NAME_FIELD, preferred_name)
    return _log(session, user, contact, TriageDecisionKind.PREFERRED_NAME, before=before)


def undo(session: Session, user: User, *, force: bool = False) -> Undone:
    """Undo the newest triage action of ``user``, restoring the previous state exactly.

    The newest decision that has not been undone yet, or every row of its batch
    when it came from a bulk apply. Each contact must still hold what the
    decision left it holding; otherwise nothing is written and
    :class:`UndoConflict` says what diverged, because an edit that arrived
    afterwards is not this decision's to overwrite. ``force=True`` restores
    anyway and reports which contacts it overrode. The decisions are marked
    spent either way, so the next undo reaches the one before.

    :class:`NothingToUndo` when the stack is empty; ``RuntimeError`` when
    ``session`` is not a writer.
    """
    _require_writer(session)
    newest = session.scalars(
        scoped(user, TriageDecision)
        .where(TriageDecision.undone_at.is_(None))
        .order_by(TriageDecision.id.desc())
        .limit(1)
    ).first()
    if newest is None:
        raise NothingToUndo(f"user {user.id} has no triage decision left to undo")
    decisions = _batch(session, user, newest)
    contacts = _contacts_by_id(session, user, [row.contact_id for row in decisions])
    forced: list[int] = []
    for row in decisions:
        contact = contacts.get(row.contact_id)
        if contact is None:  # the contact is gone, so there is nothing to restore it to
            raise UndoConflict(row.id, row.contact_id, "contact", "present", None)
        diverged = _diverged(row, contact)
        if diverged is not None:
            if not force:
                raise UndoConflict(row.id, contact.id, *diverged)
            forced.append(contact.id)
    moment = utcnow()
    for row in decisions:
        _restore(contacts[row.contact_id], row)
        row.undone_at = moment
    session.flush()
    single = contacts[decisions[0].contact_id] if len(decisions) == 1 else None
    log.info(
        "undid %d triage decision(s) of user %d (kind %s)",
        len(decisions),
        user.id,
        newest.kind.value,
    )
    return Undone(
        kind=newest.kind,
        decisions=len(decisions),
        batch_id=newest.batch_id,
        contact=single,
        forced=forced,
    )


# --- the bulk suggestion ----------------------------------------------------


def suggestions(
    session: Session, user: User, *, states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES
) -> list[Suggestion]:
    """The bulk actions worth offering right now, with their counts (spec 10.2).

    A suggestion whose count is zero is left out, so the banner appears only when
    there is something to accept. Nothing here writes: the count is a preview,
    and :func:`apply_suggestion` is a separate, explicit call.
    """
    count = session.scalar(_with_messages(_queue_count(user, states), user)) or 0
    if count == 0:
        return []
    return [
        Suggestion(
            key=SUGGESTION_MET_WITH_MESSAGES,
            title="Mark everyone with message history as met",
            description=(
                f"You have message threads with {count} untriaged "
                f"{'person' if count == 1 else 'people'}."
            ),
            count=count,
        )
    ]


def apply_suggestion(
    session: Session,
    user: User,
    key: str,
    *,
    states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES,
    expected_count: int | None = None,
    at: datetime | None = None,
) -> Applied:
    """Apply a bulk suggestion, as one batch that undo takes back in one step.

    ``expected_count`` is the count the user was shown: when the set has moved on
    since, nothing is written and :class:`CountChanged` says so, so a click never
    applies to more people than the banner named. Every contact gets its own
    decision row with its own previous state, so undo restores each one exactly,
    including a contact that was already ``not_met``. :class:`InvalidDecision`
    for an unknown key; ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    if key != SUGGESTION_MET_WITH_MESSAGES:
        raise InvalidDecision(f"no bulk suggestion named {key!r}")
    moment = at if at is not None else utcnow()
    _require_aware(moment)
    statement = _with_messages(_queue(user, states), user).order_by(Contact.id)
    matches = list(session.scalars(statement).all())
    if expected_count is not None and expected_count != len(matches):
        raise CountChanged(expected_count, len(matches))
    batch_id = uuid.uuid4().hex
    for contact in matches:
        before = _snapshot(contact, (_MET_FIELD, _TRIAGED_AT_FIELD))
        set_manual_field(contact, _MET_FIELD, ContactMet.MET)
        contact.triaged_at = moment
        _log(
            session,
            user,
            contact,
            TriageDecisionKind.BULK_MET,
            before=before,
            at=moment,
            batch_id=batch_id,
            flush=False,
        )
    session.flush()
    log.info("bulk suggestion %s marked %d contacts met for user %d", key, len(matches), user.id)
    return Applied(key=key, applied=len(matches), batch_id=batch_id)


# --- evidence ---------------------------------------------------------------


def _evidence(session: Session, user: User, contact: Contact) -> Evidence:
    return Evidence(
        messages=_messages(session, user, contact),
        timeline=timeline(session, user, contact.id, limit=TIMELINE_LIMIT),
        shared_companies=_shared_companies(session, user, contact),
    )


def _messages(session: Session, user: User, contact: Contact) -> MessageEvidence:
    """The message counts in one query, then the newest few in another."""
    base = scoped(user, Interaction).where(
        Interaction.contact_id == contact.id, Interaction.kind.in_(sorted(MESSAGE_KINDS))
    )
    outbound = case((Interaction.kind.in_(sorted(OUTBOUND_KINDS)), 1), else_=0)
    total, sent, first_at, last_at = session.execute(
        base.with_only_columns(
            func.count(),
            func.coalesce(func.sum(outbound), 0),
            func.min(Interaction.at),
            func.max(Interaction.at),
        )
    ).one()
    recent = session.scalars(
        base.order_by(Interaction.at.desc(), Interaction.id.desc()).limit(RECENT_MESSAGES)
    ).all()
    return MessageEvidence(
        total=total,
        inbound=total - sent,
        outbound=sent,
        first_at=first_at,
        last_at=last_at,
        recent=list(recent),
    )


def _shared_companies(session: Session, user: User, contact: Contact) -> list[SharedCompany]:
    """Companies from the contact's current job and positions, with the overlap counted.

    One query for every company at once, grouped on the lowercased current
    company of the other live contacts, so the cost does not grow with how many
    positions this contact has.
    """
    spellings: dict[str, str] = {}
    for name in [contact.current_company, *(row.company for row in contact.positions)]:
        cleaned = (name or "").strip()
        if cleaned:
            spellings.setdefault(cleaned.lower(), cleaned)
    if not spellings:
        return []
    company = func.lower(func.trim(Contact.current_company))
    statement = (
        scoped(user, Contact)
        .with_only_columns(
            company,
            func.count(),
            func.coalesce(func.sum(case((Contact.met == ContactMet.MET, 1), else_=0)), 0),
        )
        .where(
            Contact.id != contact.id,
            Contact.archived_at.is_(None),
            Contact.merged_into_id.is_(None),
            company.in_(sorted(spellings)),
        )
        .group_by(company)
    )
    counted = {key: (total, met) for key, total, met in session.execute(statement).all()}
    shared = []
    for key, spelling in spellings.items():
        total, met = counted.get(key, (0, 0))
        shared.append(SharedCompany(company=spelling, contact_count=total, met_count=met))
    return shared


# --- the decision log -------------------------------------------------------


def _log(
    session: Session,
    user: User,
    contact: Contact,
    kind: TriageDecisionKind,
    *,
    before: dict[str, str | None],
    at: datetime | None = None,
    batch_id: str | None = None,
    flush: bool = True,
) -> TriageDecision:
    """Write the decision row for a change already made to ``contact``.

    ``after_state`` is read off the contact, not off the request, so it is what
    the column actually holds: the ``preferred_name`` validator turns an empty
    edit into the first name, and undo has to compare against that.
    """
    row = TriageDecision(
        user_id=user.id,
        contact_id=contact.id,
        kind=kind,
        before_state=before,
        after_state=_snapshot(contact, before),
        batch_id=batch_id,
        decided_at=at if at is not None else utcnow(),
    )
    session.add(row)
    if flush:
        session.flush()
    return row


def _snapshot(contact: Contact, fields: Iterable[str]) -> dict[str, str | None]:
    """The named contact columns as the log stores them, in the order given."""
    return {name: _encode(name, getattr(contact, name)) for name in fields}


def _encode(name: str, value: Any) -> str | None:
    """One column value as text. ``None`` stays ``None`` so a cleared field round-trips.

    A moment is normalized to UTC first. The column returns UTC whatever offset
    was written (``UTCDateTime``), so an offset kept here would make the same
    instant compare unequal on the next request and undo would report a conflict
    that is not one.
    """
    if name not in _RECORDED_FIELDS:
        raise ValueError(f"{name!r} is not a field the triage log records")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _decode(name: str, raw: str | None) -> Any:
    if name == _MET_FIELD:
        return None if raw is None else ContactMet(raw)
    if name == _TRIAGED_AT_FIELD:
        return None if raw is None else datetime.fromisoformat(raw)
    if name == _PREFERRED_NAME_FIELD:
        return raw if raw is not None else ""
    raise ValueError(f"{name!r} is not a field the triage log records")


def _diverged(row: TriageDecision, contact: Contact) -> tuple[str, str | None, str | None] | None:
    """The first field where ``contact`` no longer holds what ``row`` left, or ``None``."""
    for name, expected in row.after_state.items():
        found = _encode(name, getattr(contact, name))
        if found != expected:
            return name, expected, found
    return None


def _restore(contact: Contact, row: TriageDecision) -> None:
    """Put every field of ``row.before_state`` back on ``contact``.

    ``met`` and ``preferred_name`` go through
    :func:`~netkeeper.crm.provenance.set_manual_field`, as the decision did:
    undoing an edit is an edit, and the field stays the person's.
    """
    for name, raw in row.before_state.items():
        value = _decode(name, raw)
        if name == _TRIAGED_AT_FIELD:
            contact.triaged_at = value
        else:
            set_manual_field(contact, name, value)


def _batch(session: Session, user: User, newest: TriageDecision) -> list[TriageDecision]:
    """``newest`` alone, or every row of its bulk batch that is still on the stack."""
    if newest.batch_id is None:
        return [newest]
    rows = session.scalars(
        scoped(user, TriageDecision)
        .where(
            TriageDecision.batch_id == newest.batch_id,
            TriageDecision.undone_at.is_(None),
        )
        .order_by(TriageDecision.id)
    ).all()
    return list(rows)


def _contacts_by_id(session: Session, user: User, ids: Sequence[int]) -> dict[int, Contact]:
    """The user's contacts with those ids, in chunks so a large batch stays one statement each."""
    found: dict[int, Contact] = {}
    unique = sorted(set(ids))
    for start in range(0, len(unique), _CHUNK):
        chunk = unique[start : start + _CHUNK]
        for contact in session.scalars(scoped(user, Contact).where(Contact.id.in_(chunk))):
            found[contact.id] = contact
    return found


# --- queue helpers ----------------------------------------------------------


def _queue(user: User, states: Sequence[ContactMet]) -> Select[tuple[Contact]]:
    """``scoped(user, Contact)`` narrowed to the live contacts in ``states``."""
    return scoped(user, Contact).where(*_queue_where(states))


def _queue_count(user: User, states: Sequence[ContactMet]) -> Select[tuple[int]]:
    return scoped_count(user, Contact).where(*_queue_where(states))


def _queue_where(states: Sequence[ContactMet]) -> list[ColumnElement[bool]]:
    return [
        Contact.met.in_(_states(states)),
        Contact.archived_at.is_(None),
        Contact.merged_into_id.is_(None),
    ]


def _with_messages[T: (Select[tuple[Contact]], Select[tuple[int]])](statement: T, user: User) -> T:
    """Narrow a contacts statement to the contacts with at least one message interaction.

    An ``IN`` over a scoped subquery rather than a join, so the outer statement
    still returns one row per contact however many messages there are. The
    subquery carries its own ``user_id``, as every subquery on an owned table
    must (ADR 0005); the outer statement carries the scope mark for the guard.
    """
    messages = select(Interaction.contact_id).where(
        Interaction.user_id == user.id, Interaction.kind.in_(sorted(MESSAGE_KINDS))
    )
    narrowed: T = statement.where(Contact.id.in_(messages))
    return narrowed


def _states(states: Sequence[ContactMet]) -> list[ContactMet]:
    if not states:
        raise InvalidDecision("the queue needs at least one met state")
    return [ContactMet(state) for state in states]


# --- shared helpers ---------------------------------------------------------


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "triage writes need a writer session; use session_scope(factory, write=True)"
        )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")


def _owned_contact(session: Session, user: User, contact_id: int) -> Contact:
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        raise NotFound(f"contact {contact_id} is not one of user {user.id}'s")
    return contact
