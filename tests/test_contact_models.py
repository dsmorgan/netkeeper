"""Contact models (spec 8.1 and 8.2): constraints, validators, defaults, cascade, and scoping."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import factories
import pytest
from sqlalchemy import Table, UniqueConstraint, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    class_mapper,
    configure_mappers,
    joinedload,
    relationship,
    selectinload,
)

from netkeeper.models import (
    CONTACT_CHILDREN,
    Base,
    Contact,
    ContactAlias,
    ContactChild,
    ContactEmail,
    ContactLink,
    ContactList,
    ContactMet,
    ContactPosition,
    ContactSnapshot,
    ContactSource,
    Interaction,
    InteractionKind,
    ListKind,
    ListMember,
    User,
    UserKind,
    UserOwned,
    linkedin_profile_url,
    normalize_email,
    normalize_public_id,
    single_address,
)
from netkeeper.scoping import (
    UnscopedQueryError,
    get_scoped,
    scoped,
    scoped_count,
    scoped_delete,
    scoped_update,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def users(session: Session) -> tuple[User, User]:
    return factories.make_user(session), factories.make_user(session, kind=UserKind.HOSTED)


def _with_every_child(session: Session, user: User) -> Contact:
    """A contact with one row in every table that names a contact.

    ``list_members`` is one of them (CONTACT_CHILDREN, #81), so it needs a
    static list of its own to belong to.
    """
    contact = factories.make_contact(
        session,
        user,
        emails=["a@example.test"],
        phones=["+15550100"],
        positions=[{"title": "Engineer", "company": "Acme"}],
    )
    contact.links.append(ContactLink(user_id=user.id, url="https://example.test"))
    contact.snapshots.append(ContactSnapshot(user_id=user.id, headline="then"))
    contact.aliases.append(ContactAlias(user_id=user.id, li_public_id="old-slug"))
    contact.interactions.append(
        Interaction(user_id=user.id, kind=InteractionKind.NOTE, at=NOW, summary="hi")
    )
    session.flush()
    listing = ContactList(user_id=user.id, name="First 100", kind=ListKind.STATIC)
    session.add(listing)
    session.flush()
    session.add(ListMember(user_id=user.id, list_id=listing.id, contact_id=contact.id))
    session.flush()
    return contact


# --- factories --------------------------------------------------------------


def test_factories_produce_valid_rows(session: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    assert (alice.id, alice.display_name, alice.kind) == (1, "User 1", UserKind.LOCAL)
    assert (bob.display_name, bob.kind) == ("User 2", UserKind.HOSTED)
    contact = factories.make_contact(
        session,
        alice,
        emails=["A@Example.test", "b@example.test"],
        phones=["+15550100", "555-0101"],
        positions=[{"title": "CTO", "company": "Acme"}, {"title": "Dev", "is_current": False}],
    )
    assert contact.id is not None
    assert contact.user_id == alice.id
    assert (contact.first_name, contact.last_name) == ("First1", "Last1")
    assert contact.preferred_name == "First1"
    assert contact.li_urn == "urn:li:fsd_profile/TEST000001"
    assert contact.li_public_id == "first1-last1"
    assert contact.li_url == linkedin_profile_url("first1-last1")
    assert [(e.email, e.is_primary) for e in contact.emails] == [
        ("a@example.test", True),
        ("b@example.test", False),
    ]
    assert [(p.raw, p.number_e164, p.is_primary) for p in contact.phones] == [
        ("+15550100", "+15550100", True),
        ("555-0101", None, False),
    ]
    assert [(p.title, p.is_current) for p in contact.positions] == [("CTO", True), ("Dev", False)]
    children: list[ContactChild] = [*contact.emails, *contact.phones]
    assert all(child.user_id == alice.id for child in children)
    assert factories.make_contact(session, alice, li_urn=None, li_public_id=None).li_url is None


def test_factory_counters_reset_for_every_test(session: Session) -> None:
    """The autouse fixture in conftest: the previous test also saw User 1 and First1."""
    user = factories.make_user(session)
    assert user.display_name == "User 1"
    assert factories.make_contact(session, user).first_name == "First1"


# --- uniqueness is per user -------------------------------------------------


@pytest.mark.parametrize("column", ["li_urn", "li_public_id"])
def test_linkedin_identity_is_unique_per_user(
    session: Session, users: tuple[User, User], column: str
) -> None:
    alice, bob = users
    shared: dict[str, Any] = {column: "shared"}
    factories.make_contact(session, alice, **shared)
    factories.make_contact(session, bob, **shared)  # another user: fine
    with pytest.raises(IntegrityError):
        factories.make_contact(session, alice, **shared)


def test_contacts_without_linkedin_identity_do_not_collide(
    session: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    for _ in range(2):
        factories.make_contact(session, alice, li_urn=None, li_public_id=None)
    assert session.scalar(scoped_count(alice, Contact)) == 2


def test_alias_slug_is_unique_per_user(session: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    first, second = factories.make_contact(session, alice), factories.make_contact(session, alice)
    first.aliases.append(ContactAlias(user_id=alice.id, li_public_id="Old-Slug"))
    bobs = factories.make_contact(session, bob)
    bobs.aliases.append(ContactAlias(user_id=bob.id, li_public_id="old-slug"))
    session.flush()
    second.aliases.append(ContactAlias(user_id=alice.id, li_public_id="OLD-SLUG"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_address_is_unique_per_contact(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    factories.make_contact(session, alice, emails=["dup@example.test"])
    other = factories.make_contact(session, alice, emails=["dup@example.test"])  # resolver's job
    other.emails.append(ContactEmail(user_id=alice.id, email="DUP@example.test"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_every_unique_constraint_on_an_owned_table_includes_user_id() -> None:
    """Structural (ADR 0005): uniqueness is always per user."""
    owned = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, UserOwned) and isinstance(mapper.local_table, Table)
    }
    offenders = sorted(
        f"{table.name}: {constraint.name}"
        for table in Base.metadata.sorted_tables
        if table.name in owned
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and "user_id" not in {column.name for column in constraint.columns}
    )
    assert offenders == []


# --- CHECK constraints ------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "column"),
    [(Contact, "met"), (Contact, "source"), (ContactEmail, "kind"), (Interaction, "kind")],
)
def test_enum_columns_are_checked_by_the_database(
    session: Session, users: tuple[User, User], model: type[UserOwned], column: str
) -> None:
    alice, _ = users
    _with_every_child(session, alice)
    with pytest.raises(IntegrityError):
        session.execute(scoped_update(alice, model).values({column: "bogus"}))


def test_enum_columns_round_trip_as_enums(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    contact = _with_every_child(session, alice)
    session.expire_all()
    assert contact.met is ContactMet.UNKNOWN
    assert contact.source is ContactSource.MANUAL
    assert contact.interactions[0].kind is InteractionKind.NOTE
    assert contact.created_at.tzinfo is UTC
    assert contact.emails[0].observed_at.tzinfo is UTC


# --- validators and defaults ------------------------------------------------


def test_email_and_slug_are_stored_lowercase(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    contact = factories.make_contact(session, alice, li_public_id=" Bob-Smith ")
    assert contact.li_public_id == "bob-smith"
    assert contact.li_url == "https://www.linkedin.com/in/bob-smith/"
    contact.emails.append(ContactEmail(user_id=alice.id, email="  Bob@Example.COM "))
    contact.aliases.append(ContactAlias(user_id=alice.id, li_public_id=" OLD-Slug "))
    session.flush()
    assert contact.emails[0].email == "bob@example.com"
    assert contact.aliases[0].li_public_id == "old-slug"
    contact.emails[0].email = "BOB@EXAMPLE.ORG"  # assignment after construction too
    assert contact.emails[0].email == "bob@example.org"


def test_empty_values_are_rejected_or_become_none() -> None:
    assert normalize_public_id("  ") is None
    assert normalize_public_id(None) is None
    assert Contact(li_public_id="  ").li_public_id is None
    with pytest.raises(ValueError, match="empty"):
        ContactAlias(li_public_id=" ")
    with pytest.raises(ValueError, match="empty"):
        normalize_email(" ")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bob",
        "bob@",
        "@example.com",
        "bob@@example.com",
        "bob@example..com",
        "bob..smith@example.com",
        "bob@example.com, eve@evil.example",
        "bob@example.com; eve@evil.example",
        "bob@example.com eve@evil.example",
        " bob@example.com",
        "bob@example.com\nBcc: eve@evil.example",
        "friends: bob@example.com, eve@evil.example;",
        "Eve <eve@evil.example>",
        '"Bob Smith" <bob@example.com>',
        '"bob smith"@example.com',
        "bob(comment)@example.com",
        "bob@example.com (Bob)",
        "bob@[192.0.2.1]",
        "bob\\@example.com",
    ],
)
def test_single_address_refuses_all_but_one_bare_address(value: str) -> None:
    """A ``To`` built from any of these sends to another, or a further, recipient (#269)."""
    with pytest.raises(ValueError, match="one bare"):
        single_address(value)


@pytest.mark.parametrize(
    "value", ["bob@example.com", "Bob.Smith+work@mail.example.co.uk", "o'neil@example.com"]
)
def test_single_address_returns_one_bare_address_unchanged(value: str) -> None:
    assert single_address(value) == value


def test_li_url_follows_the_slug_unless_set_by_hand() -> None:
    contact = Contact(li_public_id="first")
    assert contact.li_url == linkedin_profile_url("first")
    contact.li_public_id = "Renamed"
    assert contact.li_url == linkedin_profile_url("renamed")
    contact.li_url = "https://www.linkedin.com/in/custom/"
    contact.li_public_id = "third"
    assert contact.li_url == "https://www.linkedin.com/in/custom/"
    assert Contact(li_public_id="x", li_url="https://custom/").li_url == "https://custom/"
    assert Contact(li_url="https://custom/", li_public_id="x").li_url == "https://custom/"
    assert Contact(first_name="no slug").li_url is None


def test_preferred_name_defaults_to_first_name(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    robert = Contact(user_id=alice.id, first_name="Robert", last_name="Roe")
    blank = Contact(user_id=alice.id, first_name="Ann", last_name="Lee", preferred_name="  ")
    bob = Contact(user_id=alice.id, first_name="Robert", last_name="Roe", preferred_name="Bob")
    nameless = Contact(user_id=alice.id)
    session.add_all([robert, blank, bob, nameless])  # one flush: the multi-row insert path
    session.flush()
    assert [c.preferred_name for c in (robert, blank, bob)] == ["Robert", "Ann", "Bob"]
    assert (nameless.first_name, nameless.last_name, nameless.preferred_name) == ("", "", "")
    # A Core insert takes the same default.
    session.execute(insert(Contact).values(user_id=alice.id, first_name="Cy", last_name="Yu"))
    cy = session.scalars(scoped(alice, Contact).where(Contact.first_name == "Cy")).one()
    assert cy.preferred_name == "Cy"
    # Clearing it on a stored row (the PATCH case) falls back to first_name, not NULL.
    bob.preferred_name = "  "
    session.flush()
    session.refresh(bob)
    assert bob.preferred_name == "Robert"


def test_defaults_and_dates(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    contact = factories.make_contact(
        session,
        alice,
        connected_on=date(2024, 1, 2),
        positions=[{"title": "x", "started_on": date(2020, 3, 1), "ended_on": None}],
    )
    session.expire_all()
    assert (contact.degree, contact.met, contact.do_not_contact) == (1, ContactMet.UNKNOWN, False)
    assert (contact.li_missing_count, contact.enrich_priority) == (0, 0)
    assert contact.connected_on == date(2024, 1, 2)
    assert contact.positions[0].started_on == date(2020, 3, 1)
    assert contact.positions[0].ended_on is None
    assert contact.archived_at is None and contact.merged_into_id is None


# --- children: ordering, cascade, ownership ---------------------------------


def test_children_are_ordered_sensibly(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    contact = factories.make_contact(session, alice)
    contact.emails.append(ContactEmail(user_id=alice.id, email="second@example.test"))
    contact.emails.append(
        ContactEmail(user_id=alice.id, email="primary@example.test", is_primary=True)
    )
    contact.positions.extend(
        [
            ContactPosition(user_id=alice.id, title="old", started_on=date(2010, 1, 1)),
            ContactPosition(user_id=alice.id, title="undated"),
            ContactPosition(user_id=alice.id, title="now", is_current=True),
            ContactPosition(user_id=alice.id, title="recent", started_on=date(2020, 1, 1)),
        ]
    )
    for hours in (1, 3, 2):
        moment = NOW + timedelta(hours=hours)
        contact.snapshots.append(
            ContactSnapshot(user_id=alice.id, headline=str(hours), observed_at=moment)
        )
        contact.interactions.append(
            Interaction(user_id=alice.id, kind=InteractionKind.CALL, at=moment, summary=str(hours))
        )
    session.flush()
    session.expire_all()
    assert [e.email for e in contact.emails] == ["primary@example.test", "second@example.test"]
    assert [p.title for p in contact.positions] == ["now", "recent", "old", "undated"]
    assert [s.headline for s in contact.snapshots] == ["3", "2", "1"]
    assert [i.summary for i in contact.interactions] == ["3", "2", "1"]


def test_scoped_delete_removes_children_through_the_database(
    session: Session, users: tuple[User, User]
) -> None:
    """scoped_delete() is a Core delete: only the ON DELETE CASCADE can reach the children."""
    alice, bob = users
    _with_every_child(session, alice)
    _with_every_child(session, bob)
    session.execute(scoped_delete(alice, Contact))
    assert session.scalar(scoped_count(alice, Contact)) == 0
    for child in CONTACT_CHILDREN:
        assert session.scalar(scoped_count(alice, child)) == 0, child.__name__
        assert session.scalar(scoped_count(bob, child)) == 1, child.__name__


def test_orm_delete_removes_children_too(session: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    contact = _with_every_child(session, alice)
    session.delete(contact)
    session.flush()
    for child in CONTACT_CHILDREN:
        assert session.scalar(scoped_count(alice, child)) == 0, child.__name__


def test_removing_a_child_from_its_collection_deletes_it(
    session: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = factories.make_contact(session, alice, emails=["a@example.test", "b@example.test"])
    del contact.emails[1]
    session.flush()
    assert session.scalar(scoped_count(alice, ContactEmail)) == 1


def test_deleting_the_merge_winner_clears_merged_into(
    session: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    winner, loser = factories.make_contact(session, alice), factories.make_contact(session, alice)
    loser.merged_into = winner
    session.flush()
    assert loser.merged_into_id == winner.id
    session.delete(winner)
    session.flush()
    session.refresh(loser)
    assert loser.merged_into_id is None


def test_a_child_needs_its_own_user_id(session: Session, users: tuple[User, User]) -> None:
    """Nothing copies user_id from the parent; the guard names the row instead."""
    alice, _ = users
    contact = factories.make_contact(session, alice)
    contact.emails.append(ContactEmail(email="x@example.test"))
    with pytest.raises(UnscopedQueryError, match="new ContactEmail has no user_id"):
        session.flush()


# --- scoping ----------------------------------------------------------------


def test_scoped_never_returns_another_users_contacts(
    session: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    mine = [factories.make_contact(session, alice) for _ in range(2)]
    theirs = factories.make_contact(session, bob)
    assert set(session.scalars(scoped(alice, Contact))) == set(mine)
    assert list(session.scalars(scoped(bob, Contact))) == [theirs]
    assert session.scalar(scoped_count(alice, Contact)) == 2
    assert get_scoped(session, alice, Contact, theirs.id) is None
    assert get_scoped(session, bob, Contact, theirs.id) is theirs


def test_loader_options_from_an_owned_root_pass_the_guard(
    session: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    _with_every_child(session, alice)
    session.expunge_all()
    contact = session.scalars(scoped(alice, Contact).options(selectinload(Contact.emails))).one()
    assert [e.email for e in contact.emails] == ["a@example.test"]
    session.expunge_all()
    contact = (
        session.scalars(scoped(alice, Contact).options(joinedload(Contact.positions)))
        .unique()
        .one()
    )
    assert [p.title for p in contact.positions] == ["Engineer"]
    assert contact.phones[0].raw == "+15550100"  # a lazy load from an owned parent
    assert contact.phones[0].contact is contact


def test_every_contact_relationship_is_owned_to_owned() -> None:
    configure_mappers()
    models: tuple[type[UserOwned], ...] = (Contact, *CONTACT_CHILDREN)
    for model in models:
        for rel in class_mapper(model).relationships:
            if rel.key == "user":
                continue
            assert issubclass(rel.mapper.class_, UserOwned), f"{model.__name__}.{rel.key}"


class _Throwaway(DeclarativeBase):
    """Its own registry, so the structural test in test_scoping never sees this mapper."""


class Owner(_Throwaway):
    """The users table with the relationship the rule forbids, to test the guard."""

    __table__ = User.__table__
    id: Mapped[int]
    contacts: Mapped[list[Contact]] = relationship(Contact, viewonly=True)


def test_a_nested_loader_chain_from_an_unowned_root_is_blocked(
    session: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    factories.make_contact(session, alice, emails=["a@example.test"])
    statement = select(Owner).options(selectinload(Owner.contacts).selectinload(Contact.emails))
    with pytest.raises(UnscopedQueryError, match=r"relationship Owner\.contacts reaches Contact"):
        session.scalars(statement).all()
