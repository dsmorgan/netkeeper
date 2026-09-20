"""netkeeper.crm.interactions (spec 8.1, 10.1): CRUD, last_contacted_at, the timeline, notes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import interactions as module
from netkeeper.crm import provenance
from netkeeper.crm.interactions import (
    MISSING,
    OUTBOUND_KINDS,
    NotFound,
    TimelineEntry,
    add_interaction,
    delete_interaction,
    get_interaction,
    is_outbound,
    list_interactions,
    recompute_last_contacted,
    set_notes,
    timeline,
    update_interaction,
)
from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactMet,
    ContactSnapshot,
    ContactSource,
    Interaction,
    InteractionKind,
    User,
)
from netkeeper.scoping import scoped

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
EARLIER = NOW - timedelta(days=1)
OLDEST = NOW - timedelta(days=2)
INBOUND_KINDS = frozenset(InteractionKind) - OUTBOUND_KINDS


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller that writes must use."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def other(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def contact(writer: Session, user: User) -> Contact:
    return factories.make_contact(writer, user)


def snapshot(session: Session, contact: Contact, observed_at: datetime, **fields: str) -> int:
    row = ContactSnapshot(
        user_id=contact.user_id, contact_id=contact.id, observed_at=observed_at, **fields
    )
    session.add(row)
    session.flush()
    return row.id


def shape(entries: list[TimelineEntry]) -> list[tuple[str, datetime, int]]:
    return [(entry.kind, entry.at, entry.row.id) for entry in entries]


# Read through a call so mypy does not narrow the attribute across the service calls.
def last_contacted(contact: Contact) -> datetime | None:
    return contact.last_contacted_at


def notes_of(contact: Contact) -> str | None:
    return contact.notes


def fields(row: Interaction) -> tuple[InteractionKind, datetime, str | None, int | None]:
    return (row.kind, row.at, row.summary, row.message_id)


# --- kinds ------------------------------------------------------------------


def test_outbound_kinds_are_the_ones_where_you_reached_out() -> None:
    assert {
        InteractionKind.EMAIL_OUT,
        InteractionKind.LI_OUT,
        InteractionKind.CALL,
        InteractionKind.MEETING,
    } == OUTBOUND_KINDS
    assert {
        InteractionKind.NOTE,
        InteractionKind.EMAIL_IN,
        InteractionKind.LI_IN,
        InteractionKind.LI_VIEW,
    } == INBOUND_KINDS
    for kind in InteractionKind:
        assert is_outbound(kind) is (kind in OUTBOUND_KINDS)


# --- add --------------------------------------------------------------------


def test_add_returns_a_flushed_row_of_the_user(
    writer: Session, user: User, contact: Contact
) -> None:
    row = add_interaction(
        writer, user, contact.id, InteractionKind.NOTE, NOW, "met at a meetup", message_id=7
    )
    assert row.id is not None
    assert row.user_id == user.id
    assert row.contact_id == contact.id
    assert row.kind is InteractionKind.NOTE
    assert row.at == NOW
    assert row.summary == "met at a meetup"
    assert row.message_id == 7
    assert row.source is ContactSource.MANUAL
    assert contact.interactions == [row]


def test_add_takes_the_source_of_an_importer(writer: Session, user: User, contact: Contact) -> None:
    row = add_interaction(
        writer, user, contact.id, InteractionKind.LI_IN, NOW, source=ContactSource.ARCHIVE
    )
    assert row.source is ContactSource.ARCHIVE


@pytest.mark.parametrize("kind", sorted(OUTBOUND_KINDS))
def test_an_outbound_interaction_sets_last_contacted(
    writer: Session, user: User, contact: Contact, kind: InteractionKind
) -> None:
    assert last_contacted(contact) is None
    add_interaction(writer, user, contact.id, kind, NOW)
    assert last_contacted(contact) == NOW


@pytest.mark.parametrize("kind", sorted(INBOUND_KINDS))
def test_an_inbound_interaction_leaves_last_contacted(
    writer: Session, user: User, contact: Contact, kind: InteractionKind
) -> None:
    add_interaction(writer, user, contact.id, kind, NOW)
    assert last_contacted(contact) is None
    contact.last_contacted_at = EARLIER
    add_interaction(writer, user, contact.id, kind, LATER)
    assert last_contacted(contact) == EARLIER


def test_last_contacted_is_the_newest_outbound_time(
    writer: Session, user: User, contact: Contact
) -> None:
    add_interaction(writer, user, contact.id, InteractionKind.CALL, NOW)
    assert last_contacted(contact) == NOW
    # A backfilled older call does not move it back.
    add_interaction(writer, user, contact.id, InteractionKind.EMAIL_OUT, EARLIER)
    assert last_contacted(contact) == NOW
    add_interaction(writer, user, contact.id, InteractionKind.MEETING, LATER)
    assert last_contacted(contact) == LATER


def test_add_rejects_a_naive_time(writer: Session, user: User, contact: Contact) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW.replace(tzinfo=None))
    assert contact.interactions == []


def test_add_needs_a_writer_session(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(session, user)
    with pytest.raises(RuntimeError, match="writer session"):
        add_interaction(session, user, contact.id, InteractionKind.NOTE, NOW)


def test_add_to_another_users_contact_is_not_found(
    writer: Session, user: User, other: User, contact: Contact
) -> None:
    with pytest.raises(NotFound):
        add_interaction(writer, other, contact.id, InteractionKind.NOTE, NOW)
    with pytest.raises(NotFound):
        add_interaction(writer, user, contact.id + 1000, InteractionKind.NOTE, NOW)
    assert contact.interactions == []


# --- get and list -----------------------------------------------------------


def test_get_finds_only_the_users_own_interaction(
    writer: Session, user: User, other: User, contact: Contact
) -> None:
    row = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    assert get_interaction(writer, user, row.id) is row
    with pytest.raises(NotFound):
        get_interaction(writer, other, row.id)
    with pytest.raises(NotFound):
        get_interaction(writer, user, row.id + 1000)


def test_list_pages_newest_first_with_the_total(
    writer: Session, user: User, contact: Contact
) -> None:
    oldest = add_interaction(writer, user, contact.id, InteractionKind.NOTE, OLDEST)
    newest = add_interaction(writer, user, contact.id, InteractionKind.NOTE, LATER)
    middle = add_interaction(writer, user, contact.id, InteractionKind.CALL, NOW)
    same_time = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    elsewhere = factories.make_contact(writer, user)
    add_interaction(writer, user, elsewhere.id, InteractionKind.NOTE, LATER)

    rows, total = list_interactions(writer, user, contact.id, limit=10)
    assert total == 4
    assert rows == [newest, same_time, middle, oldest]  # ties: the newer row first

    rows, total = list_interactions(writer, user, contact.id, limit=2, offset=1)
    assert total == 4
    assert rows == [same_time, middle]


def test_list_of_another_users_contact_is_not_found(
    writer: Session, other: User, contact: Contact
) -> None:
    with pytest.raises(NotFound):
        list_interactions(writer, other, contact.id, limit=10)


def test_list_checks_its_page_arguments(writer: Session, user: User, contact: Contact) -> None:
    with pytest.raises(ValueError, match="limit"):
        list_interactions(writer, user, contact.id, limit=0)
    with pytest.raises(ValueError, match="offset"):
        list_interactions(writer, user, contact.id, limit=1, offset=-1)


# --- update -----------------------------------------------------------------


def test_update_changes_only_the_given_fields(
    writer: Session, user: User, contact: Contact
) -> None:
    row = add_interaction(
        writer, user, contact.id, InteractionKind.NOTE, NOW, "first", message_id=3
    )
    same = update_interaction(writer, user, row.id, summary="second")
    assert same is row
    assert fields(row) == (InteractionKind.NOTE, NOW, "second", 3)
    update_interaction(writer, user, row.id, summary=None, message_id=None)
    assert fields(row) == (InteractionKind.NOTE, NOW, None, None)
    update_interaction(writer, user, row.id, kind=InteractionKind.LI_IN, at=LATER, message_id=4)
    assert fields(row) == (InteractionKind.LI_IN, LATER, None, 4)
    assert update_interaction(writer, user, row.id) is row  # nothing given, nothing changed


def test_moving_the_newest_outbound_back_lowers_last_contacted(
    writer: Session, user: User, contact: Contact
) -> None:
    add_interaction(writer, user, contact.id, InteractionKind.EMAIL_OUT, NOW)
    newest = add_interaction(writer, user, contact.id, InteractionKind.CALL, LATER)
    assert last_contacted(contact) == LATER
    update_interaction(writer, user, newest.id, at=EARLIER)
    assert last_contacted(contact) == NOW  # from the row that remains newest, not a decrement
    update_interaction(writer, user, newest.id, at=LATER + timedelta(days=1))
    assert last_contacted(contact) == LATER + timedelta(days=1)


def test_turning_the_newest_outbound_into_a_note_lowers_last_contacted(
    writer: Session, user: User, contact: Contact
) -> None:
    add_interaction(writer, user, contact.id, InteractionKind.LI_OUT, NOW)
    newest = add_interaction(writer, user, contact.id, InteractionKind.MEETING, LATER)
    update_interaction(writer, user, newest.id, kind=InteractionKind.NOTE)
    assert last_contacted(contact) == NOW
    update_interaction(writer, user, newest.id, kind=InteractionKind.LI_VIEW)
    assert last_contacted(contact) == NOW


def test_turning_a_note_into_an_outbound_kind_raises_last_contacted(
    writer: Session, user: User, contact: Contact
) -> None:
    add_interaction(writer, user, contact.id, InteractionKind.CALL, NOW)
    note = add_interaction(writer, user, contact.id, InteractionKind.NOTE, LATER)
    assert last_contacted(contact) == NOW
    update_interaction(writer, user, note.id, kind=InteractionKind.EMAIL_OUT)
    assert last_contacted(contact) == LATER


def test_editing_an_inbound_row_never_touches_last_contacted(
    writer: Session, user: User, contact: Contact
) -> None:
    note = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    contact.last_contacted_at = OLDEST  # drifted on purpose: an inbound edit must not repair it
    update_interaction(writer, user, note.id, at=LATER, summary="moved")
    assert last_contacted(contact) == OLDEST


def test_update_rejects_a_naive_time_before_writing(
    writer: Session, user: User, contact: Contact
) -> None:
    row = add_interaction(writer, user, contact.id, InteractionKind.CALL, NOW, "call")
    with pytest.raises(ValueError, match="timezone-aware"):
        update_interaction(writer, user, row.id, at=LATER.replace(tzinfo=None), summary="x")
    assert row.summary == "call"


def test_update_of_another_users_interaction_is_not_found(
    writer: Session, user: User, other: User, contact: Contact
) -> None:
    row = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW, "mine")
    with pytest.raises(NotFound):
        update_interaction(writer, other, row.id, summary="theirs")
    assert row.summary == "mine"


def test_update_needs_a_writer_session(
    session_factory: sessionmaker[Session], writer: Session, user: User, contact: Contact
) -> None:
    row = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    writer.commit()
    with session_factory() as reader:
        with pytest.raises(RuntimeError, match="writer session"):
            update_interaction(reader, user, row.id, summary="x")
        with pytest.raises(RuntimeError, match="writer session"):
            delete_interaction(reader, user, row.id)
        with pytest.raises(RuntimeError, match="writer session"):
            set_notes(reader, user, contact.id, "x")
        with pytest.raises(RuntimeError, match="writer session"):
            recompute_last_contacted(reader, user)


# --- delete -----------------------------------------------------------------


def test_deleting_outbound_rows_recomputes_from_the_rest(
    writer: Session, user: User, contact: Contact
) -> None:
    oldest = add_interaction(writer, user, contact.id, InteractionKind.CALL, EARLIER)
    middle = add_interaction(writer, user, contact.id, InteractionKind.EMAIL_OUT, NOW)
    newest = add_interaction(writer, user, contact.id, InteractionKind.LI_OUT, LATER)
    note = add_interaction(writer, user, contact.id, InteractionKind.NOTE, LATER + timedelta(1))
    assert last_contacted(contact) == LATER

    delete_interaction(writer, user, newest.id)
    assert last_contacted(contact) == NOW
    delete_interaction(writer, user, middle.id)
    assert last_contacted(contact) == EARLIER
    delete_interaction(writer, user, note.id)  # inbound: no effect
    assert last_contacted(contact) == EARLIER
    delete_interaction(writer, user, oldest.id)
    assert last_contacted(contact) is None
    assert writer.scalars(scoped(user, Interaction)).all() == []


def test_delete_of_another_users_interaction_is_not_found(
    writer: Session, user: User, other: User, contact: Contact
) -> None:
    row = add_interaction(writer, user, contact.id, InteractionKind.CALL, NOW)
    with pytest.raises(NotFound):
        delete_interaction(writer, other, row.id)
    assert contact.interactions == [row]
    assert last_contacted(contact) == NOW
    delete_interaction(writer, user, row.id)
    with pytest.raises(NotFound):
        delete_interaction(writer, user, row.id)


# --- recompute --------------------------------------------------------------


def test_recompute_repairs_every_contact_of_the_user(
    writer: Session, user: User, other: User
) -> None:
    drifted = factories.make_contact(writer, user)
    add_interaction(writer, user, drifted.id, InteractionKind.CALL, NOW)
    add_interaction(writer, user, drifted.id, InteractionKind.NOTE, LATER)
    drifted.last_contacted_at = LATER  # wrong: the note is not outbound
    never = factories.make_contact(writer, user)
    add_interaction(writer, user, never.id, InteractionKind.EMAIL_IN, NOW)
    never.last_contacted_at = NOW  # wrong: nothing outbound
    theirs = factories.make_contact(writer, other)
    theirs.last_contacted_at = OLDEST  # wrong too, but not this user's to fix
    writer.flush()

    assert recompute_last_contacted(writer, user) == 2
    assert last_contacted(drifted) == NOW
    assert last_contacted(never) is None
    assert last_contacted(theirs) == OLDEST


def test_recompute_can_be_limited_to_some_contacts(writer: Session, user: User) -> None:
    first = factories.make_contact(writer, user)
    second = factories.make_contact(writer, user)
    add_interaction(writer, user, first.id, InteractionKind.CALL, NOW)
    add_interaction(writer, user, second.id, InteractionKind.CALL, NOW)
    first.last_contacted_at = LATER
    second.last_contacted_at = LATER
    writer.flush()

    assert recompute_last_contacted(writer, user, [first.id]) == 1
    assert last_contacted(first) == NOW
    assert last_contacted(second) == LATER
    assert recompute_last_contacted(writer, user, []) == 0


def test_recompute_is_visible_from_a_fresh_session(
    session_factory: sessionmaker[Session], writer: Session, user: User, contact: Contact
) -> None:
    add_interaction(writer, user, contact.id, InteractionKind.CALL, NOW)
    contact.last_contacted_at = LATER
    writer.flush()
    recompute_last_contacted(writer, user)
    writer.commit()
    with session_factory() as reader:
        fresh = reader.scalars(scoped(user, Contact)).one()
        assert last_contacted(fresh) == NOW


# --- timeline ---------------------------------------------------------------


def test_timeline_interleaves_interactions_and_snapshots_newest_first(
    writer: Session, user: User, contact: Contact
) -> None:
    middle = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    older = add_interaction(writer, user, contact.id, InteractionKind.CALL, EARLIER)
    newest = snapshot(writer, contact, LATER, headline="New job")
    oldest = snapshot(writer, contact, OLDEST, headline="Old job")
    elsewhere = factories.make_contact(writer, user)
    add_interaction(writer, user, elsewhere.id, InteractionKind.NOTE, LATER)
    snapshot(writer, elsewhere, LATER)

    entries = timeline(writer, user, contact.id, limit=10)
    assert shape(entries) == [
        ("snapshot", LATER, newest),
        ("interaction", NOW, middle.id),
        ("interaction", EARLIER, older.id),
        ("snapshot", OLDEST, oldest),
    ]
    assert isinstance(entries[0].row, ContactSnapshot)
    assert entries[0].row.headline == "New job"
    assert entries[1].row is middle


def test_timeline_orders_ties_interaction_first_then_newer_id(
    writer: Session, user: User, contact: Contact
) -> None:
    first_snapshot = snapshot(writer, contact, NOW)
    first = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    second = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    second_snapshot = snapshot(writer, contact, NOW)
    assert shape(timeline(writer, user, contact.id, limit=10)) == [
        ("interaction", NOW, second.id),
        ("interaction", NOW, first.id),
        ("snapshot", NOW, second_snapshot),
        ("snapshot", NOW, first_snapshot),
    ]


def test_timeline_cursor_walks_every_entry_once(
    writer: Session, user: User, contact: Contact
) -> None:
    for days in range(7):
        at = NOW - timedelta(days=days)
        if days % 2:
            snapshot(writer, contact, at)
        else:
            add_interaction(writer, user, contact.id, InteractionKind.NOTE, at)
    everything = timeline(writer, user, contact.id, limit=100)
    assert len(everything) == 7

    pages: list[list[TimelineEntry]] = []
    before: datetime | None = None
    while True:
        page = timeline(writer, user, contact.id, limit=3, before=before)
        pages.append(page)
        if len(page) < 3:
            break
        before = page[-1].at
    assert [len(page) for page in pages] == [3, 3, 1]
    assert shape([entry for page in pages for entry in page]) == shape(everything)
    assert timeline(writer, user, contact.id, limit=3, before=everything[-1].at) == []


def test_timeline_never_splits_entries_that_share_a_time(
    writer: Session, user: User, contact: Contact
) -> None:
    newest = add_interaction(writer, user, contact.id, InteractionKind.NOTE, LATER)
    tied = [
        add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW).id for _ in range(3)
    ]
    tied += [snapshot(writer, contact, NOW) for _ in range(2)]
    older = add_interaction(writer, user, contact.id, InteractionKind.NOTE, EARLIER)

    page = timeline(writer, user, contact.id, limit=2)
    # The boundary entry shares NOW with four more; the page takes them all.
    assert len(page) == 6
    assert page[0].row is newest
    assert {entry.row.id for entry in page[1:]} == set(tied)
    assert all(entry.at == NOW for entry in page[1:])
    rest = timeline(writer, user, contact.id, limit=2, before=page[-1].at)
    assert shape(rest) == [("interaction", EARLIER, older.id)]


def test_timeline_page_that_is_one_tie_group_is_returned_whole(
    writer: Session, user: User, contact: Contact
) -> None:
    ids = {
        add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW).id for _ in range(4)
    }
    page = timeline(writer, user, contact.id, limit=2)
    assert {entry.row.id for entry in page} == ids
    assert timeline(writer, user, contact.id, limit=2, before=NOW) == []


def test_timeline_cuts_cleanly_when_the_boundary_is_not_a_tie(
    writer: Session, user: User, contact: Contact
) -> None:
    rows = [
        add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW - timedelta(hours=n))
        for n in range(4)
    ]
    page = timeline(writer, user, contact.id, limit=2)
    assert [entry.row for entry in page] == rows[:2]


def test_timeline_reads_without_a_writer_session(
    session_factory: sessionmaker[Session], writer: Session, user: User, contact: Contact
) -> None:
    row = add_interaction(writer, user, contact.id, InteractionKind.NOTE, NOW)
    writer.commit()
    with session_factory() as reader:
        entries = timeline(reader, user, contact.id, limit=5)
        assert shape(entries) == [("interaction", NOW, row.id)]


def test_timeline_checks_its_arguments(
    writer: Session, user: User, other: User, contact: Contact
) -> None:
    with pytest.raises(NotFound):
        timeline(writer, other, contact.id, limit=5)
    with pytest.raises(ValueError, match="limit"):
        timeline(writer, user, contact.id, limit=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        timeline(writer, user, contact.id, limit=5, before=NOW.replace(tzinfo=None))


# --- notes ------------------------------------------------------------------


def test_set_notes_stores_markdown_as_given(writer: Session, user: User, contact: Contact) -> None:
    text = "# Met at PyCon\n\n- likes **Rust**  \n"
    assert set_notes(writer, user, contact.id, text) is contact
    assert notes_of(contact) == text
    set_notes(writer, user, contact.id, None)
    assert notes_of(contact) is None
    set_notes(writer, user, contact.id, "")
    assert notes_of(contact) == ""


def test_set_notes_goes_through_set_manual_field(
    writer: Session, user: User, contact: Contact, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Contact, str, object]] = []

    def spy(target: Contact, field: str, value: str | date | ContactMet | None) -> None:
        calls.append((target, field, value))
        provenance.set_manual_field(target, field, value)

    monkeypatch.setattr(module, "set_manual_field", spy)
    set_notes(writer, user, contact.id, "hello")
    assert calls == [(contact, "notes", "hello")]
    assert notes_of(contact) == "hello"
    assert "notes" not in contact.field_sources  # a person-owned field carries no provenance


def test_set_notes_on_another_users_contact_is_not_found(
    writer: Session, other: User, contact: Contact
) -> None:
    with pytest.raises(NotFound):
        set_notes(writer, other, contact.id, "theirs")
    assert notes_of(contact) is None


# --- the sentinel -----------------------------------------------------------


def test_missing_is_distinct_from_none() -> None:
    assert MISSING is not None
    assert MISSING is module._Missing.MISSING
