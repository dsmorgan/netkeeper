"""Scan Gmail for what the old tool's recipients sent back (#65, Part B).

``netkeeper history scan-gmail`` runs this after ``netkeeper history import``. For
each imported recipient it makes two searches in the campaign's window, from the
day before the campaign started until :data:`WINDOW_AFTER_LAST_BATCH` after its
last batch:

- ``from:<address>``: what the person sent. Each message found is read (metadata
  only) and classified with the reply poll's own tests
  (:mod:`netkeeper.services.campaign_replies`): an automatic answer
  (:func:`~netkeeper.services.campaign_replies.is_auto_reply`), an unsubscribe
  request (:func:`~netkeeper.services.campaign_replies.asks_to_unsubscribe`), or
  any other reply.
- ``from:mailer-daemon <address>``: delivery notices that name the address. One
  that is a hard bounce
  (:func:`~netkeeper.services.campaign_replies.is_hard_bounce`) and, when it names
  its failed recipients, names this one, is a bounce.

**Read-only.** The scan calls only :meth:`Gmail.search` and :meth:`Gmail.get_message`.
It never sends, drafts, labels, or deletes anything (ADR 0003), and it holds no
database session while Gmail answers.

**What applying does** (:func:`apply_scan`), in a writer session:

- a bounce puts the address on the do-not-send list as ``bounced`` and sets
  ``bounced_at``;
- an unsubscribe sets the contact's ``do_not_contact`` (reason
  :data:`UNSUBSCRIBE_REASON`) and puts every address the contact holds, and the
  recipient's own, on the do-not-send list as ``opted_out``, as
  :func:`~netkeeper.services.campaign_replies.record_reply` does for a live
  unsubscribe; it is also recorded as a reply;
- any other reply records an ``email_in`` interaction marked as imported history,
  sets ``replied_at``, and sets the contact's ``needs_review_at``, so the campaign
  guards skip the contact until a person reads the reply and confirms the contact;
- an automatic answer is recorded on the recipient row and does nothing else.

A recipient row keeps the strongest kind found
(:data:`~netkeeper.models.history.REPLY_KIND_RANK`) and is marked scanned, so the
next scan skips it unless it is asked to rescan. A dry run is the same apply,
rolled back.

**Rate limits.** The Gmail client never retries by itself. The scan stops at the
first failure that is not a missing message (a rate limit, a quota, a dead token,
the network), applies what it finished, and reports the failure; the rows it did
not reach stay unscanned, and the next run picks them up.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from email.utils import getaddresses
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.campaigns.gmail import Gmail, GmailError, GmailNotFound, Message, MessageRef
from netkeeper.crm import do_not_send
from netkeeper.crm.history import HISTORY_SUMMARY
from netkeeper.crm.identity import resolve_survivor
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import is_writer
from netkeeper.models import (
    MESSAGE_SNIPPET_MAX_LENGTH,
    REPLY_KIND_RANK,
    Contact,
    ContactSource,
    DoNotSendReason,
    HistoryCampaign,
    HistoryRecipient,
    HistoryReplyKind,
    InteractionKind,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services.campaign_replies import (
    asks_to_unsubscribe,
    is_auto_reply,
    is_daemon,
    is_hard_bounce,
    sender_of,
)

log = logging.getLogger(__name__)

WINDOW_AFTER_LAST_BATCH: Final = timedelta(days=120)
"""How long after a campaign's last batch a message from a recipient still counts (#65)."""

WINDOW_BEFORE_START: Final = timedelta(days=1)
"""How far before the start date the window opens: the workbook's dates carry no time
zone, so the window starts a day early rather than miss a reply on the first day."""

SEARCH_MAX: Final = 20
"""At most this many messages one search reads."""

UNSUBSCRIBE_REASON: Final = "unsubscribe (old campaign)"
"""The contact's ``do_not_contact_reason`` when a reply to an old campaign asks to unsubscribe."""

DAEMON_QUERY: Final = "from:mailer-daemon"
"""The sender of Gmail's delivery-status notices (spec 11.5)."""

READ_ONLY_METHODS: Final = frozenset({"messages.list", "messages.get"})
"""The only Gmail methods the scan calls. Tests hold it to this through the fake's call log."""

_SEARCHABLE: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+")
_SAMPLE: Final = 20

HUMAN: Final = frozenset({HistoryReplyKind.REPLY, HistoryReplyKind.UNSUBSCRIBE})


# --- what to scan -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """One recipient row to scan, and its campaign's window."""

    recipient_id: int
    campaign_id: int
    email: str
    after: datetime
    before: datetime


def window(started_on: date, last_batch_on: date | None) -> tuple[datetime, datetime]:
    """The campaign's window, ``(after, before)``, both aware UTC."""
    start = datetime.combine(started_on, time(0, 0), tzinfo=UTC) - WINDOW_BEFORE_START
    last = datetime.combine(last_batch_on or started_on, time(0, 0), tzinfo=UTC)
    return start, last + timedelta(days=1) + WINDOW_AFTER_LAST_BATCH


def scan_targets(
    session: Session, user: User, *, rescan: bool = False, limit: int | None = None
) -> list[Target]:
    """The recipient rows to scan, by campaign then address. A scanned row is left out
    unless ``rescan``. Reads only."""
    statement = (
        scoped(user, HistoryRecipient)
        .with_only_columns(
            HistoryRecipient.id,
            HistoryRecipient.history_campaign_id,
            HistoryRecipient.email,
            HistoryCampaign.started_on,
            HistoryCampaign.last_batch_on,
        )
        .join(HistoryCampaign, HistoryCampaign.id == HistoryRecipient.history_campaign_id)
        .where(HistoryCampaign.user_id == user.id)
        .order_by(HistoryCampaign.started_on, HistoryCampaign.id, HistoryRecipient.email)
    )
    if not rescan:
        statement = statement.where(HistoryRecipient.scanned_at.is_(None))
    if limit is not None:
        statement = statement.limit(limit)
    targets: list[Target] = []
    for recipient_id, campaign_id, email, started_on, last_batch_on in session.execute(statement):
        after, before = window(started_on, last_batch_on)
        targets.append(Target(recipient_id, campaign_id, email, after, before))
    return targets


# --- reading Gmail ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One message that says something about a recipient."""

    kind: HistoryReplyKind
    gmail_id: str
    at: datetime
    subject: str | None
    snippet: str


@dataclass(frozen=True, slots=True)
class Scanned:
    """What the scan found for one recipient (maybe nothing)."""

    target: Target
    findings: tuple[Finding, ...]


@dataclass
class ScanRead:
    """What one run read: the recipients it finished, and why it stopped early, if it did."""

    scanned: list[Scanned] = field(default_factory=list)
    stopped: str | None = None
    remaining: int = 0


def read_gmail(gmail: Gmail, targets: Sequence[Target]) -> ScanRead:
    """Search and read Gmail for each target, in order (the module docstring). Blocking:
    call it with no database session open."""
    out = ScanRead()
    cache: dict[str, Message | None] = {}
    for index, target in enumerate(targets):
        purpose = f"history scan of old campaign {target.campaign_id}"
        try:
            findings = _scan_one(gmail, target, purpose, cache)
        except GmailError as exc:
            out.stopped = exc.code
            out.remaining = len(targets) - index
            log.warning(
                "history scan stopped after %d of %d recipients (%s)",
                index,
                len(targets),
                exc.code,
            )
            return out
        out.scanned.append(Scanned(target, findings))
    return out


def _scan_one(
    gmail: Gmail, target: Target, purpose: str, cache: dict[str, Message | None]
) -> tuple[Finding, ...]:
    if not _SEARCHABLE.fullmatch(target.email):
        log.info("a history recipient's address cannot be searched for; skipped")
        return ()
    span = f"after:{int(target.after.timestamp())} before:{int(target.before.timestamp())}"
    findings: list[Finding] = []
    for message in _read(
        gmail,
        gmail.search(f"from:{target.email} {span}", max_results=SEARCH_MAX, purpose=purpose),
        purpose,
        cache,
    ):
        kind = classify_from(message, target.email)
        if kind is not None:
            findings.append(_finding(kind, message))
    notices = gmail.search(
        f"{DAEMON_QUERY} {target.email} {span}", max_results=SEARCH_MAX, purpose=purpose
    )
    for message in _read(gmail, notices, purpose, cache):
        if names_failed_recipient(message, target.email):
            findings.append(_finding(HistoryReplyKind.BOUNCE, message))
    return tuple(findings)


def _read(
    gmail: Gmail, refs: Iterable[MessageRef], purpose: str, cache: dict[str, Message | None]
) -> list[Message]:
    found: list[Message] = []
    for ref in refs:
        gmail_id = ref.id
        if gmail_id not in cache:
            try:
                cache[gmail_id] = gmail.get_message(gmail_id, purpose=purpose)
            except GmailNotFound:  # deleted since the search
                cache[gmail_id] = None
        message = cache[gmail_id]
        if message is not None:
            found.append(message)
    return found


_OWN: Final = frozenset({"SENT", "DRAFT", "SCHEDULED"})


def classify_from(message: Message, address: str) -> HistoryReplyKind | None:
    """What a message from ``address`` is: ``None`` for one that is not from them (a
    ``from:`` search matches words, not whole addresses), the mailbox's own, or a mail
    system's."""
    if message.label_ids & _OWN or sender_of(message) != address or is_daemon(message):
        return None
    if is_auto_reply(message):
        return HistoryReplyKind.AUTO
    if asks_to_unsubscribe(message):
        return HistoryReplyKind.UNSUBSCRIBE
    return HistoryReplyKind.REPLY


def names_failed_recipient(message: Message, address: str) -> bool:
    """Whether a notice found by a search for ``address`` is a hard bounce of it. A notice
    that lists its failed recipients (``X-Failed-Recipients``) must list this one; one that
    lists none counts, since the search found the address in it."""
    if not is_hard_bounce(message):
        return False
    failed = message.header("X-Failed-Recipients")
    if failed is None or not failed.strip():
        return True
    return address in {a.lower() for _, a in getaddresses([failed])}


def _finding(kind: HistoryReplyKind, message: Message) -> Finding:
    return Finding(
        kind=kind,
        gmail_id=message.id,
        at=message.internal_date,
        subject=message.header("Subject"),
        snippet=message.snippet,
    )


# --- applying -----------------------------------------------------------------------------


@dataclass
class KindCount:
    reply: int = 0
    unsubscribe: int = 0
    auto: int = 0
    bounce: int = 0

    def add(self, kind: HistoryReplyKind) -> None:
        setattr(self, kind.value, getattr(self, kind.value) + 1)


@dataclass
class ScanReport:
    """What a scan found, per campaign and kind, and a sample of addresses per kind."""

    scanned: int = 0
    nothing: int = 0
    by_campaign: dict[int, KindCount] = field(default_factory=dict)
    campaign_names: dict[int, str] = field(default_factory=dict)
    samples: dict[HistoryReplyKind, list[str]] = field(default_factory=dict)
    totals: KindCount = field(default_factory=KindCount)
    flagged: int = 0
    opted_out: int = 0
    no_contact: list[str] = field(default_factory=list)
    """Addresses that wrote back but match no single contact: nothing to flag."""
    new_interactions: int = 0
    stopped: str | None = None
    remaining: int = 0


def apply_scan(session: Session, user: User, read: ScanRead, *, now: datetime) -> ScanReport:
    """Record what ``read`` found (the module docstring) and mark each recipient scanned.
    Needs a writer session; commits nothing."""
    if not is_writer(session):
        raise RuntimeError("applying the history scan needs a writer session")
    report = ScanReport(stopped=read.stopped, remaining=read.remaining)
    names = {
        c.id: c.name
        for c in session.scalars(
            scoped(user, HistoryCampaign).where(
                HistoryCampaign.id.in_({s.target.campaign_id for s in read.scanned})
            )
        )
    }
    report.campaign_names = names
    for scanned in read.scanned:
        row = get_scoped(session, user, HistoryRecipient, scanned.target.recipient_id)
        if row is None:  # deleted since the read
            continue
        report.scanned += 1
        row.scanned_at = now
        if not scanned.findings:
            report.nothing += 1
            continue
        _apply_one(
            session, user, row, scanned.findings, names.get(row.history_campaign_id), report, now
        )
    session.flush()
    log.info(
        "history scan for user %d: %d scanned, %d flagged for review, %d opted out",
        user.id,
        report.scanned,
        report.flagged,
        report.opted_out,
    )
    return report


def _apply_one(
    session: Session,
    user: User,
    row: HistoryRecipient,
    findings: Sequence[Finding],
    campaign_name: str | None,
    report: ScanReport,
    now: datetime,
) -> None:
    kinds = {f.kind for f in findings}
    counts = report.by_campaign.setdefault(row.history_campaign_id, KindCount())
    for kind in sorted(kinds, key=lambda k: -REPLY_KIND_RANK[k]):
        counts.add(kind)
        report.totals.add(kind)
        sample = report.samples.setdefault(kind, [])
        if len(sample) < _SAMPLE and row.email not in sample:
            sample.append(row.email)
    strongest = max(kinds, key=lambda k: REPLY_KIND_RANK[k])
    if row.reply_kind is None or REPLY_KIND_RANK[strongest] > REPLY_KIND_RANK[row.reply_kind]:
        row.reply_kind = strongest
    contact = _contact(session, user, row)

    bounces = sorted(f.at for f in findings if f.kind is HistoryReplyKind.BOUNCE)
    if bounces:
        row.bounced_at = min(bounces[0], row.bounced_at or bounces[0])
        do_not_send.add(
            session,
            user,
            row.email,
            DoNotSendReason.BOUNCED,
            contact_id=None if contact is None else contact.id,
        )

    human = sorted((f for f in findings if f.kind in HUMAN), key=lambda f: (f.at, f.gmail_id))
    if not human:
        return
    unsubscribe = next((f for f in human if f.kind is HistoryReplyKind.UNSUBSCRIBE), None)
    recorded = unsubscribe or human[0]
    row.replied_at = min(human[0].at, row.replied_at or human[0].at)
    if unsubscribe is not None:
        _opt_out(session, user, row, contact)
        report.opted_out += 1
    if contact is None:
        if row.email not in report.no_contact:
            report.no_contact.append(row.email)
        return
    # Flag once per message: a rescan, or the same message in a second campaign's
    # window, does not flag again a contact a person has already confirmed.
    new_reply = row.reply_gmail_id is None and _recorded_on(session, user, recorded) is None
    if new_reply and unsubscribe is None and contact.needs_review_at is None:
        contact.needs_review_at = now
        report.flagged += 1
    if row.email_in_interaction_id is None:
        row.email_in_interaction_id = _recorded_on(session, user, recorded) or _interaction_for(
            session, user, contact, recorded, campaign_name, len(human), report
        )
    row.reply_gmail_id = row.reply_gmail_id or recorded.gmail_id


def _recorded_on(session: Session, user: User, finding: Finding) -> int | None:
    """The ``email_in`` interaction another recipient row already wrote for this message."""
    return session.scalar(
        scoped(user, HistoryRecipient)
        .with_only_columns(HistoryRecipient.email_in_interaction_id)
        .where(
            HistoryRecipient.reply_gmail_id == finding.gmail_id,
            HistoryRecipient.email_in_interaction_id.is_not(None),
        )
        .limit(1)
    )


def _contact(session: Session, user: User, row: HistoryRecipient) -> Contact | None:
    if row.contact_id is None:
        return None
    try:
        return resolve_survivor(session, user, row.contact_id)
    except ValueError:
        return None


def _opt_out(session: Session, user: User, row: HistoryRecipient, contact: Contact | None) -> None:
    """``record_reply``'s unsubscribe, for an old campaign: the contact is not to be
    contacted, and every address of theirs, and this one, is off limits."""
    addresses = {row.email}
    if contact is not None:
        if not contact.do_not_contact:
            contact.do_not_contact = True
            contact.do_not_contact_reason = UNSUBSCRIBE_REASON
        addresses |= {e.email for e in contact.emails}
    for address in sorted(addresses):
        do_not_send.add(
            session,
            user,
            address,
            DoNotSendReason.OPTED_OUT,
            contact_id=None if contact is None else contact.id,
        )


def _interaction_for(
    session: Session,
    user: User,
    contact: Contact,
    finding: Finding,
    campaign_name: str | None,
    replies: int,
    report: ScanReport,
) -> int:
    """A new ``email_in`` interaction for ``finding``, marked as imported history."""
    what = "asked to unsubscribe" if finding.kind is HistoryReplyKind.UNSUBSCRIBE else "wrote"
    campaign = f" after the old mailing tool's campaign {campaign_name!r}" if campaign_name else ""
    more = f" ({replies - 1} more message(s) in the window)" if replies > 1 else ""
    detail = " · ".join(
        part for part in (finding.subject, finding.snippet[:MESSAGE_SNIPPET_MAX_LENGTH]) if part
    )
    summary = f"{HISTORY_SUMMARY}: {what}{campaign}{more}" + (f": {detail}" if detail else "")
    interaction = add_interaction(
        session,
        user,
        contact.id,
        InteractionKind.EMAIL_IN,
        finding.at,
        summary=summary,
        source=ContactSource.SYNC,
    )
    report.new_interactions += 1
    return interaction.id
