"""The Gmail scan of the old tool's recipients (#65, Part B), against the Gmail fake only.

Every person and message here is invented. The scan reads; the fake's call log
proves it never calls a method that changes the mailbox.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage

import factories
import pytest
from campaign_fakes import make_mailbox
from history_fixtures import Tab, workbook_bytes
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.gmail import GmailRateLimited, MessageRef
from netkeeper.campaigns.gmail_fake import MAILER_DAEMON, FakeGmail
from netkeeper.crm import apply as mapping
from netkeeper.crm import do_not_send
from netkeeper.crm.contacts import confirm_contact
from netkeeper.crm.history import HISTORY_SUMMARY, import_workbook
from netkeeper.crm.history_workbook import read_workbook
from netkeeper.db import session_scope
from netkeeper.linkedin.connections import ConnectionsPage, SyncMode
from netkeeper.linkedin.voyager import ConnectionSummary
from netkeeper.models import (
    Contact,
    ContactEmail,
    DoNotSendReason,
    HistoryRecipient,
    HistoryReplyKind,
    Interaction,
    InteractionKind,
    User,
)
from netkeeper.scoping import scoped, scoped_count
from netkeeper.services import history_scan
from netkeeper.services.campaign_guards import Reason, check_enrollment
from netkeeper.services.history_scan import (
    READ_ONLY_METHODS,
    SEARCH_MAX,
    UNSUBSCRIBE_REASON,
    WINDOW_AFTER_LAST_BATCH,
    WINDOW_BEFORE_START,
    apply_scan,
    read_gmail,
    scan_targets,
    window,
)

ME = "me@example.test"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
MUTATING = frozenset(
    {"messages.send", "drafts.create", "messages.modify", "labels.create", "history.list"}
)

OPENED = (
    ("Ada", "Lovelace", "ada@example.test"),
    ("Bob", "Babbage", "bob@example.test"),
    ("Cy", "Hopper", "cy@example.test"),
    ("Eve", "Noether", "eve@example.test"),
    ("Fay", "Curie", "fay@example.test"),
    ("Gus", "Knuth", "gus@example.test"),
)
SPRING = Tab(opened=OPENED, opens="6 (75.0%)", recipients=8, clicked=(), bounces=(), bounced=0)
FOLLOW_UP = Tab(
    title="Follow-up",
    name="Spring follow-up",
    started=datetime(2026, 3, 16, 9, 0),
    last_batch=datetime(2026, 3, 16, 9, 0),
    opened=(("Ada", "Lovelace", "ada@example.test"),),
    opens="1 (100.0%)",
    recipients=1,
    clicked=(),
    bounces=(),
    bounced=0,
)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def people(writer: Session, user: User) -> dict[str, Contact]:
    """A contact for each recipient but Gus, imported. Bob has a second address."""
    contacts = {
        name: factories.make_contact(writer, user, emails=[f"{name}@example.test"])
        for name in ("ada", "bob", "cy", "eve", "fay")
    }
    contacts["bob"].emails.append(
        ContactEmail(user_id=user.id, email="bob.work@example.test", is_primary=False)
    )
    writer.flush()
    import_workbook(writer, user, read_workbook(workbook_bytes(SPRING, FOLLOW_UP)))
    return contacts


def _mail(sender: str, subject: str, body: str, **headers: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ME
    message["Subject"] = subject
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    message.set_content(body)
    return message


def _at(day: int, month: int = 3) -> datetime:
    return datetime(2026, month, day, 15, 0, tzinfo=UTC)


@pytest.fixture
def gmail() -> FakeGmail:
    """A mailbox holding one of each kind of answer, and some that are none."""
    fake = FakeGmail(ME, mailbox_id=1, clock=lambda: NOW)
    fake.deliver(
        _mail("Ada <ada@example.test>", "Re: Catching up", "Lovely to hear from you."), at=_at(4)
    )
    fake.deliver(_mail("ada@example.test", "Lunch?", "Long after the window."), at=_at(1, 9))
    fake.deliver(
        _mail("bob@example.test", "Re: Catching up", "Please remove me from this."), at=_at(5)
    )
    fake.deliver(
        _mail("cy@example.test", "Out of office", "Back next week.", Auto_Submitted="auto-replied"),
        at=_at(3),
    )
    fake.deliver(
        _mail(
            f"Mail Delivery Subsystem <{MAILER_DAEMON}>",
            "Delivery Status Notification (Failure)",
            "Address not found: eve@example.test",
            X_Failed_Recipients="eve@example.test",
        ),
        at=_at(2),
    )
    fake.deliver(
        _mail(
            f"Mail Delivery Subsystem <{MAILER_DAEMON}>",
            "Delivery Status Notification (Delay)",
            "Still trying to deliver to fay@example.test.",
        ),
        at=_at(2),
    )
    fake.deliver(_mail("bada@example.test", "Re: Catching up", "Not Ada."), at=_at(4))
    fake.deliver(_mail("gus@example.test", "Re: Catching up", "Who is this?"), at=_at(6))
    return fake


def _scan(writer: Session, user: User, gmail: FakeGmail, **kwargs: bool) -> history_scan.ScanReport:
    read = read_gmail(gmail, scan_targets(writer, user, **kwargs))
    return apply_scan(writer, user, read, now=NOW)


def _row(
    writer: Session, user: User, email: str, campaign: str = "Spring check-in"
) -> HistoryRecipient:
    rows = [
        r
        for r in writer.scalars(
            scoped(user, HistoryRecipient).where(HistoryRecipient.email == email)
        )
        if r.campaign.name == campaign
    ]
    assert len(rows) == 1
    return rows[0]


def _inbound(writer: Session, user: User, contact: Contact) -> list[Interaction]:
    return list(
        writer.scalars(
            scoped(user, Interaction).where(
                Interaction.contact_id == contact.id, Interaction.kind == InteractionKind.EMAIL_IN
            )
        )
    )


# --- the constants ---------------------------------------------------------------------


def test_the_scan_constants_are_the_decided_values() -> None:
    assert timedelta(days=120) == WINDOW_AFTER_LAST_BATCH
    assert timedelta(days=1) == WINDOW_BEFORE_START
    assert SEARCH_MAX == 20
    assert UNSUBSCRIBE_REASON == "unsubscribe (old campaign)"
    assert frozenset({"messages.list", "messages.get"}) == READ_ONLY_METHODS


def test_the_window_runs_from_the_day_before_the_start_to_120_days_after_the_last_batch() -> None:
    assert window(date(2026, 3, 2), date(2026, 3, 9)) == (
        datetime(2026, 3, 1, tzinfo=UTC),
        datetime(2026, 7, 8, tzinfo=UTC),
    )
    assert window(date(2026, 3, 2), None)[1] == datetime(2026, 7, 1, tzinfo=UTC)


# --- each kind of answer ---------------------------------------------------------------


def test_each_kind_of_answer_gets_its_action(
    writer: Session, user: User, people: dict[str, Contact], gmail: FakeGmail
) -> None:
    report = _scan(writer, user, gmail)

    # A human reply: an imported email_in, replied_at, and a review flag.
    ada = people["ada"]
    row = _row(writer, user, "ada@example.test")
    assert (row.reply_kind, row.replied_at) == (HistoryReplyKind.REPLY, _at(4))
    assert ada.needs_review_at == NOW
    (entry,) = _inbound(writer, user, ada)
    assert entry.at == _at(4)
    assert entry.summary is not None and entry.summary.startswith(f"{HISTORY_SUMMARY}: wrote")
    assert "Re: Catching up" in entry.summary
    assert row.email_in_interaction_id == entry.id
    assert not ada.do_not_contact

    # An unsubscribe: do-not-contact, and every address of theirs opted out.
    bob = people["bob"]
    assert _row(writer, user, "bob@example.test").reply_kind is HistoryReplyKind.UNSUBSCRIBE
    assert (bob.do_not_contact, bob.do_not_contact_reason) == (True, UNSUBSCRIBE_REASON)
    for address in ("bob@example.test", "bob.work@example.test"):
        entry_ = do_not_send.find(writer, user, address)
        assert entry_ is not None and entry_.reason is DoNotSendReason.OPTED_OUT
    assert len(_inbound(writer, user, bob)) == 1
    assert bob.needs_review_at is None

    # An automatic answer: on the row only.
    cy = people["cy"]
    cy_row = _row(writer, user, "cy@example.test")
    assert (cy_row.reply_kind, cy_row.replied_at) == (HistoryReplyKind.AUTO, None)
    assert (cy.needs_review_at, cy.do_not_contact) == (None, False)
    assert _inbound(writer, user, cy) == []
    assert do_not_send.find(writer, user, "cy@example.test") is None

    # A hard bounce: do-not-send as bounced, and bounced_at.
    eve_row = _row(writer, user, "eve@example.test")
    assert (eve_row.reply_kind, eve_row.bounced_at) == (HistoryReplyKind.BOUNCE, _at(2))
    eve_entry = do_not_send.find(writer, user, "eve@example.test")
    assert eve_entry is not None and eve_entry.reason is DoNotSendReason.BOUNCED

    # A delay notice, another sender's mail, and mail after the window are nothing.
    fay_row = _row(writer, user, "fay@example.test")
    assert (fay_row.reply_kind, fay_row.scanned_at) == (None, NOW)
    assert do_not_send.find(writer, user, "fay@example.test") is None

    assert (report.totals.reply, report.totals.unsubscribe) == (2, 1)  # Ada and Gus
    assert (report.totals.auto, report.totals.bounce) == (1, 1)
    assert report.samples[HistoryReplyKind.REPLY] == ["ada@example.test", "gus@example.test"]
    assert report.flagged == 1
    assert report.no_contact == ["gus@example.test"]


def test_the_scan_never_calls_a_method_that_changes_the_mailbox(
    writer: Session, user: User, people: dict[str, Contact], gmail: FakeGmail
) -> None:
    _scan(writer, user, gmail)

    methods = {method for method, _ in gmail.calls}
    assert methods <= READ_ONLY_METHODS
    assert not methods & MUTATING
    assert all("@" not in purpose for _, purpose in gmail.calls)


def test_after_apply_the_guards_exclude_the_unsubscriber_the_bounce_and_the_replier(
    writer: Session, user: User, people: dict[str, Contact], gmail: FakeGmail
) -> None:
    """End to end through the guards, not only the rows (the recency guard is off here)."""
    _scan(writer, user, gmail)
    campaign = factories.make_campaign(writer, user, contacted_within_days_guard=0)

    verdicts = {
        v.contact_id: v.reasons
        for v in check_enrollment(writer, user, campaign, [c.id for c in people.values()], now=NOW)
    }

    assert Reason.NEEDS_REVIEW in verdicts[people["ada"].id]
    assert {Reason.DO_NOT_CONTACT, Reason.DO_NOT_SEND} <= set(verdicts[people["bob"].id])
    assert Reason.DO_NOT_SEND in verdicts[people["eve"].id]
    assert verdicts[people["cy"].id] == ()  # an out-of-office answer changes nothing
    assert verdicts[people["fay"].id] == ()


def test_the_same_reply_in_two_campaigns_windows_is_one_timeline_entry(
    writer: Session, user: User, people: dict[str, Contact], gmail: FakeGmail
) -> None:
    _scan(writer, user, gmail)

    spring = _row(writer, user, "ada@example.test")
    follow_up = _row(writer, user, "ada@example.test", "Spring follow-up")
    # The follow-up's window starts 2026-03-15: Ada's reply on 03-04 is not in it.
    assert follow_up.reply_kind is None
    assert spring.reply_kind is HistoryReplyKind.REPLY
    assert len(_inbound(writer, user, people["ada"])) == 1


def test_a_reply_in_overlapping_windows_is_recorded_once(
    writer: Session, user: User, people: dict[str, Contact]
) -> None:
    fake = FakeGmail(ME, mailbox_id=1, clock=lambda: NOW)
    fake.deliver(_mail("ada@example.test", "Re: Catching up", "Yes, let's talk."), at=_at(20))

    _scan(writer, user, fake)

    for campaign in ("Spring check-in", "Spring follow-up"):
        row = _row(writer, user, "ada@example.test", campaign)
        assert row.reply_kind is HistoryReplyKind.REPLY
    assert len(_inbound(writer, user, people["ada"])) == 1
    assert (
        _row(writer, user, "ada@example.test").email_in_interaction_id
        == _row(writer, user, "ada@example.test", "Spring follow-up").email_in_interaction_id
    )


def test_an_unsubscribe_from_an_address_no_contact_holds_still_lists_it(
    writer: Session, user: User, people: dict[str, Contact]
) -> None:
    fake = FakeGmail(ME, mailbox_id=1, clock=lambda: NOW)
    fake.deliver(_mail("gus@example.test", "Unsubscribe", "No more, thanks."), at=_at(6))

    report = _scan(writer, user, fake)

    entry = do_not_send.find(writer, user, "gus@example.test")
    assert entry is not None and entry.reason is DoNotSendReason.OPTED_OUT
    assert report.no_contact == ["gus@example.test"]


# --- resuming, rescanning ---------------------------------------------------------------


class _LimitedGmail(FakeGmail):
    """Answers ``allowed`` searches, then says slow down."""

    def __init__(self, allowed: int) -> None:
        super().__init__(ME, mailbox_id=1, clock=lambda: NOW)
        self.allowed = allowed

    def search(self, query: str, *, max_results: int = 100, purpose: str) -> list[MessageRef]:
        if self.allowed <= 0:
            raise GmailRateLimited("Gmail answered 429", code="rateLimitExceeded")
        self.allowed -= 1
        return super().search(query, max_results=max_results, purpose=purpose)


def test_a_rate_limit_stops_the_scan_and_the_next_run_resumes(
    writer: Session, user: User, people: dict[str, Contact]
) -> None:
    total = len(scan_targets(writer, user))
    limited = _LimitedGmail(allowed=5)  # two searches a recipient: two done, the third cut off

    report = _scan(writer, user, limited)

    assert (report.stopped, report.scanned, report.remaining) == ("rateLimitExceeded", 2, total - 2)
    assert len(scan_targets(writer, user)) == total - 2
    resumed = _scan(writer, user, FakeGmail(ME, mailbox_id=1, clock=lambda: NOW))
    assert (resumed.stopped, resumed.scanned) == (None, total - 2)
    assert scan_targets(writer, user) == []
    assert len(scan_targets(writer, user, rescan=True)) == total


def test_a_rescan_adds_nothing_twice_and_does_not_flag_a_confirmed_contact_again(
    writer: Session, user: User, people: dict[str, Contact], gmail: FakeGmail
) -> None:
    _scan(writer, user, gmail)
    ada = people["ada"]
    confirm_contact(writer, user, ada.id)
    interactions = writer.scalar(scoped_count(user, Interaction))

    again = _scan(writer, user, gmail, rescan=True)

    assert again.scanned == len(scan_targets(writer, user, rescan=True))
    assert writer.scalar(scoped_count(user, Interaction)) == interactions
    assert ada.needs_review_at is None
    assert again.flagged == 0


def test_a_connections_sync_does_not_clear_a_replier_s_review_flag(
    writer: Session, user: User, people: dict[str, Contact], gmail: FakeGmail
) -> None:
    """A URN match confirms a card's contact (#184); it does not triage a reply (#65)."""
    _scan(writer, user, gmail)
    ada = people["ada"]
    assert ada.li_urn is not None and ada.li_public_id is not None
    page = ConnectionsPage(
        mode=SyncMode.FULL,
        number=0,
        start=0,
        total=1,
        connections=(
            ConnectionSummary(
                urn=ada.li_urn,
                public_id=ada.li_public_id,
                first_name=ada.first_name or "",
                last_name=ada.last_name or "",
                headline=ada.headline,
                connected_at=None,
            ),
        ),
        observed_at=NOW,
    )

    counts = mapping.apply_page(writer, user, page)

    assert counts.confirmed_by_urn == 0
    assert ada.needs_review_at == NOW


def test_applying_needs_a_writer_session(session: Session) -> None:
    user = factories.make_user(session)
    with pytest.raises(RuntimeError, match="writer"):
        apply_scan(session, user, history_scan.ScanRead(), now=NOW)


def test_a_notice_naming_another_failed_recipient_is_not_this_ones_bounce() -> None:
    fake = FakeGmail(ME, mailbox_id=1, clock=lambda: NOW)
    ref = fake.deliver(
        _mail(
            MAILER_DAEMON,
            "Undeliverable: Catching up",
            "eve@example.test was mentioned, but the failure is someone else's.",
            X_Failed_Recipients="zed@example.test",
        ),
        at=_at(2),
    )
    message = fake.get_message(ref.id, purpose="test")
    assert not history_scan.names_failed_recipient(message, "eve@example.test")
    assert history_scan.names_failed_recipient(message, "zed@example.test")


# --- the subject pass: people the workbook does not list ---------------------------------


def _subject_scan(
    writer: Session, user: User, gmail: FakeGmail, *, rescan: bool = False
) -> history_scan.ScanReport:
    read = read_gmail(
        gmail,
        scan_targets(writer, user, rescan=rescan),
        history_scan.subject_targets(writer, user, rescan=rescan),
    )
    return apply_scan(writer, user, read, now=NOW)


@pytest.fixture
def answers() -> FakeGmail:
    """Replies to "Catching up" from people the workbook does not list, and near misses."""
    fake = FakeGmail(ME, mailbox_id=1, clock=lambda: NOW, aliases=["alias@example.test"])
    fake.deliver(
        _mail("Hal Abelson <hal@example.test>", "RE: Catching up", "Sounds good."), at=_at(5)
    )
    fake.deliver(_mail("kim@example.test", "Re: Catching up", "Please remove me."), at=_at(6))
    # Not exactly the subject, before the start day, another of the user's mailboxes
    # (which -from:me does not cover), and Ada,
    # whom the workbook already lists.
    fake.deliver(
        _mail("ivy@example.test", "Re: Catching up on the conference", "Hello!"), at=_at(5)
    )
    fake.deliver(_mail("jo@example.test", "Re: Catching up", "Early."), at=_at(1))
    fake.deliver(_mail("old@example.test", "Re: Catching up", "Note to self."), at=_at(5))
    fake.deliver(_mail("ada@example.test", "Re: Catching up", "Listed already."), at=_at(4))
    return fake


def test_the_subject_pass_finds_an_unlisted_replier_and_flags_them(
    writer: Session, user: User, people: dict[str, Contact], answers: FakeGmail
) -> None:
    make_mailbox(writer, user, email="old@example.test")
    hal = factories.make_contact(writer, user, emails=["hal@example.test"])

    report = _subject_scan(writer, user, answers)

    spring = _row(writer, user, "ada@example.test").history_campaign_id
    found = {
        r.email: r
        for r in writer.scalars(
            scoped(user, HistoryRecipient).where(HistoryRecipient.found_by_subject.is_(True))
        )
    }
    assert set(found) == {"hal@example.test", "kim@example.test"}
    assert all(r.history_campaign_id == spring for r in found.values())
    assert report.by_subject == {spring: 2}
    assert sorted(report.subject_samples) == ["hal@example.test", "kim@example.test"]

    # Hal: matched, emailed by the campaign, and waiting for review.
    row = found["hal@example.test"]
    assert (row.contact_id, row.reply_kind, row.replied_at) == (
        hal.id,
        HistoryReplyKind.REPLY,
        _at(5),
    )
    assert hal.needs_review_at == NOW
    kinds = sorted(
        e.kind
        for e in writer.scalars(scoped(user, Interaction).where(Interaction.contact_id == hal.id))
    )
    assert kinds == sorted([InteractionKind.EMAIL_OUT, InteractionKind.EMAIL_IN])
    campaign = factories.make_campaign(writer, user, contacted_within_days_guard=0)
    (verdict,) = check_enrollment(writer, user, campaign, [hal.id], now=NOW)
    assert Reason.NEEDS_REVIEW in verdict.reasons

    # Kim: no contact, asked to unsubscribe: the address is off limits anyway.
    assert found["kim@example.test"].reply_kind is HistoryReplyKind.UNSUBSCRIBE
    entry = do_not_send.find(writer, user, "kim@example.test")
    assert entry is not None and entry.reason is DoNotSendReason.OPTED_OUT
    # Ada was already listed: no second row, and the listed pass did not run here for her.
    assert _row(writer, user, "ada@example.test").found_by_subject is False


def test_a_re_run_of_the_subject_pass_adds_nothing(
    writer: Session, user: User, people: dict[str, Contact], answers: FakeGmail
) -> None:
    _subject_scan(writer, user, answers)
    counts = (
        writer.scalar(scoped_count(user, HistoryRecipient)),
        writer.scalar(scoped_count(user, Interaction)),
    )

    assert history_scan.subject_targets(writer, user) == []
    again = _subject_scan(writer, user, answers, rescan=True)

    assert again.by_subject == {}
    assert (
        writer.scalar(scoped_count(user, HistoryRecipient)),
        writer.scalar(scoped_count(user, Interaction)),
    ) == counts


def test_subjects_compare_without_reply_prefixes_and_quotes_never_reach_the_query() -> None:
    assert history_scan.normalize_subject("RE: Fwd:  re:Catching   UP") == "catching up"
    assert history_scan.normalize_subject("Catching up on things") != "catching up"
    target = history_scan.SubjectTarget(
        campaign_id=1,
        subject='Say "hi" \\ now',
        starts=_at(2),
        after=datetime(2026, 3, 1, tzinfo=UTC),
        before=datetime(2026, 3, 2, tzinfo=UTC),
        known=frozenset(),
        own=frozenset(),
    )
    assert history_scan.subject_query(target) == (
        'subject:"Say hi now" -from:me after:1772323200 before:1772409600'
    )
