"""Importing the old mailing tool's history (#65, Part A, and Part C's timeline label).

Hand-built workbooks with invented people (tests/history_fixtures.py).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import factories
import pytest
from history_fixtures import Tab, workbook_bytes
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import do_not_send
from netkeeper.crm.history import (
    HISTORY_SUMMARY,
    awaiting_reply_triage,
    campaign_start,
    import_workbook,
    prior_contact,
)
from netkeeper.crm.history_workbook import read_workbook
from netkeeper.crm.interactions import timeline
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactSource,
    DoNotSendReason,
    HistoryCampaign,
    HistoryRecipient,
    HistoryReplyKind,
    Interaction,
    InteractionKind,
    User,
)
from netkeeper.scoping import scoped, scoped_count
from netkeeper.services.campaign_guards import Reason, check_enrollment

FOLLOW_UP = Tab(
    title="Follow-up",
    name="Spring follow-up",
    started=datetime(2026, 3, 16, 9, 0),
    last_batch=datetime(2026, 3, 16, 9, 0),
    opened=(("Ada", "Lovelace", "ada@example.test"),),
    opens="1 (25.0%)",
    recipients=4,
    clicked=(),
    bounces=(),
    bounced=0,
    clicks_col=10,
)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _workbook(*tabs: Tab) -> bytes:
    return workbook_bytes(*(tabs or (Tab(), FOLLOW_UP)))


def _count(session: Session, user: User, model: type[HistoryCampaign | HistoryRecipient]) -> int:
    return session.scalar(scoped_count(user, model)) or 0


def _interactions(session: Session, user: User, contact: Contact) -> list[Interaction]:
    return list(
        session.scalars(
            scoped(user, Interaction)
            .where(Interaction.contact_id == contact.id)
            .order_by(Interaction.at)
        )
    )


def test_an_import_records_campaigns_recipients_and_one_email_out_per_campaign(
    writer: Session, user: User
) -> None:
    ada = factories.make_contact(writer, user, emails=["ada@example.test"])
    bob = factories.make_contact(writer, user, emails=["BOB@example.test"])

    report = import_workbook(writer, user, read_workbook(_workbook()))

    assert _count(writer, user, HistoryCampaign) == 2
    assert _count(writer, user, HistoryRecipient) == 5  # four in the first tab, Ada again
    first, second = report.campaigns
    assert (first.listed, first.matched, first.unmatched, first.clicked) == (4, 2, 2, 1)
    assert (first.opens_count, first.bounces_count, first.recipients_count) == (3, 1, 5)
    assert first.unlisted == 1
    assert (second.listed, second.matched) == (1, 1)
    assert report.unmatched == ["cy@example.test", "dee@example.test"]
    assert report.ambiguous == []

    entries = _interactions(writer, user, ada)
    assert [(e.kind, e.at, e.source) for e in entries] == [
        (InteractionKind.EMAIL_OUT, datetime(2026, 3, 2, tzinfo=UTC), ContactSource.CSV),
        (InteractionKind.EMAIL_OUT, datetime(2026, 3, 16, tzinfo=UTC), ContactSource.CSV),
    ]
    assert all(e.summary and e.summary.startswith(f"{HISTORY_SUMMARY}: ") for e in entries)
    assert ada.last_contacted_at == datetime(2026, 3, 16, tzinfo=UTC)
    assert len(_interactions(writer, user, bob)) == 1

    rows = {
        r.email: r for r in writer.scalars(scoped(user, HistoryRecipient)) if r.contact_id == ada.id
    }
    assert set(rows) == {"ada@example.test"}


def test_a_re_import_adds_no_rows_and_no_interactions(writer: Session, user: User) -> None:
    factories.make_contact(writer, user, emails=["ada@example.test"])
    import_workbook(writer, user, read_workbook(_workbook()))
    before = (
        _count(writer, user, HistoryCampaign),
        _count(writer, user, HistoryRecipient),
        writer.scalar(scoped_count(user, Interaction)),
    )

    again = import_workbook(writer, user, read_workbook(_workbook()))

    after = (
        _count(writer, user, HistoryCampaign),
        _count(writer, user, HistoryRecipient),
        writer.scalar(scoped_count(user, Interaction)),
    )
    assert after == before
    assert again.new_campaigns == 0
    assert sum(c.new_rows + c.new_interactions for c in again.campaigns) == 0


def test_a_re_import_matches_an_address_a_contact_gained_since(writer: Session, user: User) -> None:
    import_workbook(writer, user, read_workbook(_workbook()))
    cy = factories.make_contact(writer, user, emails=["cy@example.test"])

    again = import_workbook(writer, user, read_workbook(_workbook()))

    assert again.campaigns[0].new_interactions == 1
    assert [e.kind for e in _interactions(writer, user, cy)] == [InteractionKind.EMAIL_OUT]


def test_create_missing_creates_a_contact_per_unmatched_address(
    writer: Session, user: User
) -> None:
    report = import_workbook(writer, user, read_workbook(_workbook()), create_missing=True)

    assert report.unmatched == []
    assert report.campaigns[0].created == 4
    contacts = list(writer.scalars(scoped(user, Contact).order_by(Contact.id)))
    assert [(c.first_name, c.last_name, c.source) for c in contacts] == [
        ("Ada", "Lovelace", ContactSource.CSV),
        ("Bob", "Babbage", ContactSource.CSV),
        ("Cy", "Hopper", ContactSource.CSV),
        ("Dee", "Turing", ContactSource.CSV),
    ]
    # Created in the first tab, matched in the second: one contact, two entries.
    assert len(_interactions(writer, user, contacts[0])) == 2


def test_an_address_two_contacts_hold_is_ambiguous_and_matched_to_neither(
    writer: Session, user: User
) -> None:
    one = factories.make_contact(writer, user, emails=["ada@example.test"])
    two = factories.make_contact(writer, user, emails=["ada@example.test"])

    report = import_workbook(writer, user, read_workbook(_workbook()))

    assert report.ambiguous == ["ada@example.test"]
    assert _interactions(writer, user, one) == _interactions(writer, user, two) == []


def test_a_person_on_the_bounce_list_goes_on_the_do_not_send_list(
    writer: Session, user: User
) -> None:
    import_workbook(writer, user, read_workbook(_workbook()))

    entry = do_not_send.find(writer, user, "dee@example.test")
    assert entry is not None
    assert (entry.reason, entry.bounced) == (DoNotSendReason.BOUNCED, True)
    row = writer.scalars(
        scoped(user, HistoryRecipient).where(HistoryRecipient.email == "dee@example.test")
    ).one()
    assert row.bounce_listed and row.reply_kind is None


def test_the_rows_are_the_importing_users_only(
    writer: Session, user: User, session_factory: sessionmaker[Session]
) -> None:
    other = factories.make_user(writer)
    theirs = factories.make_contact(writer, other, emails=["ada@example.test"])

    import_workbook(writer, user, read_workbook(_workbook()))

    assert _count(writer, other, HistoryCampaign) == 0
    assert _count(writer, other, HistoryRecipient) == 0
    # Another user's contact holding the same address is not matched.
    assert _interactions(writer, other, theirs) == []
    assert all(r.contact_id is None for r in writer.scalars(scoped(user, HistoryRecipient)))


def test_the_contact_timeline_labels_the_entry_as_imported_history(
    writer: Session, user: User
) -> None:
    ada = factories.make_contact(writer, user, emails=["ada@example.test"])
    import_workbook(writer, user, read_workbook(_workbook()))

    entries = timeline(writer, user, ada.id, limit=10)

    assert [e.row.summary for e in entries if isinstance(e.row, Interaction)] == [
        "Imported history: emailed by the old mailing tool, campaign 'Spring follow-up'",
        "Imported history: emailed by the old mailing tool, campaign 'Spring check-in'",
    ]


def test_the_recency_guard_counts_an_imported_campaign(writer: Session, user: User) -> None:
    """End to end through the guard: imported history is someone contacted recently."""
    ada = factories.make_contact(writer, user, emails=["ada@example.test"])
    eve = factories.make_contact(writer, user, emails=["eve@example.test"])
    import_workbook(writer, user, read_workbook(_workbook()))
    campaign = factories.make_campaign(writer, user, contacted_within_days_guard=30)

    verdicts = check_enrollment(
        writer,
        user,
        campaign,
        [ada.id, eve.id],
        now=campaign_start(date(2026, 3, 16)) + timedelta(days=10),
    )

    assert {v.contact_id: v.reasons for v in verdicts} == {
        ada.id: (Reason.CONTACTED_RECENTLY,),
        eve.id: (),
    }


def test_prior_contact_counts_the_contacts_an_old_campaign_reached(
    writer: Session, user: User
) -> None:
    ada = factories.make_contact(writer, user, emails=["ada@example.test"])
    eve = factories.make_contact(writer, user, emails=["eve@example.test"])
    import_workbook(writer, user, read_workbook(_workbook()))
    # Gained the address after the import: counted through the address.
    cy = factories.make_contact(writer, user, emails=["cy@example.test"])

    prior = prior_contact(writer, user, [ada.id, eve.id, cy.id])

    assert (prior.contacts, prior.last_on) == (2, date(2026, 3, 16))
    assert prior.note() == "2 were emailed by the old tool; last on 2026-03-16"
    assert prior_contact(writer, user, [eve.id]).note() is None


def test_awaiting_reply_triage_reads_only_a_human_reply(writer: Session, user: User) -> None:
    ada = factories.make_contact(writer, user, emails=["ada@example.test"])
    import_workbook(writer, user, read_workbook(_workbook()))
    row = writer.scalars(
        scoped(user, HistoryRecipient).where(HistoryRecipient.contact_id == ada.id)
    ).first()
    assert row is not None
    assert not awaiting_reply_triage(writer, user, ada.id)
    row.reply_kind = HistoryReplyKind.AUTO
    assert not awaiting_reply_triage(writer, user, ada.id)
    row.reply_kind = HistoryReplyKind.REPLY
    assert awaiting_reply_triage(writer, user, ada.id)


def test_an_import_needs_a_writer_session(session: Session) -> None:
    user = factories.make_user(session)
    with pytest.raises(RuntimeError, match="writer"):
        import_workbook(session, user, read_workbook(_workbook()))


def test_a_recipient_is_unique_per_campaign_and_address_whatever_its_case(
    session: Session,
) -> None:
    user = factories.make_user(session)
    campaign = HistoryCampaign(
        user_id=user.id, name="Spring", started_on=date(2026, 3, 2), source_sha256="0" * 64
    )
    session.add(campaign)
    session.flush()
    session.add(
        HistoryRecipient(user_id=user.id, history_campaign_id=campaign.id, email="a@x.test")
    )
    session.flush()
    session.add(
        HistoryRecipient(user_id=user.id, history_campaign_id=campaign.id, email=" A@X.test ")
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_re_import_does_not_put_back_a_bounce_a_person_removed(
    writer: Session, user: User
) -> None:
    """N7: the workbook's bounce goes on the list once; removing it by hand sticks."""
    import_workbook(writer, user, read_workbook(_workbook()))
    entry = do_not_send.find(writer, user, "dee@example.test")
    assert entry is not None
    do_not_send.remove(writer, user, entry.id)

    import_workbook(writer, user, read_workbook(_workbook()))

    assert do_not_send.find(writer, user, "dee@example.test") is None
