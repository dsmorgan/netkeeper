"""netkeeper.crm.positions: the user's own job history, CRUD and archive import (P1-26, #84)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.positions import (
    MISSING,
    InvalidPosition,
    NotFound,
    add_position,
    delete_position,
    get_position,
    import_positions,
    list_positions,
    update_position,
)
from netkeeper.db import session_scope
from netkeeper.linkedin.archive import PositionRow
from netkeeper.models import ContactSource, User, UserPosition
from netkeeper.scoping import scoped

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
EARLIER = NOW - timedelta(days=1)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller that writes must use."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _row(
    company: str | None,
    title: str | None,
    started_on: date | None,
    ended_on: date | None,
    *,
    row_number: int = 1,
) -> PositionRow:
    return PositionRow(
        row_number=row_number,
        company=company,
        title=title,
        started_on=started_on,
        ended_on=ended_on,
    )


# --- manual CRUD --------------------------------------------------------------


def test_add_position_infers_current_from_the_dates(writer: Session, user: User) -> None:
    current = add_position(writer, user, company="Acme", started_on=date(2020, 1, 1))
    assert current.is_current is True
    dated = add_position(
        writer, user, company="Acme", started_on=date(2018, 1, 1), ended_on=date(2019, 1, 1)
    )
    assert dated.is_current is False
    undated = add_position(writer, user, company="Acme")
    assert undated.is_current is False


def test_add_position_needs_a_title_or_a_company(writer: Session, user: User) -> None:
    with pytest.raises(InvalidPosition):
        add_position(writer, user)
    with pytest.raises(InvalidPosition):
        add_position(writer, user, title="  ", company="   ")


def test_add_position_trims_and_empties_become_none(writer: Session, user: User) -> None:
    row = add_position(writer, user, title="  Engineer  ", company="  Acme  ")
    assert (row.title, row.company) == ("Engineer", "Acme")


def test_get_and_list_positions_are_scoped_to_the_user(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    mine = add_position(writer, user, company="Acme")
    add_position(writer, other, company="Not Mine")

    assert get_position(writer, user, mine.id).id == mine.id
    with pytest.raises(NotFound):
        get_position(writer, other, mine.id)
    assert [row.id for row in list_positions(writer, other)] != [mine.id]


def test_list_positions_orders_current_first_then_most_recent(writer: Session, user: User) -> None:
    old = add_position(
        writer, user, company="Old Co", started_on=date(2010, 1, 1), ended_on=date(2012, 1, 1)
    )
    recent = add_position(
        writer, user, company="Recent Co", started_on=date(2018, 1, 1), ended_on=date(2020, 1, 1)
    )
    current = add_position(writer, user, company="Now Co", started_on=date(2021, 1, 1))
    assert [row.id for row in list_positions(writer, user)] == [current.id, recent.id, old.id]


def test_update_position_changes_only_given_fields(writer: Session, user: User) -> None:
    row = add_position(writer, user, title="Engineer", company="Acme", started_on=date(2020, 1, 1))
    updated = update_position(writer, user, row.id, title="Staff Engineer")
    assert updated.title == "Staff Engineer"
    assert updated.company == "Acme"
    assert updated.started_on == date(2020, 1, 1)


def test_update_position_never_infers_is_current(writer: Session, user: User) -> None:
    """Unlike create, changing dates on an update does not silently flip the flag."""
    row = add_position(writer, user, company="Acme", started_on=date(2020, 1, 1), is_current=False)
    updated = update_position(writer, user, row.id, ended_on=None)
    assert updated.is_current is False


def test_update_position_refuses_to_leave_neither_title_nor_company(
    writer: Session, user: User
) -> None:
    row = add_position(writer, user, title="Engineer")
    with pytest.raises(InvalidPosition):
        update_position(writer, user, row.id, title=None)


def test_update_position_records_a_manual_edit(writer: Session, user: User) -> None:
    row = add_position(writer, user, company="Acme")
    row.source = ContactSource.ARCHIVE
    writer.flush()
    updated = update_position(writer, user, row.id, title="New Title")
    assert updated.source is ContactSource.MANUAL


def test_delete_position_is_scoped_to_the_user(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    row = add_position(writer, user, company="Acme")
    with pytest.raises(NotFound):
        delete_position(writer, other, row.id)
    delete_position(writer, user, row.id)
    with pytest.raises(NotFound):
        get_position(writer, user, row.id)


def test_missing_sentinel_leaves_started_on_and_ended_on_untouched(
    writer: Session, user: User
) -> None:
    row = add_position(writer, user, company="Acme", started_on=date(2020, 1, 1))
    updated = update_position(writer, user, row.id, started_on=MISSING, ended_on=MISSING)
    assert updated.started_on == date(2020, 1, 1)
    assert updated.ended_on is None


# --- archive import ------------------------------------------------------------


def test_import_positions_creates_and_counts(writer: Session, user: User) -> None:
    rows = [
        _row("Acme", "Engineer", date(2018, 1, 1), date(2019, 1, 1), row_number=1),
        _row("Acme Current", "Lead", date(2020, 1, 1), None, row_number=2),
        _row("Undated Co", "Advisor", None, None, row_number=3),
        _row(None, None, None, None, row_number=4),
    ]
    counts = import_positions(writer, user, rows, source=ContactSource.ARCHIVE, observed_at=NOW)
    assert counts.rows == 4
    assert counts.created == 3
    assert counts.unchanged == 0
    assert counts.skipped == 1
    assert counts.undated == 1  # "Undated Co" has no start date

    stored = {row.company: row for row in writer.scalars(scoped(user, UserPosition))}
    assert stored["Acme"].is_current is False
    assert stored["Acme Current"].is_current is True
    assert stored["Undated Co"].is_current is False


def test_import_positions_is_idempotent_by_natural_key(writer: Session, user: User) -> None:
    rows = [_row("Acme", "Engineer", date(2018, 1, 1), date(2019, 1, 1))]
    first = import_positions(writer, user, rows, source=ContactSource.ARCHIVE, observed_at=NOW)
    assert first.created == 1

    second = import_positions(writer, user, rows, source=ContactSource.ARCHIVE, observed_at=LATER)
    assert second.created == 0
    assert second.updated == 1
    assert len(list(writer.scalars(scoped(user, UserPosition)))) == 1


def test_import_positions_refreshes_an_open_ended_row_when_a_newer_export_closes_it(
    writer: Session, user: User
) -> None:
    import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), None)],
        source=ContactSource.ARCHIVE,
        observed_at=EARLIER,
    )
    import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), date(2021, 1, 1))],
        source=ContactSource.ARCHIVE,
        observed_at=NOW,
    )
    (row,) = list(writer.scalars(scoped(user, UserPosition)))
    assert row.ended_on == date(2021, 1, 1)
    assert row.is_current is False


def test_import_positions_counts_an_older_reimport_as_unchanged(
    writer: Session, user: User
) -> None:
    import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), None)],
        source=ContactSource.ARCHIVE,
        observed_at=NOW,
    )
    result = import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), None)],
        source=ContactSource.ARCHIVE,
        observed_at=EARLIER,
    )
    assert (result.updated, result.unchanged) == (0, 1)


def test_a_manual_edit_survives_a_reimport_even_when_the_export_is_newer(
    writer: Session, user: User
) -> None:
    """The sticky rule (module docstring, decided on the pre-merge review of #127): once a
    row's source is manual, an import never writes to it again at any observed time -- not
    just one exported before the edit. Edits ``ended_on``, not a natural-key field, so this
    actually exercises the chronological/sticky guard rather than sidestepping it by making
    the edited row invisible to the natural-key match (see the "editing a key field" test
    below for that separate scenario)."""
    import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), date(2021, 1, 1))],
        source=ContactSource.ARCHIVE,
        observed_at=EARLIER,
    )
    (row,) = list(writer.scalars(scoped(user, UserPosition)))
    update_position(writer, user, row.id, ended_on=date(2024, 1, 1))
    writer.flush()
    edited_at = row.observed_at

    # A re-import with a *newer* observation than the edit: must still not touch it.
    result = import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), date(2021, 1, 1))],
        source=ContactSource.ARCHIVE,
        observed_at=LATER,
    )
    assert (result.updated, result.unchanged) == (0, 1)
    assert row.ended_on == date(2024, 1, 1)
    assert row.is_current is False
    assert row.source is ContactSource.MANUAL
    assert row.observed_at == edited_at


def test_import_positions_leaves_a_manually_added_row_alone_too(
    writer: Session, user: User
) -> None:
    """The sticky rule applies to a row that was never archive-sourced at all, not only to
    one an import wrote and a person later edited."""
    row = add_position(writer, user, company="Acme", title="Lead", started_on=date(2020, 1, 1))
    result = import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), date(2022, 1, 1))],
        source=ContactSource.ARCHIVE,
        observed_at=NOW,
    )
    assert (result.created, result.updated, result.unchanged) == (0, 0, 1)
    assert row.ended_on is None
    assert row.source is ContactSource.MANUAL


def test_editing_a_key_field_is_not_matched_by_a_later_reimport(
    writer: Session, user: User
) -> None:
    """A known, accepted limit of natural-key matching (module docstring): editing
    ``title`` -- part of the key -- moves the row out from under the key an import
    would look for, so the next import of the very same archive does not find it and
    creates an additional row instead of updating the one that was renamed."""
    import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), None)],
        source=ContactSource.ARCHIVE,
        observed_at=EARLIER,
    )
    (row,) = list(writer.scalars(scoped(user, UserPosition)))
    update_position(writer, user, row.id, title="Staff Lead")

    result = import_positions(
        writer,
        user,
        [_row("Acme", "Lead", date(2020, 1, 1), None)],
        source=ContactSource.ARCHIVE,
        observed_at=LATER,
    )
    assert result.created == 1  # the archive's own row: not matched to the renamed one
    rows = list(writer.scalars(scoped(user, UserPosition)))
    assert len(rows) == 2
    assert {r.title for r in rows} == {"Staff Lead", "Lead"}
    assert {r.source for r in rows} == {ContactSource.MANUAL, ContactSource.ARCHIVE}


def test_import_positions_requires_a_writer_session(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        import_positions(session, owner, [], source=ContactSource.ARCHIVE, observed_at=NOW)


def test_import_positions_refuses_a_naive_observed_at(writer: Session, user: User) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        import_positions(
            writer, user, [], source=ContactSource.ARCHIVE, observed_at=datetime(2026, 9, 20)
        )
