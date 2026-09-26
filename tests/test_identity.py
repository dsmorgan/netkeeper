"""netkeeper.crm.identity (spec 8.2): resolution, apply under provenance (spec 10.5), and merge."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import factories
import pytest
from sqlalchemy import Table
from sqlalchemy.orm import Session, class_mapper, sessionmaker

from netkeeper.crm.identity import (
    JOB_FIELDS,
    LINK_SCHEMES,
    Candidate,
    CreateNew,
    IncomingContact,
    IncomingEmail,
    IncomingLink,
    IncomingPhone,
    IncomingPosition,
    Matched,
    MergeInto,
    New,
    apply,
    merge,
    phone_key,
    position_key,
    public_id_from_url,
    resolve,
    resolve_survivor,
)
from netkeeper.crm.lists import add_members, create_list, list_members, member_count
from netkeeper.crm.provenance import (
    PROVENANCE_FIELDS,
    PROVENANCE_ORDER,
    overridden_fields,
    revert_to_synced,
    set_manual_field,
)
from netkeeper.crm.tags import create_rule, create_tag, run_rules, tag_contact, untag_contact
from netkeeper.db import is_writer, session_scope
from netkeeper.models import (
    CONTACT_CHILDREN,
    Contact,
    ContactAlias,
    ContactEmail,
    ContactLink,
    ContactMet,
    ContactSnapshot,
    ContactSource,
    ContactTag,
    ContactTagSuppression,
    EmailKind,
    Interaction,
    InteractionKind,
    LinkKind,
    ListKind,
    ListMember,
    MetSource,
    RuleField,
    Tag,
    TagSource,
    User,
    UserOwned,
)
from netkeeper.scoping import scoped, scoped_count

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
EARLIER = NOW - timedelta(days=1)


def synced(value: str | None, source: str = "sync", at: datetime = NOW) -> dict[str, str | None]:
    """A ``synced_values`` entry as the ledger stores it."""
    return {"value": value, "source": source, "observed_at": at.isoformat()}


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller of this module must use: it reads, then writes."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def users(writer: Session) -> tuple[User, User]:
    return factories.make_user(writer), factories.make_user(writer)


def incoming(source: ContactSource = ContactSource.SYNC, **fields: object) -> IncomingContact:
    fields.setdefault("observed_at", NOW)
    return IncomingContact(source=source, **fields)  # type: ignore[arg-type]


def counts(session: Session, user: User) -> dict[str, int]:
    """Row counts per table for ``user``, contacts included."""
    result: dict[str, int] = {}
    models: tuple[type[UserOwned], ...] = (Contact, *CONTACT_CHILDREN)
    for model in models:
        table = class_mapper(model).local_table
        assert isinstance(table, Table)
        result[table.name] = session.scalar(scoped_count(user, model)) or 0
    return result


def columns_of(contact: Contact) -> dict[str, object]:
    """Every column value of ``contact``, provenance and timestamps included."""
    return {column.key: getattr(contact, column.key) for column in Contact.__table__.c}


def aliases_of(contact: Contact) -> list[str]:
    return sorted(alias.li_public_id for alias in contact.aliases)


# --- normalization ----------------------------------------------------------


def test_provenance_order_is_exactly_the_provenance_fields() -> None:
    assert set(PROVENANCE_ORDER) == PROVENANCE_FIELDS
    assert len(PROVENANCE_ORDER) == len(PROVENANCE_FIELDS)
    assert set(JOB_FIELDS) < PROVENANCE_FIELDS


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/in/Ann-Lee/", "ann-lee"),
        ("https://www.linkedin.com/in/ann-lee", "ann-lee"),
        ("http://linkedin.com/in/ann-lee/", "ann-lee"),
        ("linkedin.com/in/ann-lee", "ann-lee"),
        ("www.linkedin.com/in/ann-lee/?trk=contact", "ann-lee"),
        ("https://uk.linkedin.com/in/ann-lee#top", "ann-lee"),
        ("  https://www.linkedin.com/in/ann-lee/  ", "ann-lee"),
        ("https://www.linkedin.com/in/ren%C3%A9-l%C3%B6we/", "rené-löwe"),
        ("https://www.linkedin.com/pub/ann-lee/1/2/3", None),
        ("https://www.linkedin.com/company/acme/", None),
        ("https://www.linkedin.com/in/", None),
        ("https://example.test/in/ann-lee/", None),
        ("https://notlinkedin.com/in/ann-lee/", None),
        ("urn:li:fsd_profile/ACoAAA", None),
        ("", None),
        (None, None),
    ],
)
def test_public_id_from_url(url: str | None, expected: str | None) -> None:
    assert public_id_from_url(url) == expected


def test_incoming_contact_normalizes_identity_and_text() -> None:
    row = incoming(
        li_urn="  urn:li:fsd_profile/ABC  ",
        li_public_id="  Ann-Lee%2Dx ",
        li_url="linkedin.com/in/ignored",
        first_name="  Ann ",
        last_name="",
        headline="   ",
        location=" Berlin ",
    )
    assert row.li_urn == "urn:li:fsd_profile/ABC"
    assert row.li_public_id == "ann-lee-x"
    assert row.li_url == "https://www.linkedin.com/in/ann-lee-x/"
    assert (row.first_name, row.last_name, row.headline, row.location) == (
        "Ann",
        None,
        None,
        "Berlin",
    )
    assert row.provided_fields() == {
        "li_urn": "urn:li:fsd_profile/ABC",
        "li_public_id": "ann-lee-x",
        "li_url": "https://www.linkedin.com/in/ann-lee-x/",
        "first_name": "Ann",
        "location": "Berlin",
    }
    assert list(row.provided_fields()) == [
        name for name in PROVENANCE_ORDER if name in row.provided_fields()
    ]


def test_incoming_contact_derives_the_slug_from_the_url_never_from_the_urn() -> None:
    from_url = incoming(li_url="https://www.linkedin.com/in/Ann-Lee/")
    assert (from_url.li_public_id, from_url.li_url) == (
        "ann-lee",
        "https://www.linkedin.com/in/ann-lee/",
    )
    other_url = incoming(li_url="https://www.linkedin.com/pub/ann-lee/1/2/3")
    assert (other_url.li_public_id, other_url.li_url) == (
        None,
        "https://www.linkedin.com/pub/ann-lee/1/2/3",
    )
    from_urn = incoming(li_urn="urn:li:fsd_profile/ABC")
    assert (from_urn.li_public_id, from_urn.li_url) == (None, None)
    assert incoming(li_public_id="x", li_url="").li_url == "https://www.linkedin.com/in/x/"


def test_incoming_children_normalize_and_deduplicate() -> None:
    row = incoming(
        emails=(
            IncomingEmail(" Ann@Example.TEST ", kind=EmailKind.WORK),
            IncomingEmail("ann@example.test", is_primary=True),
            IncomingEmail("b@example.test"),
        ),
        phones=(
            IncomingPhone(" +1 (555) 010-0100 "),
            IncomingPhone("15550100100", number_e164="+15550100100"),
            IncomingPhone("555-0101"),
        ),
        links=(
            IncomingLink(" https://a.test "),
            IncomingLink("https://a.test", kind=LinkKind.WEBSITE),
        ),
        positions=(
            IncomingPosition(title=" Engineer ", company="Acme", started_on=date(2020, 1, 1)),
            IncomingPosition(title="engineer", company="ACME", started_on=date(2020, 1, 1)),
            IncomingPosition(title="Engineer", company="Acme"),
        ),
    )
    assert [(e.email, e.kind, e.is_primary) for e in row.emails] == [
        ("ann@example.test", EmailKind.WORK, False),
        ("b@example.test", EmailKind.OTHER, False),
    ]
    assert [(p.raw, p.number_e164, p.key) for p in row.phones] == [
        ("+1 (555) 010-0100", "+15550100100", "15550100100"),
        ("555-0101", None, "5550101"),
    ]
    assert [(l.url, l.kind) for l in row.links] == [("https://a.test", LinkKind.OTHER)]  # noqa: E741
    assert [(p.title, p.company, p.started_on) for p in row.positions] == [
        ("Engineer", "Acme", date(2020, 1, 1)),
        ("Engineer", "Acme", None),
    ]
    assert phone_key("+1 (555) 010-0100") == "15550100100"
    assert position_key(" ACME ", "Engineer", None) == ("acme", "engineer", None)
    assert position_key(None, "", None) == (None, None, None)


def test_incoming_rejects_what_it_cannot_use() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        IncomingContact(source=ContactSource.CSV, observed_at=datetime(2026, 9, 20, 12, 0))
    with pytest.raises(ValueError, match="empty"):
        IncomingEmail("  ")
    with pytest.raises(ValueError, match="digits"):
        IncomingPhone("call me")
    with pytest.raises(ValueError, match="empty"):
        IncomingLink(" ")
    with pytest.raises(ValueError, match="title or a company"):
        IncomingPosition(started_on=date(2020, 1, 1))
    with pytest.raises(ValueError):
        incoming(source="bogus")  # type: ignore[arg-type]


# --- resolve ----------------------------------------------------------------


def test_resolve_by_urn_is_per_user(writer: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    mine = factories.make_contact(writer, alice, li_urn="urn:li:fsd_profile/X", emails=["x@a.test"])
    theirs = factories.make_contact(writer, bob, li_urn="urn:li:fsd_profile/X", emails=["x@a.test"])
    row = incoming(
        li_urn="urn:li:fsd_profile/X",
        li_public_id="unknown-slug",
        emails=(IncomingEmail("other@a.test"),),
    )
    assert resolve(writer, alice, row) == Matched(mine.id, by="urn")
    assert resolve(writer, bob, row) == Matched(theirs.id, by="urn")
    only_urn = incoming(li_urn="urn:li:fsd_profile/X")
    assert resolve(writer, alice, only_urn) == Matched(mine.id, by="urn")
    assert resolve(writer, alice, incoming(li_urn="urn:li:fsd_profile/Y")) == New()


def test_resolve_by_public_id_is_case_insensitive_and_adopts_the_urn(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    mine = factories.make_contact(
        writer, alice, li_urn=None, li_public_id="ann-lee", source=ContactSource.CSV
    )
    factories.make_contact(writer, bob, li_urn=None, li_public_id="ann-lee")
    row = incoming(li_urn="urn:li:fsd_profile/ANN", li_url="https://www.linkedin.com/in/Ann-Lee/")
    assert resolve(writer, alice, row) == Matched(mine.id, by="public_id")
    assert mine.li_urn == "urn:li:fsd_profile/ANN"
    assert mine.field_sources == {"li_urn": "sync"}
    # Now step 1 finds it; and the URN is not on bob's contact.
    assert resolve(writer, alice, row) == Matched(mine.id, by="urn")
    assert resolve(writer, bob, incoming(li_urn="urn:li:fsd_profile/ANN")) == New()
    # Without a URN there is nothing to adopt.
    other = factories.make_contact(writer, alice, li_urn=None, li_public_id="cy-yu")
    assert resolve(writer, alice, incoming(li_public_id="CY-YU")) == Matched(
        other.id, by="public_id"
    )
    assert other.li_urn is None


def test_resolve_by_alias_after_a_slug_change(writer: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    mine = factories.make_contact(writer, alice, li_public_id="old-slug")
    apply(writer, alice, incoming(li_public_id="new-slug"), Matched(mine.id, by="urn"))
    assert mine.li_public_id == "new-slug"
    assert aliases_of(mine) == ["old-slug"]
    assert resolve(writer, alice, incoming(li_public_id="OLD-SLUG")) == Matched(mine.id, by="alias")
    assert resolve(writer, alice, incoming(li_public_id="new-slug")) == Matched(
        mine.id, by="public_id"
    )
    assert resolve(writer, bob, incoming(li_public_id="old-slug")) == New()


def test_resolve_by_email_is_per_user(writer: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    mine = factories.make_contact(
        writer, alice, li_urn=None, li_public_id=None, emails=["ann@a.test"]
    )
    factories.make_contact(writer, bob, li_urn=None, li_public_id=None, emails=["ann@a.test"])
    row = incoming(emails=(IncomingEmail("nobody@a.test"), IncomingEmail(" ANN@A.test ")))
    assert resolve(writer, alice, row) == Matched(mine.id, by="email")
    assert resolve(writer, alice, incoming(emails=(IncomingEmail("nobody@a.test"),))) == New()
    assert resolve(writer, alice, incoming()) == New()


def test_resolve_by_name_and_company_is_a_candidate(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    mine = factories.make_contact(
        writer, alice, first_name=" Ann", last_name="Lee ", current_company="Acme", li_urn=None
    )
    factories.make_contact(writer, bob, first_name="Ann", last_name="Lee", current_company="Acme")
    factories.make_contact(
        writer, alice, first_name="Ann", last_name="Lee", current_company="Other"
    )
    row = incoming(first_name="ann ", last_name=" LEE", current_company="acme")
    assert resolve(writer, alice, row) == Candidate((mine.id,), by="name")
    # All three parts are needed; two of them never match anyone.
    assert resolve(writer, alice, incoming(first_name="Ann", last_name="Lee")) == New()
    assert resolve(writer, alice, incoming(first_name="Ann", current_company="Acme")) == New()


def test_a_shared_email_is_a_candidate_with_ids_ascending(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    second = factories.make_contact(writer, alice, emails=["shared@a.test"])
    first = factories.make_contact(writer, alice, emails=["shared@a.test"])
    assert second.id < first.id
    row = incoming(emails=(IncomingEmail("shared@a.test"),))
    assert resolve(writer, alice, row) == Candidate((second.id, first.id), by="email")


def test_identity_disagreement_is_a_candidate(writer: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    ann = factories.make_contact(writer, alice, li_urn="urn:li:fsd_profile/ANN", li_public_id="ann")
    cy = factories.make_contact(
        writer, alice, li_urn="urn:li:fsd_profile/CY", li_public_id="cy", emails=["cy@a.test"]
    )
    # The slug's contact carries another URN: someone else may hold the slug now.
    reassigned = incoming(li_urn="urn:li:fsd_profile/NEW", li_public_id="ann")
    assert resolve(writer, alice, reassigned) == Candidate((ann.id,), by="identity")
    assert ann.li_urn == "urn:li:fsd_profile/ANN"
    # The URN and the slug point at different contacts.
    split = incoming(li_urn="urn:li:fsd_profile/ANN", li_public_id="cy")
    assert resolve(writer, alice, split) == Candidate((ann.id, cy.id), by="identity")
    # An email match whose contact carries another URN.
    by_email = incoming(li_urn="urn:li:fsd_profile/NEW", emails=(IncomingEmail("cy@a.test"),))
    assert resolve(writer, alice, by_email) == Candidate((cy.id,), by="email")
    # The same URN on both sides is a step 1 match, whatever else the row carries.
    agreed = incoming(li_urn="urn:li:fsd_profile/CY", emails=(IncomingEmail("cy@a.test"),))
    assert resolve(writer, alice, agreed) == Matched(cy.id, by="urn")


def test_steps_run_in_order_and_the_first_hit_wins(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    by_urn = factories.make_contact(writer, alice, li_urn="urn:li:fsd_profile/A", li_public_id="a")
    by_email = factories.make_contact(
        writer, alice, li_urn=None, li_public_id="b", emails=["b@a.test"]
    )
    by_name = factories.make_contact(
        writer,
        alice,
        li_urn=None,
        li_public_id=None,
        first_name="N",
        last_name="M",
        current_company="C",
    )
    everything = incoming(
        li_urn="urn:li:fsd_profile/A",
        emails=(IncomingEmail("b@a.test"),),
        first_name="N",
        last_name="M",
        current_company="C",
    )
    assert resolve(writer, alice, everything) == Matched(by_urn.id, by="urn")
    no_urn = incoming(
        li_public_id="b",
        emails=(IncomingEmail("b@a.test"),),
        first_name="N",
        last_name="M",
        current_company="C",
    )
    assert resolve(writer, alice, no_urn) == Matched(by_email.id, by="public_id")
    email_and_name = incoming(
        emails=(IncomingEmail("b@a.test"),), first_name="N", last_name="M", current_company="C"
    )
    assert resolve(writer, alice, email_and_name) == Matched(by_email.id, by="email")
    name_only = incoming(first_name="N", last_name="M", current_company="C")
    assert resolve(writer, alice, name_only) == Candidate((by_name.id,), by="name")


def test_a_merged_away_contact_resolves_to_its_survivor(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(
        writer, alice, li_urn="urn:li:fsd_profile/L", emails=["l@a.test"]
    )
    loser.merged_into_id = survivor.id  # by hand: merge() would also move the identity across
    writer.flush()
    assert resolve(writer, alice, incoming(li_urn="urn:li:fsd_profile/L")) == Matched(
        survivor.id, by="urn"
    )
    assert resolve(writer, alice, incoming(emails=(IncomingEmail("l@a.test"),))) == Matched(
        survivor.id, by="email"
    )
    assert resolve(writer, alice, incoming(li_public_id=loser.li_public_id)) == Matched(
        survivor.id, by="public_id"
    )
    # Two losers of one survivor sharing an address are one contact, not a candidate.
    other = factories.make_contact(writer, alice, emails=["l@a.test"])
    other.merged_into_id = survivor.id
    writer.flush()
    assert resolve(writer, alice, incoming(emails=(IncomingEmail("l@a.test"),))) == Matched(
        survivor.id, by="email"
    )


def test_resolve_survivor_follows_chains_and_stays_in_scope(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    a, b, c = (factories.make_contact(writer, alice) for _ in range(3))
    a.merged_into_id, b.merged_into_id = b.id, c.id
    writer.flush()
    assert resolve_survivor(writer, alice, a.id) is c
    assert resolve_survivor(writer, alice, c.id) is c
    with pytest.raises(ValueError, match="not one of user"):
        resolve_survivor(writer, bob, a.id)
    with pytest.raises(ValueError, match="not one of user"):
        resolve_survivor(writer, alice, 999)
    c.merged_into_id = a.id
    writer.flush()
    with pytest.raises(RuntimeError, match="loops"):
        resolve_survivor(writer, alice, a.id)


# --- apply ------------------------------------------------------------------


def test_apply_new_creates_a_contact_with_provenance_and_children(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    row = incoming(
        ContactSource.ARCHIVE,
        li_url="https://www.linkedin.com/in/Ann-Lee/",
        first_name="Ann",
        last_name="Lee",
        current_company="Acme",
        connected_on=date(2024, 5, 6),
        emails=(
            IncomingEmail("ann@a.test", kind=EmailKind.WORK, is_primary=True),
            IncomingEmail("b@a.test", is_primary=True),
        ),
        phones=(IncomingPhone("+1 555 0100", is_primary=True),),
        links=(IncomingLink("https://ann.test", kind=LinkKind.WEBSITE),),
        positions=(IncomingPosition(title="CTO", company="Acme", is_current=True),),
    )
    contact = apply(writer, alice, row, New())
    assert contact.id is not None and contact.user_id == alice.id
    assert contact.source is ContactSource.ARCHIVE
    assert (contact.li_urn, contact.li_public_id, contact.li_url) == (
        None,
        "ann-lee",
        "https://www.linkedin.com/in/ann-lee/",
    )
    assert (contact.first_name, contact.last_name, contact.preferred_name) == ("Ann", "Lee", "Ann")
    assert contact.headline is None and contact.connected_on == date(2024, 5, 6)
    assert contact.field_sources == {
        "li_public_id": "archive",
        "li_url": "archive",
        "first_name": "archive",
        "last_name": "archive",
        "current_company": "archive",
        "connected_on": "archive",
    }
    assert contact.synced_values == {  # the archive counts as synced; a date is stored ISO
        "li_public_id": synced("ann-lee", "archive"),
        "li_url": synced("https://www.linkedin.com/in/ann-lee/", "archive"),
        "first_name": synced("Ann", "archive"),
        "last_name": synced("Lee", "archive"),
        "current_company": synced("Acme", "archive"),
        "connected_on": synced("2024-05-06", "archive"),
    }
    assert [(e.email, e.kind, e.is_primary) for e in contact.emails] == [
        ("ann@a.test", EmailKind.WORK, True),
        ("b@a.test", EmailKind.OTHER, False),  # one primary: the first flagged wins
    ]
    assert [(p.raw, p.number_e164, p.is_primary) for p in contact.phones] == [
        ("+1 555 0100", "+15550100", True)
    ]
    assert [(l.url, l.kind) for l in contact.links] == [("https://ann.test", LinkKind.WEBSITE)]  # noqa: E741
    assert [(p.title, p.company, p.is_current) for p in contact.positions] == [
        ("CTO", "Acme", True)
    ]
    for child in [*contact.emails, *contact.phones, *contact.links, *contact.positions]:
        assert (child.user_id, child.source, child.observed_at) == (
            alice.id,
            ContactSource.ARCHIVE,
            NOW,
        )
    assert contact.snapshots == []
    assert counts(writer, bob) == dict.fromkeys(counts(writer, bob), 0)
    assert resolve(writer, alice, incoming(li_public_id="ann-lee")) == Matched(
        contact.id, by="public_id"
    )


def test_apply_update_follows_the_provenance_matrix(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = factories.make_contact(
        writer,
        alice,
        headline="from csv",
        current_title="csv title",
        location=None,
        source=ContactSource.CSV,
        field_sources={"headline": "csv", "current_title": "csv", "first_name": "csv"},
        notes="my notes",
        preferred_name="Bob",
        met=ContactMet.MET,
    )
    by_sync = incoming(
        ContactSource.SYNC, headline="from sync", location="Berlin", first_name="First1"
    )
    assert apply(writer, alice, by_sync, Matched(contact.id, by="urn")) is contact
    assert (contact.headline, contact.current_title, contact.location) == (
        "from sync",
        "csv title",
        "Berlin",
    )
    assert contact.field_sources == {
        "headline": "sync",  # sync over csv: written and recorded
        "current_title": "csv",  # not in the row: untouched
        "first_name": "sync",  # equal value, but sync now vouches for it
        "location": "sync",  # was empty: free to any source
    }
    assert contact.synced_values == {  # every field the row carried, as the row said
        "headline": synced("from sync"),
        "location": synced("Berlin"),
        "first_name": synced("First1"),
    }
    assert len(contact.snapshots) == 1  # the headline changed from a real value
    by_csv = incoming(
        ContactSource.CSV,
        headline="csv again",
        location="Paris",
        current_title="new csv title",
        observed_at=LATER,
    )
    apply(writer, alice, by_csv, Matched(contact.id, by="urn"))
    assert (contact.headline, contact.location) == ("from sync", "Berlin")  # csv over sync: skipped
    assert contact.current_title == "new csv title"  # csv over csv: equal rank may overwrite
    assert (
        contact.field_sources["headline"] == "sync"
        and contact.field_sources["current_title"] == "csv"
    )
    # The ledger is chronological and takes what the column refused, too.
    assert contact.synced_values == {
        "headline": synced("csv again", "csv", LATER),
        "location": synced("Paris", "csv", LATER),
        "first_name": synced("First1"),
        "current_title": synced("new csv title", "csv", LATER),
    }
    assert len(contact.snapshots) == 2  # current_title changed
    by_manual = incoming(
        ContactSource.MANUAL, headline="typed", current_title="typed", connected_on=date(2020, 1, 1)
    )
    apply(writer, alice, by_manual, Matched(contact.id, by="urn"))
    # manual outranks every source (CP1): written, recorded, and never a ledger entry
    assert (contact.headline, contact.current_title) == ("typed", "typed")
    assert contact.connected_on == date(2020, 1, 1)
    assert {name: contact.field_sources[name] for name in ("headline", "current_title")} == {
        "headline": "manual",
        "current_title": "manual",
    }
    assert contact.field_sources["connected_on"] == "manual"
    assert "connected_on" not in contact.synced_values
    assert contact.synced_values["headline"] == synced("csv again", "csv", LATER)
    assert overridden_fields(contact) == ["headline", "current_title"]
    assert len(contact.snapshots) == 3  # a manual row through apply() snapshots like any other
    # The person-owned fields are never touched by an import, whatever its source.
    assert (contact.notes, contact.preferred_name, contact.met) == (
        "my notes",
        "Bob",
        ContactMet.MET,
    )
    assert contact.source is ContactSource.CSV  # the first source stays


def test_a_manual_edit_sticks_and_every_later_source_still_records_what_it_saw(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = apply(
        writer, alice, incoming(li_urn="urn:li:fsd_profile/A", headline="from sync"), New()
    )
    set_manual_field(contact, "headline", "typed")
    day = timedelta(days=1)
    apply(
        writer,
        alice,
        incoming(headline="newer from sync", current_title="VP", observed_at=LATER),
        Matched(contact.id, by="urn"),
    )
    assert (contact.headline, contact.field_sources["headline"]) == ("typed", "manual")
    assert contact.synced_values["headline"] == synced("newer from sync", "sync", LATER)
    assert contact.current_title == "VP"  # the other fields still flow
    assert overridden_fields(contact) == ["headline"]
    for source, at in ((ContactSource.ARCHIVE, LATER + day), (ContactSource.CSV, LATER + 2 * day)):
        apply(
            writer,
            alice,
            incoming(source, headline=f"from {source.value}", observed_at=at),
            Matched(contact.id, by="urn"),
        )
        assert contact.headline == "typed"
        assert contact.synced_values["headline"] == synced(f"from {source.value}", source.value, at)
    writer.flush()
    writer.expire_all()
    assert (contact.headline, contact.field_sources["headline"]) == ("typed", "manual")
    revert_to_synced(contact, "headline")
    writer.flush()
    assert (contact.headline, contact.field_sources["headline"]) == ("from csv", "csv")
    assert overridden_fields(contact) == []
    apply(
        writer,
        alice,
        incoming(headline="back in sync", observed_at=LATER + 3 * day),
        Matched(contact.id, by="urn"),
    )
    assert (contact.headline, contact.field_sources["headline"]) == ("back in sync", "sync")


def test_a_manual_edit_after_a_sync_wins_and_keeps_the_synced_value(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = apply(
        writer,
        alice,
        incoming(li_urn="urn:li:fsd_profile/A", first_name="Ann", headline="from sync"),
        New(),
    )
    set_manual_field(contact, "headline", "typed")
    assert (contact.headline, contact.field_sources["headline"]) == ("typed", "manual")
    assert contact.synced_values["headline"] == synced("from sync")  # retained for the revert
    assert overridden_fields(contact) == ["headline"]
    # A manual row through apply() (an "add contact" form) wins the same way and
    # leaves the ledger alone.
    apply(
        writer,
        alice,
        incoming(ContactSource.MANUAL, headline="typed again", first_name="Annie"),
        Matched(contact.id, by="urn"),
    )
    assert (contact.headline, contact.first_name) == ("typed again", "Annie")
    assert contact.field_sources["first_name"] == "manual"
    assert contact.synced_values["first_name"] == synced("Ann")
    assert overridden_fields(contact) == ["first_name", "headline"]


def test_a_manual_clear_sticks_like_any_other_edit(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = apply(
        writer,
        alice,
        incoming(li_urn="urn:li:fsd_profile/A", headline="from sync", location="Berlin"),
        New(),
    )
    set_manual_field(contact, "headline", "")
    set_manual_field(contact, "location", None)
    day = timedelta(days=1)
    for n, source in enumerate((ContactSource.SYNC, ContactSource.ARCHIVE, ContactSource.CSV), 1):
        at = NOW + n * day
        apply(
            writer,
            alice,
            incoming(source, headline=f"from {source.value}", location="Paris", observed_at=at),
            Matched(contact.id, by="urn"),
        )
        assert (contact.headline, contact.location) == ("", None), source
        assert (contact.field_sources["headline"], contact.field_sources["location"]) == (
            "manual",
            "manual",
        ), source
        assert contact.synced_values["headline"] == synced(f"from {source.value}", source.value, at)
    assert overridden_fields(contact) == ["headline", "location"]
    revert_to_synced(contact, "headline")
    assert (contact.headline, contact.field_sources["headline"]) == ("from csv", "csv")


def test_apply_decides_each_field_once_from_the_state_before_it_writes(
    writer: Session, users: tuple[User, User]
) -> None:
    """An empty field with sync provenance is free to a csv row, and the row's source is
    recorded: the decision is not made again after the write, when csv would rank below sync."""
    alice, _ = users
    contact = factories.make_contact(
        writer,
        alice,
        headline=None,
        location="",
        field_sources={"headline": "sync", "location": "archive"},
    )
    apply(
        writer,
        alice,
        incoming(ContactSource.CSV, headline="from csv", location="Paris"),
        Matched(contact.id, by="urn"),
    )
    assert (contact.headline, contact.location) == ("from csv", "Paris")
    assert (contact.field_sources["headline"], contact.field_sources["location"]) == ("csv", "csv")


def test_an_older_observation_never_replaces_a_newer_synced_value(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = apply(writer, alice, incoming(li_urn="urn:li:fsd_profile/A", headline="today"), New())
    set_manual_field(contact, "headline", "typed")
    apply(
        writer,
        alice,
        incoming(ContactSource.ARCHIVE, headline="yesterday", observed_at=EARLIER),
        Matched(contact.id, by="urn"),
    )
    assert contact.synced_values["headline"] == synced("today")
    apply(
        writer,
        alice,
        incoming(ContactSource.CSV, headline="tomorrow", observed_at=LATER),
        Matched(contact.id, by="urn"),
    )
    assert contact.synced_values["headline"] == synced("tomorrow", "csv", LATER)  # newer wins
    assert contact.headline == "typed"


def test_a_slug_change_records_an_alias_and_renaming_back_removes_it(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = factories.make_contact(writer, alice, li_public_id="one")
    apply(writer, alice, incoming(li_public_id="two"), Matched(contact.id, by="urn"))
    assert (contact.li_public_id, contact.li_url) == ("two", "https://www.linkedin.com/in/two/")
    assert aliases_of(contact) == ["one"]
    assert contact.aliases[0].source is ContactSource.SYNC and contact.aliases[0].observed_at == NOW
    apply(writer, alice, incoming(li_public_id="three"), Matched(contact.id, by="urn"))
    assert aliases_of(contact) == ["one", "two"]
    apply(writer, alice, incoming(li_public_id="one"), Matched(contact.id, by="urn"))
    assert contact.li_public_id == "one"
    assert aliases_of(contact) == ["three", "two"]  # "one" is current again, not an alias
    assert writer.scalar(scoped_count(alice, ContactAlias)) == 2
    # A lower-ranked source may not rename, so no alias is written either.
    contact.field_sources["li_public_id"] = "sync"
    apply(
        writer,
        alice,
        incoming(ContactSource.CSV, li_public_id="csv-slug"),
        Matched(contact.id, by="urn"),
    )
    assert contact.li_public_id == "one" and aliases_of(contact) == ["three", "two"]


def test_a_stale_alias_on_another_contact_goes_when_the_slug_is_taken(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    former = factories.make_contact(writer, alice, li_public_id="former-now")
    former.aliases.append(ContactAlias(user_id=alice.id, li_public_id="moved"))
    writer.flush()
    current = factories.make_contact(writer, alice, li_public_id="current-old")
    apply(writer, alice, incoming(li_public_id="moved"), Matched(current.id, by="urn"))
    assert current.li_public_id == "moved" and aliases_of(current) == ["current-old"]
    assert aliases_of(former) == []
    assert resolve(writer, alice, incoming(li_public_id="moved")) == Matched(
        current.id, by="public_id"
    )


def test_child_upsert_is_idempotent_and_keeps_is_primary(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = factories.make_contact(
        writer,
        alice,
        emails=["primary@a.test"],
        phones=["+15550100"],
        positions=[{"title": "Dev", "company": "Acme", "started_on": date(2020, 1, 1)}],
        source=ContactSource.CSV,
    )
    contact.links.append(ContactLink(user_id=alice.id, url="https://a.test"))
    for child in [*contact.emails, *contact.phones, *contact.positions, *contact.links]:
        child.observed_at = EARLIER
    writer.flush()
    row = incoming(
        emails=(
            IncomingEmail("PRIMARY@a.test", kind=EmailKind.WORK),
            IncomingEmail("second@a.test", is_primary=True),
        ),
        phones=(IncomingPhone("+1 (555) 0100", is_primary=True), IncomingPhone("555-0199")),
        links=(
            IncomingLink("https://a.test", kind=LinkKind.WEBSITE),
            IncomingLink("https://b.test"),
        ),
        positions=(
            IncomingPosition(
                title="dev",
                company="ACME",
                started_on=date(2020, 1, 1),
                ended_on=date(2024, 1, 1),
                company_urn="urn:li:company/1",
                is_current=False,
            ),
            IncomingPosition(
                title="Lead", company="Acme", started_on=date(2024, 1, 1), is_current=True
            ),
        ),
    )
    before = counts(writer, alice)
    apply(writer, alice, row, Matched(contact.id, by="urn"))
    after = counts(writer, alice)
    assert after == {
        **before,
        "contact_emails": 2,
        "contact_phones": 2,
        "contact_links": 2,
        "contact_positions": 2,
    }
    apply(writer, alice, row, Matched(contact.id, by="urn"))
    assert counts(writer, alice) == after
    writer.expire_all()
    assert [(e.email, e.kind, e.is_primary, e.source, e.observed_at) for e in contact.emails] == [
        (
            "primary@a.test",
            EmailKind.WORK,
            True,
            ContactSource.SYNC,
            NOW,
        ),  # kept primary, kind learned
        (
            "second@a.test",
            EmailKind.OTHER,
            False,
            ContactSource.SYNC,
            NOW,
        ),  # a primary exists already
    ]
    assert [(p.raw, p.is_primary) for p in contact.phones] == [
        ("+15550100", True),
        ("555-0199", False),
    ]
    assert [(l.url, l.kind, l.observed_at) for l in contact.links] == [  # noqa: E741
        ("https://a.test", LinkKind.WEBSITE, NOW),
        ("https://b.test", LinkKind.OTHER, NOW),
    ]
    assert [(p.title, p.is_current, p.ended_on, p.company_urn) for p in contact.positions] == [
        ("Lead", True, None, None),
        ("Dev", False, date(2024, 1, 1), "urn:li:company/1"),
    ]
    # An older observation never moves a child backwards.
    stale = incoming(
        ContactSource.CSV,
        observed_at=EARLIER,
        emails=(IncomingEmail("primary@a.test", kind=EmailKind.PERSONAL),),
    )
    apply(writer, alice, stale, Matched(contact.id, by="urn"))
    assert (contact.emails[0].kind, contact.emails[0].source, contact.emails[0].observed_at) == (
        EmailKind.WORK,
        ContactSource.SYNC,
        NOW,
    )


def test_apply_across_writer_sessions_is_idempotent(session_factory: sessionmaker[Session]) -> None:
    row = incoming(
        li_urn="urn:li:fsd_profile/ANN",
        li_public_id="ann",
        first_name="Ann",
        last_name="Lee",
        emails=(IncomingEmail("ann@a.test"),),
        positions=(IncomingPosition(title="CTO", company="Acme"),),
    )
    with session_scope(session_factory, write=True) as session:
        alice = factories.make_user(session)
        first = apply(session, alice, row, resolve(session, alice, row))
        user_id, first_id = alice.id, first.id
    for _ in range(2):
        with session_scope(session_factory, write=True) as session:
            alice = session.get_one(User, user_id)
            resolution = resolve(session, alice, row)
            assert resolution == Matched(first_id, by="urn")
            assert apply(session, alice, row, resolution).id == first_id
    with session_scope(session_factory) as session:
        alice = session.get_one(User, user_id)
        assert counts(session, alice) == {
            "contacts": 1,
            "contact_emails": 1,
            "contact_phones": 0,
            "contact_links": 0,
            "contact_positions": 1,
            "contact_snapshots": 0,
            "contact_aliases": 0,
            "interactions": 0,
            "list_members": 0,
        }


def test_a_candidate_needs_a_decision(writer: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    contact = factories.make_contact(
        writer,
        alice,
        first_name="Ann",
        last_name="Lee",
        current_company="Acme",
        li_urn=None,
        li_public_id=None,
    )
    row = incoming(first_name="Ann", last_name="Lee", current_company="Acme", headline="new")
    candidate = resolve(writer, alice, row)
    assert candidate == Candidate((contact.id,), by="name")
    with pytest.raises(ValueError, match="needs a decision"):
        apply(writer, alice, row, candidate)
    assert apply(writer, alice, row, candidate, decision=MergeInto(contact.id)) is contact
    assert contact.headline == "new"
    created = apply(writer, alice, row, candidate, decision=CreateNew())
    assert created.id != contact.id and created.headline == "new"
    assert writer.scalar(scoped_count(alice, Contact)) == 2
    # A person may merge into any of their contacts, not only a listed candidate.
    elsewhere = factories.make_contact(writer, alice)
    assert apply(writer, alice, row, candidate, decision=MergeInto(elsewhere.id)) is elsewhere
    for resolution in (Matched(contact.id, by="urn"), New()):
        with pytest.raises(ValueError, match="Candidate resolution only"):
            apply(writer, alice, row, resolution, decision=CreateNew())


def test_apply_never_takes_an_identity_from_another_contact(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    holder = factories.make_contact(
        writer, alice, li_urn="urn:li:fsd_profile/H", li_public_id="held"
    )
    other = factories.make_contact(writer, alice, li_urn=None, li_public_id=None)
    with pytest.raises(ValueError, match="li_public_id 'held' already belongs"):
        apply(writer, alice, incoming(li_public_id="held"), Matched(other.id, by="email"))
    with pytest.raises(ValueError, match="li_urn 'urn:li:fsd_profile/H' already belongs"):
        apply(
            writer,
            alice,
            incoming(li_urn="urn:li:fsd_profile/H"),
            Candidate((holder.id,), by="identity"),
            decision=CreateNew(),
        )
    assert (other.li_public_id, other.li_urn) == (None, None)
    assert writer.scalar(scoped_count(alice, Contact)) == 2
    # Another user's contacts are not in the way.
    theirs = apply(writer, bob, incoming(li_urn="urn:li:fsd_profile/H", li_public_id="held"), New())
    assert theirs.user_id == bob.id


def test_a_job_change_writes_a_snapshot_of_the_previous_values(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = factories.make_contact(
        writer, alice, headline=None, current_title=None, current_company=None, location=None
    )
    apply(
        writer,
        alice,
        incoming(ContactSource.CSV, headline="h1", current_title="t1", current_company="c1"),
        Matched(contact.id, by="urn"),
    )
    assert contact.snapshots == []  # first values are not a change
    apply(
        writer,
        alice,
        incoming(ContactSource.CSV, headline="h1", current_title="t1", current_company="c1"),
        Matched(contact.id, by="urn"),
    )
    assert contact.snapshots == []  # nothing changed
    apply(
        writer,
        alice,
        incoming(
            ContactSource.ARCHIVE,
            observed_at=LATER,
            current_title="t2",
            current_company="c2",
            location="Berlin",
        ),
        Matched(contact.id, by="urn"),
    )
    (snapshot,) = contact.snapshots
    assert (
        snapshot.headline,
        snapshot.current_title,
        snapshot.current_company,
        snapshot.location,
    ) == ("h1", "t1", "c1", None)
    assert (snapshot.source, snapshot.observed_at, snapshot.user_id) == (
        ContactSource.ARCHIVE,
        LATER,
        alice.id,
    )
    assert (contact.current_title, contact.current_company, contact.location) == (
        "t2",
        "c2",
        "Berlin",
    )


def test_a_positions_current_status_changes_only_when_the_row_says_so(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    contact = factories.make_contact(writer, alice, positions=[{"title": "CTO", "company": "Acme"}])
    (position,) = contact.positions
    position.observed_at = EARLIER
    writer.flush()
    assert (position.is_current, position.observed_at) == (True, EARLIER)
    silent = IncomingPosition(title="CTO", company="Acme")
    assert silent.is_current is None
    apply(writer, alice, incoming(positions=(silent,)), Matched(contact.id, by="urn"))
    assert (position.is_current, position.observed_at) == (True, NOW)  # refreshed, kept
    ended = IncomingPosition(title="CTO", company="Acme", is_current=False)
    apply(
        writer,
        alice,
        incoming(observed_at=LATER, positions=(ended,)),
        Matched(contact.id, by="urn"),
    )
    assert (position.is_current, position.observed_at) == (False, LATER)
    back = IncomingPosition(title="CTO", company="Acme", is_current=True)
    apply(
        writer, alice, incoming(observed_at=LATER, positions=(back,)), Matched(contact.id, by="urn")
    )
    assert (position.is_current, position.observed_at) == (True, LATER)
    # A new position that does not say starts as not current.
    new = IncomingPosition(title="Dev", company="Other")
    apply(
        writer, alice, incoming(observed_at=LATER, positions=(new,)), Matched(contact.id, by="urn")
    )
    assert [(p.title, p.is_current) for p in contact.positions] == [("CTO", True), ("Dev", False)]


def test_apply_changes_nothing_when_an_identity_field_is_held(
    session_factory: sessionmaker[Session],
) -> None:
    """The URN is free and the slug is held: the row applies not at all, not by half.

    An importer catches the ValueError per row and commits the rest of its batch,
    so a half-applied row would be committed with it.
    """
    with session_scope(session_factory, write=True) as session:
        alice = factories.make_user(session)
        factories.make_contact(session, alice, li_public_id="held")
        mine = factories.make_contact(
            session, alice, li_urn=None, li_public_id="mine", emails=["mine@a.test"]
        )
        user_id, mine_id = alice.id, mine.id
    row = incoming(
        li_urn="urn:li:fsd_profile/FREE",
        li_public_id="held",
        headline="changed",
        emails=(IncomingEmail("new@a.test"),),
        positions=(IncomingPosition(title="New", company="Co"),),
    )
    with session_scope(session_factory, write=True) as session:
        alice = session.get_one(User, user_id)
        mine = session.scalars(scoped(alice, Contact).where(Contact.id == mine_id)).one()
        before, before_counts = columns_of(mine), counts(session, alice)
        with pytest.raises(ValueError, match="li_public_id 'held' already belongs"):
            apply(session, alice, row, Matched(mine_id, by="email"))
        assert not session.new
        assert not any(session.is_modified(obj) for obj in session.dirty)
        # A fresh select in the same transaction (it autoflushes anything pending).
        mine = session.scalars(scoped(alice, Contact).where(Contact.id == mine_id)).one()
        assert columns_of(mine) == before
        assert counts(session, alice) == before_counts
        # ... and the importer commits the rest of its batch.
    with session_scope(session_factory) as session:
        alice = session.get_one(User, user_id)
        mine = session.scalars(scoped(alice, Contact).where(Contact.id == mine_id)).one()
        assert columns_of(mine) == before
        assert counts(session, alice) == before_counts
        free = scoped(alice, Contact).where(Contact.li_urn == "urn:li:fsd_profile/FREE")
        assert session.scalars(free).first() is None


def test_identity_operations_need_a_writer_session(session: Session) -> None:
    """A reader would fail later with "database is locked" on its first write; fail at once."""
    alice = factories.make_user(session)
    a, b = factories.make_contact(session, alice), factories.make_contact(session, alice)
    row = incoming(li_urn="urn:li:fsd_profile/X")
    assert not is_writer(session)
    with pytest.raises(RuntimeError, match="writer session"):
        resolve(session, alice, row)
    with pytest.raises(RuntimeError, match="writer session"):
        apply(session, alice, row, New())
    with pytest.raises(RuntimeError, match="writer session"):
        merge(session, alice, a.id, b.id)
    assert session.scalar(scoped_count(alice, Contact)) == 2
    assert b.merged_into_id is None


# --- merge ------------------------------------------------------------------


def _pair(writer: Session, user: User) -> tuple[Contact, Contact]:
    """A survivor and a loser with overlapping and distinct children of every kind."""
    survivor = factories.make_contact(
        writer,
        user,
        emails=["keep@a.test", "both@a.test"],
        phones=["+15550100"],
        positions=[{"title": "CTO", "company": "Acme", "started_on": date(2020, 1, 1)}],
    )
    loser = factories.make_contact(
        writer,
        user,
        emails=["BOTH@a.test", "theirs@a.test"],
        phones=["+1 (555) 0100", "555-0199"],
        positions=[
            {
                "title": "cto",
                "company": "ACME",
                "started_on": date(2020, 1, 1),
                "is_current": False,
            },
            {"title": "Dev", "company": "Other"},
        ],
    )
    for contact, url in (
        (survivor, "https://s.test"),
        (loser, "https://s.test"),
        (loser, "https://l.test"),
    ):
        contact.links.append(ContactLink(user_id=user.id, url=url))
    for contact in (survivor, loser):
        contact.snapshots.append(ContactSnapshot(user_id=user.id, headline=f"was {contact.id}"))
        contact.interactions.append(
            Interaction(
                user_id=user.id, kind=InteractionKind.NOTE, at=NOW, summary=f"note {contact.id}"
            )
        )
        contact.aliases.append(ContactAlias(user_id=user.id, li_public_id=f"old-{contact.id}"))
    writer.flush()
    return survivor, loser


def test_merge_moves_and_dedupes_children(writer: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    survivor, loser = _pair(writer, alice)
    theirs = counts(writer, bob)
    kept_email = next(e for e in survivor.emails if e.email == "both@a.test")
    loser_slug = loser.li_public_id
    assert loser_slug is not None
    assert merge(writer, alice, survivor.id, loser.id) is survivor
    writer.expire_all()
    assert [(e.email, e.is_primary) for e in survivor.emails] == [
        ("keep@a.test", True),
        ("both@a.test", False),
        ("theirs@a.test", False),  # was the loser's second, never primary
    ]
    assert kept_email in survivor.emails  # the survivor's row, not the loser's copy
    assert [(p.raw, p.is_primary) for p in survivor.phones] == [
        ("+15550100", True),
        ("555-0199", False),
    ]
    assert sorted(l.url for l in survivor.links) == ["https://l.test", "https://s.test"]  # noqa: E741
    assert [(p.title, p.company, p.is_current) for p in survivor.positions] == [
        ("CTO", "Acme", True),
        ("Dev", "Other", False),
    ]
    assert sorted(s.headline or "" for s in survivor.snapshots) == sorted(
        [f"was {loser.id}", f"was {survivor.id}"]
    )
    assert sorted(i.summary or "" for i in survivor.interactions) == sorted(
        [f"note {loser.id}", f"note {survivor.id}"]
    )
    assert aliases_of(survivor) == sorted([loser_slug, f"old-{loser.id}", f"old-{survivor.id}"])
    for child in CONTACT_CHILDREN:
        assert writer.scalar(scoped_count(alice, child).where(child.contact_id == loser.id)) == 0, (
            child.__name__
        )
    assert counts(writer, alice) == {
        "contacts": 2,
        "contact_emails": 3,
        "contact_phones": 2,
        "contact_links": 2,
        "contact_positions": 2,
        "contact_snapshots": 2,
        "contact_aliases": 3,
        "interactions": 2,
        "list_members": 0,
    }
    assert counts(writer, bob) == theirs


def test_merge_moves_the_losers_primary_when_the_survivor_has_none(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(
        writer, alice, emails=["p@a.test", "q@a.test"], phones=["+15550100"]
    )
    merge(writer, alice, survivor.id, loser.id)
    assert [(e.email, e.is_primary) for e in survivor.emails] == [
        ("p@a.test", True),
        ("q@a.test", False),
    ]
    assert [(p.raw, p.is_primary) for p in survivor.phones] == [("+15550100", True)]


def test_merge_fills_empty_fields_with_the_losers_source(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = factories.make_contact(
        writer,
        alice,
        headline="mine",
        location=None,
        connected_on=None,
        first_name="",
        field_sources={"headline": "csv"},
    )
    loser = factories.make_contact(
        writer,
        alice,
        headline="theirs",
        location="Berlin",
        connected_on=date(2020, 1, 1),
        first_name="Ann",
        source=ContactSource.ARCHIVE,
        field_sources={"location": "sync"},
    )
    merge(writer, alice, survivor.id, loser.id)
    assert (survivor.headline, survivor.location, survivor.connected_on, survivor.first_name) == (
        "mine",
        "Berlin",
        date(2020, 1, 1),
        "Ann",
    )
    assert survivor.field_sources == {
        "headline": "csv",  # kept
        "location": "sync",  # the loser's recorded source
        "connected_on": "archive",  # the loser's first source when nothing was recorded
        "first_name": "archive",
    }
    assert survivor.preferred_name == "Ann"  # the default followed the filled first_name


def test_merge_fills_the_survivors_missing_synced_values_from_the_loser(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = apply(
        writer, alice, incoming(li_urn="urn:li:fsd_profile/S", headline="s-headline"), New()
    )
    loser = apply(
        writer,
        alice,
        incoming(
            ContactSource.CSV,
            li_public_id="loser",
            headline="l-headline",
            location="Paris",
            observed_at=LATER,
        ),
        New(),
    )
    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()
    assert survivor.synced_values["headline"] == synced("s-headline")  # its own stays
    assert survivor.synced_values["location"] == synced("Paris", "csv", LATER)
    assert (survivor.location, survivor.field_sources["location"]) == ("Paris", "csv")


def test_merge_takes_the_losers_identity_when_the_survivor_has_none(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice, li_urn=None, li_public_id=None)
    loser = factories.make_contact(
        writer,
        alice,
        li_urn="urn:li:fsd_profile/L",
        li_public_id="loser-slug",
        source=ContactSource.CSV,
        field_sources={"li_urn": "sync"},
    )
    loser.aliases.append(ContactAlias(user_id=alice.id, li_public_id="loser-old"))
    writer.flush()
    merge(writer, alice, survivor.id, loser.id)
    assert (survivor.li_urn, survivor.li_public_id, survivor.li_url) == (
        "urn:li:fsd_profile/L",
        "loser-slug",
        "https://www.linkedin.com/in/loser-slug/",
    )
    assert survivor.field_sources == {"li_urn": "sync", "li_public_id": "csv"}
    assert (loser.li_urn, loser.li_public_id, loser.merged_into_id) == (None, None, survivor.id)
    assert "li_urn" not in loser.field_sources and "li_public_id" not in loser.field_sources
    assert aliases_of(survivor) == ["loser-old"]  # the current slug is not an alias
    assert resolve(writer, alice, incoming(li_urn="urn:li:fsd_profile/L")) == Matched(
        survivor.id, by="urn"
    )
    assert resolve(writer, alice, incoming(li_public_id="loser-old")) == Matched(
        survivor.id, by="alias"
    )


def test_merge_aliases_the_losers_slug_and_drops_its_urn_when_the_survivor_has_its_own(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = factories.make_contact(
        writer, alice, li_urn="urn:li:fsd_profile/S", li_public_id="s"
    )
    loser = factories.make_contact(writer, alice, li_urn="urn:li:fsd_profile/L", li_public_id="l")
    loser.aliases.append(
        ContactAlias(user_id=alice.id, li_public_id="s")
    )  # stale: the survivor's own slug
    loser.aliases.append(ContactAlias(user_id=alice.id, li_public_id="l-old"))
    writer.flush()
    merge(writer, alice, survivor.id, loser.id)
    assert (survivor.li_urn, survivor.li_public_id) == ("urn:li:fsd_profile/S", "s")
    assert aliases_of(survivor) == ["l", "l-old"]
    assert (loser.li_urn, loser.li_public_id) == (None, None)
    assert writer.scalar(scoped_count(alice, ContactAlias)) == 2
    assert resolve(writer, alice, incoming(li_public_id="L")) == Matched(survivor.id, by="alias")
    assert resolve(writer, alice, incoming(li_urn="urn:li:fsd_profile/L")) == New()  # dropped
    # A third contact may take the freed URN and slug: the uniques are clear.
    apply(writer, alice, incoming(li_urn="urn:li:fsd_profile/L"), New())


@pytest.mark.parametrize(
    ("mine", "theirs", "expected"),
    [
        (ContactMet.UNKNOWN, ContactMet.MET, ContactMet.MET),
        (ContactMet.MET, ContactMet.UNKNOWN, ContactMet.MET),
        (ContactMet.NOT_MET, ContactMet.MET, ContactMet.MET),
        (ContactMet.MET, ContactMet.NOT_MET, ContactMet.MET),
        (ContactMet.SKIP, ContactMet.NOT_MET, ContactMet.NOT_MET),
        (ContactMet.UNKNOWN, ContactMet.SKIP, ContactMet.SKIP),
        (ContactMet.SKIP, ContactMet.UNKNOWN, ContactMet.SKIP),
        (ContactMet.NOT_MET, ContactMet.NOT_MET, ContactMet.NOT_MET),
    ],
)
def test_merge_met_takes_the_more_decided_value(
    writer: Session,
    users: tuple[User, User],
    mine: ContactMet,
    theirs: ContactMet,
    expected: ContactMet,
) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice, met=mine, triaged_at=NOW)
    loser = factories.make_contact(writer, alice, met=theirs, triaged_at=LATER)
    merge(writer, alice, survivor.id, loser.id)
    assert survivor.met is expected
    assert survivor.triaged_at == (LATER if expected is theirs and theirs is not mine else NOW)


def test_merge_carries_who_decided_met(writer: Session, users: tuple[User, User]) -> None:
    """A batch's decision stays a batch's decision when it moves to the survivor (spec 10.2)."""
    alice, _ = users
    survivor = factories.make_contact(writer, alice, met=ContactMet.UNKNOWN)
    loser = factories.make_contact(
        writer, alice, met=ContactMet.MET, met_source=MetSource.AUTOMATIC, triaged_at=LATER
    )
    merge(writer, alice, survivor.id, loser.id)
    assert (survivor.met, survivor.met_source) == (ContactMet.MET, MetSource.AUTOMATIC)


def test_merge_keeps_the_answer_the_person_gave_over_the_same_one_a_batch_gave(
    writer: Session, users: tuple[User, User]
) -> None:
    """Same value, two sources: the person's own answer is the one that survives.

    Ranking alone cannot choose here — the values are equal, so the survivor
    keeps theirs — and keeping ``automatic`` would throw away a confirmation
    and leave the survivor in the review queue for a decision that has already
    been reviewed.
    """
    alice, _ = users
    survivor = factories.make_contact(
        writer, alice, met=ContactMet.MET, met_source=MetSource.AUTOMATIC, triaged_at=NOW
    )
    loser = factories.make_contact(
        writer, alice, met=ContactMet.MET, met_source=MetSource.MANUAL, triaged_at=LATER
    )
    merge(writer, alice, survivor.id, loser.id)
    assert (survivor.met, survivor.met_source) == (ContactMet.MET, MetSource.MANUAL)
    # The value did not move, so neither did the moment it was decided.
    assert survivor.triaged_at == NOW


def test_merge_does_not_let_a_batch_overwrite_the_person_s_own_answer(
    writer: Session, users: tuple[User, User]
) -> None:
    """The other direction: a batch's ``met`` never relabels a decision made by hand."""
    alice, _ = users
    survivor = factories.make_contact(
        writer, alice, met=ContactMet.MET, met_source=MetSource.MANUAL, triaged_at=NOW
    )
    loser = factories.make_contact(
        writer, alice, met=ContactMet.MET, met_source=MetSource.AUTOMATIC, triaged_at=LATER
    )
    merge(writer, alice, survivor.id, loser.id)
    assert (survivor.met, survivor.met_source) == (ContactMet.MET, MetSource.MANUAL)


def test_merge_person_fields(writer: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    survivor = factories.make_contact(
        writer,
        alice,
        notes="mine",
        last_contacted_at=NOW,
        archived_at=NOW,
        first_name="Robert",
        preferred_name="Robert",
    )
    loser = factories.make_contact(
        writer,
        alice,
        notes="theirs",
        last_contacted_at=LATER,
        do_not_contact=True,
        do_not_contact_reason="asked",
        first_name="Robert",
        preferred_name="Bob",
    )
    merge(writer, alice, survivor.id, loser.id)
    assert survivor.notes == "mine\n\n---\n\ntheirs"
    assert survivor.last_contacted_at == LATER
    assert (survivor.do_not_contact, survivor.do_not_contact_reason) == (True, "asked")
    assert (
        survivor.preferred_name == "Bob"
    )  # the loser's was customized, the survivor's was the default
    assert survivor.archived_at is None  # the loser was live
    # The other way round: everything of the survivor's stands.
    second = factories.make_contact(
        writer,
        alice,
        notes=None,
        last_contacted_at=EARLIER,
        do_not_contact=True,
        do_not_contact_reason="",
        first_name="Ann",
        preferred_name="Annie",
        archived_at=NOW,
    )
    survivor.archived_at = LATER
    merge(writer, alice, second.id, survivor.id)
    assert second.notes == "mine\n\n---\n\ntheirs"
    assert second.last_contacted_at == LATER
    assert (second.do_not_contact, second.do_not_contact_reason) == (True, "asked")
    assert second.preferred_name == "Annie"
    assert second.archived_at == NOW  # both were archived by then


def test_merge_is_idempotent(writer: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    survivor, loser = _pair(writer, alice)
    merge(writer, alice, survivor.id, loser.id)
    after = counts(writer, alice)
    notes, aliases = survivor.notes, aliases_of(survivor)
    assert merge(writer, alice, survivor.id, loser.id) is survivor
    assert counts(writer, alice) == after
    assert (survivor.notes, aliases_of(survivor)) == (notes, aliases)
    third = factories.make_contact(writer, alice)
    with pytest.raises(ValueError, match="already merged into"):
        merge(writer, alice, third.id, loser.id)


def test_merge_refuses_itself_and_other_users(writer: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    mine = factories.make_contact(writer, alice)
    theirs = factories.make_contact(writer, bob)
    with pytest.raises(ValueError, match="into itself"):
        merge(writer, alice, mine.id, mine.id)
    with pytest.raises(ValueError, match="not one of user"):
        merge(writer, alice, mine.id, theirs.id)
    with pytest.raises(ValueError, match="not one of user"):
        merge(writer, alice, theirs.id, mine.id)
    with pytest.raises(ValueError, match="not one of user"):
        merge(writer, alice, mine.id, 999)
    assert mine.merged_into_id is None and theirs.merged_into_id is None


def test_merge_chains_resolve_to_the_final_survivor(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    a = factories.make_contact(writer, alice, emails=["a@a.test"])
    b = factories.make_contact(writer, alice, emails=["b@a.test"])
    c = factories.make_contact(writer, alice, emails=["c@a.test"])
    slugs = [a.li_public_id, b.li_public_id]
    merge(writer, alice, b.id, a.id)
    merge(writer, alice, c.id, b.id)
    assert (a.merged_into_id, b.merged_into_id, c.merged_into_id) == (b.id, c.id, None)
    assert resolve_survivor(writer, alice, a.id) is c
    assert sorted(e.email for e in c.emails) == ["a@a.test", "b@a.test", "c@a.test"]
    assert aliases_of(c) == sorted(slug for slug in slugs if slug is not None)
    assert merge(writer, alice, c.id, a.id) is c  # already there through the chain
    d = factories.make_contact(writer, alice, emails=["d@a.test"])
    assert merge(writer, alice, a.id, d.id) is c  # a merged-away survivor stands for its own
    assert d.merged_into_id == c.id
    with pytest.raises(ValueError, match="nothing to merge"):
        merge(writer, alice, a.id, c.id)


def test_merge_runs_in_the_callers_transaction(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        alice = factories.make_user(session)
        survivor, loser = _pair(session, alice)
        user_id, survivor_id, loser_id = alice.id, survivor.id, loser.id
    with (
        pytest.raises(RuntimeError, match="after the merge"),
        session_scope(session_factory, write=True) as session,
    ):
        alice = session.get_one(User, user_id)
        merge(session, alice, survivor_id, loser_id)
        raise RuntimeError("after the merge")
    with session_scope(session_factory) as session:
        alice = session.get_one(User, user_id)
        loser = session.scalars(scoped(alice, Contact).where(Contact.id == loser_id)).one()
        assert loser.merged_into_id is None and loser.li_urn is not None
        assert len(loser.emails) == 2
    with session_scope(session_factory, write=True) as session:
        alice = session.get_one(User, user_id)
        assert merge(session, alice, survivor_id, loser_id).id == survivor_id
    with session_scope(session_factory) as session:
        alice = session.get_one(User, user_id)
        loser = session.scalars(scoped(alice, Contact).where(Contact.id == loser_id)).one()
        assert loser.merged_into_id == survivor_id
        assert (
            session.scalar(
                scoped_count(alice, ContactEmail).where(ContactEmail.contact_id == survivor_id)
            )
            == 3
        )


# --- merge: tags and suppressions -------------------------------------------


def assignments_of(
    session: Session, user: User, contact: Contact
) -> dict[int, tuple[TagSource, int | None]]:
    """Tag id to (source, credited rule id) for every assignment on ``contact``."""
    return {
        row.tag_id: (row.source, row.rule_id)
        for row in session.scalars(
            scoped(user, ContactTag).where(ContactTag.contact_id == contact.id)
        )
    }


def suppressions_of(session: Session, user: User, contact: Contact) -> set[int]:
    """The tag ids suppressed for ``contact``."""
    return {
        row.tag_id
        for row in session.scalars(
            scoped(user, ContactTagSuppression).where(
                ContactTagSuppression.contact_id == contact.id
            )
        )
    }


def assign(
    session: Session,
    user: User,
    contact: Contact,
    tag: Tag,
    source: TagSource,
    rule_id: int | None = None,
) -> ContactTag:
    """A ``contact_tags`` row with the source and rule credit a run or the user would leave."""
    row = ContactTag(
        user_id=user.id, contact_id=contact.id, tag_id=tag.id, source=source, rule_id=rule_id
    )
    session.add(row)
    session.flush()
    return row


# (tag name, the survivor's source, the loser's source, what the survivor ends with).
# None is "no assignment on that side"; every pairing tag_contact ranks, both ways round.
MERGE_TAG_MATRIX: tuple[tuple[str, TagSource | None, TagSource | None, TagSource], ...] = (
    ("manual-manual", TagSource.MANUAL, TagSource.MANUAL, TagSource.MANUAL),
    ("manual-rule", TagSource.MANUAL, TagSource.RULE, TagSource.MANUAL),
    ("rule-manual", TagSource.RULE, TagSource.MANUAL, TagSource.MANUAL),
    ("rule-rule", TagSource.RULE, TagSource.RULE, TagSource.RULE),
    ("manual-llm", TagSource.MANUAL, TagSource.LLM, TagSource.MANUAL),
    ("llm-manual", TagSource.LLM, TagSource.MANUAL, TagSource.MANUAL),
    ("llm-rule", TagSource.LLM, TagSource.RULE, TagSource.LLM),
    ("rule-llm", TagSource.RULE, TagSource.LLM, TagSource.RULE),
    ("llm-llm", TagSource.LLM, TagSource.LLM, TagSource.LLM),
    ("none-manual", None, TagSource.MANUAL, TagSource.MANUAL),
    ("none-rule", None, TagSource.RULE, TagSource.RULE),
    ("none-llm", None, TagSource.LLM, TagSource.LLM),
    ("manual-none", TagSource.MANUAL, None, TagSource.MANUAL),
    ("rule-none", TagSource.RULE, None, TagSource.RULE),
    ("llm-none", TagSource.LLM, None, TagSource.LLM),
)


def test_merge_tags_follow_tag_contacts_precedence(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    expected: dict[int, tuple[TagSource, int | None]] = {}
    kept_rows: dict[int, int] = {}  # tag id -> the contact_tags row that should survive
    for name, mine_source, their_source, wanted in MERGE_TAG_MATRIX:
        tag = create_tag(writer, alice, name)
        my_rule = create_rule(writer, alice, tag.id, RuleField.TITLE, f"^{name}$")
        their_rule = create_rule(writer, alice, tag.id, RuleField.HEADLINE, f"^{name}$")
        mine = (
            None
            if mine_source is None
            else assign(
                writer,
                alice,
                survivor,
                tag,
                mine_source,
                my_rule.id if mine_source is TagSource.RULE else None,
            )
        )
        theirs = (
            None
            if their_source is None
            else assign(
                writer,
                alice,
                loser,
                tag,
                their_source,
                their_rule.id if their_source is TagSource.RULE else None,
            )
        )
        credit = None
        if wanted is TagSource.RULE:
            credit = my_rule.id if mine_source is TagSource.RULE else their_rule.id
        expected[tag.id] = (wanted, credit)
        kept = mine if mine is not None else theirs
        assert kept is not None
        kept_rows[tag.id] = kept.id
    assert merge(writer, alice, survivor.id, loser.id) is survivor
    writer.expire_all()
    assert assignments_of(writer, alice, survivor) == expected
    assert assignments_of(writer, alice, loser) == {}
    assert suppressions_of(writer, alice, survivor) == set()
    # A tie keeps the survivor's own row, and a moved row is moved, not re-made.
    assert {
        row.tag_id: row.id
        for row in writer.scalars(
            scoped(alice, ContactTag).where(ContactTag.contact_id == survivor.id)
        )
    } == kept_rows


def test_merge_carries_suppressions_over(writer: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    moved = create_tag(writer, alice, "moved")
    shared = create_tag(writer, alice, "shared")
    for tag in (moved, shared):
        tag_contact(writer, alice, loser.id, tag.id, source=TagSource.RULE)
        untag_contact(writer, alice, loser.id, tag.id)
    tag_contact(writer, alice, survivor.id, shared.id, source=TagSource.LLM)
    untag_contact(writer, alice, survivor.id, shared.id)
    assert suppressions_of(writer, alice, survivor) == {shared.id}
    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()
    assert suppressions_of(writer, alice, survivor) == {moved.id, shared.id}
    assert suppressions_of(writer, alice, loser) == set()
    assert assignments_of(writer, alice, survivor) == {}
    assert writer.scalar(scoped_count(alice, ContactTagSuppression)) == 2


def _rule_tagged_pair(writer: Session, user: User) -> tuple[Tag, int, Contact, Contact]:
    """Two contacts a run has tagged from one rule, plus that tag and the rule's id."""
    tag = create_tag(writer, user, "investor")
    rule = create_rule(writer, user, tag.id, RuleField.TITLE, r"\binvestor\b")
    survivor = factories.make_contact(writer, user, current_title="Investor")
    loser = factories.make_contact(writer, user, current_title="Investor")
    assert run_rules(writer, user).added == 2
    assert assignments_of(writer, user, survivor) == {tag.id: (TagSource.RULE, rule.id)}
    assert assignments_of(writer, user, loser) == {tag.id: (TagSource.RULE, rule.id)}
    return tag, rule.id, survivor, loser


def test_merge_keeps_a_tag_the_user_removed_from_the_loser(
    writer: Session, users: tuple[User, User]
) -> None:
    """The loser's suppression beats the survivor's rule assignment."""
    alice, _ = users
    tag, _rule_id, survivor, loser = _rule_tagged_pair(writer, alice)
    assert untag_contact(writer, alice, loser.id, tag.id) is True
    assert suppressions_of(writer, alice, loser) == {tag.id}
    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()
    assert assignments_of(writer, alice, survivor) == {}
    assert suppressions_of(writer, alice, survivor) == {tag.id}
    assert assignments_of(writer, alice, loser) == {}
    assert suppressions_of(writer, alice, loser) == set()
    # And no later run puts it back, which is the whole point of the suppression.
    assert run_rules(writer, alice).added == 0
    assert assignments_of(writer, alice, survivor) == {}


def test_merge_keeps_a_tag_the_user_removed_from_the_survivor(
    writer: Session, users: tuple[User, User]
) -> None:
    """The survivor's suppression beats the loser's rule assignment."""
    alice, _ = users
    tag, rule_id, survivor, loser = _rule_tagged_pair(writer, alice)
    assert untag_contact(writer, alice, survivor.id, tag.id) is True
    assert assignments_of(writer, alice, loser) == {tag.id: (TagSource.RULE, rule_id)}
    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()
    assert assignments_of(writer, alice, survivor) == {}
    assert suppressions_of(writer, alice, survivor) == {tag.id}
    assert assignments_of(writer, alice, loser) == {}
    assert run_rules(writer, alice).added == 0
    assert assignments_of(writer, alice, survivor) == {}


def test_merge_manual_tag_beats_a_suppression_either_way(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    by_loser = create_tag(writer, alice, "tagged-on-the-loser")
    by_survivor = create_tag(writer, alice, "tagged-on-the-survivor")
    # The user tagged the loser by hand and suppressed the same tag on the survivor.
    tag_contact(writer, alice, loser.id, by_loser.id)
    tag_contact(writer, alice, survivor.id, by_loser.id, source=TagSource.RULE)
    untag_contact(writer, alice, survivor.id, by_loser.id)
    # And the other way round.
    tag_contact(writer, alice, survivor.id, by_survivor.id)
    tag_contact(writer, alice, loser.id, by_survivor.id, source=TagSource.RULE)
    untag_contact(writer, alice, loser.id, by_survivor.id)
    assert suppressions_of(writer, alice, survivor) == {by_loser.id}
    assert suppressions_of(writer, alice, loser) == {by_survivor.id}
    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()
    assert assignments_of(writer, alice, survivor) == {
        by_loser.id: (TagSource.MANUAL, None),
        by_survivor.id: (TagSource.MANUAL, None),
    }
    assert suppressions_of(writer, alice, survivor) == set()
    assert assignments_of(writer, alice, loser) == {}
    assert suppressions_of(writer, alice, loser) == set()


def test_merge_tags_when_only_one_side_has_any(writer: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    bare = factories.make_contact(writer, alice)
    by_hand = create_tag(writer, alice, "by-hand")
    by_rule = create_tag(writer, alice, "by-rule")
    blocked = create_tag(writer, alice, "blocked")
    rule = create_rule(writer, alice, by_rule.id, RuleField.TITLE, "nothing-here-matches")
    assign(writer, alice, loser, by_rule, TagSource.RULE, rule.id)
    tag_contact(writer, alice, loser.id, by_hand.id)
    tag_contact(writer, alice, loser.id, blocked.id, source=TagSource.LLM)
    untag_contact(writer, alice, loser.id, blocked.id)
    merge(writer, alice, survivor.id, loser.id)  # the survivor has nothing: it all moves
    writer.expire_all()
    moved = {
        by_rule.id: (TagSource.RULE, rule.id),
        by_hand.id: (TagSource.MANUAL, None),
    }
    assert assignments_of(writer, alice, survivor) == moved
    assert suppressions_of(writer, alice, survivor) == {blocked.id}
    assert assignments_of(writer, alice, loser) == {}
    assert suppressions_of(writer, alice, loser) == set()
    merge(writer, alice, survivor.id, bare.id)  # the loser has nothing: nothing changes
    writer.expire_all()
    assert assignments_of(writer, alice, survivor) == moved
    assert suppressions_of(writer, alice, survivor) == {blocked.id}


def test_merging_twice_does_not_duplicate_a_tag(writer: Session, users: tuple[User, User]) -> None:
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    kept = create_tag(writer, alice, "investor")
    blocked = create_tag(writer, alice, "recruiter")
    tag_contact(writer, alice, survivor.id, kept.id, source=TagSource.RULE)
    tag_contact(writer, alice, loser.id, kept.id)
    tag_contact(writer, alice, loser.id, blocked.id, source=TagSource.RULE)
    untag_contact(writer, alice, loser.id, blocked.id)
    merge(writer, alice, survivor.id, loser.id)
    assert merge(writer, alice, survivor.id, loser.id) is survivor  # a no-op the second time
    writer.expire_all()
    assert assignments_of(writer, alice, survivor) == {kept.id: (TagSource.MANUAL, None)}
    assert suppressions_of(writer, alice, survivor) == {blocked.id}
    assert writer.scalar(scoped_count(alice, ContactTag)) == 1
    assert writer.scalar(scoped_count(alice, ContactTagSuppression)) == 1
    third = factories.make_contact(writer, alice)
    tag_contact(writer, alice, third.id, kept.id, source=TagSource.RULE)
    merge(writer, alice, survivor.id, third.id)
    writer.expire_all()
    assert assignments_of(writer, alice, survivor) == {kept.id: (TagSource.MANUAL, None)}
    assert writer.scalar(scoped_count(alice, ContactTag)) == 1


def test_merge_tags_roll_back_with_the_rest(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as session:
        alice = factories.make_user(session)
        survivor = factories.make_contact(session, alice)
        loser = factories.make_contact(session, alice)
        kept = create_tag(session, alice, "investor")
        blocked = create_tag(session, alice, "recruiter")
        tag_contact(session, alice, survivor.id, kept.id, source=TagSource.RULE)
        tag_contact(session, alice, loser.id, kept.id)
        tag_contact(session, alice, loser.id, blocked.id, source=TagSource.RULE)
        untag_contact(session, alice, loser.id, blocked.id)
        user_id, survivor_id, loser_id = alice.id, survivor.id, loser.id
        kept_id, blocked_id = kept.id, blocked.id
    with (
        pytest.raises(RuntimeError, match="after the merge"),
        session_scope(session_factory, write=True) as session,
    ):
        alice = session.get_one(User, user_id)
        merge(session, alice, survivor_id, loser_id)
        raise RuntimeError("after the merge")
    with session_scope(session_factory) as session:
        alice = session.get_one(User, user_id)
        survivor = session.scalars(scoped(alice, Contact).where(Contact.id == survivor_id)).one()
        loser = session.scalars(scoped(alice, Contact).where(Contact.id == loser_id)).one()
        assert assignments_of(session, alice, survivor) == {kept_id: (TagSource.RULE, None)}
        assert assignments_of(session, alice, loser) == {kept_id: (TagSource.MANUAL, None)}
        assert suppressions_of(session, alice, loser) == {blocked_id}
        assert suppressions_of(session, alice, survivor) == set()


def test_merge_never_moves_another_users_tags(writer: Session, users: tuple[User, User]) -> None:
    alice, bob = users
    mine = factories.make_contact(writer, alice)
    my_other = factories.make_contact(writer, alice)
    theirs = factories.make_contact(writer, bob)
    my_tag = create_tag(writer, alice, "investor")
    their_kept = create_tag(writer, bob, "investor")
    their_blocked = create_tag(writer, bob, "recruiter")
    tag_contact(writer, alice, my_other.id, my_tag.id)
    tag_contact(writer, bob, theirs.id, their_kept.id)
    tag_contact(writer, bob, theirs.id, their_blocked.id, source=TagSource.RULE)
    untag_contact(writer, bob, theirs.id, their_blocked.id)
    before = (assignments_of(writer, bob, theirs), suppressions_of(writer, bob, theirs))
    assert before == ({their_kept.id: (TagSource.MANUAL, None)}, {their_blocked.id})
    for actor, survivor_id, loser_id in (
        (bob, mine.id, my_other.id),  # B merging two of A's contacts
        (alice, mine.id, theirs.id),  # A folding one of B's into hers
        (bob, theirs.id, my_other.id),  # B folding one of A's into his
    ):
        with pytest.raises(ValueError, match="not one of user"):
            merge(writer, actor, survivor_id, loser_id)
    assert (assignments_of(writer, bob, theirs), suppressions_of(writer, bob, theirs)) == before
    assert assignments_of(writer, alice, my_other) == {my_tag.id: (TagSource.MANUAL, None)}
    assert assignments_of(writer, alice, mine) == {}
    merge(writer, alice, mine.id, my_other.id)  # her own merge moves only her rows
    writer.expire_all()
    assert assignments_of(writer, alice, mine) == {my_tag.id: (TagSource.MANUAL, None)}
    assert (assignments_of(writer, bob, theirs), suppressions_of(writer, bob, theirs)) == before


# --- merge: static-list membership (#81) ------------------------------------


def members_of(session: Session, user: User, list_id: int) -> list[int]:
    """Contact ids of a static list, the row level, tombstones and all.

    Deliberately not ``lists.list_members``, which hides a merged-away contact:
    the point of these tests is which contact the *row* names.
    """
    rows = session.scalars(
        scoped(user, ListMember).where(ListMember.list_id == list_id).order_by(ListMember.id)
    )
    return [row.contact_id for row in rows]


def test_merge_moves_list_memberships_to_the_survivor(
    writer: Session, users: tuple[User, User]
) -> None:
    """#81: the row kept naming the loser, so the person silently left the list."""
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    first = create_list(writer, alice, "First 100", ListKind.STATIC)
    second = create_list(writer, alice, "Warm", ListKind.STATIC)
    add_members(writer, alice, first.id, [loser.id])
    add_members(writer, alice, second.id, [loser.id])

    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()

    assert members_of(writer, alice, first.id) == [survivor.id]
    assert members_of(writer, alice, second.id) == [survivor.id]
    assert [c.id for c in list_members(writer, alice, first.id, limit=50)[0]] == [survivor.id]


def test_merge_dedupes_a_list_both_contacts_were_in(
    writer: Session, users: tuple[User, User]
) -> None:
    """``uq_list_members_user_id_list_id_contact_id`` allows one row per person per list."""
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    row = create_list(writer, alice, "First 100", ListKind.STATIC)
    add_members(writer, alice, row.id, [survivor.id, loser.id])
    assert member_count(writer, alice, row.id) == 2

    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()

    assert members_of(writer, alice, row.id) == [survivor.id]
    assert member_count(writer, alice, row.id) == 1


def test_merge_leaves_no_row_pointing_at_the_loser(
    writer: Session, users: tuple[User, User]
) -> None:
    """The assertion #81 asked for: CONTACT_CHILDREN now walks ``list_members`` too."""
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    shared = create_list(writer, alice, "Both", ListKind.STATIC)
    only_loser = create_list(writer, alice, "Loser only", ListKind.STATIC)
    add_members(writer, alice, shared.id, [survivor.id, loser.id])
    add_members(writer, alice, only_loser.id, [loser.id])

    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()

    assert ListMember in CONTACT_CHILDREN
    for child in CONTACT_CHILDREN:
        assert writer.scalar(scoped_count(alice, child).where(child.contact_id == loser.id)) == 0, (
            child.__name__
        )


def test_merge_never_moves_another_users_list_memberships(
    writer: Session, users: tuple[User, User]
) -> None:
    alice, bob = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    theirs = factories.make_contact(writer, bob)
    mine = create_list(writer, alice, "First 100", ListKind.STATIC)
    yours = create_list(writer, bob, "First 100", ListKind.STATIC)
    add_members(writer, alice, mine.id, [loser.id])
    add_members(writer, bob, yours.id, [theirs.id])

    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()

    assert members_of(writer, alice, mine.id) == [survivor.id]
    assert members_of(writer, bob, yours.id) == [theirs.id]


def test_merge_keeps_the_date_the_person_joined_the_list(
    writer: Session, users: tuple[User, User]
) -> None:
    """A moved row keeps its ``added_at``: the merge is not a new addition."""
    alice, _ = users
    survivor = factories.make_contact(writer, alice)
    loser = factories.make_contact(writer, alice)
    row = create_list(writer, alice, "First 100", ListKind.STATIC)
    add_members(writer, alice, row.id, [loser.id])
    membership = writer.scalars(scoped(alice, ListMember)).one()
    added_at = membership.added_at

    merge(writer, alice, survivor.id, loser.id)
    writer.expire_all()

    moved = writer.scalars(scoped(alice, ListMember)).one()
    assert (moved.contact_id, moved.added_at) == (survivor.id, added_at)


# --- #206 review: a link is http or https, or has no scheme ------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "  javascript:alert(1)",
        "\tjavascript:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        "\x01javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        "VBScript:msgbox(1)",
        "mailto:priya.fake@example.test",
    ],
)
def test_a_link_with_another_scheme_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="http or https"):
        IncomingLink(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://a.test",
        "HTTP://a.test/x",
        " http://a.test ",
        "a.test/path",  # no scheme: kept as given
        "//a.test/x",  # protocol-relative: no scheme of its own
        "javascript%3Aalert(1)",  # an encoded colon is no scheme
        "&#106;avascript:alert(1)",  # nor is an entity: nothing decodes it
    ],
)
def test_an_http_link_or_one_with_no_scheme_is_kept(url: str) -> None:
    assert IncomingLink(url).url == url.strip()


def test_the_link_schemes_are_pinned() -> None:
    assert frozenset({"http", "https"}) == LINK_SCHEMES
