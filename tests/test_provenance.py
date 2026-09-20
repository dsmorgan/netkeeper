"""netkeeper.crm.provenance (spec 10.5): the precedence rule, contacts.field_sources,
and the synced-values ledger a manual override reverts to (CP1, #28)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import factories
import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from netkeeper.crm.provenance import (
    PERSON_OWNED_FIELDS,
    PROVENANCE_FIELDS,
    PROVENANCE_ORDER,
    SOURCE_RANK,
    may_overwrite,
    overridden_fields,
    record_synced_value,
    revert_to_synced,
    set_manual_field,
)
from netkeeper.models import Contact, ContactMet, ContactSource
from netkeeper.scoping import scoped

OBSERVED = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
OBSERVED_ISO = "2026-09-20T12:00:00+00:00"


def test_rank_order_covers_every_source_and_manual_is_on_top() -> None:
    assert SOURCE_RANK == {"manual": 4, "sync": 3, "archive": 2, "csv": 1}
    assert set(SOURCE_RANK) == {member.value for member in ContactSource}


def test_provenance_fields_are_contact_columns_and_never_person_owned() -> None:
    columns = set(Contact.__table__.c.keys())
    assert frozenset(PROVENANCE_ORDER) == PROVENANCE_FIELDS
    assert len(PROVENANCE_ORDER) == len(PROVENANCE_FIELDS)
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
        ("sync", "manual", True),  # a person's edit outranks the sync
        ("archive", "sync", True),
        ("archive", "csv", False),
        ("archive", "manual", True),
        ("csv", "archive", True),
        ("csv", "manual", True),
        ("manual", "manual", True),  # a later edit replaces an earlier one
        ("manual", "sync", False),  # the override sticks until revert_to_synced()
        ("manual", "archive", False),
        ("manual", "csv", False),
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


@pytest.mark.parametrize("cleared", [None, ""])
def test_a_field_cleared_by_hand_is_an_override_too(cleared: str | None) -> None:
    contact = Contact(headline="from sync", field_sources={"headline": "sync"})
    set_manual_field(contact, "headline", cleared)
    assert (contact.headline, contact.field_sources["headline"]) == (cleared, "manual")
    for source in ("sync", "archive", "csv"):
        assert not may_overwrite("headline", source, contact), source
    assert may_overwrite("headline", "manual", contact)
    # Emptied by anything but a person (a revert to a null synced value), it is free again.
    record_synced_value(contact, "headline", None, source="sync", observed_at=OBSERVED)
    revert_to_synced(contact, "headline")
    assert (contact.headline, contact.field_sources["headline"]) == (None, "sync")
    assert may_overwrite("headline", "csv", contact)


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
    # The edit sticks (spec 10.5, CP1): no import or sync may overwrite it, only another edit.
    assert not may_overwrite("headline", "sync", contact)
    assert may_overwrite("headline", "manual", contact)
    assert contact.synced_values == {}  # an edit is never a ledger entry
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


# --- the synced-values ledger -----------------------------------------------


def test_synced_values_round_trips_and_tracks_in_place_changes(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(session, user)
    assert contact.synced_values == {}
    assert record_synced_value(
        contact, "headline", "from sync", source="sync", observed_at=OBSERVED
    )
    assert record_synced_value(
        contact,
        "connected_on",
        date(2020, 1, 2),
        source=ContactSource.ARCHIVE,
        observed_at=OBSERVED,
    )
    session.flush()
    session.expire_all()
    assert contact.synced_values == {
        "headline": {"value": "from sync", "source": "sync", "observed_at": OBSERVED_ISO},
        "connected_on": {"value": "2020-01-02", "source": "archive", "observed_at": OBSERVED_ISO},
    }
    # A MutableDict: a later entry is flushed without reassigning the column.
    record_synced_value(contact, "headline", "newer", source="csv", observed_at=OBSERVED)
    session.flush()
    session.expire_all()
    assert contact.synced_values["headline"] == {
        "value": "newer",
        "source": "csv",
        "observed_at": OBSERVED_ISO,
    }
    assert contact.headline == "Title1 at Company1"  # the ledger never touches the column
    session.execute(insert(Contact).values(user_id=user.id, first_name="Core", last_name="Row"))
    core = session.scalars(scoped(user, Contact).where(Contact.first_name == "Core")).one()
    assert core.synced_values == {}


def test_record_synced_value_keeps_the_newest_observation() -> None:
    contact = Contact(headline="live")  # synced_values is unset until the first flush
    assert record_synced_value(contact, "headline", "first", source="csv", observed_at=OBSERVED)
    first = {"value": "first", "source": "csv", "observed_at": OBSERVED_ISO}
    assert contact.synced_values == {"headline": first}
    # Older is dropped, even from a higher-ranked source: the ledger is chronological.
    assert not record_synced_value(
        contact, "headline", "stale", source="sync", observed_at=OBSERVED - timedelta(seconds=1)
    )
    assert contact.synced_values == {"headline": first}
    # The same instant replaces, as it does for a child row.
    assert record_synced_value(
        contact, "headline", "same time", source="archive", observed_at=OBSERVED
    )
    assert contact.synced_values["headline"]["value"] == "same time"
    # Newer, in another zone: stored in UTC.
    later = datetime(2026, 9, 21, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    assert record_synced_value(contact, "headline", "newest", source="sync", observed_at=later)
    assert contact.synced_values["headline"] == {
        "value": "newest",
        "source": "sync",
        "observed_at": "2026-09-21T00:00:00+00:00",
    }
    assert contact.headline == "live"


def test_record_synced_value_refuses_manual_unknown_fields_and_naive_times() -> None:
    contact = Contact()
    with pytest.raises(ValueError, match="manual is not a synced source"):
        record_synced_value(contact, "headline", "x", source="manual", observed_at=OBSERVED)
    with pytest.raises(ValueError, match="carries no provenance"):
        record_synced_value(contact, "notes", "x", source="sync", observed_at=OBSERVED)
    with pytest.raises(ValueError, match="timezone-aware"):
        record_synced_value(
            contact, "headline", "x", source="sync", observed_at=OBSERVED.replace(tzinfo=None)
        )
    with pytest.raises(ValueError):
        record_synced_value(contact, "headline", "x", source="bogus", observed_at=OBSERVED)
    assert not contact.synced_values


def test_revert_to_synced_restores_the_value_and_its_source(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(
        session,
        user,
        headline="from sync",
        connected_on=date(2020, 1, 2),
        field_sources={"headline": "sync", "connected_on": "archive"},
    )
    record_synced_value(contact, "headline", "from sync", source="sync", observed_at=OBSERVED)
    record_synced_value(
        contact, "connected_on", date(2020, 1, 2), source="archive", observed_at=OBSERVED
    )
    assert overridden_fields(contact) == []
    set_manual_field(contact, "headline", "typed")
    set_manual_field(contact, "connected_on", date(1999, 9, 9))
    assert overridden_fields(contact) == ["headline", "connected_on"]
    session.flush()
    session.expire_all()
    revert_to_synced(contact, "headline")
    assert (contact.headline, contact.field_sources["headline"]) == ("from sync", "sync")
    assert overridden_fields(contact) == ["connected_on"]
    revert_to_synced(contact, "connected_on")  # a date comes back as a date
    assert (contact.connected_on, contact.field_sources["connected_on"]) == (
        date(2020, 1, 2),
        "archive",
    )
    assert overridden_fields(contact) == []
    assert may_overwrite("headline", "sync", contact)  # the sync may write again
    assert not may_overwrite("connected_on", "csv", contact)  # archive over csv, as before
    session.flush()
    session.expire_all()
    assert contact.headline == "from sync"
    assert contact.synced_values["headline"]["value"] == "from sync"  # the entry stays


def test_revert_to_synced_refuses_a_field_never_synced_or_without_provenance() -> None:
    contact = Contact(headline="typed", field_sources={"headline": "manual"})
    with pytest.raises(ValueError, match="headline has no synced value to revert to"):
        revert_to_synced(contact, "headline")
    record_synced_value(contact, "location", "Berlin", source="sync", observed_at=OBSERVED)
    with pytest.raises(ValueError, match="headline has no synced value to revert to"):
        revert_to_synced(contact, "headline")
    for field in ("notes", "preferred_name", "met", "degree", "source", "bogus"):
        with pytest.raises(ValueError, match="carries no provenance"):
            revert_to_synced(contact, field)
    assert (contact.headline, contact.field_sources) == ("typed", {"headline": "manual"})


def test_revert_to_synced_before_the_first_flush_and_a_null_value() -> None:
    contact = Contact(first_name="Typed", location="Typed")  # field_sources unset too
    record_synced_value(contact, "first_name", None, source="sync", observed_at=OBSERVED)
    record_synced_value(contact, "location", None, source="sync", observed_at=OBSERVED)
    revert_to_synced(contact, "first_name")
    revert_to_synced(contact, "location")
    assert (contact.first_name, contact.location) == ("", None)  # a name is '' when unknown
    assert contact.field_sources == {"first_name": "sync", "location": "sync"}


def test_overridden_fields_lists_edits_that_hide_a_different_synced_value() -> None:
    contact = Contact(
        headline="typed", current_title="CTO", location="Berlin", first_name="", field_sources={}
    )
    assert overridden_fields(contact) == []  # nothing edited, nothing synced
    reported = {
        "headline": "from sync",
        "current_title": "CTO",
        "location": "Paris",
        "first_name": None,
    }
    for field, value in reported.items():
        record_synced_value(contact, field, value, source="sync", observed_at=OBSERVED)
    assert overridden_fields(contact) == []  # nothing edited
    for field in ("headline", "current_title", "location", "first_name", "last_name"):
        contact.field_sources[field] = "manual"
    # headline and location differ, in PROVENANCE_ORDER; current_title equals its synced
    # value; '' equals a null synced name; last_name was never synced: nothing to revert to.
    assert overridden_fields(contact) == ["headline", "location"]
