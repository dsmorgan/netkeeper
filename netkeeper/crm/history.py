"""Import the old mailing tool's history: who each campaign reached, and when (#65, Part A).

``netkeeper history import <workbook>`` reads the workbook
(:mod:`netkeeper.crm.history_workbook`) and calls :func:`import_workbook`. For each
campaign tab it upserts a ``history_campaigns`` row, and for each person a list
names, a ``history_recipients`` row. Then, for each recipient whose address matches
a contact:

- one ``email_out`` interaction, dated at the campaign's start, summarized as
  imported history (:data:`HISTORY_SUMMARY`). It moves the contact's
  ``last_contacted_at``, so the campaign guard ``not_contacted_recently`` counts it.
- for a person on the workbook's own bounce list, the address goes on the
  do-not-send list as ``bounced``.

Matching is by email address through :func:`netkeeper.crm.identity.resolve`. An
address no contact holds is reported as unmatched, and ``create_missing`` creates a
contact for it (name and address only, source ``csv``). An address several contacts
hold is reported as ambiguous and matched to none: merge them, then import again.

**Idempotent.** A campaign is keyed by name and start date, a recipient by campaign
and address, and the interaction a recipient row wrote is remembered on it, so a
re-run adds no rows and no interactions. A re-run fills in what an earlier one could
not, such as a contact created or merged since.

**Not complete.** The workbook names only the people who opened, clicked, or
bounced. Its ``Recipients`` count is larger, and nobody can import the rest from it:
each campaign's report says how many are missing (``unlisted``).

Every function here needs a writer session and commits nothing. A dry run is
:func:`import_workbook` followed by a rollback.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Final, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from netkeeper.crm import do_not_send
from netkeeper.crm.history_workbook import Recipient, SkippedTab, Workbook, WorkbookCampaign
from netkeeper.crm.identity import (
    Candidate,
    IncomingContact,
    IncomingEmail,
    Matched,
    New,
    apply,
    resolve,
    resolve_survivor,
)
from netkeeper.crm.interactions import add_interaction
from netkeeper.db import is_writer
from netkeeper.models import (
    ContactEmail,
    ContactSource,
    DoNotSendReason,
    HistoryCampaign,
    HistoryRecipient,
    HistoryReplyKind,
    InteractionKind,
    User,
)
from netkeeper.models.history import HISTORY_CAMPAIGN_NAME_MAX_LENGTH
from netkeeper.scoping import scoped

log = logging.getLogger(__name__)

HISTORY_SUMMARY: Final = "Imported history"
"""What every timeline entry the import or the Gmail scan writes starts with, so the
contact page labels it as imported history (#65, Part C)."""

HISTORY_SOURCE: Final = ContactSource.CSV
"""The provenance of what the import writes: a file a person brought in, as a CSV is."""

Match = Literal["matched", "ambiguous", "unmatched", "created"]


@dataclass
class CampaignReport:
    """What the import did, or would do, for one campaign tab."""

    index: int
    name: str
    started_on: date
    last_batch_on: date | None
    recipients_count: int | None
    opens_count: int | None
    bounces_count: int | None
    listed: int = 0
    opened: int = 0
    clicked: int = 0
    bounce_listed: int = 0
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    created: int = 0
    new_rows: int = 0
    new_interactions: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def unlisted(self) -> int | None:
        """Recipients the campaign's count has that no list in the workbook names."""
        if self.recipients_count is None:
            return None
        return max(self.recipients_count - self.listed, 0)


@dataclass
class ImportReport:
    sha256: str
    campaigns: list[CampaignReport] = field(default_factory=list)
    skipped: tuple[SkippedTab, ...] = ()
    unmatched: list[str] = field(default_factory=list)
    """Addresses no contact holds (none, when ``create_missing`` created them all)."""
    ambiguous: list[str] = field(default_factory=list)
    """Addresses more than one contact holds."""
    new_campaigns: int = 0

    @property
    def unlisted(self) -> int:
        return sum(c.unlisted or 0 for c in self.campaigns)


def import_workbook(
    session: Session, user: User, workbook: Workbook, *, create_missing: bool = False
) -> ImportReport:
    """Upsert every campaign of ``workbook`` and its recipients for ``user`` (the module).

    ``RuntimeError`` when ``session`` is not a writer. Nothing is committed.
    """
    if not is_writer(session):
        raise RuntimeError("the history import needs a writer session")
    report = ImportReport(sha256=workbook.sha256, skipped=workbook.skipped)
    matches: dict[str, tuple[Match, int | None]] = {}
    for campaign in workbook.campaigns:
        report.campaigns.append(
            _import_campaign(
                session, user, campaign, workbook.sha256, matches, create_missing, report
            )
        )
    report.unmatched = sorted(a for a, (m, _) in matches.items() if m == "unmatched")
    report.ambiguous = sorted(a for a, (m, _) in matches.items() if m == "ambiguous")
    session.flush()
    log.info(
        "history import for user %d: %d campaign(s), %d unmatched, %d ambiguous",
        user.id,
        len(report.campaigns),
        len(report.unmatched),
        len(report.ambiguous),
    )
    return report


def _import_campaign(
    session: Session,
    user: User,
    source: WorkbookCampaign,
    sha256: str,
    matches: dict[str, tuple[Match, int | None]],
    create_missing: bool,
    report: ImportReport,
) -> CampaignReport:
    name = source.name[:HISTORY_CAMPAIGN_NAME_MAX_LENGTH]
    row = session.scalars(
        scoped(user, HistoryCampaign).where(
            HistoryCampaign.name == name, HistoryCampaign.started_on == source.started_on
        )
    ).one_or_none()
    if row is None:
        row = HistoryCampaign(user_id=user.id, name=name, started_on=source.started_on)
        session.add(row)
        report.new_campaigns += 1
    row.subject = source.subject
    row.last_batch_on = source.last_batch_on
    row.recipients_count = source.recipients_count
    row.opens_count = source.opens_count
    row.bounces_count = source.bounces_count
    row.source_sha256 = sha256
    session.flush()
    out = CampaignReport(
        index=source.index,
        name=name,
        started_on=source.started_on,
        last_batch_on=source.last_batch_on,
        recipients_count=source.recipients_count,
        opens_count=source.opens_count,
        bounces_count=source.bounces_count,
        warnings=source.warnings,
    )
    existing = {
        r.email: r
        for r in session.scalars(
            scoped(user, HistoryRecipient).where(HistoryRecipient.history_campaign_id == row.id)
        )
    }
    started_at = campaign_start(source.started_on)
    for person in source.recipients:
        out.listed += 1
        out.opened += person.opened
        out.clicked += person.clicked
        out.bounce_listed += person.bounced
        match, contact_id = _match(session, user, person, matches, create_missing)
        if match == "created":
            out.created += 1
        elif match == "matched":
            out.matched += 1
        elif match == "ambiguous":
            out.ambiguous += 1
        else:
            out.unmatched += 1
        recipient = existing.get(person.email)
        if recipient is None:
            recipient = HistoryRecipient(
                user_id=user.id,
                history_campaign_id=row.id,
                email=person.email,
                opened=False,
                clicked=False,
                bounce_listed=False,
            )
            session.add(recipient)
            existing[person.email] = recipient
            out.new_rows += 1
        # A later export only ever adds to what an earlier one said.
        recipient.opened = recipient.opened or person.opened
        recipient.clicked = recipient.clicked or person.clicked
        recipient.bounce_listed = recipient.bounce_listed or person.bounced
        if contact_id is not None:
            recipient.contact_id = contact_id
        session.flush()
        if record_email_out(session, user, recipient, name, started_at):
            out.new_interactions += 1
        if person.bounced:
            do_not_send.add(
                session, user, person.email, DoNotSendReason.BOUNCED, contact_id=contact_id
            )
    session.flush()
    return out


def _match(
    session: Session,
    user: User,
    person: Recipient,
    matches: dict[str, tuple[Match, int | None]],
    create_missing: bool,
) -> tuple[Match, int | None]:
    """Which contact holds ``person.email``, once per address and run."""
    known = matches.get(person.email)
    if known is not None:
        # Created earlier in this run: a match for every later tab.
        return ("matched", known[1]) if known[0] == "created" else known
    found = match_address(
        session,
        user,
        person.email,
        first_name=person.first_name,
        last_name=person.last_name,
        create_missing=create_missing,
    )
    matches[person.email] = found
    return found


def match_address(
    session: Session,
    user: User,
    email: str,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    create_missing: bool = False,
) -> tuple[Match, int | None]:
    """Which of ``user``'s contacts holds ``email``, through :func:`identity.resolve`:
    ``matched`` (the survivor's id), ``ambiguous`` (several do), ``unmatched``, or
    ``created`` when ``create_missing`` made one. Needs a writer session."""
    incoming = IncomingContact(
        source=HISTORY_SOURCE,
        first_name=first_name,
        last_name=last_name,
        emails=(IncomingEmail(email, is_primary=True),),
    )
    resolution = resolve(session, user, incoming)
    found: tuple[Match, int | None]
    match resolution:
        case Matched(contact_id=contact_id):
            found = ("matched", resolve_survivor(session, user, contact_id).id)
        case Candidate():
            found = ("ambiguous", None)
        case New():
            if create_missing:
                contact = apply(session, user, incoming, resolution)
                found = ("created", contact.id)
            else:
                found = ("unmatched", None)
    return found


def record_email_out(
    session: Session, user: User, recipient: HistoryRecipient, campaign: str, at: datetime
) -> bool:
    """The recipient's imported ``email_out`` on its contact's timeline, once: False when
    the row has no contact or already wrote one."""
    if recipient.contact_id is None or recipient.email_out_interaction_id is not None:
        return False
    contact = resolve_survivor(session, user, recipient.contact_id)
    interaction = add_interaction(
        session,
        user,
        contact.id,
        InteractionKind.EMAIL_OUT,
        at,
        summary=f"{HISTORY_SUMMARY}: emailed by the old mailing tool, campaign {campaign!r}",
        source=HISTORY_SOURCE,
    )
    recipient.email_out_interaction_id = interaction.id
    return True


def campaign_start(started_on: date) -> datetime:
    """When an imported campaign counts as sent: midnight UTC on its start date."""
    return datetime.combine(started_on, time(0, 0), tzinfo=UTC)


# --- what the rest of netkeeper reads ------------------------------------------------


def awaiting_reply_triage(session: Session, user: User, contact_id: int) -> bool:
    """Whether the Gmail scan flagged this contact for review because they wrote back to
    an old campaign. The connections sync reads it so that a sync matching the contact by
    URN does not clear a review flag it did not set (#65)."""
    held = session.scalar(
        scoped(user, HistoryRecipient)
        .with_only_columns(HistoryRecipient.id)
        .where(
            HistoryRecipient.contact_id == contact_id,
            HistoryRecipient.reply_kind.in_((HistoryReplyKind.REPLY, HistoryReplyKind.UNSUBSCRIBE)),
        )
        .limit(1)
    )
    return held is not None


@dataclass(frozen=True, slots=True)
class PriorContact:
    """How many of a set of contacts the old tool emailed, and when it last did."""

    contacts: int
    last_on: date | None

    def note(self) -> str | None:
        """``"12 were emailed by the old tool; last on 2026-05-01"``, or ``None`` for none."""
        if not self.contacts:
            return None
        verb = "was" if self.contacts == 1 else "were"
        last = f"; last on {self.last_on.isoformat()}" if self.last_on is not None else ""
        return f"{self.contacts} {verb} emailed by the old tool{last}"


def prior_contact(session: Session, user: User, contact_ids: Collection[int]) -> PriorContact:
    """How many of ``contact_ids`` an imported campaign reached, for an informational note
    at activation (#65, Part C). Reads only. A contact counts through any of its
    addresses: a recipient row's own contact, or an address the contact holds now."""
    ids = sorted(set(contact_ids))
    if not ids:
        return PriorContact(0, None)
    addresses = {
        email: contact_id
        for contact_id, email in session.execute(
            scoped(user, ContactEmail)
            .with_only_columns(ContactEmail.contact_id, ContactEmail.email)
            .where(ContactEmail.contact_id.in_(ids))
        )
    }
    reached: set[int] = set()
    last: date | None = None
    rows = session.execute(
        scoped(user, HistoryRecipient)
        .with_only_columns(
            HistoryRecipient.contact_id,
            HistoryRecipient.email,
            func.coalesce(HistoryCampaign.last_batch_on, HistoryCampaign.started_on),
        )
        .join(HistoryCampaign, HistoryCampaign.id == HistoryRecipient.history_campaign_id)
        .where(HistoryCampaign.user_id == user.id)
    )
    wanted = set(ids)
    for contact_id, email, on in rows:
        who = contact_id if contact_id in wanted else addresses.get(email)
        if who is None:
            continue
        reached.add(who)
        if last is None or on > last:
            last = on
    return PriorContact(len(reached), last)
