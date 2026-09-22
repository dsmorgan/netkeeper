"""Triage: the queue, its evidence, decisions, undo, and the bulk batches (spec 10.2).

Triage is the screen where you go through the people LinkedIn says you are
connected to and answer one question about each: have you actually met them?
Fifty contacts in ten minutes with the keyboard alone (P1-14), which sets the
shape of everything here.

Starting from what is already decided
-------------------------------------
An imported archive is six hundred cold cards, and answering six hundred
questions by hand is the failure this module exists to avoid. So the work goes
the other way round: the import tags what it can (:mod:`netkeeper.crm.archive`,
#64), :func:`suggestions` offers the batches those tags and the message history
make defensible, and the manual pass reviews what was decided instead of
starting from nothing.

Three rules hold that together:

* **Nothing applies itself.** :func:`suggestions` and
  :func:`suggestion_contacts` write nothing; a batch is a count and a list of
  names until :func:`apply_suggestion` is called, and it refuses when the set
  has moved since the count the person was shown (:class:`CountChanged`).
* **An automatic decision says so.** Every contact a batch decides carries
  ``met_source = automatic`` and a decision row with the batch id and the key of
  the suggestion behind it, so it is never mistaken for an answer the person
  gave and the log can always say what was decided, when, and why.
* **One batch, one undo.** The rows of a batch share a ``batch_id``, so undo
  takes the whole thing back in one step, each contact to exactly where it was.

The queue
---------
The untriaged contacts, in ``id`` order, which is the order they entered the
address book. Nothing in the order depends on a decision, so a contact that is
put back by undo lands exactly where it was. :func:`next_contact` takes
``after_id`` as its cursor, so the ``→`` key ("show me the next one, I am not
deciding this one") moves forward without writing anything.

``decided_by=MetSource.AUTOMATIC`` over :data:`REVIEW_QUEUE_STATES` is the
review pass: the same queue and the same cards, holding the contacts a batch
decided and nobody has corrected. Deciding one by hand makes it ``manual``,
which is how a contact leaves that queue.

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

The writes go through :mod:`netkeeper.crm.provenance`, for a decision and for
undoing one: ``met`` (with ``met_source``, through :func:`~netkeeper.crm.
provenance.set_met`) and ``preferred_name`` are fields the person owns, and an
edit made here outranks every later sync and import (spec 10.5).

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

from sqlalchemy import ColumnElement, Select, and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from netkeeper.crm.interactions import (
    OUTBOUND_KINDS,
    TimelineEntry,
    has_invitation_note,
    is_invitation,
    timeline,
)
from netkeeper.crm.provenance import set_manual_field, set_met
from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactMet,
    ContactPosition,
    ContactTag,
    Interaction,
    InteractionKind,
    MetSource,
    Tag,
    TagMetSignal,
    TagSource,
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

SUGGESTION_PAGE: Final[int] = 50
"""Contacts per page when a suggestion is previewed before it is applied."""

REVIEW_QUEUE_STATES: Final[tuple[ContactMet, ...]] = (
    ContactMet.MET,
    ContactMet.NOT_MET,
    ContactMet.SKIP,
)
"""The states the review queue covers: everything a batch can have decided."""

SUGGESTION_MET_WITH_MESSAGES: Final[str] = "met_with_messages"
"""Message history in either direction, which is the strongest evidence there is."""

SUGGESTION_MET_INVITATION_NOTE: Final[str] = "met_invitation_note"
"""An invitation that carried a note either way, and no message thread since."""

SUGGESTION_NOT_MET_NO_EVIDENCE: Final[str] = "not_met_no_evidence"
"""Nothing on file at all: the large "not met" tail of an imported address book."""

TAG_KEY_PREFIX: Final[str] = "tag:"
"""``tag:<id>``: the batch for one tag the user gave a meaning (``Tag.met_signal``)."""

BATCH_STATES: Final[frozenset[ContactMet]] = frozenset({ContactMet.UNKNOWN, ContactMet.SKIP})
"""The only states a batch may reach: nobody has answered for these people yet.

``skip`` is "I passed on this one", not an answer, so a batch may still offer
it. ``met`` and ``not_met`` are answers, and a batch that overwrote one would
turn the person's own decision into netkeeper's — the mirror image of the rule
this module exists to keep, and it would push a contact they had already
reviewed back into the review queue. :class:`AlreadyDecided` says so instead.
"""

# Contacts per statement when a bulk apply walks its matches.
_CHUNK: Final[int] = 500

# The contact columns a decision may change, and how each one is stored in
# ``before_state`` and ``after_state``. Text, so one JSON map holds them all.
_MET_FIELD: Final[str] = "met"
_MET_SOURCE_FIELD: Final[str] = "met_source"
_TRIAGED_AT_FIELD: Final[str] = "triaged_at"
_PREFERRED_NAME_FIELD: Final[str] = "preferred_name"
_RECORDED_FIELDS: Final[frozenset[str]] = frozenset(
    {_MET_FIELD, _MET_SOURCE_FIELD, _TRIAGED_AT_FIELD, _PREFERRED_NAME_FIELD}
)
_MET_FIELDS: Final[tuple[str, ...]] = (_MET_FIELD, _MET_SOURCE_FIELD, _TRIAGED_AT_FIELD)
"""What a met decision writes, and therefore what it records and undo restores."""

# Columns restored by assignment rather than through ``set_manual_field``:
# neither carries provenance and neither is a field a person edits directly.
_DIRECTLY_RESTORED: Final[frozenset[str]] = frozenset({_MET_SOURCE_FIELD, _TRIAGED_AT_FIELD})


_DIVERGED_FIELD: Final[str] = (
    "something changed it after the decision, so undo would overwrite that change"
)
"""Why a recorded field that has moved on stops an undo. See :class:`UndoConflict`."""


# --- errors -----------------------------------------------------------------


class NotFound(LookupError):
    """The contact is not one of the user's. Never says which user has it."""


class InvalidDecision(ValueError):
    """A met value that is not one of the three keys, or an empty preferred name."""


class NothingToUndo(LookupError):
    """The user's undo stack is empty: every decision has been undone already."""


class UndoConflict(RuntimeError):
    """Undo cannot put this contact back the way the decision found it.

    Either a field the decision changed no longer holds what the decision left
    it holding, or the contact has left the queue since (archived, or merged
    into another). ``field``, ``expected``, and ``found`` say what diverged, as
    the decision log stores them, and ``reason`` says why that stops the undo.
    ``force`` on :func:`undo` restores the recorded previous state anyway.
    """

    def __init__(
        self,
        decision_id: int,
        contact_id: int,
        field: str,
        expected: str | None,
        found: str | None,
        reason: str = _DIVERGED_FIELD,
    ) -> None:
        self.decision_id = decision_id
        self.contact_id = contact_id
        self.field = field
        self.expected = expected
        self.found = found
        self.reason = reason
        super().__init__(
            f"contact {contact_id} has {field}={found!r} where the decision left "
            f"{expected!r}; {reason}"
        )


class AlreadyDecided(ValueError):
    """A batch was pointed at contacts whose ``met`` is somebody's own answer.

    ``states`` names what the queue holds, and for a batch that is
    :data:`BATCH_STATES` and nothing else.
    """

    def __init__(self, states: Iterable[ContactMet]) -> None:
        self.states = tuple(states)
        shown = ", ".join(state.value for state in self.states)
        allowed = ", ".join(sorted(state.value for state in BATCH_STATES))
        super().__init__(
            f"a batch decides for people nobody has answered for, so it cannot be applied "
            f"to {shown}; the states it takes are {allowed}"
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
    the same room. It is **not** the LinkedIn "you both worked at X" signal that
    spec 10.2 reads like, and 10.2 now records that divergence; a panel must not
    label it that way.

    ``company`` is the contact's own spelling; the overlap is counted without
    regard to case against other live contacts' current company. Two asymmetries
    follow: this contact's side counts their current company and every one of
    their positions, while the other side is matched on ``current_company``
    alone, so someone who was there with them and has moved on is not counted;
    and a company where nobody else is comes back with ``contact_count`` 0
    rather than being dropped, so the client sees the whole employment history
    and filters what it does not want.
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
    """Triaged against total, and how much of the current queue is left (spec 10.2).

    ``automatic`` counts the live contacts a batch decided and nobody has
    changed since: the size of the review pass, whatever queue is being served.
    """

    total: int
    triaged: int
    remaining: int
    by_state: dict[ContactMet, int]
    automatic: int = 0


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A bulk action offered to the user, with the count it would apply to.

    Never applied on its own: :func:`apply_suggestion` is a separate call, and
    what it does is one undoable batch. ``met`` is the value it would write, so
    a client can say which way a batch decides without parsing its key;
    ``tag_id`` names the tag a tag batch is built on, and is ``None`` otherwise.
    """

    key: str
    title: str
    description: str
    count: int
    met: ContactMet
    tag_id: int | None = None


@dataclass(frozen=True, slots=True)
class Applied:
    """The result of applying a bulk suggestion: how many, and the batch undo takes back."""

    key: str
    applied: int
    batch_id: str
    met: ContactMet


@dataclass(frozen=True, slots=True)
class Undone:
    """What one undo put back.

    ``contact`` is the contact to show next when a single decision was undone;
    a batch has no single contact, so it is ``None`` and ``decisions`` says how
    many rows were restored. ``forced`` lists the contacts whose state had moved
    on and was overwritten anyway; a contact named there may have been archived
    or merged away, so it is restored but not back in the queue.
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
    decided_by: MetSource | None = None,
) -> Contact | None:
    """The next contact to triage, or ``None`` when the queue is empty.

    The queue is the user's live contacts whose ``met`` is one of ``states``, in
    ``id`` order. ``after_id`` is the cursor: the first contact past it, which is
    how "next without deciding" moves on. Membership depends only on ``met`` and
    the order only on ``id``, so undoing a decision puts a contact back exactly
    where it stood.

    ``decided_by`` narrows further, and ``MetSource.AUTOMATIC`` with
    :data:`REVIEW_QUEUE_STATES` is the review pass: the contacts a batch decided
    and nobody has touched since, in the same order, on the same cards, with the
    same keys. Deciding one by hand takes it out of that queue, because deciding
    by hand is what ``manual`` means.
    """
    statement = _queue(user, states, decided_by=decided_by).order_by(Contact.id).limit(1)
    if after_id is not None:
        statement = statement.where(Contact.id > after_id)
    return session.scalars(statement).first()


def next_card(
    session: Session,
    user: User,
    *,
    states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES,
    after_id: int | None = None,
    decided_by: MetSource | None = None,
) -> Card | None:
    """:func:`next_contact` with its evidence loaded, ready to render."""
    contact = next_contact(session, user, states=states, after_id=after_id, decided_by=decided_by)
    return None if contact is None else load_card(session, user, contact)


def load_card(session: Session, user: User, contact: Contact) -> Card:
    """The evidence panel for one contact of ``user`` (spec 10.2).

    A fixed number of queries: the message counts, the newest messages, the
    timeline, the contact's positions, and the address-book overlap. None of them
    grows with how much history the contact has.
    """
    return Card(contact=contact, evidence=_evidence(session, user, contact))


def progress(
    session: Session,
    user: User,
    *,
    states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES,
    decided_by: MetSource | None = None,
) -> Progress:
    """How far triage has got, from one grouped count over the live contacts.

    Still one query: the group is ``(met, met_source)``, which gives the
    per-state counts, ``automatic`` (the size of the review pass), and the
    per-source split ``remaining`` needs. ``remaining`` counts the queue being
    served: the states asked for, narrowed to the source ``decided_by`` names,
    so the counter and :func:`next_contact` never disagree about what is left.
    """
    statement = (
        scoped(user, Contact)
        .with_only_columns(Contact.met, Contact.met_source, func.count())
        .where(Contact.archived_at.is_(None), Contact.merged_into_id.is_(None))
        .group_by(Contact.met, Contact.met_source)
    )
    by_state = dict.fromkeys(ContactMet, 0)
    by_source: dict[MetSource, dict[ContactMet, int]] = {
        source: dict.fromkeys(ContactMet, 0) for source in MetSource
    }
    for met, source, count in session.execute(statement).all():
        state = ContactMet(met)
        by_state[state] += count
        by_source[MetSource(source)][state] += count
    automatic = by_source[MetSource.AUTOMATIC]
    total = sum(by_state.values())
    wanted = _states(states)
    # ``remaining`` counts the queue being served, so it follows ``decided_by``
    # whichever source that names -- not only ``automatic``.
    counted = by_state if decided_by is None else by_source[MetSource(decided_by)]
    return Progress(
        total=total,
        triaged=total - by_state[ContactMet.UNKNOWN],
        remaining=sum(counted[state] for state in wanted),
        by_state=by_state,
        automatic=sum(automatic.values()),
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
    before = _snapshot(contact, _MET_FIELDS)
    # The person's own answer, whatever a batch decided before: that is what
    # takes a contact out of the review queue.
    set_met(contact, decided, source=MetSource.MANUAL)
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
    when it came from a bulk apply. Each contact must still be in the queue and
    still hold what the decision left it holding; otherwise nothing is written
    and :class:`UndoConflict` says what diverged (see :func:`_diverged` for the
    two ways that happens, an edit in between and a contact that was archived or
    merged away). ``force=True`` restores anyway and reports which contacts it
    overrode. The decisions it does undo are marked spent, so the next undo
    reaches the one before.

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


# --- the bulk suggestions ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Batch:
    """One offer in the catalogue: what it covers, what it decides, how it reads.

    ``where`` narrows the queue to the contacts it covers, and the same clauses
    serve the count, the preview, and the apply, so the three can never disagree
    about who is in it. ``template`` takes ``count`` and ``people``.
    """

    key: str
    met: ContactMet
    kind: TriageDecisionKind
    title: str
    template: str
    where: tuple[ColumnElement[bool], ...]
    tag_id: int | None = None

    def offer(self, count: int) -> Suggestion:
        return Suggestion(
            key=self.key,
            title=self.title,
            description=self.template.format(count=count, people=_people(count)),
            count=count,
            met=self.met,
            tag_id=self.tag_id,
        )


def suggestions(
    session: Session, user: User, *, states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES
) -> list[Suggestion]:
    """The bulk actions worth offering right now, with their counts (spec 10.2).

    Strongest evidence first, and the batch that assumes the most last. A
    suggestion whose count is zero is left out, so the banner appears only when
    there is something to accept. Nothing here writes: the count is a preview,
    :func:`suggestion_contacts` shows who it covers, and
    :func:`apply_suggestion` is a separate, explicit call.

    Every count is taken against the queue as it stands, so accepting one batch
    shrinks the others: a contact only ever belongs to whichever batch reaches
    them first, and the counts refresh after each apply. ``states`` may only be
    :data:`BATCH_STATES`; :class:`AlreadyDecided` for anything a person has
    answered themselves.
    """
    wanted = _batch_states(states)
    offers: list[Suggestion] = []
    for batch in _catalogue(session, user):
        count = session.scalar(_queue_count(user, wanted).where(*batch.where)) or 0
        if count:
            offers.append(batch.offer(count))
    return offers


def suggestion_contacts(
    session: Session,
    user: User,
    key: str,
    *,
    states: Sequence[ContactMet] = DEFAULT_QUEUE_STATES,
    limit: int = SUGGESTION_PAGE,
    offset: int = 0,
) -> tuple[list[Contact], int]:
    """One page of the contacts a suggestion covers, in queue order, and the total.

    The preview the count alone cannot give: a batch that decides hundreds of
    people at once has to be readable as a list of names before anyone says yes.
    The page is narrowed by the same clauses the apply uses, so what is shown is
    what would be decided. Three queries whatever the page holds: the count, the
    contacts, and their tags in one go rather than one query per row (a tag is
    why some of these batches exist, so the rows carry them). Writes nothing.
    :class:`InvalidDecision` for an unknown key, a limit under 1, or a negative
    offset; :class:`AlreadyDecided` for a state outside :data:`BATCH_STATES`.
    """
    batch = _batch_by_key(session, user, key)
    wanted = _batch_states(states)
    if limit < 1 or offset < 0:
        raise InvalidDecision("limit must be at least 1 and offset cannot be negative")
    total = session.scalar(_queue_count(user, wanted).where(*batch.where)) or 0
    page = session.scalars(
        _queue(user, wanted)
        .where(*batch.where)
        .order_by(Contact.id)
        .limit(limit)
        .offset(offset)
        .options(selectinload(Contact.tags))
    ).all()
    return list(page), total


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
    decision row carrying its own previous state, the batch id, and the key of
    the suggestion that decided it, so undo restores each one exactly — including
    a contact that was already ``not_met`` — and the log can always say what
    netkeeper decided and why. Each contact is left with ``met_source`` set to
    ``automatic``, which is what the review queue serves and what keeps this from
    passing as a decision the person made.

    It reaches only the contacts in :data:`BATCH_STATES`, whatever ``states``
    asks for: overwriting an answer the person gave would replace their
    ``manual`` with ``automatic`` and put a contact they had already reviewed
    back in the review queue, which is this module's promise run backwards.
    :class:`AlreadyDecided` says so, and nothing is written.
    :class:`InvalidDecision` for an unknown key; ``RuntimeError`` when
    ``session`` is not a writer.
    """
    _require_writer(session)
    batch = _batch_by_key(session, user, key)
    wanted = _batch_states(states)
    moment = at if at is not None else utcnow()
    _require_aware(moment)
    statement = _queue(user, wanted).where(*batch.where).order_by(Contact.id)
    matches = list(session.scalars(statement).all())
    if expected_count is not None and expected_count != len(matches):
        raise CountChanged(expected_count, len(matches))
    batch_id = uuid.uuid4().hex
    for contact in matches:
        before = _snapshot(contact, _MET_FIELDS)
        set_met(contact, batch.met, source=MetSource.AUTOMATIC)
        contact.triaged_at = moment
        _log(
            session,
            user,
            contact,
            batch.kind,
            before=before,
            at=moment,
            batch_id=batch_id,
            reason=batch.key,
            flush=False,
        )
    session.flush()
    log.info(
        "bulk suggestion %s marked %d contacts %s for user %d (batch %s)",
        key,
        len(matches),
        batch.met.value,
        user.id,
        batch_id,
    )
    return Applied(key=key, applied=len(matches), batch_id=batch_id, met=batch.met)


# --- the catalogue ----------------------------------------------------------


def _catalogue(session: Session, user: User) -> list[_Batch]:
    """Every batch that exists for this user, strongest evidence first.

    Why each one earns its place, and what it assumes:

    * **Message history.** You and this person wrote to each other. Nothing else
      in an archive comes close, and it is the batch that was here first.
      Invitations are excluded, which they were not before: the importer stores
      an invitation as an ``li_in``/``li_out`` row like any message, so the old
      count quietly swept in everyone who had ever clicked Connect.
    * **An invitation note.** One of you wrote something personal with the
      invitation. Weaker than a thread and much rarer, so it is its own offer
      rather than a widening of the first, and it covers only people with no
      message thread — a person who has both is already in the batch above.
    * **A tag the user gave a meaning.** Only tags carrying
      :class:`~netkeeper.models.TagMetSignal` count, one batch per tag, so
      "everyone I tagged *recruiter* is someone I have not met" is one decision
      the user makes once and then previews against the handful it covers rather
      than the whole address book.
      The tag may have been applied by a rule or by hand: the tag means what the
      user says it means however it got there, and a tag they placed themselves
      is the stronger evidence of the two. A rule never decides ``met`` on its
      own — it tags, and a batch the person accepts turns tags into decisions.
    * **No evidence at all.** No interaction of any kind on file, no tag the
      user has given a meaning, and nothing on the card
      (:func:`_card_carries_evidence`). This is the one batch that argues from
      absence, so it is offered last, described plainly, and held to one rule:
      nobody is decided in bulk while the card the batch could have shown has
      something on it. What the panel counts is what the batch withholds on —
      the same question, asked once in :func:`_shared_companies` and once here.
    """
    return [
        _Batch(
            key=SUGGESTION_MET_WITH_MESSAGES,
            met=ContactMet.MET,
            kind=TriageDecisionKind.BULK_MET,
            title="Mark everyone with message history as met",
            template="You have message threads with {count} untriaged {people}.",
            where=(_messaged(user),),
        ),
        _Batch(
            key=SUGGESTION_MET_INVITATION_NOTE,
            met=ContactMet.MET,
            kind=TriageDecisionKind.BULK_MET,
            title="Mark everyone who swapped an invitation note as met",
            template=(
                "An invitation carried a personal note one way or the other with "
                "{count} untriaged {people}, and no message thread followed."
            ),
            where=(_invited_with_a_note(user), ~_messaged(user)),
        ),
        *_tag_batches(session, user),
        _Batch(
            key=SUGGESTION_NOT_MET_NO_EVIDENCE,
            met=ContactMet.NOT_MET,
            kind=TriageDecisionKind.BULK_NOT_MET,
            title="Mark everyone there is no evidence about as not met",
            template=(
                "There is nothing on file for {count} untriaged {people}: no messages, "
                "no invitation, no note, no tag of your own, and nobody else at their "
                "company — their card would be empty."
            ),
            where=(
                _has_no_interactions(user),
                ~_carries_a_meaningful_tag(user),
                ~_card_carries_evidence(user),
            ),
        ),
    ]


def _tag_batches(session: Session, user: User) -> list[_Batch]:
    """One batch per tag the user has given a met signal, met before not met."""
    tags = session.scalars(
        scoped(user, Tag).where(Tag.met_signal.is_not(None)).order_by(Tag.met_signal, Tag.name_key)
    ).all()
    return [_tag_batch(tag) for tag in tags]


def _tag_batch(tag: Tag) -> _Batch:
    """The batch for one tag the user has given a meaning."""
    if tag.met_signal is None:  # pragma: no cover - every caller filters on it first
        raise InvalidDecision(f"the tag {tag.name!r} says nothing about having met someone")
    decides_met = TagMetSignal(tag.met_signal) is TagMetSignal.MET
    met = ContactMet.MET if decides_met else ContactMet.NOT_MET
    reading = "have met" if decides_met else "have not met"
    return _Batch(
        key=f"{TAG_KEY_PREFIX}{tag.id}",
        met=met,
        kind=TriageDecisionKind.BULK_MET if decides_met else TriageDecisionKind.BULK_NOT_MET,
        title=f"Mark everyone tagged {tag.name} as {'met' if decides_met else 'not met'}",
        template=(
            f"{{count}} untriaged {{people}} carry the tag {tag.name}, "
            f"which you have said means you {reading} them."
        ),
        where=(_carries_tag(tag),),
        tag_id=tag.id,
    )


def _batch_by_key(session: Session, user: User, key: str) -> _Batch:
    """The batch called ``key``, or :class:`InvalidDecision`.

    A tag batch is looked up by id rather than found in the catalogue, so the
    error for a tag that is not the user's, or that carries no signal, says
    which of the two it is.
    """
    if key.startswith(TAG_KEY_PREFIX):
        return _tag_batch(_signalled_tag(session, user, key))
    for batch in _catalogue(session, user):
        if batch.key == key:
            return batch
    raise InvalidDecision(f"no bulk suggestion named {key!r}")


def _signalled_tag(session: Session, user: User, key: str) -> Tag:
    raw = key[len(TAG_KEY_PREFIX) :]
    if not raw.isdigit():
        raise InvalidDecision(f"no bulk suggestion named {key!r}")
    tag = get_scoped(session, user, Tag, int(raw))
    if tag is None:
        raise InvalidDecision(f"no bulk suggestion named {key!r}")
    if tag.met_signal is None:
        raise InvalidDecision(
            f"the tag {tag.name!r} says nothing about having met someone; "
            "give it a meaning before triaging by it"
        )
    return tag


def _batch_states(states: Sequence[ContactMet]) -> list[ContactMet]:
    """``states`` as a batch may use them, or :class:`AlreadyDecided`.

    The count, the preview, and the apply all pass through here, so a client can
    never be shown a batch it would be refused for.
    """
    wanted = _states(states)
    answered = [state for state in wanted if state not in BATCH_STATES]
    if answered:
        raise AlreadyDecided(answered)
    return wanted


def _people(count: int) -> str:
    return "person" if count == 1 else "people"


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
    reason: str | None = None,
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
        reason=reason,
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
    if name == _MET_SOURCE_FIELD:
        # A row written before met_source existed records none; a contact whose
        # decision predates the column was decided by hand or not at all.
        return MetSource.MANUAL if raw is None else MetSource(raw)
    if name == _TRIAGED_AT_FIELD:
        return None if raw is None else datetime.fromisoformat(raw)
    if name == _PREFERRED_NAME_FIELD:
        return raw if raw is not None else ""
    raise ValueError(f"{name!r} is not a field the triage log records")


type Divergence = tuple[str, str | None, str | None, str]


def _diverged(row: TriageDecision, contact: Contact) -> Divergence | None:
    """The first thing that stops undo from putting ``contact`` back, or ``None``.

    Two kinds. A field ``row`` changed that no longer holds what the decision
    left it holding: something edited it in between, and that edit is not this
    decision's to overwrite.

    And a contact that has left the queue since, which the fields alone never
    show, because neither archiving nor merging touches one of them:

    - Merged away. :func:`netkeeper.crm.identity.merge` copies the loser's
      ``met`` and ``triaged_at`` onto the survivor and leaves the loser's
      columns as they were, so the loser still holds exactly what the decision
      left. Restoring it would clear the decision on a row nobody reads while
      the survivor quietly keeps it, and mark the row spent, so no second undo
      could reach it.
    - Archived. Restoring ``met`` would report a contact back in the queue that
      :func:`next_contact` will never serve again, which is the one promise undo
      makes about position.

    Either way the state the decision found no longer exists to be restored, so
    undo says so instead of half-doing it. A contact decided while it was
    already archived (only reachable by hand: the queue never serves one) is
    refused too, which is the conservative side of the same rule.
    """
    if contact.merged_into_id is not None:
        return (
            "merged_into_id",
            None,
            str(contact.merged_into_id),
            "it was merged away after the decision, and the survivor carries that decision now",
        )
    if contact.archived_at is not None:
        return (
            "archived_at",
            None,
            contact.archived_at.isoformat(),
            "it was archived after the decision, so undo cannot put it back in the queue",
        )
    for name, expected in row.after_state.items():
        found = _encode(name, getattr(contact, name))
        if found != expected:
            return name, expected, found, _DIVERGED_FIELD
    return None


def _restore(contact: Contact, row: TriageDecision) -> None:
    """Put every field of ``row.before_state`` back on ``contact``.

    ``met`` and ``preferred_name`` go through
    :func:`~netkeeper.crm.provenance.set_manual_field`, as the decision did:
    undoing an edit is an edit, and the field stays the person's. ``met_source``
    and ``triaged_at`` are assigned: neither carries provenance, and
    ``met_source`` records who decided, which undo restores rather than claims —
    so undoing a hand-made decision over a batch's puts the contact back in the
    review queue exactly as the batch left it.
    """
    for name, raw in row.before_state.items():
        value = _decode(name, raw)
        if name in _DIRECTLY_RESTORED:
            setattr(contact, name, value)
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


def _queue(
    user: User, states: Sequence[ContactMet], *, decided_by: MetSource | None = None
) -> Select[tuple[Contact]]:
    """``scoped(user, Contact)`` narrowed to the live contacts in ``states``."""
    return scoped(user, Contact).where(*_queue_where(states, decided_by))


def _queue_count(
    user: User, states: Sequence[ContactMet], *, decided_by: MetSource | None = None
) -> Select[tuple[int]]:
    return scoped_count(user, Contact).where(*_queue_where(states, decided_by))


def _queue_where(
    states: Sequence[ContactMet], decided_by: MetSource | None = None
) -> list[ColumnElement[bool]]:
    where: list[ColumnElement[bool]] = [
        Contact.met.in_(_states(states)),
        Contact.archived_at.is_(None),
        Contact.merged_into_id.is_(None),
    ]
    if decided_by is not None:
        where.append(Contact.met_source == MetSource(decided_by))
    return where


# --- what a batch covers ----------------------------------------------------
#
# Every one of these is a single ``ColumnElement[bool]`` over ``Contact``, so a
# batch narrows the count, the preview, and the apply with the same clauses, and
# a clause can be negated (``~``) to build the complement. Each is an ``IN`` over
# a scoped subquery rather than a join, so the outer statement still returns one
# row per contact however much history there is. Every subquery carries its own
# ``user_id``, as one on an owned table must (ADR 0005); the outer statement
# carries the scope mark for the guard.


def _message_senders(user: User) -> Select[tuple[int]]:
    """The ids of the contacts with at least one real message, either direction."""
    return select(Interaction.contact_id).where(
        Interaction.user_id == user.id,
        Interaction.kind.in_(sorted(MESSAGE_KINDS)),
        ~is_invitation(),
    )


def _messaged(user: User) -> ColumnElement[bool]:
    """The contacts you and they have actually written to each other.

    Invitations are excluded. The importer writes one as an ``li_in``/``li_out``
    row like any message, so without this the batch would count a bare "they
    clicked Connect" as a conversation; against the reference archive that was 7
    people out of 178.
    """
    return Contact.id.in_(_message_senders(user))


def _invited_with_a_note(user: User) -> ColumnElement[bool]:
    """The contacts an invitation note passed between, in either direction."""
    return Contact.id.in_(
        select(Interaction.contact_id).where(
            Interaction.user_id == user.id,
            Interaction.kind.in_(sorted(MESSAGE_KINDS)),
            has_invitation_note(),
        )
    )


def _has_no_interactions(user: User) -> ColumnElement[bool]:
    """The contacts with no interaction of any kind: no message, no invitation, no note."""
    return Contact.id.not_in(select(Interaction.contact_id).where(Interaction.user_id == user.id))


def _carries_a_meaningful_tag(user: User) -> ColumnElement[bool]:
    """The contacts carrying any tag the user has given a met signal."""
    return Contact.id.in_(
        select(ContactTag.contact_id)
        .join(Tag, Tag.id == ContactTag.tag_id)
        .where(
            ContactTag.user_id == user.id,
            Tag.user_id == user.id,
            Tag.met_signal.is_not(None),
        )
    )


def _carries_tag(tag: Tag) -> ColumnElement[bool]:
    """The contacts carrying one tag, however it was assigned.

    A rule, the LLM module, or the user's own hand: the tag means what the user
    said it means whichever put it there, and the one they placed themselves is
    the more trustworthy of the two.
    """
    return Contact.id.in_(
        select(ContactTag.contact_id).where(
            ContactTag.user_id == tag.user_id, ContactTag.tag_id == tag.id
        )
    )


def _card_carries_evidence(user: User) -> ColumnElement[bool]:
    """The contacts whose card has something on it, whatever that something is.

    The rule the "no evidence" batch is built on: nobody is decided in bulk
    while the card that batch could have shown has something to say about them.
    So this is the panel's own question, asked of the database — one clause per
    thing :class:`Card` carries that a batch does not otherwise read:

    * a company somebody else in the address book is at (:func:`_shared_companies`
      is the panel, and :func:`_shares_a_company_the_panel_shows` asks exactly
      what it counts, past positions and all);
    * a note the person typed, which ``TriageContactOut`` carries and calls part
      of the evidence panel;
    * a tag the person put on by hand — a rule's tag is not evidence *they* know
      this contact, but their own is;
    * a preferred name they set with the ``p`` key, which is a thing only triage
      writes;
    * ``do_not_contact``, which is a decision about this person already.

    The last four never fire on a freshly imported archive. They fire on the
    second import, when somebody has been using netkeeper for a month, met
    someone, written a note about them and not got round to triaging them — and
    the review pass is exactly where they would find out netkeeper had ignored
    their own note.
    """
    return or_(
        _shares_a_company_the_panel_shows(user),
        func.trim(func.coalesce(Contact.notes, "")) != "",
        _carries_a_hand_placed_tag(user),
        _has_a_chosen_preferred_name(),
        Contact.do_not_contact.is_(True),
    )


def _shares_a_company_the_panel_shows(user: User) -> ColumnElement[bool]:
    """The contacts whose card shows a company with somebody else in it.

    Exactly what :func:`_shared_companies` counts, so the batch and the panel
    cannot disagree: every company this contact is at *or has been at*
    (``contact_positions``), against the current company of every other live
    contact, without regard to case or surrounding space. ``other.id !=
    Contact.id`` is the panel's "other contacts" and keeps a contact from
    withholding itself.

    Correlated, so it costs a lookup per candidate row rather than one scan;
    ``ix_contacts_user_id_current_company`` is what that lookup uses.
    """
    other = aliased(Contact)
    theirs = func.lower(func.trim(other.current_company))
    live = (
        other.user_id == user.id,
        other.id != Contact.id,
        other.archived_at.is_(None),
        other.merged_into_id.is_(None),
        func.trim(func.coalesce(other.current_company, "")) != "",
    )
    # Two ``EXISTS`` rather than one with a nested subquery: a subquery two
    # levels down does not correlate back to the outer ``contacts`` row, and
    # silently reads "any position of anybody" instead of "this contact's".
    at_the_same_company = select(other.id).where(
        *live, theirs == func.lower(func.trim(Contact.current_company))
    )
    where_they_used_to_be = (
        select(other.id)
        .join(
            ContactPosition,
            and_(
                ContactPosition.user_id == user.id,
                ContactPosition.contact_id == Contact.id,
                func.trim(func.coalesce(ContactPosition.company, "")) != "",
                theirs == func.lower(func.trim(ContactPosition.company)),
            ),
        )
        .where(*live)
    )
    return or_(at_the_same_company.exists(), where_they_used_to_be.exists())


def _carries_a_hand_placed_tag(user: User) -> ColumnElement[bool]:
    """The contacts carrying a tag the person put on themselves.

    ``manual`` only: a rule's tag says what a pattern matched, and nearly every
    imported contact carries one, so it is no evidence that the person knows
    this contact. Theirs is.
    """
    return Contact.id.in_(
        select(ContactTag.contact_id).where(
            ContactTag.user_id == user.id, ContactTag.source == TagSource.MANUAL
        )
    )


def _has_a_chosen_preferred_name() -> ColumnElement[bool]:
    """The contacts whose preferred name is not simply their first name.

    The column defaults to ``first_name`` and the ``p`` key is what changes it,
    so a difference is a thing the person typed about this contact.
    """
    chosen = func.trim(Contact.preferred_name)
    return and_(chosen != "", chosen != func.trim(Contact.first_name))


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
