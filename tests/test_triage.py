"""netkeeper.crm.triage (spec 10.2): the queue, evidence, decisions, undo, the suggestion."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import triage as module
from netkeeper.crm.identity import IncomingContact, apply, resolve
from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.triage import (
    CountChanged,
    InvalidDecision,
    NotFound,
    NothingToUndo,
    UndoConflict,
)
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactMet,
    ContactSource,
    Interaction,
    InteractionKind,
    TriageDecision,
    TriageDecisionKind,
    User,
)
from netkeeper.scoping import scoped

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=30)
LONG_AGO = NOW - timedelta(days=400)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller that writes must use."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _contacts(writer: Session, user: User, count: int, **overrides: Any) -> list[Contact]:
    return [factories.make_contact(writer, user, **overrides) for _ in range(count)]


def _message(
    writer: Session,
    user: User,
    contact: Contact,
    *,
    outbound: bool = True,
    at: datetime = EARLIER,
    summary: str = "a message",
) -> Interaction:
    kind = InteractionKind.LI_OUT if outbound else InteractionKind.LI_IN
    return add_interaction(
        writer, user, contact.id, kind, at, summary, source=ContactSource.ARCHIVE
    )


def _queue_ids(
    writer: Session, user: User, states: Sequence[ContactMet] = module.DEFAULT_QUEUE_STATES
) -> list[int]:
    """Every contact the queue would hand out, in order, by walking the cursor."""
    ids: list[int] = []
    after: int | None = None
    while True:
        contact = module.next_contact(writer, user, states=states, after_id=after)
        if contact is None:
            return ids
        ids.append(contact.id)
        after = contact.id


# --- the queue --------------------------------------------------------------


def test_the_queue_is_the_untriaged_in_id_order(writer: Session, user: User) -> None:
    first, second, third = _contacts(writer, user, 3)
    assert _queue_ids(writer, user) == [first.id, second.id, third.id]
    module.decide(writer, user, second.id, ContactMet.MET)
    assert _queue_ids(writer, user) == [first.id, third.id]


def test_the_cursor_moves_on_without_deciding(writer: Session, user: User) -> None:
    first, second, _third = _contacts(writer, user, 3)
    moved = module.next_contact(writer, user, after_id=first.id)
    assert moved is not None and moved.id == second.id
    assert first.met is ContactMet.UNKNOWN  # the arrow key decides nothing


def test_the_queue_can_revisit_the_skipped(writer: Session, user: User) -> None:
    first, second = _contacts(writer, user, 2)
    module.decide(writer, user, first.id, ContactMet.SKIP)
    module.decide(writer, user, second.id, ContactMet.MET)
    assert _queue_ids(writer, user, states=(ContactMet.SKIP,)) == [first.id]


def test_archived_and_merged_contacts_are_not_in_the_queue(writer: Session, user: User) -> None:
    live, archived, merged = _contacts(writer, user, 3)
    archived.archived_at = NOW
    merged.merged_into_id = live.id
    writer.flush()
    assert _queue_ids(writer, user) == [live.id]


def test_the_queue_is_one_user_s(writer: Session, user: User) -> None:
    mine = factories.make_contact(writer, user)
    other = factories.make_user(writer)
    factories.make_contact(writer, other)
    assert _queue_ids(writer, user) == [mine.id]
    assert module.next_contact(writer, other) is not None


def test_progress_counts_the_live_contacts_by_state(writer: Session, user: User) -> None:
    first, second, third = _contacts(writer, user, 3)
    module.decide(writer, user, first.id, ContactMet.MET)
    module.decide(writer, user, second.id, ContactMet.SKIP)
    counters = module.progress(writer, user)
    assert (counters.total, counters.triaged, counters.remaining) == (3, 2, 1)
    assert counters.by_state[ContactMet.MET] == 1
    assert counters.by_state[ContactMet.UNKNOWN] == 1
    skipped = module.progress(writer, user, states=(ContactMet.SKIP,))
    assert skipped.remaining == 1
    assert third.met is ContactMet.UNKNOWN


# --- evidence ---------------------------------------------------------------


def test_a_card_carries_the_message_history(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    _message(writer, user, contact, outbound=True, at=LONG_AGO, summary="first hello")
    _message(writer, user, contact, outbound=False, at=EARLIER, summary="a reply")
    _message(writer, user, contact, outbound=False, at=NOW, summary="another reply")
    add_interaction(writer, user, contact.id, InteractionKind.LI_VIEW, NOW)
    card = module.load_card(writer, user, contact)
    messages = card.evidence.messages
    assert (messages.total, messages.outbound, messages.inbound) == (3, 1, 2)
    assert (messages.first_at, messages.last_at) == (LONG_AGO, NOW)
    assert [row.summary for row in messages.recent] == ["another reply", "a reply", "first hello"]


def test_a_card_with_no_messages_reports_nothing(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    messages = module.load_card(writer, user, contact).evidence.messages
    assert (messages.total, messages.inbound, messages.outbound) == (0, 0, 0)
    assert messages.first_at is None and messages.last_at is None and messages.recent == []


def test_a_card_carries_the_timeline_and_the_notes(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user, notes="knows the org inside out")
    add_interaction(writer, user, contact.id, InteractionKind.NOTE, EARLIER, "a note")
    _message(writer, user, contact, at=NOW)
    card = module.load_card(writer, user, contact)
    assert [entry.kind for entry in card.evidence.timeline] == ["interaction", "interaction"]
    assert [entry.at for entry in card.evidence.timeline] == [NOW, EARLIER]
    assert card.contact.notes == "knows the org inside out"


def test_shared_companies_count_the_rest_of_the_address_book(writer: Session, user: User) -> None:
    contact = factories.make_contact(
        writer,
        user,
        current_company="Northwind Pottery",
        positions=[{"title": "Potter", "company": "Blue Harbor Tools", "is_current": False}],
    )
    factories.make_contact(writer, user, current_company="northwind pottery", met=ContactMet.MET)
    factories.make_contact(writer, user, current_company="Northwind Pottery")
    factories.make_contact(writer, user, current_company="Blue Harbor Tools", met=ContactMet.MET)
    factories.make_contact(writer, user, current_company="Somewhere Else")
    shared = {
        item.company: item
        for item in module.load_card(writer, user, contact).evidence.shared_companies
    }
    assert shared["Northwind Pottery"].contact_count == 2
    assert shared["Northwind Pottery"].met_count == 1
    assert shared["Blue Harbor Tools"].contact_count == 1
    assert shared["Blue Harbor Tools"].met_count == 1


def test_shared_companies_never_count_another_user_s_contacts(writer: Session, user: User) -> None:
    mine = factories.make_contact(writer, user, current_company="Northwind Pottery")
    other = factories.make_user(writer)
    factories.make_contact(writer, other, current_company="Northwind Pottery")
    shared = module.load_card(writer, user, mine).evidence.shared_companies
    assert [(item.company, item.contact_count) for item in shared] == [("Northwind Pottery", 0)]


# --- decisions --------------------------------------------------------------


def test_deciding_writes_met_and_logs_what_it_changed(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    decision = module.decide(writer, user, contact.id, ContactMet.MET, at=NOW)
    assert contact.met is ContactMet.MET
    assert contact.triaged_at == NOW
    assert decision.kind is TriageDecisionKind.DECIDE
    assert decision.before_state == {"met": "unknown", "triaged_at": None}
    assert decision.after_state == {"met": "met", "triaged_at": NOW.isoformat()}
    assert decision.undone_at is None


def test_unknown_is_not_a_decision(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    with pytest.raises(InvalidDecision):
        module.decide(writer, user, contact.id, ContactMet.UNKNOWN)


def test_deciding_another_user_s_contact_is_not_found(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    theirs = factories.make_contact(writer, other)
    with pytest.raises(NotFound):
        module.decide(writer, user, theirs.id, ContactMet.MET)
    assert theirs.met is ContactMet.UNKNOWN


def test_writes_need_a_writer_session(session: Session, writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    writer.commit()
    reader_user = session.get(User, user.id)
    assert reader_user is not None
    for call in (
        lambda: module.decide(session, reader_user, contact.id, ContactMet.MET),
        lambda: module.set_preferred_name(session, reader_user, contact.id, "Bo"),
        lambda: module.undo(session, reader_user),
        lambda: module.apply_suggestion(session, reader_user, module.SUGGESTION_MET_WITH_MESSAGES),
    ):
        with pytest.raises(RuntimeError, match="writer session"):
            call()


# --- the preferred-name edit ------------------------------------------------


def test_the_preferred_name_edit_sticks_through_a_later_sync(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user, first_name="Roberta", last_name="Quill")
    module.set_preferred_name(writer, user, contact.id, "Bobbie")
    incoming = IncomingContact(
        source=ContactSource.SYNC,
        observed_at=NOW,
        li_urn=contact.li_urn,
        first_name="Roberta",
        last_name="Quill",
        headline="Now at Northwind Pottery",
    )
    apply(writer, user, incoming, resolve(writer, user, incoming))
    assert contact.preferred_name == "Bobbie"  # manual wins for a person-owned field (spec 10.5)
    assert contact.headline == "Now at Northwind Pottery"


def test_an_empty_preferred_name_falls_back_to_the_first_name(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user, first_name="Roberta")
    module.set_preferred_name(writer, user, contact.id, "Bobbie")
    decision = module.set_preferred_name(writer, user, contact.id, "")
    assert contact.preferred_name == "Roberta"
    assert decision.before_state == {"preferred_name": "Bobbie"}
    assert decision.after_state == {"preferred_name": "Roberta"}


def test_renaming_another_user_s_contact_is_not_found(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    theirs = factories.make_contact(writer, other, first_name="Roberta")
    with pytest.raises(NotFound):
        module.set_preferred_name(writer, user, theirs.id, "Bobbie")
    assert theirs.preferred_name == "Roberta"


# --- undo -------------------------------------------------------------------


def test_undo_restores_the_previous_state_exactly(writer: Session, user: User) -> None:
    """Not "back to untriaged": back to whatever the contact held, timestamp included."""
    contact = factories.make_contact(writer, user)
    module.decide(writer, user, contact.id, ContactMet.NOT_MET, at=EARLIER)
    module.decide(writer, user, contact.id, ContactMet.MET, at=NOW)
    undone = module.undo(writer, user)
    assert contact.met is ContactMet.NOT_MET
    assert contact.triaged_at == EARLIER
    assert undone.decisions == 1
    assert undone.contact is not None and undone.contact.id == contact.id
    assert undone.forced == []


def test_undo_survives_a_decision_made_in_another_timezone(
    session_factory: sessionmaker[Session], writer: Session, user: User
) -> None:
    """The log stores moments in UTC, so the column read back later compares equal.

    Without that, undoing a decision made at an offset would report a conflict
    against the very instant it wrote.
    """
    elsewhere = NOW.astimezone(ZoneInfo("Australia/Sydney"))
    contact = factories.make_contact(writer, user)
    module.decide(writer, user, contact.id, ContactMet.NOT_MET, at=EARLIER)
    module.decide(writer, user, contact.id, ContactMet.MET, at=elsewhere)
    writer.commit()
    # A fresh session, as the next request would have: the column comes back UTC.
    with session_scope(session_factory, write=True) as later:
        reloaded = later.get(User, user.id)
        assert reloaded is not None
        module.undo(later, reloaded)
    writer.expire_all()
    assert _met(contact) is ContactMet.NOT_MET
    assert contact.triaged_at == EARLIER


def test_undo_walks_back_one_decision_at_a_time(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    module.decide(writer, user, contact.id, ContactMet.NOT_MET, at=EARLIER)
    module.decide(writer, user, contact.id, ContactMet.MET, at=NOW)
    module.undo(writer, user)
    module.undo(writer, user)
    assert contact.met is ContactMet.UNKNOWN
    assert contact.triaged_at is None
    with pytest.raises(NothingToUndo):
        module.undo(writer, user)


def test_the_second_undo_reaches_the_decision_before_it(writer: Session, user: User) -> None:
    first, second = _contacts(writer, user, 2)
    module.decide(writer, user, first.id, ContactMet.MET)
    module.decide(writer, user, second.id, ContactMet.NOT_MET)
    module.undo(writer, user)
    assert (first.met, second.met) == (ContactMet.MET, ContactMet.UNKNOWN)
    module.undo(writer, user)
    assert (first.met, second.met) == (ContactMet.UNKNOWN, ContactMet.UNKNOWN)


def test_undo_puts_the_contact_back_in_its_queue_position(writer: Session, user: User) -> None:
    first, second, third = _contacts(writer, user, 3)
    before = _queue_ids(writer, user)
    module.decide(writer, user, second.id, ContactMet.MET)
    module.undo(writer, user)
    assert _queue_ids(writer, user) == before == [first.id, second.id, third.id]


def test_undo_takes_back_the_newest_action_whatever_kind_it_was(
    writer: Session, user: User
) -> None:
    contact = factories.make_contact(writer, user, first_name="Roberta")
    module.decide(writer, user, contact.id, ContactMet.MET, at=NOW)
    module.set_preferred_name(writer, user, contact.id, "Bobbie")
    undone = module.undo(writer, user)
    assert undone.kind is TriageDecisionKind.PREFERRED_NAME
    assert contact.preferred_name == "Roberta"
    assert _met(contact) is ContactMet.MET  # the decision is still standing
    module.undo(writer, user)
    assert _met(contact) is ContactMet.UNKNOWN


def test_undo_refuses_when_the_contact_changed_after_the_decision(
    writer: Session, user: User
) -> None:
    contact = factories.make_contact(writer, user)
    module.decide(writer, user, contact.id, ContactMet.MET, at=NOW)
    contact.met = ContactMet.NOT_MET  # something else edited it in between
    writer.flush()
    with pytest.raises(UndoConflict) as caught:
        module.undo(writer, user)
    assert caught.value.field == "met"
    assert (caught.value.expected, caught.value.found) == ("met", "not_met")
    assert contact.met is ContactMet.NOT_MET  # nothing was written
    assert _open_decisions(writer, user) == 1  # and the decision is still on the stack


def test_a_forced_undo_restores_anyway_and_says_so(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    module.decide(writer, user, contact.id, ContactMet.MET, at=NOW)
    contact.met = ContactMet.NOT_MET
    writer.flush()
    undone = module.undo(writer, user, force=True)
    assert undone.forced == [contact.id]
    assert contact.met is ContactMet.UNKNOWN
    assert _open_decisions(writer, user) == 0


def test_undo_never_reaches_another_user_s_decisions(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    theirs = factories.make_contact(writer, other)
    module.decide(writer, other, theirs.id, ContactMet.MET)
    with pytest.raises(NothingToUndo):
        module.undo(writer, user)
    assert theirs.met is ContactMet.MET


# --- the bulk suggestion ----------------------------------------------------


def test_the_suggestion_counts_the_untriaged_with_message_history(
    writer: Session, user: User
) -> None:
    with_messages, also_with_messages, quiet, decided = _contacts(writer, user, 4)
    _message(writer, user, with_messages)
    _message(writer, user, also_with_messages, outbound=False)
    _message(writer, user, decided)
    add_interaction(writer, user, quiet.id, InteractionKind.NOTE, NOW, "a note, not a message")
    module.decide(writer, user, decided.id, ContactMet.NOT_MET)
    (suggestion,) = module.suggestions(writer, user)
    assert suggestion.key == module.SUGGESTION_MET_WITH_MESSAGES
    assert suggestion.count == 2
    assert "2" in suggestion.description


def test_no_suggestion_when_it_would_touch_nobody(writer: Session, user: User) -> None:
    factories.make_contact(writer, user)
    assert module.suggestions(writer, user) == []


def test_the_suggestion_is_never_applied_by_looking_at_it(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    _message(writer, user, contact)
    module.suggestions(writer, user)
    assert contact.met is ContactMet.UNKNOWN
    assert _open_decisions(writer, user) == 0


def test_applying_the_suggestion_marks_them_met_as_one_batch(writer: Session, user: User) -> None:
    first, second, quiet = _contacts(writer, user, 3)
    _message(writer, user, first)
    _message(writer, user, second, outbound=False)
    applied = module.apply_suggestion(
        writer, user, module.SUGGESTION_MET_WITH_MESSAGES, expected_count=2, at=NOW
    )
    assert applied.applied == 2
    assert (first.met, second.met, quiet.met) == (
        ContactMet.MET,
        ContactMet.MET,
        ContactMet.UNKNOWN,
    )
    rows = writer.scalars(scoped(user, TriageDecision)).all()
    assert {row.batch_id for row in rows} == {applied.batch_id}
    assert all(row.kind is TriageDecisionKind.BULK_MET for row in rows)


def test_one_undo_takes_the_whole_batch_back_to_where_each_contact_was(
    writer: Session, user: User
) -> None:
    fresh, skipped = _contacts(writer, user, 2)
    _message(writer, user, fresh)
    _message(writer, user, skipped)
    module.decide(writer, user, skipped.id, ContactMet.SKIP, at=EARLIER)
    module.apply_suggestion(
        writer,
        user,
        module.SUGGESTION_MET_WITH_MESSAGES,
        states=(ContactMet.UNKNOWN, ContactMet.SKIP),
        at=NOW,
    )
    assert (fresh.met, skipped.met) == (ContactMet.MET, ContactMet.MET)
    undone = module.undo(writer, user)
    assert undone.decisions == 2
    assert undone.contact is None
    assert undone.batch_id is not None
    assert (fresh.met, fresh.triaged_at) == (ContactMet.UNKNOWN, None)
    assert (skipped.met, skipped.triaged_at) == (ContactMet.SKIP, EARLIER)


def test_a_stale_count_stops_the_apply(writer: Session, user: User) -> None:
    first, second = _contacts(writer, user, 2)
    _message(writer, user, first)
    _message(writer, user, second)
    with pytest.raises(CountChanged) as caught:
        module.apply_suggestion(writer, user, module.SUGGESTION_MET_WITH_MESSAGES, expected_count=1)
    assert (caught.value.expected, caught.value.found) == (1, 2)
    assert (first.met, second.met) == (ContactMet.UNKNOWN, ContactMet.UNKNOWN)


def test_an_unknown_suggestion_key_is_refused(writer: Session, user: User) -> None:
    with pytest.raises(InvalidDecision):
        module.apply_suggestion(writer, user, "mark-everyone-as-a-friend")


def test_the_suggestion_never_reaches_another_user_s_contacts(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    theirs = factories.make_contact(writer, other)
    _message(writer, other, theirs)
    assert module.suggestions(writer, user) == []
    applied = module.apply_suggestion(writer, user, module.SUGGESTION_MET_WITH_MESSAGES)
    assert applied.applied == 0
    assert theirs.met is ContactMet.UNKNOWN


def _met(contact: Contact) -> ContactMet:
    """``contact.met`` as a plain enum, so a check after a write is not narrowed away."""
    return ContactMet(contact.met)


def _open_decisions(writer: Session, user: User) -> int:
    rows = writer.scalars(
        scoped(user, TriageDecision).where(TriageDecision.undone_at.is_(None))
    ).all()
    return len(rows)
