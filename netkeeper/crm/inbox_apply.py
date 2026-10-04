"""Apply an inbox poll's delta: interactions and conversation rows (P4-08, #378).

The core half of the LinkedIn inbox poll. :mod:`netkeeper.linkedin.inbox` hands
back an :class:`~netkeeper.linkedin.inbox.InboxDelta`; this module, the only one
that maps it onto rows (spec 9.10), writes it in the caller's writer session.
It also answers the two questions the runner asks before a poll: whose
messages the core cares about (:func:`watched_urns`) and which threads the poll
may open (:func:`threads_to_open`).

**Matching is by URN only.** A conversation's ``counterpart_urn`` is matched
against ``contacts.li_urn``, never against a name. A conversation with nobody
known is ignored and counted (``ignored_unknown``); the inbox never creates a
contact. A conversation that holds an inbound message from anyone but its
counterpart is a group thread the page did not catch, and is skipped and
counted with the page's own group count, so nobody's words are attributed to
the wrong person.

**What is written.** For a match, one ``li_conversations`` row per
conversation URN, upserted, and one ``li_in`` or ``li_out`` interaction per
message, with ``external_id`` set to the message URN, ``at`` truncated to whole
seconds, ``source = sync``, and the summary ``LinkedIn message: <snippet>``.
``external_id`` is unique per user, so a repeat poll writes nothing twice. A
message an archive import already recorded at the same contact, kind, and
second is not written again either: the archive's rows have no message URN, so
the poll adopts the matching row instead, writing the URN into its
``external_id``; an adopted inbound message still reaches the reply hook. The
archive import counts polled rows the other way (``crm/archive.py``). Archive
invitation rows are not messages and take part in neither.

**Message text** goes into the interaction's summary and nowhere else: not a
log line, not a count, not an exception message.

**The reply hook.** After the rows are written, every handler in
:data:`REPLY_HANDLERS` is called with the new inbound messages, in the same
writer session. The list is empty until P4-02 (#381) adds the handler that
reads a reply into a campaign.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy import Select, or_
from sqlalchemy.orm import Session

from netkeeper.crm.interactions import add_interaction, summary_is_invitation
from netkeeper.db import is_writer
from netkeeper.linkedin.inbox import (
    MAX_THREADS_OPENED,
    SNIPPET_MAX,
    InboxConversation,
    InboxDelta,
)
from netkeeper.models import (
    Campaign,
    Contact,
    ContactSource,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    LiConversation,
    Message,
    MessageDirection,
    MessageStatus,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services.campaign_guards import (
    LIVE_ENROLLMENT_STATUSES,
    RUNNING_CAMPAIGN_STATUSES,
)
from netkeeper.services.settings_kv import delete_setting, get_setting, set_setting

log = logging.getLogger(__name__)

#: How a polled message's interaction summary starts.
SUMMARY_PREFIX: Final = "LinkedIn message"

#: The message statuses whose conversation a poll may open: a prefill waiting to be
#: seen sent, or one not seen sent in time (P4-06's decision, #374).
OPEN_FOR_STATUSES: Final = frozenset({MessageStatus.PREFILLED, MessageStatus.STALE})

_POLLED_KINDS: Final = (InteractionKind.LI_IN, InteractionKind.LI_OUT)


@dataclass(frozen=True, slots=True)
class NewInbound:
    """One inbound message new to the poll: written now, or an archive row it adopted. No text."""

    contact_id: int
    interaction_id: int
    conversation_urn: str
    message_urn: str
    at: datetime


@dataclass(slots=True)
class InboxCounts:
    """What applying one delta did. Numbers only: the run's ``counts_json``."""

    conversations_read: int = 0
    matched: int = 0
    ignored_unknown: int = 0
    skipped_group: int = 0
    skipped_other: int = 0
    messages_new: int = 0
    new_inbound: list[NewInbound] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "conversations_read": self.conversations_read,
            "matched": self.matched,
            "ignored_unknown": self.ignored_unknown,
            "skipped_group": self.skipped_group,
            "skipped_other": self.skipped_other,
            "messages_new": self.messages_new,
        }


ReplyHandler = Callable[[Session, User, tuple[NewInbound, ...]], None]

#: Called after every applied delta with its new inbound messages, in the same writer
#: session. Empty until P4-02 (#381) appends its handler.
REPLY_HANDLERS: Final[list[ReplyHandler]] = []


# --- what the poll asks before it reads ---------------------------------------------


def _live_enrollment_contacts(user: User) -> Select[tuple[Contact]]:
    """Contacts with a LinkedIn URN in a live enrollment of a running campaign."""
    return (
        scoped(user, Contact)
        .join(Enrollment, Enrollment.contact_id == Contact.id)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(
            Enrollment.user_id == user.id,
            Campaign.user_id == user.id,
            Enrollment.status.in_(LIVE_ENROLLMENT_STATUSES),
            Campaign.status.in_(RUNNING_CAMPAIGN_STATUSES),
            Contact.li_urn.is_not(None),
            Contact.merged_into_id.is_(None),
        )
    )


def watched_urns(session: Session, user: User) -> frozenset[str]:
    """The ``li_urn`` of every contact in a live enrollment, any channel. Read-only.

    A live enrollment is one in :data:`LIVE_ENROLLMENT_STATUSES` of a campaign in
    :data:`RUNNING_CAMPAIGN_STATUSES`, as the campaign guards read it.
    """
    statement = _live_enrollment_contacts(user).with_only_columns(Contact.li_urn).distinct()
    return frozenset(urn for urn in session.scalars(statement) if urn)


def has_anything_to_watch(session: Session, user: User) -> bool:
    """Whether any live enrollment has a contact with a LinkedIn URN. Read-only."""
    statement = _live_enrollment_contacts(user).with_only_columns(Contact.id).limit(1)
    return session.scalar(statement) is not None


def first_live_outreach(session: Session, user: User, *, now: datetime) -> datetime | None:
    """The earliest sent outbound campaign message the poll watches for replies to.

    The first poll's ``since`` when no poll has completed yet (#388 review, S3): what a
    reply could be answering starts there. It counts every sent outbound message, any
    channel, of a live enrollment, and of a ``completed`` one whose latest send is
    within ``WATCH_AFTER_COMPLETED`` of ``now``, the set the Gmail reply poll watches
    (``services.campaign_replies``). ``None`` when none has sent anything. Read-only.
    """
    # Imported here: campaign_replies reaches the campaign engine, which this module
    # must not load just to be imported.
    from netkeeper.services.campaign_replies import WATCH_AFTER_COMPLETED

    live = (
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .with_only_columns(Enrollment.id)
        .where(
            Campaign.user_id == user.id,
            Enrollment.status.in_(LIVE_ENROLLMENT_STATUSES),
            Campaign.status.in_(RUNNING_CAMPAIGN_STATUSES),
        )
    )
    completed = (
        scoped(user, Enrollment)
        .with_only_columns(Enrollment.id)
        .where(Enrollment.status == EnrollmentStatus.COMPLETED)
    )
    sent = session.execute(
        scoped(user, Message)
        .with_only_columns(Message.enrollment_id, Message.sent_at)
        .where(
            or_(Message.enrollment_id.in_(live), Message.enrollment_id.in_(completed)),
            Message.direction == MessageDirection.OUT,
            Message.sent_at.is_not(None),
        )
    ).all()
    live_ids = set(session.scalars(live))
    by_enrollment: dict[int, list[datetime]] = {}
    for enrollment_id, sent_at in sent:
        by_enrollment.setdefault(enrollment_id, []).append(sent_at)
    watched = [
        min(times)
        for enrollment_id, times in by_enrollment.items()
        if enrollment_id in live_ids or now - max(times) <= WATCH_AFTER_COMPLETED
    ]
    return min(watched, default=None)


# --- a first poll that could not read back far enough ------------------------------------

#: The ``settings_kv`` key holding the date a short first poll could not read back to.
SHORT_FIRST_POLL_KEY: Final = "linkedin.inbox.first_poll_short_of"


def short_first_poll(session: Session, user: User) -> datetime | None:
    """The date the first poll could not read back to, while that still stands. Read-only."""
    raw = get_setting(session, user, SHORT_FIRST_POLL_KEY)
    if not isinstance(raw, str):
        return None
    try:
        found = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return found if found.tzinfo is not None else None


def record_short_first_poll(session: Session, user: User, since: datetime) -> None:
    """Remember that the first poll could not read back to ``since``. Needs a writer."""
    set_setting(session, user, SHORT_FIRST_POLL_KEY, since.isoformat())


def clear_short_first_poll(session: Session, user: User) -> bool:
    """Forget it: a person acknowledged it, or a complete poll covered it. Needs a writer."""
    return delete_setting(session, user, SHORT_FIRST_POLL_KEY)


def threads_to_open(session: Session, user: User) -> frozenset[str]:
    """The conversation URNs a poll may open, newest first, at most :data:`MAX_THREADS_OPENED`.

    A conversation qualifies when one of its campaign messages is ``prefilled`` or
    ``stale``, or when it is a known conversation with a contact in a live
    enrollment. Read-only.
    """
    newest: dict[str, datetime] = {}

    def offer(urn: str | None, at: datetime | None) -> None:
        if not urn or at is None:
            return
        if urn not in newest or at > newest[urn]:
            newest[urn] = at

    messages = scoped(user, Message).where(
        Message.status.in_(OPEN_FOR_STATUSES), Message.li_conversation_urn.is_not(None)
    )
    for message in session.scalars(messages):
        offer(message.li_conversation_urn, message.updated_at)
    live = _live_enrollment_contacts(user).with_only_columns(Contact.id)
    conversations = scoped(user, LiConversation).where(LiConversation.contact_id.in_(live))
    for conversation in session.scalars(conversations):
        offer(conversation.conversation_urn, conversation.last_activity_at)
    ordered = sorted(newest, key=lambda urn: (newest[urn], urn), reverse=True)
    return frozenset(ordered[:MAX_THREADS_OPENED])


# --- applying a delta ----------------------------------------------------------------


def apply_delta(
    session: Session, user: User, delta: InboxDelta, *, polled_at: datetime
) -> InboxCounts:
    """Write ``delta`` for ``user`` and return what it did. See the module docstring.

    Needs a writer session. Idempotent: applying the same delta again writes no
    interaction and leaves the conversation rows as they were, apart from
    ``polled_at``.
    """
    if not is_writer(session):
        raise RuntimeError(
            "applying an inbox delta needs a writer session; use session_scope(factory, write=True)"
        )
    if polled_at.tzinfo is None or polled_at.utcoffset() is None:
        raise ValueError("polled_at must be timezone-aware")
    counts = InboxCounts(
        conversations_read=len(delta.conversations),
        skipped_group=delta.skipped_group,
        skipped_other=delta.skipped_other,
    )
    one_to_one = [c for c in delta.conversations if not _is_group(c)]
    counts.skipped_group += len(delta.conversations) - len(one_to_one)
    contacts = _contacts_by_urn(session, user, {c.counterpart_urn for c in one_to_one})
    matched = [
        (c, contacts[c.counterpart_urn]) for c in one_to_one if c.counterpart_urn in contacts
    ]
    counts.ignored_unknown = len(one_to_one) - len(matched)
    counts.matched = len(matched)
    if not matched:
        _call_reply_handlers(session, user, counts)
        return counts
    known_ids = _known_external_ids(
        session, user, {m.message_urn for c, _ in matched for m in c.messages}
    )
    archived = _ArchiveLedger(session, user, {contact_id for _, contact_id in matched})
    for conversation, contact_id in matched:
        _upsert_conversation(session, user, conversation, contact_id, polled_at=polled_at)
        for message in conversation.messages:
            if message.message_urn in known_ids:
                continue
            known_ids.add(message.message_urn)
            kind = InteractionKind.LI_OUT if message.outbound else InteractionKind.LI_IN
            at = message.at.replace(microsecond=0)
            adopted = archived.claim(contact_id, kind, at)
            if adopted is not None:
                # An archive import recorded this message already: the row takes its
                # URN, and an inbound one still reaches the reply hook (#388 review, S2).
                adopted.external_id = message.message_urn
                session.flush()
                if not message.outbound:
                    counts.new_inbound.append(
                        NewInbound(
                            contact_id=contact_id,
                            interaction_id=adopted.id,
                            conversation_urn=conversation.conversation_urn,
                            message_urn=message.message_urn,
                            at=at,
                        )
                    )
                continue
            interaction = add_interaction(
                session,
                user,
                contact_id,
                kind,
                at,
                summary(message.text_snippet),
                source=ContactSource.SYNC,
                external_id=message.message_urn,
            )
            counts.messages_new += 1
            if not message.outbound:
                counts.new_inbound.append(
                    NewInbound(
                        contact_id=contact_id,
                        interaction_id=interaction.id,
                        conversation_urn=conversation.conversation_urn,
                        message_urn=message.message_urn,
                        at=at,
                    )
                )
    log.info(
        "inbox: %d conversations read, %d matched, %d ignored, %d new messages",
        counts.conversations_read,
        counts.matched,
        counts.ignored_unknown,
        counts.messages_new,
    )
    _call_reply_handlers(session, user, counts)
    return counts


def summary(snippet: str) -> str:
    """A polled message's interaction summary: ``LinkedIn message: <snippet>``."""
    text = snippet.strip()[:SNIPPET_MAX]
    return f"{SUMMARY_PREFIX}: {text}" if text else SUMMARY_PREFIX


def _is_group(conversation: InboxConversation) -> bool:
    """A conversation with an inbound message from anyone but its counterpart."""
    return any(
        not message.outbound and message.sender_urn != conversation.counterpart_urn
        for message in conversation.messages
    )


def _contacts_by_urn(session: Session, user: User, urns: set[str]) -> dict[str, int]:
    if not urns:
        return {}
    statement = (
        scoped(user, Contact)
        .with_only_columns(Contact.li_urn, Contact.id)
        .where(Contact.li_urn.in_(urns), Contact.merged_into_id.is_(None))
    )
    return {urn: contact_id for urn, contact_id in session.execute(statement) if urn}


def _known_external_ids(session: Session, user: User, urns: set[str]) -> set[str]:
    if not urns:
        return set()
    statement = (
        scoped(user, Interaction)
        .with_only_columns(Interaction.external_id)
        .where(Interaction.external_id.in_(urns))
    )
    return {urn for urn in session.scalars(statement) if urn}


def _upsert_conversation(
    session: Session,
    user: User,
    conversation: InboxConversation,
    contact_id: int,
    *,
    polled_at: datetime,
) -> None:
    inbound = [m.at for m in conversation.messages if not m.outbound]
    outbound = [m.at for m in conversation.messages if m.outbound]
    row = session.scalars(
        scoped(user, LiConversation).where(
            LiConversation.conversation_urn == conversation.conversation_urn
        )
    ).one_or_none()
    if row is None:
        row = LiConversation(
            user_id=user.id,
            contact_id=contact_id,
            conversation_urn=conversation.conversation_urn,
            last_activity_at=conversation.last_activity_at,
            polled_at=polled_at,
        )
        session.add(row)
    row.contact_id = contact_id
    row.last_activity_at = _later(row.last_activity_at, conversation.last_activity_at)
    row.last_inbound_at = _later(row.last_inbound_at, max(inbound, default=None))
    row.last_outbound_at = _later(row.last_outbound_at, max(outbound, default=None))
    row.polled_at = polled_at
    session.flush()


def _later[T: datetime | None](current: T, seen: T) -> T:
    if current is None:
        return seen
    if seen is None:
        return current
    return seen if seen > current else current


class _ArchiveLedger:
    """Archive-imported LinkedIn messages at ``(contact, kind, second)``, by row.

    The archive import has no message URN, so a message it recorded is known here
    only by its contact, kind, and second. A polled message that matches one adopts
    that row (:meth:`claim`): the poll writes its URN into the row's ``external_id``,
    so the next poll finds it by URN and the archive row stands for exactly one
    message. Rows that already carry a URN are not in the ledger, and neither are
    the archive's invitation rows, which are not messages.
    """

    __slots__ = ("_rows",)

    def __init__(self, session: Session, user: User, contact_ids: set[int]) -> None:
        self._rows: dict[tuple[int, InteractionKind, datetime], list[Interaction]] = {}
        statement = (
            scoped(user, Interaction)
            .where(
                Interaction.contact_id.in_(contact_ids),
                Interaction.kind.in_(_POLLED_KINDS),
                Interaction.source == ContactSource.ARCHIVE,
                Interaction.external_id.is_(None),
            )
            .order_by(Interaction.id)
        )
        for row in session.scalars(statement):
            if summary_is_invitation(row.summary):
                continue
            key = (row.contact_id, row.kind, row.at.replace(microsecond=0))
            self._rows.setdefault(key, []).append(row)

    def claim(self, contact_id: int, kind: InteractionKind, at: datetime) -> Interaction | None:
        """The archive row this message adopts, removed from the ledger; ``None`` if none."""
        rows = self._rows.get((contact_id, kind, at))
        return rows.pop(0) if rows else None


def _call_reply_handlers(session: Session, user: User, counts: InboxCounts) -> None:
    new = tuple(counts.new_inbound)
    for handler in REPLY_HANDLERS:
        handler(session, user, new)
