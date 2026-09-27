"""Campaign guards: who may be enrolled, and who may be sent the next step (spec 11.9; P3-05).

Spec 11.9 checks every contact at enrollment and again at every step fire,
because a contact's state changes in between. This module is those checks.

Shape
-----
The guards are pure functions over a :class:`ContactFacts`, a snapshot of
everything they need to know about one contact, and a :class:`GuardPolicy`,
what the campaign asks. Each answers a :class:`Reason` to exclude the contact,
or ``None``. :func:`check_contact` runs them all and returns a :class:`Verdict`.

:func:`load_facts` is the only function here that touches the database. It
reads the contacts and their history in a few scoped queries, and never
writes. :func:`check_enrollment` and :func:`check_step` put the two together
for the two moments spec 11.9 names. :func:`excluded_summary` turns a set of
verdicts into the review screen's "212 in audience, 37 excluded: ..." line
(spec 11.8).

The channel's own health (a mailbox under its cap, a browser under its budget)
does not depend on the contact. :func:`check_channel` is its guard, over a
:class:`ChannelState` the caller fills in.

Safety
------
These guards are ``safety``: a guard that cannot decide excludes, with a
reason, and never includes. Concretely:

- A contact :func:`load_facts` could not find (another user's, or deleted
  since) gets :attr:`Reason.UNKNOWN_CONTACT`.
- A channel with no guard for its address gets :attr:`Reason.UNKNOWN_CHANNEL`,
  so a channel added later without a guard sends nothing.
- A :class:`ChannelState` field left unknown (``None``) excludes.

A verdict lists every reason that applies, not only the first, so the review
screen can show them all. :attr:`Verdict.reason` is the first in
:data:`REASON_ORDER`, and it is the one :func:`excluded_summary` counts, so
that the summary's parts add up to the number excluded.
"""

from __future__ import annotations

import enum
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, aliased, selectinload

from netkeeper.crm.contacts import sendable_email
from netkeeper.crm.interactions import OUTBOUND_KINDS
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    ContactEmail,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    Message,
    MessageDirection,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped

# --- what a guard can say ----------------------------------------------------------


class Reason(enum.StrEnum):
    """Why a contact is excluded. The order here is :data:`REASON_ORDER`."""

    CAMPAIGN_NOT_ACTIVE = "campaign_not_active"
    ENROLLMENT_NOT_ACTIVE = "enrollment_not_active"
    UNKNOWN_CONTACT = "unknown_contact"
    MERGED = "merged"
    ARCHIVED = "archived"
    NEEDS_REVIEW = "needs_review"
    DO_NOT_CONTACT = "do_not_contact"
    DISCONNECTED = "disconnected"
    UNKNOWN_CHANNEL = "unknown_channel"
    NO_EMAIL = "no_email"
    EMAIL_BOUNCED = "email_bounced"
    EMAIL_INVALID = "email_invalid"
    ADDRESS_BOUNCED_ELSEWHERE = "address_bounced_elsewhere"
    NO_LINKEDIN = "no_linkedin"
    DUPLICATE_ADDRESS = "duplicate_address"
    IN_ANOTHER_CAMPAIGN = "in_another_campaign"
    CONTACTED_RECENTLY = "contacted_recently"


REASON_ORDER: Final[tuple[Reason, ...]] = tuple(Reason)
"""Most fundamental first: a merged contact is reported as merged, not as having no email.
The two step-fire reasons (:func:`enrollment_state`) come before anything about the contact."""


class ChannelReason(enum.StrEnum):
    """Why nothing may go out on a channel right now (spec 11.9, the last bullet)."""

    MAILBOX_UNKNOWN = "mailbox_unknown"
    MAILBOX_UNHEALTHY = "mailbox_unhealthy"
    MAILBOX_AT_CAP = "mailbox_at_cap"
    BROWSER_UNKNOWN = "browser_unknown"
    BROWSER_UNHEALTHY = "browser_unhealthy"
    BROWSER_OUT_OF_BUDGET = "browser_out_of_budget"
    UNKNOWN_CHANNEL = "unknown_channel"


# --- the inputs ---------------------------------------------------------------------


UNSENDABLE_EMAIL_STATUSES: Final[frozenset[EmailStatus]] = frozenset(
    {EmailStatus.BOUNCED, EmailStatus.INVALID}
)
"""Address statuses a campaign never sends to (spec 11.9, #226). The export refuses bounces only."""

LIVE_ENROLLMENT_STATUSES: Final[frozenset[EnrollmentStatus]] = frozenset(
    {EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED}
)
"""An enrollment in one of these has steps still to send (spec 11.3)."""

RUNNING_CAMPAIGN_STATUSES: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.REVIEWING, CampaignStatus.ACTIVE, CampaignStatus.PAUSED}
)
"""A campaign in one of these is, or is about to be, sending. A draft is not yet."""


@dataclass(frozen=True, slots=True)
class ContactFacts:
    """What the guards know about one contact, read by :func:`load_facts`.

    ``email_statuses`` are the contact's addresses' statuses, primary first.
    ``other_campaigns`` are the running campaigns (:data:`RUNNING_CAMPAIGN_STATUSES`)
    the contact has a live enrollment in (:data:`LIVE_ENROLLMENT_STATUSES`), other
    than this one, whose enrollment came first. ``last_outbound_at`` is the newest
    time anyone contacted them, counting every outbound interaction and every
    sent campaign message, except the firing enrollment's own.

    The two address facts are about the contact's ``sendable_email`` (#238, Part A).
    ``address_bounced_elsewhere``: another contact of the user holds the same
    address as bounced or invalid. ``duplicate_address``: the address is already
    on another enrollment in this campaign (see :func:`load_facts` for which).
    Both are ``False`` for a contact with no sendable address.
    """

    contact_id: int
    merged: bool
    archived: bool
    needs_review: bool
    do_not_contact: bool
    disconnected: bool
    email_statuses: tuple[EmailStatus, ...]
    sendable_email: str | None
    has_linkedin: bool
    other_campaigns: frozenset[int]
    last_outbound_at: datetime | None
    address_bounced_elsewhere: bool
    duplicate_address: bool


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    """What the campaign asks of the guards.

    ``contacted_within_days`` is the campaign's ``contacted_within_days_guard``;
    0 turns the recency guard off. ``allow_other_campaigns`` is spec 11.9's
    "configurable to allow"; where that setting lives is not decided yet, so
    only callers that pass it get it, and every caller in this module passes
    the default, ``False``.
    """

    contacted_within_days: int
    allow_other_campaigns: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    """One contact's result: eligible when ``reasons`` is empty."""

    contact_id: int
    reasons: tuple[Reason, ...]

    @property
    def eligible(self) -> bool:
        return not self.reasons

    @property
    def reason(self) -> Reason | None:
        """The reason the summary counts: the first in :data:`REASON_ORDER`."""
        return self.reasons[0] if self.reasons else None


# --- the guards --------------------------------------------------------------------

Guard = Callable[[ContactFacts, TemplateChannel, GuardPolicy, datetime], Reason | None]


def not_merged(facts: ContactFacts, *_: object) -> Reason | None:
    return Reason.MERGED if facts.merged else None


def not_archived(facts: ContactFacts, *_: object) -> Reason | None:
    return Reason.ARCHIVED if facts.archived else None


def not_waiting_for_review(facts: ContactFacts, *_: object) -> Reason | None:
    """A contact read off a connections-page card is nobody to reach until confirmed (#184)."""
    return Reason.NEEDS_REVIEW if facts.needs_review else None


def not_do_not_contact(facts: ContactFacts, *_: object) -> Reason | None:
    return Reason.DO_NOT_CONTACT if facts.do_not_contact else None


def not_disconnected(facts: ContactFacts, *_: object) -> Reason | None:
    return Reason.DISCONNECTED if facts.disconnected else None


def _email_address(facts: ContactFacts) -> Reason | None:
    if facts.sendable_email is not None:
        return None
    if not facts.email_statuses:
        return Reason.NO_EMAIL
    if EmailStatus.BOUNCED in facts.email_statuses:
        return Reason.EMAIL_BOUNCED
    return Reason.EMAIL_INVALID


def _linkedin_address(facts: ContactFacts) -> Reason | None:
    return None if facts.has_linkedin else Reason.NO_LINKEDIN


_ADDRESS_GUARDS: Final[Mapping[str, Callable[[ContactFacts], Reason | None]]] = {
    TemplateChannel.EMAIL.value: _email_address,
    TemplateChannel.LINKEDIN.value: _linkedin_address,
}


def has_channel_address(facts: ContactFacts, channel: TemplateChannel, *_: object) -> Reason | None:
    """An address on the step's channel, and for email, one that has not bounced or is invalid.

    A bounce leaves the contact eligible for LinkedIn steps (spec 11.5): only
    the channel being sent on is checked.
    """
    guard = _ADDRESS_GUARDS.get(str(channel))
    return Reason.UNKNOWN_CHANNEL if guard is None else guard(facts)


def address_not_bounced_elsewhere(
    facts: ContactFacts, channel: TemplateChannel, *_: object
) -> Reason | None:
    """An email step's address has not bounced, nor been found invalid, on another contact.

    Address statuses live per contact, so a bounce on one row would otherwise leave
    the same address sendable on another (#238). Email only: a bounce leaves a
    contact eligible for LinkedIn steps (spec 11.5).
    """
    if channel is TemplateChannel.EMAIL and facts.address_bounced_elsewhere:
        return Reason.ADDRESS_BOUNCED_ELSEWHERE
    return None


def not_duplicate_address(facts: ContactFacts, *_: object) -> Reason | None:
    """The address is not already on another enrollment in this campaign (#238).

    Two unmerged contacts sharing an address are one person with two rows: without
    this, both are eligible and the address gets every step twice. Checked on
    every channel, because a shared address says it is the same person whichever
    channel the step uses.
    """
    return Reason.DUPLICATE_ADDRESS if facts.duplicate_address else None


def not_in_another_campaign(
    facts: ContactFacts, channel: TemplateChannel, policy: GuardPolicy, *_: object
) -> Reason | None:
    if policy.allow_other_campaigns or not facts.other_campaigns:
        return None
    return Reason.IN_ANOTHER_CAMPAIGN


def not_contacted_recently(
    facts: ContactFacts, channel: TemplateChannel, policy: GuardPolicy, now: datetime
) -> Reason | None:
    """No outbound contact within ``contacted_within_days``, other than this enrollment's own.

    Outbound means any :data:`~netkeeper.crm.interactions.OUTBOUND_KINDS`
    interaction (an email, a LinkedIn message, a call, a meeting) or a sent
    campaign message. A time in the future counts as recent.
    """
    days = policy.contacted_within_days
    if days <= 0 or facts.last_outbound_at is None:
        return None
    if facts.last_outbound_at > now - timedelta(days=days):
        return Reason.CONTACTED_RECENTLY
    return None


GUARDS: Final[tuple[Guard, ...]] = (
    not_merged,
    not_archived,
    not_waiting_for_review,
    not_do_not_contact,
    not_disconnected,
    has_channel_address,
    address_not_bounced_elsewhere,
    not_duplicate_address,
    not_in_another_campaign,
    not_contacted_recently,
)
"""Every per-contact guard, run in this order."""


def check_contact(
    facts: ContactFacts | None,
    contact_id: int,
    channel: TemplateChannel,
    policy: GuardPolicy,
    *,
    now: datetime,
) -> Verdict:
    """Run every guard on one contact. ``facts`` of ``None`` is a contact nobody could find."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if facts is None:
        return Verdict(contact_id, (Reason.UNKNOWN_CONTACT,))
    found = {guard(facts, channel, policy, now) for guard in GUARDS}
    return Verdict(contact_id, tuple(r for r in REASON_ORDER if r in found))


def enrollment_state(enrollment: EnrollmentStatus, campaign: CampaignStatus) -> tuple[Reason, ...]:
    """Why a step may not fire for an enrollment in this state; empty when it may.

    Only an ``active`` enrollment (spec 11.3: a pending one becomes active when
    its campaign is activated) of an ``active`` campaign is sent to. A paused,
    replied, completed, bounced, opted-out, or removed enrollment, and any
    enrollment of a campaign that is not active, is excluded with a reason.
    """
    reasons: list[Reason] = []
    if campaign is not CampaignStatus.ACTIVE:
        reasons.append(Reason.CAMPAIGN_NOT_ACTIVE)
    if enrollment is not EnrollmentStatus.ACTIVE:
        reasons.append(Reason.ENROLLMENT_NOT_ACTIVE)
    return tuple(reasons)


# --- the channel ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelState:
    """The sending side's health, for :func:`check_channel`. ``None`` is "not known".

    Email: ``mailbox_ok`` is the mailbox's status is ``ok`` (not
    ``reauth_required`` or ``disabled``, spec 11.5), and ``mailbox_sent_today``
    and ``mailbox_daily_cap`` are its recipients today and its cap. LinkedIn:
    ``browser_ok`` is a browser attached with a healthy session, and
    ``browser_budget_left`` the sends the day's budget still allows.
    """

    mailbox_ok: bool | None = None
    mailbox_sent_today: int | None = None
    mailbox_daily_cap: int | None = None
    browser_ok: bool | None = None
    browser_budget_left: int | None = None


def _mailbox(state: ChannelState) -> tuple[ChannelReason, ...]:
    if state.mailbox_ok is None or state.mailbox_sent_today is None:
        return (ChannelReason.MAILBOX_UNKNOWN,)
    if state.mailbox_daily_cap is None:
        return (ChannelReason.MAILBOX_UNKNOWN,)
    reasons: list[ChannelReason] = []
    if not state.mailbox_ok:
        reasons.append(ChannelReason.MAILBOX_UNHEALTHY)
    if state.mailbox_sent_today >= state.mailbox_daily_cap:
        reasons.append(ChannelReason.MAILBOX_AT_CAP)
    return tuple(reasons)


def _browser(state: ChannelState) -> tuple[ChannelReason, ...]:
    if state.browser_ok is None or state.browser_budget_left is None:
        return (ChannelReason.BROWSER_UNKNOWN,)
    reasons: list[ChannelReason] = []
    if not state.browser_ok:
        reasons.append(ChannelReason.BROWSER_UNHEALTHY)
    if state.browser_budget_left <= 0:
        reasons.append(ChannelReason.BROWSER_OUT_OF_BUDGET)
    return tuple(reasons)


_CHANNEL_GUARDS: Final[Mapping[str, Callable[[ChannelState], tuple[ChannelReason, ...]]]] = {
    TemplateChannel.EMAIL.value: _mailbox,
    TemplateChannel.LINKEDIN.value: _browser,
}


def check_channel(channel: TemplateChannel, state: ChannelState) -> tuple[ChannelReason, ...]:
    """Why nothing may be sent on ``channel`` now; empty when it may.

    Mailbox healthy and under cap for email, browser healthy and under budget
    for LinkedIn (spec 11.9). Anything not known excludes.
    """
    guard = _CHANNEL_GUARDS.get(str(channel))
    return (ChannelReason.UNKNOWN_CHANNEL,) if guard is None else guard(state)


# --- the summary --------------------------------------------------------------------


def reason_label(reason: Reason, *, contacted_within_days: int) -> str:
    """How the review screen names a reason (spec 11.8)."""
    if reason is Reason.CONTACTED_RECENTLY:
        unit = "day" if contacted_within_days == 1 else "days"
        return f"contacted in the last {contacted_within_days} {unit}"
    return _LABELS[reason]


_LABELS: Final[Mapping[Reason, str]] = {
    Reason.CAMPAIGN_NOT_ACTIVE: "campaign not active",
    Reason.ENROLLMENT_NOT_ACTIVE: "enrollment not active",
    Reason.UNKNOWN_CONTACT: "not found",
    Reason.MERGED: "merged into another contact",
    Reason.ARCHIVED: "archived",
    Reason.NEEDS_REVIEW: "waiting for review",
    Reason.DO_NOT_CONTACT: "do-not-contact",
    Reason.DISCONNECTED: "disconnected",
    Reason.UNKNOWN_CHANNEL: "no guard for the channel",
    Reason.NO_EMAIL: "no email",
    Reason.EMAIL_BOUNCED: "bounced email",
    Reason.EMAIL_INVALID: "invalid email",
    Reason.ADDRESS_BOUNCED_ELSEWHERE: "address bounced on another contact",
    Reason.NO_LINKEDIN: "no LinkedIn profile",
    Reason.DUPLICATE_ADDRESS: "address already in this campaign",
    Reason.IN_ANOTHER_CAMPAIGN: "in another campaign",
}


def excluded_summary(verdicts: Iterable[Verdict], *, contacted_within_days: int) -> str:
    """The review screen's line: ``"212 in audience, 37 excluded: 30 no email, 4 ..."``.

    Generated from the verdicts, never written by hand (spec 11.8). Each excluded
    contact is counted once, under :attr:`Verdict.reason`, so the parts add up to
    the number excluded. Parts go largest first, and in :data:`REASON_ORDER` on a
    tie.
    """
    counts: Counter[Reason] = Counter()
    audience = 0
    for verdict in verdicts:
        audience += 1
        if verdict.reason is not None:
            counts[verdict.reason] += 1
    excluded = sum(counts.values())
    if not excluded:
        return f"{audience} in audience, none excluded"
    ordered = sorted(counts, key=lambda r: (-counts[r], REASON_ORDER.index(r)))
    parts = ", ".join(
        f"{counts[r]} {reason_label(r, contacted_within_days=contacted_within_days)}"
        for r in ordered
    )
    return f"{audience} in audience, {excluded} excluded: {parts}"


# --- reading the facts --------------------------------------------------------------


def load_facts(
    session: Session,
    user: User,
    contact_ids: Collection[int],
    *,
    campaign_id: int | None,
    enrollment_id: int | None = None,
) -> dict[int, ContactFacts]:
    """The facts for each of ``contact_ids`` that is ``user``'s, keyed by contact id.

    A contact missing from the result is one nobody could find; :func:`check_contact`
    excludes it. ``campaign_id`` is the campaign asking: its own enrollments are not
    "another campaign" (``None`` for a campaign not saved yet). ``enrollment_id`` is
    the enrollment a step is firing for (``None`` at enrollment time). Two things
    hang on it:

    - Recent contact ignores that enrollment's own messages and nothing else: not
      the rest of this campaign's. After a merge, the survivor holds the merged-away
      contact's interactions, and a step 1 that went to the other row must still
      count against the survivor's own step 1 (#235 review). At enrollment time
      nothing is ignored.
    - Another campaign's enrollment counts only if it came first, so two campaigns
      that enrolled the same person never exclude each other both at once.
    - A duplicate address, the same way: at a step fire, only an enrollment in this
      campaign older than ``enrollment_id`` counts. At enrollment time every
      enrollment already in the campaign counts, and so does a contact earlier in
      ``contact_ids`` (by id) with the same sendable address, so of a batch sharing
      one address only the first gets in.

    An enrollment of any status counts as holding its address, not only a live one:
    a completed or removed enrollment may have been sent to, and freeing the
    address when it ends would send the sequence to it a second time. An
    enrollment of a contact since merged away does not, because its person is
    the survivor. Addresses are compared as stored: ``ContactEmail`` normalizes
    them on the way in.

    The contacts and their addresses are read fresh, never taken from the
    session's identity map: sessions here keep their objects across commits
    (``expire_on_commit=False``), and a do-not-contact flag or a bounce another
    session committed must not be missed (#235 review). Reads only.
    """
    ids = sorted(set(contact_ids))
    if not ids:
        return {}
    contacts = session.scalars(
        scoped(user, Contact)
        .where(Contact.id.in_(ids))
        .options(selectinload(Contact.emails))
        .execution_options(populate_existing=True)
    ).all()
    others = _other_campaigns(session, user, ids, campaign_id, enrollment_id)
    last_out = _last_outbound(session, user, ids, enrollment_id)
    sendable: dict[int, str] = {}
    for contact in contacts:
        email = sendable_email(contact, refuse=UNSENDABLE_EMAIL_STATUSES)
        if email is not None:
            sendable[contact.id] = email.email
    bounced = _bounced_elsewhere(session, user, sendable)
    duplicates = _duplicate_addresses(session, user, sendable, campaign_id, enrollment_id)
    facts: dict[int, ContactFacts] = {}
    for contact in contacts:
        facts[contact.id] = ContactFacts(
            contact_id=contact.id,
            merged=contact.merged_into_id is not None,
            archived=contact.archived_at is not None,
            needs_review=contact.needs_review_at is not None,
            do_not_contact=contact.do_not_contact,
            disconnected=contact.li_disconnected_at is not None,
            email_statuses=tuple(e.status for e in contact.emails),
            sendable_email=sendable.get(contact.id),
            has_linkedin=bool(contact.li_urn or contact.li_public_id),
            other_campaigns=frozenset(others.get(contact.id, ())),
            last_outbound_at=last_out.get(contact.id),
            address_bounced_elsewhere=contact.id in bounced,
            duplicate_address=contact.id in duplicates,
        )
    return facts


def _bounced_elsewhere(session: Session, user: User, sendable: Mapping[int, str]) -> set[int]:
    """The contacts whose sendable address another contact holds as bounced or invalid."""
    if not sendable:
        return set()
    rows = session.execute(
        scoped(user, ContactEmail)
        .with_only_columns(ContactEmail.email, ContactEmail.contact_id)
        .where(
            ContactEmail.email.in_(sorted(set(sendable.values()))),
            ContactEmail.status.in_(UNSENDABLE_EMAIL_STATUSES),
        )
    ).tuples()
    holders: dict[str, set[int]] = {}
    for address, holder in rows:
        holders.setdefault(address, set()).add(holder)
    return {
        contact_id
        for contact_id, address in sendable.items()
        if holders.get(address, set()) - {contact_id}
    }


def _duplicate_addresses(
    session: Session,
    user: User,
    sendable: Mapping[int, str],
    campaign_id: int | None,
    enrollment_id: int | None,
) -> set[int]:
    """The contacts whose sendable address is already on another enrollment: see
    :func:`load_facts`."""
    found: set[int] = set()
    if enrollment_id is None:
        first: dict[str, int] = {}
        for contact_id in sorted(sendable):
            if first.setdefault(sendable[contact_id], contact_id) != contact_id:
                found.add(contact_id)
    if not sendable or campaign_id is None:
        return found
    statement = (
        scoped(user, Enrollment)
        .with_only_columns(ContactEmail.email, Enrollment.contact_id)
        .join(Contact, Contact.id == Enrollment.contact_id)
        .join(ContactEmail, ContactEmail.contact_id == Contact.id)
        .where(
            Contact.user_id == user.id,
            ContactEmail.user_id == user.id,
            Enrollment.campaign_id == campaign_id,
            Contact.merged_into_id.is_(None),
            ContactEmail.email.in_(sorted(set(sendable.values()))),
        )
    )
    if enrollment_id is not None:
        statement = statement.where(Enrollment.id < enrollment_id)
    holders: dict[str, set[int]] = {}
    for address, holder in session.execute(statement).tuples():
        holders.setdefault(address, set()).add(holder)
    found.update(
        contact_id
        for contact_id, address in sendable.items()
        if holders.get(address, set()) - {contact_id}
    )
    return found


def _other_campaigns(
    session: Session,
    user: User,
    ids: Sequence[int],
    campaign_id: int | None,
    enrollment_id: int | None,
) -> dict[int, set[int]]:
    statement = (
        scoped(user, Enrollment)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(
            Campaign.user_id == user.id,
            Enrollment.contact_id.in_(ids),
            Enrollment.status.in_(LIVE_ENROLLMENT_STATUSES),
            Campaign.status.in_(RUNNING_CAMPAIGN_STATUSES),
        )
    )
    if campaign_id is not None:
        statement = statement.where(Enrollment.campaign_id != campaign_id)
    if enrollment_id is not None:
        statement = statement.where(Enrollment.id < enrollment_id)
    found: dict[int, set[int]] = {}
    for enrollment in session.scalars(statement):
        found.setdefault(enrollment.contact_id, set()).add(enrollment.campaign_id)
    return found


def _last_outbound(
    session: Session, user: User, ids: Sequence[int], enrollment_id: int | None
) -> dict[int, datetime]:
    """Per contact, the newest outbound interaction or sent message, not ``enrollment_id``'s."""
    interactions = (
        scoped(user, Interaction)
        .with_only_columns(Interaction.contact_id, func.max(Interaction.at))
        .where(Interaction.contact_id.in_(ids), Interaction.kind.in_(OUTBOUND_KINDS))
        .group_by(Interaction.contact_id)
    )
    if enrollment_id is not None:
        # An interaction recording one of this enrollment's own messages does not count.
        message = aliased(Message)
        interactions = interactions.outerjoin(
            message, and_(message.id == Interaction.message_id, message.user_id == user.id)
        ).where(or_(message.id.is_(None), message.enrollment_id != enrollment_id))
    newest: dict[int, datetime] = {}
    for contact_id, at in session.execute(interactions).tuples():
        newest[contact_id] = at
    messages = (
        scoped(user, Message)
        .with_only_columns(Message.contact_id, func.max(Message.sent_at))
        .where(
            Message.contact_id.in_(ids),
            Message.direction == MessageDirection.OUT,
            Message.sent_at.is_not(None),
        )
        .group_by(Message.contact_id)
    )
    if enrollment_id is not None:
        messages = messages.where(Message.enrollment_id != enrollment_id)
    for contact_id, sent_at in session.execute(messages).tuples():
        if sent_at is not None and (contact_id not in newest or sent_at > newest[contact_id]):
            newest[contact_id] = sent_at
    return newest


# --- the two moments ----------------------------------------------------------------


def check_enrollment(
    session: Session,
    user: User,
    campaign: Campaign,
    contact_ids: Collection[int],
    *,
    now: datetime,
) -> list[Verdict]:
    """The verdict for enrolling each of ``contact_ids`` in ``campaign``, in id order.

    Checked against the first step's channel, the one the enrollment will send
    first; each later step is checked again when it fires (:func:`check_step`).
    A campaign with no steps has nothing to check a channel against, so every
    contact gets :attr:`Reason.UNKNOWN_CHANNEL`; a campaign deleted since gets
    :attr:`Reason.CAMPAIGN_NOT_ACTIVE`.

    The recency window and the first step are read fresh, like the contacts:
    the caller's ``campaign`` may be from before a commit another session made
    (``expire_on_commit=False``; #242). Reads only.
    """
    if campaign.user_id != user.id:
        raise ValueError("a campaign can only enroll its own user's contacts")
    ids = sorted(set(contact_ids))
    window = session.scalar(
        scoped(user, Campaign)
        .with_only_columns(Campaign.contacted_within_days_guard)
        .where(Campaign.id == campaign.id)
    )
    if window is None:  # deleted since: nobody to enroll in it
        return [Verdict(i, (Reason.CAMPAIGN_NOT_ACTIVE,)) for i in ids]
    first = session.scalar(
        scoped(user, CampaignStep)
        .with_only_columns(CampaignStep.channel)
        .where(CampaignStep.campaign_id == campaign.id)
        .order_by(CampaignStep.position)
        .limit(1)
    )
    if first is None:
        return [Verdict(i, (Reason.UNKNOWN_CHANNEL,)) for i in ids]
    facts = load_facts(session, user, ids, campaign_id=campaign.id)
    policy = GuardPolicy(contacted_within_days=window)
    return [check_contact(facts.get(i), i, first, policy, now=now) for i in ids]


def check_step(
    session: Session,
    user: User,
    enrollment: Enrollment,
    step: CampaignStep,
    *,
    now: datetime,
) -> Verdict:
    """The verdict for firing ``step`` for ``enrollment`` now. Reads only.

    This enrollment's own earlier steps never count as recent contact: spec
    11.9's "unless the message is the next step of this same campaign".
    Anything else does, a step this campaign sent to a contact since merged
    into this one included.

    A step fires only for an ``active`` enrollment of an ``active`` campaign:
    anything else is :attr:`Reason.ENROLLMENT_NOT_ACTIVE` or
    :attr:`Reason.CAMPAIGN_NOT_ACTIVE` (:func:`enrollment_state`), so a
    finished, paused, or removed enrollment is never sent to by a caller that
    forgot to filter it out.
    """
    if enrollment.user_id != user.id or step.user_id != user.id:
        raise ValueError("an enrollment and its step must be the user's")
    if step.campaign_id != enrollment.campaign_id:
        raise ValueError("the step is not one of the enrollment's campaign's")
    facts = load_facts(
        session,
        user,
        [enrollment.contact_id],
        campaign_id=enrollment.campaign_id,
        enrollment_id=enrollment.id,
    )
    # Read fresh, like the contact: the caller's objects may be from before a commit
    # another session made (expire_on_commit=False).
    state = session.execute(
        scoped(user, Enrollment)
        .with_only_columns(Enrollment.status, Campaign.status, Campaign.contacted_within_days_guard)
        .join(Campaign, Campaign.id == Enrollment.campaign_id)
        .where(Enrollment.id == enrollment.id, Campaign.user_id == user.id)
    ).one_or_none()
    if state is None:  # deleted since: nothing to send to
        return Verdict(enrollment.contact_id, (Reason.ENROLLMENT_NOT_ACTIVE,))
    enrollment_status, campaign_status, window = state
    verdict = check_contact(
        facts.get(enrollment.contact_id),
        enrollment.contact_id,
        step.channel,
        GuardPolicy(contacted_within_days=window),
        now=now,
    )
    found = {*enrollment_state(enrollment_status, campaign_status), *verdict.reasons}
    return Verdict(verdict.contact_id, tuple(r for r in REASON_ORDER if r in found))
