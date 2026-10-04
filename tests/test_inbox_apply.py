"""The inbox poll's contract and its core mapping (P4-08, #378).

A :class:`inbox_fakes` delta, applied by :mod:`netkeeper.crm.inbox_apply`, produces
the expected interactions and conversation rows, idempotently; nobody unknown is
created; a group thread is never attributed; an archive import and a poll never
record the same message twice; a merge moves conversations. Every URN and message
here is invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import factories
import pytest
from inbox_fakes import OWNER_URN, a_thread_with, conversation, delta, message, profile_urn
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import archive as archive_module
from netkeeper.crm import inbox_apply
from netkeeper.crm.archive import import_archive
from netkeeper.crm.identity import merge
from netkeeper.crm.interactions import (
    INVITATION_SUMMARY,
    add_interaction,
    is_invitation,
    summary_is_invitation,
)
from netkeeper.db import session_scope
from netkeeper.linkedin import inbox
from netkeeper.linkedin.archive import open_archive
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import (
    CampaignStatus,
    Contact,
    ContactSource,
    EnrollmentStatus,
    Interaction,
    InteractionKind,
    LiConversation,
    MessageDirection,
    MessageStatus,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services import campaign_guards
from netkeeper.services.campaign_replies import WATCH_AFTER_COMPLETED

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
THEN = datetime(2026, 9, 22, 14, 22, 10, 750_000, tzinfo=UTC)  # a fraction of a second on
ADA = profile_urn("ada")
BEN = profile_urn("ben")
STRANGER = profile_urn("stranger")


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _contact(session: Session, user: User, urn: str | None, **overrides: Any) -> Contact:
    return factories.make_contact(session, user, li_urn=urn, **overrides)


def _interactions(session: Session, user: User) -> list[Interaction]:
    return sorted(session.scalars(scoped(user, Interaction)), key=lambda row: (row.at, row.id))


def _conversations(session: Session, user: User) -> list[LiConversation]:
    return list(session.scalars(scoped(user, LiConversation).order_by(LiConversation.id)))


# --- the contract --------------------------------------------------------------------


def test_the_contract_constants_are_the_decisions() -> None:
    assert inbox.SNIPPET_MAX == 200
    assert inbox.MAX_THREADS_OPENED == 5


def test_the_contract_refuses_what_it_cannot_mean() -> None:
    with pytest.raises(ValueError, match="at most 200"):
        message(1, sender=ADA, at=NOW, text="x" * 201)
    with pytest.raises(ValueError, match="timezone-aware"):
        message(1, sender=ADA, at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="at most 5 threads"):
        inbox.InboxJobSpec(
            since=None,
            watched_urns=frozenset(),
            max_conversations=10,
            open_threads_for=frozenset(f"urn:li:msg_conversation:{n}" for n in range(6)),
        )
    with pytest.raises(ValueError, match="at least one"):
        inbox.InboxJobSpec(
            since=None, watched_urns=frozenset(), max_conversations=0, open_threads_for=frozenset()
        )
    with pytest.raises(ValueError, match="not a stop"):
        inbox.InboxReadStopped(Outcome.OK)


def test_message_text_never_appears_in_a_repr_or_a_refusal() -> None:
    secret = "an invented secret sentence"
    found = message(1, sender=ADA, at=NOW, text=secret)
    assert secret not in repr(found)
    assert secret not in repr(delta(conversation("one", ADA, [found])))
    with pytest.raises(ValueError) as refused:
        message(2, sender=ADA, at=NOW, text=secret * 20)
    assert secret not in str(refused.value)


# --- applying a delta ------------------------------------------------------------------


def test_a_delta_becomes_interactions_and_a_conversation(writer: Session, user: User) -> None:
    ada = _contact(writer, user, ADA)
    counts = inbox_apply.apply_delta(writer, user, delta(a_thread_with(ADA, THEN)), polled_at=NOW)

    assert counts.counts() == {
        "conversations_read": 1,
        "matched": 1,
        "ignored_unknown": 0,
        "skipped_group": 0,
        "skipped_other": 0,
        "messages_new": 2,
    }
    rows = _interactions(writer, user)
    assert [(r.kind, r.contact_id, r.source) for r in rows] == [
        (InteractionKind.LI_OUT, ada.id, ContactSource.SYNC),
        (InteractionKind.LI_IN, ada.id, ContactSource.SYNC),
    ]
    assert rows[0].at == THEN.replace(microsecond=0)  # whole seconds
    assert rows[0].summary == "LinkedIn message: Invented opener."
    assert rows[1].external_id == "urn:li:msg_message:INVENTEDONE2"
    assert ada.last_contacted_at == THEN.replace(microsecond=0)  # the recency guard sees it

    (row,) = _conversations(writer, user)
    assert row.contact_id == ada.id
    assert row.conversation_urn == "urn:li:msg_conversation:INVENTEDONE"
    assert row.last_outbound_at == THEN
    assert row.last_inbound_at == THEN + timedelta(minutes=1)
    assert row.polled_at == NOW
    assert [n.interaction_id for n in counts.new_inbound] == [rows[1].id]


def test_reapplying_the_same_delta_writes_nothing(writer: Session, user: User) -> None:
    _contact(writer, user, ADA)
    read = delta(a_thread_with(ADA, THEN))
    inbox_apply.apply_delta(writer, user, read, polled_at=NOW)
    again = inbox_apply.apply_delta(writer, user, read, polled_at=NOW + timedelta(hours=3))

    assert again.messages_new == 0 and again.matched == 1
    assert again.new_inbound == []
    assert len(_interactions(writer, user)) == 2
    (row,) = _conversations(writer, user)
    assert row.polled_at == NOW + timedelta(hours=3)


def test_unknown_participants_are_ignored_counted_and_never_created(
    writer: Session, user: User
) -> None:
    _contact(writer, user, ADA)
    before = writer.scalars(scoped(user, Contact)).all()
    counts = inbox_apply.apply_delta(
        writer,
        user,
        delta(a_thread_with(ADA, THEN), a_thread_with(STRANGER, THEN, name="two")),
        polled_at=NOW,
    )

    assert (counts.matched, counts.ignored_unknown, counts.messages_new) == (1, 1, 2)
    assert writer.scalars(scoped(user, Contact)).all() == before
    assert len(_conversations(writer, user)) == 1


def test_matching_is_by_urn_only_never_by_name(writer: Session, user: User) -> None:
    """A contact named like the stranger, but with another URN, is not the stranger."""
    _contact(writer, user, BEN, first_name="Stranger", last_name="Invented")
    _contact(writer, user, None, first_name="Stranger")
    counts = inbox_apply.apply_delta(
        writer, user, delta(a_thread_with(STRANGER, THEN)), polled_at=NOW
    )
    assert (counts.matched, counts.ignored_unknown) == (0, 1)
    assert _interactions(writer, user) == []


def test_group_threads_are_skipped_and_never_attributed(writer: Session, user: User) -> None:
    """The page's own group count is carried, and a thread with a third voice in it is
    one more group thread, not words to pin on the counterpart."""
    _contact(writer, user, ADA)
    _contact(writer, user, BEN)
    third_voice = conversation(
        "group",
        ADA,
        [
            message(1, sender=ADA, at=THEN, tag="group"),
            message(2, sender=BEN, at=THEN + timedelta(minutes=1), tag="group"),
        ],
    )
    counts = inbox_apply.apply_delta(
        writer, user, delta(third_voice, skipped_group=2, skipped_other=3), polled_at=NOW
    )

    assert (counts.skipped_group, counts.skipped_other) == (3, 3)
    assert (counts.matched, counts.messages_new) == (0, 0)
    assert _interactions(writer, user) == []
    assert _conversations(writer, user) == []


def test_a_long_or_empty_snippet_is_bounded(writer: Session, user: User) -> None:
    assert inbox_apply.summary("  ") == "LinkedIn message"
    assert inbox_apply.summary("y" * 200) == "LinkedIn message: " + "y" * 200


def test_a_merged_away_contact_is_not_matched(writer: Session, user: User) -> None:
    survivor = _contact(writer, user, BEN)
    loser = _contact(writer, user, ADA)
    merge(writer, user, survivor.id, loser.id)  # BEN keeps its URN, ADA's is dropped
    counts = inbox_apply.apply_delta(writer, user, delta(a_thread_with(ADA, THEN)), polled_at=NOW)
    assert counts.ignored_unknown == 1


def test_one_users_poll_never_touches_another_users_contacts(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    _contact(writer, other, ADA)
    counts = inbox_apply.apply_delta(writer, user, delta(a_thread_with(ADA, THEN)), polled_at=NOW)
    assert counts.ignored_unknown == 1
    assert _interactions(writer, other) == []


def test_the_reply_hook_gets_only_new_inbound_messages(
    writer: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[inbox_apply.NewInbound, ...]] = []
    monkeypatch.setattr(inbox_apply, "REPLY_HANDLERS", [lambda s, u, new: calls.append(new)])
    _contact(writer, user, ADA)
    read = delta(a_thread_with(ADA, THEN))
    inbox_apply.apply_delta(writer, user, read, polled_at=NOW)
    inbox_apply.apply_delta(writer, user, read, polled_at=NOW)

    assert [len(new) for new in calls] == [1, 0]
    (first,) = calls[0]
    assert first.message_urn == "urn:li:msg_message:INVENTEDONE2"
    assert first.conversation_urn == "urn:li:msg_conversation:INVENTEDONE"


def test_the_reply_hook_list_starts_empty() -> None:
    """P4-02 (#381) adds its handler; until then a poll calls nothing."""
    assert inbox_apply.REPLY_HANDLERS == []


def test_an_external_id_is_unique_per_user(writer: Session, user: User) -> None:
    ada = _contact(writer, user, ADA)
    other = factories.make_user(writer)
    elsewhere = _contact(writer, other, ADA)
    add_interaction(writer, user, ada.id, InteractionKind.LI_IN, NOW, external_id="urn:x")
    add_interaction(writer, other, elsewhere.id, InteractionKind.LI_IN, NOW, external_id="urn:x")
    add_interaction(writer, user, ada.id, InteractionKind.NOTE, NOW)
    add_interaction(writer, user, ada.id, InteractionKind.NOTE, NOW)  # NULLs are distinct
    with pytest.raises(IntegrityError), writer.begin_nested():
        add_interaction(writer, user, ada.id, InteractionKind.LI_IN, NOW, external_id="urn:x")


# --- what a poll asks first -------------------------------------------------------------


def test_watched_urns_are_the_live_enrollments_contacts_on_any_channel(
    writer: Session, user: User
) -> None:
    email = factories.make_campaign(writer, user)
    linkedin = factories.make_campaign(writer, user, channels=(TemplateChannel.LINKEDIN,))
    draft = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    ada, ben = _contact(writer, user, ADA), _contact(writer, user, BEN)
    done = _contact(writer, user, profile_urn("done"))
    drafted = _contact(writer, user, profile_urn("drafted"))
    no_urn = _contact(writer, user, None)
    factories.make_enrollment(writer, email, ada)
    factories.make_enrollment(writer, linkedin, ben, status=EnrollmentStatus.PENDING)
    factories.make_enrollment(writer, email, done, status=EnrollmentStatus.COMPLETED)
    factories.make_enrollment(writer, draft, drafted)
    factories.make_enrollment(writer, email, no_urn)

    assert inbox_apply.watched_urns(writer, user) == {ADA, BEN}
    assert inbox_apply.has_anything_to_watch(writer, user)
    assert not inbox_apply.has_anything_to_watch(writer, factories.make_user(writer))


def test_live_means_what_the_campaign_guards_mean() -> None:
    assert {
        EnrollmentStatus.PENDING,
        EnrollmentStatus.ACTIVE,
        EnrollmentStatus.PAUSED,
    } == campaign_guards.LIVE_ENROLLMENT_STATUSES
    assert {MessageStatus.PREFILLED, MessageStatus.STALE} == inbox_apply.OPEN_FOR_STATUSES


def test_threads_to_open_are_the_newest_five_prefilled_stale_or_live(
    writer: Session, user: User
) -> None:
    campaign = factories.make_campaign(writer, user, channels=(TemplateChannel.LINKEDIN,))
    expected: list[str] = []
    for n in range(7):
        contact = _contact(writer, user, profile_urn(f"p{n}"))
        enrollment = factories.make_enrollment(writer, campaign, contact)
        status = MessageStatus.PREFILLED if n % 2 else MessageStatus.STALE
        urn = f"urn:li:msg_conversation:INVENTEDP{n}"
        sent = factories.make_message(
            writer, enrollment, status=status, sent_at=None, li_conversation_urn=urn
        )
        sent.updated_at = NOW - timedelta(hours=10 - n)
        expected.append(urn)
    sent_one = factories.make_message(
        writer,
        factories.make_enrollment(
            writer, factories.make_campaign(writer, user), _contact(writer, user, BEN)
        ),
        li_conversation_urn="urn:li:msg_conversation:INVENTEDSENT",
    )
    sent_one.updated_at = NOW
    writer.flush()

    opened = inbox_apply.threads_to_open(writer, user)
    assert opened == set(expected[-5:])  # the newest five, a sent message's never

    # A known conversation with a live enrollment's contact qualifies on its own.
    live = _contact(writer, user, ADA)
    factories.make_enrollment(writer, campaign, live)
    inbox_apply.apply_delta(
        writer, user, delta(a_thread_with(ADA, NOW + timedelta(minutes=5))), polled_at=NOW
    )
    assert "urn:li:msg_conversation:INVENTEDONE" in inbox_apply.threads_to_open(writer, user)


# --- the self contact (#342) -------------------------------------------------------------


def test_a_thread_with_the_self_contacts_urn_is_never_attributed_to_it(
    writer: Session, user: User
) -> None:
    """The self contact holds your own details; a thread naming your URN is not a contact's."""
    me = _contact(writer, user, OWNER_URN, is_self=True)
    # Written directly: add_interaction refuses the self contact.
    writer.add(
        Interaction(
            user_id=user.id,
            contact_id=me.id,
            kind=InteractionKind.LI_IN,
            at=THEN.replace(microsecond=0),
            summary="Invented archive row.",
            source=ContactSource.ARCHIVE,
        )
    )
    # Not reachable through the app, but even enrolled it is never watched or matched.
    factories.make_enrollment(
        writer, factories.make_campaign(writer, user, channels=(TemplateChannel.LINKEDIN,)), me
    )
    writer.add(
        LiConversation(
            user_id=user.id,
            contact_id=me.id,
            conversation_urn="urn:li:msg_conversation:INVENTEDSELF",
            last_activity_at=NOW,
            polled_at=NOW,
        )
    )
    writer.flush()

    assert inbox_apply.watched_urns(writer, user) == frozenset()
    assert not inbox_apply.has_anything_to_watch(writer, user)
    assert inbox_apply.threads_to_open(writer, user) == frozenset()

    counts = inbox_apply.apply_delta(
        writer, user, delta(a_thread_with(OWNER_URN, THEN)), polled_at=NOW
    )
    assert (counts.matched, counts.ignored_unknown, counts.messages_new) == (0, 1, 0)
    assert counts.new_inbound == []
    rows = _interactions(writer, user)
    assert [(r.source, r.external_id) for r in rows] == [(ContactSource.ARCHIVE, None)]
    assert [c.conversation_urn for c in _conversations(writer, user)] == [
        "urn:li:msg_conversation:INVENTEDSELF"
    ]


# --- an ended or archived campaign (#345) -----------------------------------------------


@pytest.mark.parametrize("status", [CampaignStatus.COMPLETED, CampaignStatus.ARCHIVED])
def test_an_ended_campaigns_live_enrollment_is_watched_like_a_completed_one(
    writer: Session, user: User, status: CampaignStatus
) -> None:
    """As the Gmail reply poll reads it: the first poll's since covers its sends for the
    window after the latest, and its contact is never a live watch."""
    ended = factories.make_campaign(writer, user, status=status)
    recent = factories.make_enrollment(writer, ended, _contact(writer, user, ADA))
    old = factories.make_enrollment(writer, ended, _contact(writer, user, BEN))
    factories.make_message(writer, recent, sent_at=NOW - timedelta(days=45))
    factories.make_message(writer, recent, position=1, sent_at=NOW - timedelta(days=10))
    factories.make_message(writer, old, sent_at=NOW - timedelta(days=60))

    assert inbox_apply.first_live_outreach(writer, user, now=NOW) == NOW - timedelta(days=45)
    assert inbox_apply.watched_urns(writer, user) == frozenset()
    assert not inbox_apply.has_anything_to_watch(writer, user)


# --- the archive and the poll never record one message twice ---------------------------


def _write_archive(root: Path, *, at: str) -> Path:
    root.mkdir()
    (root / "Connections.csv").write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Fictional,https://www.linkedin.com/in/ada-fictional,,Works,Eng,12 Mar 2019\n",
        encoding="utf-8",
    )
    (root / "messages.csv").write_text(
        "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,"
        "RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER\n"
        "c1,,Nettie Keeperton,https://www.linkedin.com/in/nettie-keeperton,Ada Fictional,"
        f"https://www.linkedin.com/in/ada-fictional,{at},,Invented opener.,INBOX\n",
        encoding="utf-8",
    )
    return root


def _archive(writer: Session, user: User, root: Path) -> int:
    with open_archive(root) as opened:
        report = import_archive(
            writer, user, opened, observed_at=NOW, owner_public_id="nettie-keeperton"
        )
    return report.messages.added


def test_an_archive_import_after_a_poll_adds_nothing_at_the_same_second(
    writer: Session, user: User, tmp_path: Path
) -> None:
    ada = _contact(writer, user, ADA, li_public_id="ada-fictional")
    sent = conversation("one", ADA, [message(1, sender=ADA, at=THEN, outbound=True)])
    inbox_apply.apply_delta(writer, user, delta(sent), polled_at=NOW)

    root = _write_archive(tmp_path / "export", at="2026-09-22 14:22:10 UTC")
    assert _archive(writer, user, root) == 0
    rows = [r for r in _interactions(writer, user) if r.contact_id == ada.id]
    assert [(r.kind, r.source) for r in rows] == [(InteractionKind.LI_OUT, ContactSource.SYNC)]


def test_an_archive_import_after_a_poll_still_adds_another_second(
    writer: Session, user: User, tmp_path: Path
) -> None:
    """Anti-coincidence: the ledger matches the second, not every message."""
    _contact(writer, user, ADA, li_public_id="ada-fictional")
    sent = conversation("one", ADA, [message(1, sender=ADA, at=THEN, outbound=True)])
    inbox_apply.apply_delta(writer, user, delta(sent), polled_at=NOW)

    root = _write_archive(tmp_path / "export", at="2026-09-22 14:22:11 UTC")
    assert _archive(writer, user, root) == 1


def test_a_poll_after_an_archive_import_adds_nothing_at_the_same_second(
    writer: Session, user: User, tmp_path: Path
) -> None:
    ada = _contact(writer, user, ADA, li_public_id="ada-fictional")
    assert _archive(writer, user, _write_archive(tmp_path / "export", at="2026-09-22 14:22:10 UTC"))
    sent = conversation("one", ADA, [message(1, sender=ADA, at=THEN, outbound=True)])

    for _ in range(2):  # and again on the next poll
        counts = inbox_apply.apply_delta(writer, user, delta(sent), polled_at=NOW)
        assert counts.messages_new == 0
    rows = [r for r in _interactions(writer, user) if r.contact_id == ada.id]
    assert [(r.kind, r.source) for r in rows] == [(InteractionKind.LI_OUT, ContactSource.ARCHIVE)]


# --- a merge moves conversations --------------------------------------------------------


def test_merge_moves_conversations_to_the_survivor(writer: Session, user: User) -> None:
    survivor = _contact(writer, user, BEN)
    loser = _contact(writer, user, ADA)
    inbox_apply.apply_delta(
        writer,
        user,
        delta(a_thread_with(ADA, THEN, name="ada"), a_thread_with(BEN, THEN, name="ben")),
        polled_at=NOW,
    )
    merge(writer, user, survivor.id, loser.id)

    assert {(r.conversation_urn, r.contact_id) for r in _conversations(writer, user)} == {
        ("urn:li:msg_conversation:INVENTEDADA", survivor.id),
        ("urn:li:msg_conversation:INVENTEDBEN", survivor.id),
    }
    assert {r.contact_id for r in _interactions(writer, user)} == {survivor.id}


def test_merge_keeps_the_survivors_conversation_on_a_clash(writer: Session, user: User) -> None:
    """A conversation URN is unique per user, so a clash cannot be stored; the merge
    still keeps the survivor's row should one ever reach it, and the database refuses
    the second row outright."""
    survivor = _contact(writer, user, BEN)
    loser = _contact(writer, user, ADA)
    inbox_apply.apply_delta(writer, user, delta(a_thread_with(BEN, THEN)), polled_at=NOW)
    (kept,) = _conversations(writer, user)
    with pytest.raises(IntegrityError), writer.begin_nested():
        writer.add(
            LiConversation(
                user_id=user.id,
                contact_id=loser.id,
                conversation_urn=kept.conversation_urn,
                last_activity_at=NOW,
                polled_at=NOW,
            )
        )
        writer.flush()
    merge(writer, user, survivor.id, loser.id)
    (after,) = _conversations(writer, user)
    assert (after.id, after.contact_id) == (kept.id, survivor.id)


def test_a_conversation_goes_with_its_contact(writer: Session, user: User) -> None:
    ada = _contact(writer, user, ADA)
    inbox_apply.apply_delta(writer, user, delta(a_thread_with(ADA, THEN)), polled_at=NOW)
    writer.delete(ada)
    writer.flush()
    writer.expire_all()
    assert _conversations(writer, user) == []


def test_an_archived_inbound_message_is_adopted_and_still_reaches_the_reply_hook(
    writer: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#388 review, S2: a reply the archive already recorded is the poll's reply too."""
    calls: list[tuple[inbox_apply.NewInbound, ...]] = []
    monkeypatch.setattr(inbox_apply, "REPLY_HANDLERS", [lambda s, u, new: calls.append(new)])
    ada = _contact(writer, user, ADA)
    archived = add_interaction(
        writer,
        user,
        ada.id,
        InteractionKind.LI_IN,
        THEN.replace(microsecond=0),
        "Invented reply.",
        source=ContactSource.ARCHIVE,
    )
    reply = conversation("one", ADA, [message(2, sender=ADA, at=THEN, tag="one")])
    first = inbox_apply.apply_delta(writer, user, delta(reply), polled_at=NOW)
    again = inbox_apply.apply_delta(writer, user, delta(reply), polled_at=NOW)

    assert first.messages_new == 0 and again.messages_new == 0
    assert [[n.interaction_id for n in new] for new in calls] == [[archived.id], []]
    assert archived.external_id == "urn:li:msg_message:INVENTEDONE2"
    assert [r.id for r in _interactions(writer, user)] == [archived.id]


def test_an_archived_invitation_is_never_taken_for_a_polled_message(
    writer: Session, user: User
) -> None:
    """N1: an invitation row at the same second is not a message, in either direction."""
    ada = _contact(writer, user, ADA)
    at = THEN.replace(microsecond=0)
    add_interaction(
        writer,
        user,
        ada.id,
        InteractionKind.LI_OUT,
        at,
        INVITATION_SUMMARY,
        source=ContactSource.ARCHIVE,
    )
    sent = conversation("one", ADA, [message(1, sender=ADA, at=THEN, outbound=True)])
    counts = inbox_apply.apply_delta(writer, user, delta(sent), polled_at=NOW)
    assert counts.messages_new == 1

    # The other way: an archive invitation after a polled message at the same second.
    ben = _contact(writer, user, BEN)
    to_ben = conversation("two", BEN, [message(1, sender=BEN, at=THEN, outbound=True, tag="b")])
    inbox_apply.apply_delta(writer, user, delta(to_ben), polled_at=NOW)
    ledger = archive_module._Interactions(writer, user)
    assert ledger.add(ben.id, InteractionKind.LI_OUT, at, f"{INVITATION_SUMMARY}: invented note")
    assert not ledger.add(ben.id, InteractionKind.LI_OUT, at, "Invented opener.")  # a message is


def test_an_archive_row_that_already_has_a_urn_is_never_adopted(
    writer: Session, user: User
) -> None:
    """An archive row another message already adopted stands for that message only."""
    ada = _contact(writer, user, ADA)
    add_interaction(
        writer,
        user,
        ada.id,
        InteractionKind.LI_IN,
        THEN.replace(microsecond=0),
        "Invented reply.",
        source=ContactSource.ARCHIVE,
        external_id="urn:li:msg_message:INVENTEDEARLIER",
    )
    other = conversation("one", ADA, [message(2, sender=ADA, at=THEN, tag="one")])
    counts = inbox_apply.apply_delta(writer, user, delta(other), polled_at=NOW)
    assert counts.messages_new == 1
    assert len(_interactions(writer, user)) == 2


def test_first_live_outreach_watches_what_the_reply_poll_watches(
    writer: Session, user: User
) -> None:
    campaign = factories.make_campaign(writer, user)
    live = factories.make_enrollment(writer, campaign, _contact(writer, user, ADA))
    recent = factories.make_enrollment(
        writer, campaign, _contact(writer, user, BEN), status=EnrollmentStatus.COMPLETED
    )
    stale = factories.make_enrollment(
        writer,
        campaign,
        _contact(writer, user, profile_urn("stale")),
        status=EnrollmentStatus.COMPLETED,
    )
    assert inbox_apply.first_live_outreach(writer, user, now=NOW) is None
    factories.make_message(writer, live, sent_at=NOW - timedelta(days=5))
    # A reply is inbound: never the start of what the poll watches.
    factories.make_message(
        writer,
        live,
        direction=MessageDirection.IN,
        status=MessageStatus.RECEIVED,
        sent_at=NOW - timedelta(days=50),
    )
    assert inbox_apply.first_live_outreach(writer, user, now=NOW) == NOW - timedelta(days=5)

    # A completed enrollment whose latest send is within the window counts from its first.
    factories.make_message(writer, recent, sent_at=NOW - timedelta(days=40))
    factories.make_message(writer, recent, position=1, sent_at=NOW - timedelta(days=20))
    assert inbox_apply.first_live_outreach(writer, user, now=NOW) == NOW - timedelta(days=40)

    # One whose latest send is past it does not.
    factories.make_message(writer, stale, sent_at=NOW - timedelta(days=90))
    assert timedelta(days=30) == WATCH_AFTER_COMPLETED
    assert inbox_apply.first_live_outreach(writer, user, now=NOW) == NOW - timedelta(days=40)


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (INVITATION_SUMMARY, True),
        (f"{INVITATION_SUMMARY}: an invented note", True),
        (f"{INVITATION_SUMMARY}:\nhello", True),
        (f"{INVITATION_SUMMARY} requests are piling up, an invented line", False),
        (None, False),
    ],
)
def test_the_python_and_sql_invitation_checks_agree(
    writer: Session, user: User, summary: str | None, expected: bool
) -> None:
    """One rule, two readers: ``summary_is_invitation`` matches ``is_invitation()``."""
    ada = _contact(writer, user, ADA)
    row = add_interaction(writer, user, ada.id, InteractionKind.LI_OUT, NOW, summary)
    in_sql = writer.scalars(
        scoped(user, Interaction).where(Interaction.id == row.id, is_invitation())
    ).all()
    assert summary_is_invitation(summary) is expected
    assert bool(in_sql) is expected
