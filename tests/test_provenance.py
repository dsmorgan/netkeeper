"""netkeeper.crm.provenance (spec 10.5): the precedence rule, and contacts.field_sources."""

from __future__ import annotations

from datetime import UTC, date, datetime

import factories
import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from netkeeper.crm.provenance import (
    PERSON_OWNED_FIELDS,
    PROVENANCE_FIELDS,
    SOURCE_RANK,
    may_overwrite,
    set_manual_field,
)
from netkeeper.models import Contact, ContactMet, ContactSource
from netkeeper.scoping import scoped


def test_rank_order_covers_every_source() -> None:
    assert SOURCE_RANK == {"sync": 3, "archive": 2, "csv": 1, "manual": 0}
    assert set(SOURCE_RANK) == {member.value for member in ContactSource}


def test_provenance_fields_are_contact_columns_and_never_person_owned() -> None:
    columns = set(Contact.__table__.c.keys())
    assert columns >= PROVENANCE_FIELDS
    assert columns >= PERSON_OWNED_FIELDS
    assert not PROVENANCE_FIELDS & PERSON_OWNED_FIELDS
    assert "source" not in PROVENANCE_FIELDS and "field_sources" not in PROVENANCE_FIELDS


@pytest.mark.parametrize(
    ("recorded", "incoming", "expected"),
    [
        ("sync", "sync", True),  # equal rank: a newer sync updates an older one
        ("sync", "archive", False),
        ("sync", "csv", False),
        ("sync", "manual", False),
        ("archive", "sync", True),
        ("archive", "csv", False),
        ("csv", "archive", True),
        ("csv", "manual", False),
        ("manual", "csv", True),
        ("manual", "sync", True),
    ],
)
def test_may_overwrite_compares_ranks(recorded: str, incoming: str, expected: bool) -> None:
    contact = Contact(headline="as recorded", field_sources={"headline": recorded})
    assert may_overwrite("headline", incoming, contact) is expected
    assert may_overwrite("headline", ContactSource(incoming), contact) is expected


def test_unrecorded_or_empty_fields_are_free_to_any_source() -> None:
    assert may_overwrite("headline", "manual", Contact(headline="nobody recorded this"))
    assert may_overwrite(
        "headline", "csv", Contact(headline=None, field_sources={"headline": "sync"})
    )
    assert may_overwrite(
        "first_name", "csv", Contact(first_name="", field_sources={"first_name": "sync"})
    )
    assert may_overwrite("connected_on", "csv", Contact(field_sources={"connected_on": "sync"}))


def test_person_owned_fields_take_manual_and_nothing_else() -> None:
    contact = Contact(preferred_name="Bob", notes="n")
    for field in PERSON_OWNED_FIELDS:
        assert may_overwrite(field, "manual", contact), field
        for source in ("sync", "archive", "csv"):
            assert not may_overwrite(field, source, contact), (field, source)


def test_unknown_field_or_source_raises() -> None:
    with pytest.raises(ValueError, match="carries no provenance"):
        may_overwrite("degree", "sync", Contact())
    with pytest.raises(ValueError):
        may_overwrite("headline", "bogus", Contact(headline="x"))


# --- the columns ------------------------------------------------------------


def test_field_sources_round_trips_and_tracks_in_place_changes(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(session, user)
    assert contact.field_sources == {}
    contact.field_sources = {"li_urn": "sync", "headline": "sync"}
    session.flush()
    session.expire_all()
    assert contact.field_sources == {"li_urn": "sync", "headline": "sync"}
    contact.field_sources["headline"] = "csv"  # a MutableDict: no reassignment needed
    session.flush()
    session.expire_all()
    assert contact.field_sources == {"li_urn": "sync", "headline": "csv"}
    session.execute(insert(Contact).values(user_id=user.id, first_name="Core", last_name="Row"))
    core = session.scalars(scoped(user, Contact).where(Contact.first_name == "Core")).one()
    assert core.field_sources == {}


def test_last_contacted_at_starts_empty_and_round_trips_aware(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(session, user)
    assert contact.last_contacted_at is None
    contact.last_contacted_at = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    session.flush()
    session.expire_all()
    assert contact.last_contacted_at == datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert contact.last_contacted_at.tzinfo is UTC


# --- set_manual_field -------------------------------------------------------


def test_set_manual_field_writes_and_stamps_manual(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(
        session, user, field_sources={"headline": "sync"}, notes=None, preferred_name=""
    )
    set_manual_field(contact, "headline", "typed")
    set_manual_field(contact, "connected_on", date(2020, 1, 2))
    assert (contact.headline, contact.connected_on) == ("typed", date(2020, 1, 2))
    assert contact.field_sources == {"headline": "manual", "connected_on": "manual"}
    # By design (spec 10.5) any import may overwrite a manual LinkedIn-field edit.
    assert may_overwrite("headline", "csv", contact)
    set_manual_field(contact, "notes", "hello")
    set_manual_field(contact, "met", ContactMet.MET)
    set_manual_field(contact, "preferred_name", "Bob")
    assert (contact.notes, contact.met, contact.preferred_name) == ("hello", ContactMet.MET, "Bob")
    assert contact.field_sources == {"headline": "manual", "connected_on": "manual"}
    assert not may_overwrite("notes", "sync", contact)
    session.flush()
    session.expire_all()
    assert (contact.headline, contact.field_sources["headline"]) == ("typed", "manual")
    assert (contact.notes, contact.met, contact.preferred_name) == ("hello", ContactMet.MET, "Bob")
    set_manual_field(contact, "notes", None)
    assert contact.notes is None


def test_set_manual_field_before_the_first_flush_and_for_other_columns() -> None:
    fresh = Contact()  # field_sources is unset until the column default runs at insert
    set_manual_field(fresh, "headline", "x")
    set_manual_field(fresh, "location", "Berlin")
    assert fresh.field_sources == {"headline": "manual", "location": "manual"}
    for field in ("degree", "source", "field_sources", "archived_at", "do_not_contact", "bogus"):
        with pytest.raises(ValueError, match="neither a provenance nor a person-owned"):
            set_manual_field(fresh, field, "x")
