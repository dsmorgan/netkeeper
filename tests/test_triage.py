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
from netkeeper.crm.identity import IncomingContact, apply, merge, resolve
from netkeeper.crm.interactions import INVITATION_SUMMARY, add_interaction
from netkeeper.crm.tags import create_rule, create_tag, run_rules, tag_contact
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
    MetSource,
    RuleField,
    Tag,
    TagMetSignal,
    TriageDecision,
    TriageDecisionKind,
    User,
)
from netkeeper.scoping import scoped

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(days=30)
LONG_AGO = NOW - timedelta(days=400)

# Far above any queue a test builds, low enough to fail fast when the cursor sticks.
_QUEUE_WALK_LIMIT = 1000


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
    writer: Session,
    user: User,
    states: Sequence[ContactMet] = module.DEFAULT_QUEUE_STATES,
    decided_by: MetSource | None = None,
) -> list[int]:
    """Every contact the queue would hand out, in order, by walking the cursor.

    Bounded rather than ``while True``: a cursor that stopped advancing would
    otherwise hang the whole suite instead of failing, and under a random test
    order that is a CI timeout with nothing to read.
    """
    ids: list[int] = []
    after: int | None = None
    for _ in range(_QUEUE_WALK_LIMIT):
        contact = module.next_contact(
            writer, user, states=states, after_id=after, decided_by=decided_by
        )
        if contact is None:
            return ids
        ids.append(contact.id)
        after = contact.id
    raise AssertionError(
        f"the queue cursor did not reach the end in {_QUEUE_WALK_LIMIT} steps; "
        f"it is not advancing (last ids: {ids[-5:]})"
    )


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
    assert contact.met_source is MetSource.MANUAL
    assert contact.triaged_at == NOW
    assert decision.kind is TriageDecisionKind.DECIDE
    assert decision.before_state == {
        "met": "unknown",
        "met_source": "manual",
        "triaged_at": None,
    }
    assert decision.after_state == {
        "met": "met",
        "met_source": "manual",
        "triaged_at": NOW.isoformat(),
    }
    assert decision.reason is None, "a decision by hand came from no suggestion"
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


def test_undo_refuses_when_the_contact_was_merged_away_after_the_decision(
    writer: Session, user: User
) -> None:
    """The loser still holds what the decision left, but the survivor carries the decision.

    ``merge`` copies ``met`` and ``triaged_at`` onto the survivor and leaves the
    loser's columns alone, so comparing the recorded fields alone sees nothing
    wrong. Restoring the loser would clear the decision on a row nobody reads
    while the survivor kept it, and spend the row so no second undo could reach
    it.
    """
    survivor, loser = _contacts(writer, user, 2)
    module.decide(writer, user, loser.id, ContactMet.MET, at=NOW)
    merge(writer, user, survivor.id, loser.id)
    assert survivor.met is ContactMet.MET  # the decision now lives on the survivor
    with pytest.raises(UndoConflict) as caught:
        module.undo(writer, user)
    assert caught.value.field == "merged_into_id"
    assert _met(survivor) is ContactMet.MET
    assert _met(loser) is ContactMet.MET
    assert _open_decisions(writer, user) == 1  # still there to undo once the merge is dealt with


def test_undo_refuses_when_the_contact_was_archived_after_the_decision(
    writer: Session, user: User
) -> None:
    """Restoring it would claim a queue position that ``next_contact`` never serves."""
    first, second, third = _contacts(writer, user, 3)
    module.decide(writer, user, second.id, ContactMet.MET, at=NOW)
    second.archived_at = NOW
    writer.flush()
    with pytest.raises(UndoConflict) as caught:
        module.undo(writer, user)
    assert caught.value.field == "archived_at"
    assert "queue" in caught.value.reason
    assert _met(second) is ContactMet.MET
    assert _queue_ids(writer, user) == [first.id, third.id]


def test_a_forced_undo_restores_an_archived_contact_and_names_it(
    writer: Session, user: User
) -> None:
    """``force`` still gets through; ``forced`` is how the client knows it is not in the queue."""
    first, second, third = _contacts(writer, user, 3)
    module.decide(writer, user, second.id, ContactMet.MET, at=NOW)
    second.archived_at = NOW
    writer.flush()
    undone = module.undo(writer, user, force=True)
    assert undone.forced == [second.id]
    assert _met(second) is ContactMet.UNKNOWN
    assert second.triaged_at is None
    assert _queue_ids(writer, user) == [first.id, third.id]  # restored, but still archived


def test_a_forced_undo_restores_a_merged_away_contact(writer: Session, user: User) -> None:
    survivor, loser = _contacts(writer, user, 2)
    module.decide(writer, user, loser.id, ContactMet.MET, at=NOW)
    merge(writer, user, survivor.id, loser.id)
    undone = module.undo(writer, user, force=True)
    assert undone.forced == [loser.id]
    assert _met(loser) is ContactMet.UNKNOWN
    assert _met(survivor) is ContactMet.MET  # forcing never reaches the survivor


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
    """A contact no batch covers: a note on file is not evidence either way."""
    contact = factories.make_contact(writer, user)
    add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW, "a note, not a message")
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


# --- the rest of the catalogue ----------------------------------------------


def _invitation(
    writer: Session,
    user: User,
    contact: Contact,
    *,
    note: str | None = None,
    outbound: bool = False,
    at: datetime = EARLIER,
) -> Interaction:
    """An invitation as the archive importer writes one: the marker, then any note."""
    summary = INVITATION_SUMMARY if note is None else f"{INVITATION_SUMMARY}: {note}"
    kind = InteractionKind.LI_OUT if outbound else InteractionKind.LI_IN
    return add_interaction(
        writer, user, contact.id, kind, at, summary, source=ContactSource.ARCHIVE
    )


def _signalled_tag(writer: Session, user: User, name: str, signal: TagMetSignal) -> Tag:
    return create_tag(writer, user, name, met_signal=signal)


def _offers(writer: Session, user: User) -> dict[str, module.Suggestion]:
    return {offer.key: offer for offer in module.suggestions(writer, user)}


def test_a_bare_invitation_is_not_message_history(writer: Session, user: User) -> None:
    """The importer stores an invitation as an li_in row, and clicking Connect is not a thread."""
    invited, wrote = _contacts(writer, user, 2)
    _invitation(writer, user, invited)
    _message(writer, user, wrote)
    offers = _offers(writer, user)
    assert offers[module.SUGGESTION_MET_WITH_MESSAGES].count == 1
    applied = module.apply_suggestion(writer, user, module.SUGGESTION_MET_WITH_MESSAGES)
    assert (applied.applied, _met(wrote), _met(invited)) == (1, ContactMet.MET, ContactMet.UNKNOWN)


def test_an_invitation_note_is_its_own_batch(writer: Session, user: User) -> None:
    noted, also_noted, bare, wrote = _contacts(writer, user, 4)
    _invitation(writer, user, noted, note="we met at the pottery fair")
    _invitation(writer, user, also_noted, note="good to meet you", outbound=True)
    _invitation(writer, user, bare)
    _message(writer, user, wrote)
    offer = _offers(writer, user)[module.SUGGESTION_MET_INVITATION_NOTE]
    assert offer.count == 2
    assert offer.met is ContactMet.MET
    applied = module.apply_suggestion(
        writer, user, module.SUGGESTION_MET_INVITATION_NOTE, expected_count=2
    )
    assert applied.applied == 2
    assert (_met(noted), _met(also_noted)) == (ContactMet.MET, ContactMet.MET)
    assert (_met(bare), _met(wrote)) == (ContactMet.UNKNOWN, ContactMet.UNKNOWN)


def test_a_note_from_someone_you_went_on_to_write_to_stays_in_the_stronger_batch(
    writer: Session, user: User
) -> None:
    both = factories.make_contact(writer, user)
    _invitation(writer, user, both, note="hello")
    _message(writer, user, both)
    offers = _offers(writer, user)
    assert module.SUGGESTION_MET_INVITATION_NOTE not in offers, "no person is counted twice"
    assert offers[module.SUGGESTION_MET_WITH_MESSAGES].count == 1


def test_the_no_evidence_batch_takes_only_the_contacts_nothing_is_known_about(
    writer: Session, user: User
) -> None:
    blank = factories.make_contact(writer, user, current_company="Northwind Pottery")
    noted = factories.make_contact(writer, user, current_company="Blue Harbor Tools")
    add_interaction(writer, user, noted.id, InteractionKind.NOTE, NOW, "spoke at a conference")
    wrote = factories.make_contact(writer, user, current_company="Somewhere Else")
    _message(writer, user, wrote)
    offer = _offers(writer, user)[module.SUGGESTION_NOT_MET_NO_EVIDENCE]
    assert (offer.count, offer.met) == (1, ContactMet.NOT_MET)
    covered, total = module.suggestion_contacts(writer, user, module.SUGGESTION_NOT_MET_NO_EVIDENCE)
    assert ([contact.id for contact in covered], total) == ([blank.id], 1)


def test_the_no_evidence_batch_keeps_back_a_colleague_of_someone_you_know(
    writer: Session, user: User
) -> None:
    """A company where you have met or written to somebody is evidence the panel will show."""
    known = factories.make_contact(writer, user, current_company="Northwind Pottery")
    _message(writer, user, known)
    colleague = factories.make_contact(writer, user, current_company="northwind pottery")
    stranger = factories.make_contact(writer, user, current_company="Somewhere Else")
    covered, _total = module.suggestion_contacts(
        writer, user, module.SUGGESTION_NOT_MET_NO_EVIDENCE
    )
    assert [contact.id for contact in covered] == [stranger.id]
    factories.make_contact(writer, user, current_company="Somewhere Else", met=ContactMet.MET)
    after, _total = module.suggestion_contacts(writer, user, module.SUGGESTION_NOT_MET_NO_EVIDENCE)
    assert after == [], "a contact already marked met makes their company known too"
    assert colleague.met is ContactMet.UNKNOWN


def test_a_tag_says_nothing_until_the_user_gives_it_a_meaning(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user)
    add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW, "a note")
    tag = create_tag(writer, user, "recruiter")
    tag_contact(writer, user, contact.id, tag.id)
    assert module.suggestions(writer, user) == []
    with pytest.raises(InvalidDecision, match="says nothing"):
        module.apply_suggestion(writer, user, f"{module.TAG_KEY_PREFIX}{tag.id}")


def test_a_tag_with_a_meaning_is_offered_as_its_own_batch(writer: Session, user: User) -> None:
    recruiters = _signalled_tag(writer, user, "recruiter", TagMetSignal.NOT_MET)
    colleagues = _signalled_tag(writer, user, "colleague", TagMetSignal.MET)
    recruiter, colleague, untagged = _contacts(writer, user, 3)
    for contact in (recruiter, colleague, untagged):
        add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW, "a note")
    tag_contact(writer, user, recruiter.id, recruiters.id)
    tag_contact(writer, user, colleague.id, colleagues.id)
    offers = _offers(writer, user)
    assert [key for key in offers] == [
        f"{module.TAG_KEY_PREFIX}{colleagues.id}",
        f"{module.TAG_KEY_PREFIX}{recruiters.id}",
    ], "met before not met, and nothing else is on offer"
    assert offers[f"{module.TAG_KEY_PREFIX}{recruiters.id}"].tag_id == recruiters.id
    applied = module.apply_suggestion(
        writer, user, f"{module.TAG_KEY_PREFIX}{recruiters.id}", expected_count=1, at=NOW
    )
    assert (applied.applied, applied.met) == (1, ContactMet.NOT_MET)
    assert (_met(recruiter), _met(colleague), _met(untagged)) == (
        ContactMet.NOT_MET,
        ContactMet.UNKNOWN,
        ContactMet.UNKNOWN,
    )
    (row,) = writer.scalars(
        scoped(user, TriageDecision).where(TriageDecision.contact_id == recruiter.id)
    ).all()
    assert row.kind is TriageDecisionKind.BULK_NOT_MET
    assert row.reason == f"{module.TAG_KEY_PREFIX}{recruiters.id}"


def test_a_tag_a_rule_applied_counts_like_one_applied_by_hand(writer: Session, user: User) -> None:
    tag = _signalled_tag(writer, user, "recruiter", TagMetSignal.NOT_MET)
    create_rule(writer, user, tag.id, RuleField.TITLE, r"\brecruiter\b")
    contact = factories.make_contact(writer, user, current_title="Technical Recruiter")
    add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW, "a note")
    assert run_rules(writer, user).added == 1
    assert _offers(writer, user)[f"{module.TAG_KEY_PREFIX}{tag.id}"].count == 1


def test_a_tag_batch_never_reaches_another_user_s_tag(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    theirs = create_tag(writer, other, "colleague", met_signal=TagMetSignal.MET)
    with pytest.raises(InvalidDecision):
        module.apply_suggestion(writer, user, f"{module.TAG_KEY_PREFIX}{theirs.id}")
    with pytest.raises(InvalidDecision):
        module.suggestion_contacts(writer, user, f"{module.TAG_KEY_PREFIX}{theirs.id}")


# --- the preview ------------------------------------------------------------


def test_a_suggestion_lists_who_it_covers_in_pages(writer: Session, user: User) -> None:
    covered = _contacts(writer, user, 3)
    for contact in covered:
        _message(writer, user, contact)
    quiet = factories.make_contact(writer, user)
    add_interaction(writer, user, quiet.id, InteractionKind.NOTE, NOW, "a note")
    first, total = module.suggestion_contacts(
        writer, user, module.SUGGESTION_MET_WITH_MESSAGES, limit=2
    )
    assert ([contact.id for contact in first], total) == ([covered[0].id, covered[1].id], 3)
    second, _total = module.suggestion_contacts(
        writer, user, module.SUGGESTION_MET_WITH_MESSAGES, limit=2, offset=2
    )
    assert [contact.id for contact in second] == [covered[2].id]
    assert all(contact.met is ContactMet.UNKNOWN for contact in covered), "a preview writes nothing"
    assert _open_decisions(writer, user) == 0


def test_a_preview_of_an_unknown_suggestion_is_refused(writer: Session, user: User) -> None:
    for key in ("mark-everyone-as-a-friend", f"{module.TAG_KEY_PREFIX}nope"):
        with pytest.raises(InvalidDecision):
            module.suggestion_contacts(writer, user, key)


def test_a_preview_never_reaches_another_user_s_contacts(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    theirs = factories.make_contact(writer, other)
    _message(writer, other, theirs)
    mine = factories.make_contact(writer, user)
    _message(writer, user, mine)
    covered, total = module.suggestion_contacts(writer, user, module.SUGGESTION_MET_WITH_MESSAGES)
    assert ([contact.id for contact in covered], total) == ([mine.id], 1)


# --- what netkeeper decided, and reviewing it -------------------------------


def test_a_batch_marks_its_work_automatic_and_says_which_batch_it_was(
    writer: Session, user: User
) -> None:
    contact = factories.make_contact(writer, user)
    _message(writer, user, contact)
    applied = module.apply_suggestion(
        writer, user, module.SUGGESTION_MET_WITH_MESSAGES, at=NOW, expected_count=1
    )
    assert contact.met_source is MetSource.AUTOMATIC
    (row,) = writer.scalars(scoped(user, TriageDecision)).all()
    assert row.kind is TriageDecisionKind.BULK_MET
    assert (row.batch_id, row.reason) == (applied.batch_id, module.SUGGESTION_MET_WITH_MESSAGES)
    assert row.decided_at == NOW
    assert row.before_state["met_source"] == "manual"
    assert row.after_state["met_source"] == "automatic"


def test_the_review_queue_serves_exactly_the_contacts_a_batch_decided(
    writer: Session, user: User
) -> None:
    messaged, quiet = _contacts(writer, user, 2)
    _message(writer, user, messaged)
    add_interaction(writer, user, quiet.id, InteractionKind.NOTE, NOW, "a note")
    module.decide(writer, user, quiet.id, ContactMet.NOT_MET)
    module.apply_suggestion(writer, user, module.SUGGESTION_MET_WITH_MESSAGES)
    review = _queue_ids(
        writer, user, states=module.REVIEW_QUEUE_STATES, decided_by=MetSource.AUTOMATIC
    )
    assert review == [messaged.id], "the contact decided by hand is not up for review"
    assert _queue_ids(writer, user) == [], "and the untriaged queue is empty"


def test_deciding_by_hand_closes_the_review_and_undo_reopens_it(
    writer: Session, user: User
) -> None:
    contact = factories.make_contact(writer, user)
    _message(writer, user, contact)
    module.apply_suggestion(writer, user, module.SUGGESTION_MET_WITH_MESSAGES)
    module.decide(writer, user, contact.id, ContactMet.NOT_MET)
    assert contact.met_source is MetSource.MANUAL
    assert (
        _queue_ids(writer, user, states=module.REVIEW_QUEUE_STATES, decided_by=MetSource.AUTOMATIC)
        == []
    )
    module.undo(writer, user)
    assert (contact.met, contact.met_source) == (ContactMet.MET, MetSource.AUTOMATIC)
    assert _queue_ids(
        writer, user, states=module.REVIEW_QUEUE_STATES, decided_by=MetSource.AUTOMATIC
    ) == [contact.id]
    module.undo(writer, user)
    assert (contact.met, contact.met_source) == (ContactMet.UNKNOWN, MetSource.MANUAL)


def test_progress_counts_what_was_decided_automatically(writer: Session, user: User) -> None:
    messaged, quiet = _contacts(writer, user, 2)
    _message(writer, user, messaged)
    assert module.progress(writer, user).automatic == 0
    module.apply_suggestion(writer, user, module.SUGGESTION_MET_WITH_MESSAGES)
    counters = module.progress(writer, user)
    assert (counters.total, counters.triaged, counters.automatic) == (2, 1, 1)
    assert counters.by_state[ContactMet.MET] == 1
    review = module.progress(
        writer, user, states=module.REVIEW_QUEUE_STATES, decided_by=MetSource.AUTOMATIC
    )
    assert review.remaining == 1, "the review queue counts its own contacts, not every decided one"
    assert quiet.met is ContactMet.UNKNOWN
