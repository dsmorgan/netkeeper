"""Import runs (P1-04): draft, preview, commit in one transaction, and rollback by run id.

The rollback tests come first on purpose. "A rollback removes only what that run
created" is the part of spec 10.5 that is easiest to get subtly wrong: it needs
``import_rows`` to have recorded the value that was there *before* the import,
not only the value the import wrote.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import factories
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import identity, import_runs
from netkeeper.crm.identity import CreateNew, MergeInto
from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.lists import add_members, create_list
from netkeeper.crm.provenance import may_overwrite, set_manual_field
from netkeeper.crm.tags import create_rule, create_tag, tag_contact
from netkeeper.db import mark_for_write, session_scope
from netkeeper.models import (
    Contact,
    ContactEmail,
    ContactPhone,
    ContactSource,
    ContactTag,
    ImportResolution,
    ImportRow,
    ImportStatus,
    Interaction,
    InteractionKind,
    ListKind,
    RuleField,
    User,
)
from netkeeper.scoping import scoped, unscoped

FIXTURES = Path(__file__).parent / "fixtures" / "csv"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LINKEDHELPER = (FIXTURES / "linkedhelper-sample.csv").read_text()
NINE_COLUMN = (FIXTURES / "nine-column-sample.csv").read_text()

# The rows of the LinkedHelper sample, by what each one exercises.
ROW_MATCHED = 1  # Fern: an existing contact, enriched
ROW_REFUSED = 2  # Wilhelmina: an existing contact with a manual headline
ROW_CANDIDATE = 3  # Barnaby: name and company match, nothing else does
ROW_NEW = 4  # Imogen: nobody yet
ROW_DUPLICATE = 5  # Imogen again, with a different position
ROW_BAD_EMAIL = 6  # Crispin: new, and the address in the file is not one

# A second, smaller file that writes one field of an existing contact, so a
# rollback can be asked to undo the earlier of two runs that touched it.
SECOND_FILE = (
    "Profile Url,First Name,Last Name,Position\n"
    "https://www.linkedin.com/in/fern-oglethorpe-qz/,Fern,Oglethorpe,Kite Director\n"
)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A guarded writer session, as an importer needs. Uncommitted work is discarded."""
    session = session_factory()
    mark_for_write(session)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def seed_existing(session: Session, user: User) -> dict[str, Contact]:
    """The two contacts the LinkedHelper sample should find, and the candidate's twin."""
    fern = factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id="fern-oglethorpe-qz",
        first_name="Fern",
        last_name="Oglethorpe",
        headline=None,
        current_title="Kite Apprentice",
        current_company="Brimstone Kite Works",
        location=None,
        source=ContactSource.CSV,
    )
    wilhelmina = factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id="wilhelmina-pockrandt-qz",
        first_name="Wilhelmina",
        last_name="Pockrandt",
        headline="Tinsmith, retired",
        current_title="Chief Tinsmith",
        current_company="Gossamer Tin Ltd",
        source=ContactSource.CSV,
    )
    # A person's own edit: no import may overwrite it until they revert it (spec 10.5).
    set_manual_field(wilhelmina, "headline", "Tinsmith, retired")
    barnaby = factories.make_contact(
        session,
        user,
        first_name="Barnaby",
        last_name="Fitzmaurice",
        current_company="Wobblegong Analytics",
        current_title="Wobbler",
        source=ContactSource.SYNC,
    )
    session.flush()
    return {"fern": fern, "wilhelmina": wilhelmina, "barnaby": barnaby}


def by_slug(session: Session, user: User, slug: str) -> Contact | None:
    return session.scalars(scoped(user, Contact).where(Contact.li_public_id == slug)).one_or_none()


def import_sample(
    session: Session, user: User, *, content: str = LINKEDHELPER, **kwargs: Any
) -> int:
    """Draft the sample, commit it skipping undecided candidates, and return the run id."""
    run = import_runs.create_run(session, user, filename="sample.csv", content=content, **kwargs)
    import_runs.commit(session, user, run.id, undecided=import_runs.UndecidedPolicy.SKIP)
    return run.id


# --- the rules run with the commit ------------------------------------------


def test_committing_tags_the_contacts_it_wrote_and_counts_them(writer: Session, user: User) -> None:
    """#64: the rules run inside the commit, over the rows it wrote and no others."""
    seed_existing(writer, user)
    heads = create_tag(writer, user, "head-of")
    create_rule(writer, user, heads.id, RuleField.TITLE, r"\bhead of\b")
    stranger = factories.make_contact(
        writer, user, li_public_id="not-in-the-file", current_title="Head of Nothing"
    )

    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    assert (run.tagged_contacts, run.tags_added) == (0, 0), "a draft writes nothing to tag"
    committed = import_runs.commit(writer, user, run.id, undecided=import_runs.UndecidedPolicy.SKIP)

    # Every row that wrote a contact, counted per person: the file names Imogen
    # twice, and the second row matches the contact the first one created.
    assert committed.tagged_contacts == committed.matched_count + committed.created_count - 1
    # Fern ("Head of Kites") and Imogen ("Head of Pickles") from the rule this
    # test makes, and Wilhelmina from the "retired" default, whose headline in
    # the fixture says so.
    assert committed.tags_added == 3
    tagged = {
        row.contact_id
        for row in writer.scalars(scoped(user, ContactTag).where(ContactTag.tag_id == heads.id))
    }
    fern = by_slug(writer, user, "fern-oglethorpe-qz")
    imogen = by_slug(writer, user, "imogen-thistlewhite-qz")
    assert fern is not None and imogen is not None
    assert tagged == {fern.id, imogen.id}
    assert stranger.id not in tagged, "a contact the file never named was not examined"


# --- rollback ---------------------------------------------------------------


def test_rollback_deletes_what_the_run_created_and_restores_what_it_enriched(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    before = {
        "current_title": fern.current_title,
        "headline": fern.headline,
        "location": fern.location,
        "connected_on": fern.connected_on,
        "field_sources": dict(fern.field_sources),
        "emails": {row.email for row in fern.emails},
    }
    run_id = import_sample(writer, user)

    # The import did land: Fern is enriched and two people are new.
    assert fern.current_title == "Head of Kites"
    assert fern.headline == "Kites at altitude"
    assert fern.connected_on == date(2026, 3, 14)
    assert {row.email for row in fern.emails} == {"fern@kites.example"}
    assert by_slug(writer, user, "imogen-thistlewhite-qz") is not None
    assert by_slug(writer, user, "crispin-vandermolen-qz") is not None

    result = import_runs.rollback(writer, user, run_id)

    assert result.contacts_deleted == 2
    assert result.contacts_restored == 2  # Fern and Wilhelmina; the duplicate row's is deleted
    assert by_slug(writer, user, "imogen-thistlewhite-qz") is None
    assert by_slug(writer, user, "crispin-vandermolen-qz") is None
    # Fern was only enriched, so Fern stays, with the values from before the run.
    restored = by_slug(writer, user, "fern-oglethorpe-qz")
    assert restored is not None
    assert restored.id == fern.id
    assert restored.current_title == before["current_title"]
    assert restored.headline == before["headline"]
    assert restored.location == before["location"]
    assert restored.connected_on == before["connected_on"]
    assert dict(restored.field_sources) == before["field_sources"]
    assert {row.email for row in restored.emails} == before["emails"]
    assert restored.phones == []
    # The ledger entry the run wrote goes too: the run is the only reason it is there.
    wilhelmina = seeded["wilhelmina"]
    assert wilhelmina.location is None
    assert wilhelmina.connected_on is None
    assert "headline" not in wilhelmina.synced_values
    assert import_runs.get_run(writer, user, run_id).status is ImportStatus.ROLLED_BACK


def test_rollback_keeps_a_value_something_changed_after_the_import(
    writer: Session, user: User
) -> None:
    """The run only undoes what still holds what it wrote (spec 10.5)."""
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    run_id = import_sample(writer, user)
    assert fern.current_title == "Head of Kites"

    set_manual_field(fern, "current_title", "Kite Emeritus")
    writer.flush()
    import_runs.rollback(writer, user, run_id)

    assert fern.current_title == "Kite Emeritus"
    # The headline the run wrote was untouched since, so that one does go back.
    assert fern.headline is None


def test_rollback_leaves_a_contact_the_run_never_touched(writer: Session, user: User) -> None:
    seeded = seed_existing(writer, user)
    bystander = factories.make_contact(writer, user, first_name="Ottoline", last_name="Gubbins")
    run_id = import_sample(writer, user)
    import_runs.rollback(writer, user, run_id)

    assert writer.get(Contact, bystander.id) is not None
    # The manual headline was refused on the way in, so nothing about it changed.
    assert seeded["wilhelmina"].headline == "Tinsmith, retired"


def test_rollback_deletes_the_children_the_run_added_but_not_the_ones_it_found(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    fern.emails.append(
        ContactEmail(user_id=user.id, email="fern.old@kites.example", is_primary=True)
    )
    writer.flush()
    run_id = import_sample(writer, user)
    assert {row.email for row in fern.emails} == {"fern.old@kites.example", "fern@kites.example"}

    import_runs.rollback(writer, user, run_id)

    assert {row.email for row in fern.emails} == {"fern.old@kites.example"}


def test_a_run_can_only_be_rolled_back_once(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run_id = import_sample(writer, user)
    import_runs.rollback(writer, user, run_id)
    with pytest.raises(import_runs.RunNotCommitted):
        import_runs.rollback(writer, user, run_id)


def test_a_draft_run_cannot_be_rolled_back(writer: Session, user: User) -> None:
    run = import_runs.create_run(
        writer, user, filename="sample.csv", content=NINE_COLUMN, preset_name="nine-column"
    )
    with pytest.raises(import_runs.RunNotCommitted):
        import_runs.rollback(writer, user, run.id)


def test_rollback_needs_a_writer_session(session: Session, user_in: User) -> None:
    with pytest.raises(RuntimeError, match="writer session"):
        import_runs.rollback(session, user_in, 1)


def test_rollback_keeps_the_provenance_of_a_field_edited_after_the_import(
    writer: Session, user: User
) -> None:
    """The value and its source are one thing: undoing half of it reopens the edit.

    Putting ``field_sources`` back while leaving the later value in place would
    leave a person's own words on the contact marked as nobody's, and the next
    import would overwrite them (spec 10.5, CP1 #28).
    """
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    run_id = import_sample(writer, user)
    assert fern.current_title == "Head of Kites"

    set_manual_field(fern, "current_title", "Kite Emeritus")
    writer.flush()
    import_runs.rollback(writer, user, run_id)

    assert fern.current_title == "Kite Emeritus"
    assert fern.field_sources["current_title"] == ContactSource.MANUAL.value
    assert not may_overwrite("current_title", ContactSource.CSV.value, fern)
    # The ledger entry stays too, so the edit still has something to revert to.
    assert fern.synced_values["current_title"]["value"] == "Head of Kites"


def test_a_later_import_still_refuses_an_edit_that_outlived_a_rollback(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    first = import_sample(writer, user)
    set_manual_field(fern, "current_title", "Kite Emeritus")
    writer.flush()
    import_runs.rollback(writer, user, first)

    import_sample(writer, user)  # the same file, all over again

    assert fern.current_title == "Kite Emeritus"
    assert fern.field_sources["current_title"] == ContactSource.MANUAL.value


def test_rolling_back_the_earlier_of_two_overlapping_runs_is_refused(
    writer: Session, user: User
) -> None:
    """#78 item 1. Each row records the value it found, so undoing run 1 first would
    leave run 2's rollback putting back run 1's value: a value no live run backs.
    """
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    first = import_sample(writer, user)
    second = import_sample(writer, user, content=SECOND_FILE)
    assert fern.current_title == "Kite Director"

    with pytest.raises(import_runs.RunSuperseded) as raised:
        import_runs.rollback(writer, user, first)

    assert raised.value.run_ids == (second,)
    assert raised.value.contact_ids == (fern.id,)
    assert f"import run(s) {second}" in str(raised.value)
    # Refused whole: nothing moved.
    assert import_runs.get_run(writer, user, first).status is ImportStatus.COMMITTED
    assert fern.current_title == "Kite Director"


def test_overlapping_runs_rolled_back_newest_first_end_where_they_began(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    sources_before = dict(fern.field_sources)
    synced_before = dict(fern.synced_values)
    first = import_sample(writer, user)
    second = import_sample(writer, user, content=SECOND_FILE)

    import_runs.rollback(writer, user, second)
    assert fern.current_title == "Head of Kites"  # the first run's value, which it backs
    import_runs.rollback(writer, user, first)

    assert fern.current_title == "Kite Apprentice"
    assert fern.headline is None
    assert dict(fern.field_sources) == sources_before
    assert dict(fern.synced_values) == synced_before


def test_a_later_run_that_rolled_back_no_longer_blocks_the_earlier_one(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    first = import_sample(writer, user)
    second = import_sample(writer, user, content=SECOND_FILE)
    import_runs.rollback(writer, user, second)

    import_runs.rollback(writer, user, first)

    assert seeded["fern"].current_title == "Kite Apprentice"


def test_a_later_run_on_other_contacts_does_not_block_a_rollback(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    first = import_runs.create_run(writer, user, filename="one.csv", content=SECOND_FILE)
    import_runs.commit(writer, user, first.id)
    other = (
        "Profile Url,First Name,Last Name,Position\n"
        "https://www.linkedin.com/in/wilhelmina-pockrandt-qz/,Wilhelmina,Pockrandt,Tin Queen\n"
    )
    later = import_runs.create_run(writer, user, filename="two.csv", content=other)
    import_runs.commit(writer, user, later.id)

    import_runs.rollback(writer, user, first.id)

    assert seeded["fern"].current_title == "Kite Apprentice"
    assert seeded["wilhelmina"].current_title == "Tin Queen"


# --- what a created contact gained since is not silently deleted (#78 item 2) ----


def _created_imogen(writer: Session, user: User) -> tuple[int, Contact]:
    """Commit the sample and return the run id and the contact it created for Imogen."""
    seed_existing(writer, user)
    run_id = import_sample(writer, user)
    imogen = writer.scalars(scoped(user, Contact).where(Contact.last_name == "Thistlewhite")).one()
    return run_id, imogen


def test_a_rollback_is_refused_when_a_created_contact_gained_things_since(
    writer: Session, user: User
) -> None:
    run_id, imogen = _created_imogen(writer, user)
    add_interaction(writer, user, imogen.id, InteractionKind.NOTE, NOW, "coffee")
    add_interaction(writer, user, imogen.id, InteractionKind.EMAIL_OUT, NOW, "thanks")
    tag_contact(writer, user, imogen.id, create_tag(writer, user, "pickles").id)
    shortlist = create_list(writer, user, "shortlist", ListKind.STATIC)
    add_members(writer, user, shortlist.id, [imogen.id])
    imogen.notes = "Knows everyone in brining."
    writer.flush()

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    acquired = raised.value.acquired
    assert imogen.id in acquired.contact_ids
    assert (acquired.interactions, acquired.tags, acquired.list_memberships) == (2, 1, 1)
    assert acquired.edited_contacts == 1
    assert acquired.triage_decisions == acquired.children == acquired.later_imports == 0
    assert (
        "2 interactions, 1 tag added by hand, 1 list membership, 1 contact with your own edits"
    ) in str(raised.value)
    # Refused whole: nothing moved.
    assert import_runs.get_run(writer, user, run_id).status is ImportStatus.COMMITTED
    assert by_slug(writer, user, "fern-oglethorpe-qz") is not None


def test_force_rolls_back_anyway_and_deletes_what_the_contact_gained(
    writer: Session, user: User
) -> None:
    run_id, imogen = _created_imogen(writer, user)
    imogen_id = imogen.id
    add_interaction(writer, user, imogen_id, InteractionKind.NOTE, NOW, "coffee")
    writer.flush()

    result = import_runs.rollback(writer, user, run_id, force=True)

    assert result.contacts_deleted >= 1
    assert writer.scalars(scoped(user, Contact).where(Contact.id == imogen_id)).first() is None
    assert writer.scalars(scoped(user, Interaction)).first() is None


def test_rule_tags_and_the_runs_own_children_are_not_counted_as_gained(
    writer: Session, user: User
) -> None:
    """The commit itself runs the auto-tag rules and creates child rows; neither is a loss."""
    seed_existing(writer, user)
    create_rule(writer, user, create_tag(writer, user, "picklers").id, RuleField.COMPANY, "pickle")
    run_id = import_sample(writer, user)
    tagged = writer.scalars(scoped(user, ContactTag)).all()
    assert tagged, "the rule should have tagged the created contact"

    result = import_runs.rollback(writer, user, run_id)

    assert result.contacts_deleted >= 1


def test_a_later_import_that_changed_a_created_contact_is_counted(
    writer: Session, user: User
) -> None:
    run_id, imogen = _created_imogen(writer, user)
    later = (
        "Profile Url,First Name,Last Name,Position\n"
        f"https://www.linkedin.com/in/{imogen.li_public_id}/,Imogen,Thistlewhite,Chief Pickler\n"
    )
    later_run = import_runs.create_run(writer, user, filename="later.csv", content=later)
    import_runs.commit(writer, user, later_run.id)

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.acquired.later_imports == 1


def test_a_detail_added_after_the_import_is_counted(writer: Session, user: User) -> None:
    run_id, imogen = _created_imogen(writer, user)
    imogen.phones.append(
        ContactPhone(
            user_id=user.id, raw="+15550100999", source=ContactSource.MANUAL, observed_at=NOW
        )
    )
    writer.flush()

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.acquired.children == 1


# --- the snapshot is not an N+1 (#78 item 4) ------------------------------------


def _file_of(count: int) -> str:
    lines = ["Profile Url,First Name,Last Name,Position"]
    lines += [
        f"https://www.linkedin.com/in/person-{n}-qz/,Person,Number{n},Title {n}"
        for n in range(count)
    ]
    return "\n".join(lines) + "\n"


def _child_selects(writer: Session, user: User, count: int) -> int:
    """SELECTs against the contact child tables while committing ``count`` matched rows."""
    for n in range(count):
        factories.make_contact(
            writer, user, li_urn=None, li_public_id=f"person-{n}-qz", emails=[f"p{n}@x.example"]
        )
    writer.flush()
    writer.expire_all()
    run = import_runs.create_run(writer, user, filename="many.csv", content=_file_of(count))
    writer.expire_all()
    tables = tuple(import_runs.CHILD_MODELS)
    seen: list[str] = []

    def count_selects(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        head = statement.lstrip().upper()
        if head.startswith("SELECT") and any(f"FROM {table.upper()}" in head for table in tables):
            seen.append(statement)

    engine = writer.get_bind()
    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        import_runs.commit(writer, user, run.id)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    return len(seen)


def test_committing_matched_rows_reads_child_rows_in_a_constant_number_of_queries(
    session_factory: sessionmaker[Session],
) -> None:
    counts = []
    for size in (3, 12):
        session = session_factory()
        mark_for_write(session)
        try:
            user = factories.make_user(session)
            counts.append(_child_selects(session, user, size))
        finally:
            session.rollback()
            session.close()
    small, large = counts
    assert large == small, f"child-table SELECTs grew with the file: {small} for 3, {large} for 12"


# --- a merge afterwards is refused, not overrun -----------------------------


def _older_record(session: Session, user: User) -> Contact:
    """A second, pre-existing record of the person the sample creates."""
    return factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id=None,
        first_name="Imogen",
        last_name="Thistlewhite",
        current_company="Nimbus Pickle Co",
        emails=["imogen.older@picklestuff.example"],
    )


def test_a_rollback_is_refused_when_the_created_contact_became_a_merge_survivor(
    writer: Session, user: User
) -> None:
    """Deleting it would take the loser's rows, which the run never created."""
    seeded = seed_existing(writer, user)
    run_id = import_sample(writer, user)
    imogen = by_slug(writer, user, "imogen-thistlewhite-qz")
    assert imogen is not None
    older = _older_record(writer, user)
    identity.merge(writer, user, imogen.id, older.id)  # the created contact survives

    with pytest.raises(import_runs.RunMerged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.contact_ids == (imogen.id,)
    # Nothing was undone: the refusal comes before the first write.
    assert by_slug(writer, user, "imogen-thistlewhite-qz") is not None
    assert seeded["fern"].current_title == "Head of Kites"
    assert import_runs.get_run(writer, user, run_id).status is ImportStatus.COMMITTED


def test_a_rollback_is_refused_when_the_created_contact_was_merged_away(
    writer: Session, user: User
) -> None:
    """Its children live on the survivor now, so deleting it leaves them behind."""
    seed_existing(writer, user)
    run_id = import_sample(writer, user)
    imogen = by_slug(writer, user, "imogen-thistlewhite-qz")
    assert imogen is not None
    older = _older_record(writer, user)
    created_id = imogen.id
    identity.merge(writer, user, older.id, imogen.id)  # the created contact loses

    with pytest.raises(import_runs.RunMerged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.contact_ids == (created_id,)
    assert import_runs.get_run(writer, user, run_id).status is ImportStatus.COMMITTED


def test_a_merge_between_contacts_the_run_never_created_does_not_block_a_rollback(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    run_id = import_sample(writer, user)
    bystander = factories.make_contact(writer, user, first_name="Ottoline", last_name="Gubbins")
    identity.merge(writer, user, seeded["fern"].id, bystander.id)

    result = import_runs.rollback(writer, user, run_id)

    assert result.contacts_deleted == 2


# --- the LinkedHelper sample: the item's "done when" ------------------------


def test_the_sample_enriches_existing_contacts_without_overwriting_manual_fields(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    run_id = import_sample(writer, user)
    run = import_runs.get_run(writer, user, run_id)

    assert run.status is ImportStatus.COMMITTED
    assert run.preset == "linkedhelper"
    # Fern, Wilhelmina, and the second Imogen row all land on a contact that exists.
    assert run.matched_count == 3
    assert run.created_count == 2
    assert run.skipped_count == 1  # the undecided candidate

    # The enriched contact took every field it had nothing of its own for.
    fern = seeded["fern"]
    assert fern.current_title == "Head of Kites"
    assert fern.location == "Pelican Bay, Farland"
    assert fern.field_sources["current_title"] == ContactSource.CSV.value

    # The manually edited field was refused, and the ledger still learned the value.
    wilhelmina = seeded["wilhelmina"]
    assert wilhelmina.headline == "Tinsmith, retired"
    assert wilhelmina.field_sources["headline"] == ContactSource.MANUAL.value
    assert wilhelmina.synced_values["headline"]["value"] == "Tinsmith to the gentry"

    rows, _ = import_runs.list_rows(writer, user, run_id)
    by_number = {row.row_number: row for row in rows}
    refused = (by_number[ROW_REFUSED].changes_json or {"refused": []})["refused"]
    assert [entry["field"] for entry in refused] == ["headline"]
    assert refused[0]["source"] == ContactSource.MANUAL.value
    assert refused[0]["kept"] == "Tinsmith, retired"


def test_a_row_repeated_in_the_file_lands_on_one_contact(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run_id = import_sample(writer, user)
    rows, _ = import_runs.list_rows(writer, user, run_id)
    by_number = {row.row_number: row for row in rows}

    assert by_number[ROW_NEW].resolution is ImportResolution.CREATED
    assert by_number[ROW_DUPLICATE].resolution is ImportResolution.MATCHED
    assert by_number[ROW_DUPLICATE].contact_id == by_number[ROW_NEW].contact_id
    imogen = by_slug(writer, user, "imogen-thistlewhite-qz")
    assert imogen is not None
    # The later row wins the field, as any second observation from the same source does.
    assert imogen.current_title == "Head of Pickles"


def test_a_malformed_address_is_dropped_and_the_rest_of_the_row_imports(
    writer: Session, user: User
) -> None:
    seed_existing(writer, user)
    run_id = import_sample(writer, user)
    rows, _ = import_runs.list_rows(writer, user, run_id)
    row = {row.row_number: row for row in rows}[ROW_BAD_EMAIL]

    assert row.resolution is ImportResolution.CREATED
    assert row.error is not None and "is not an email address" in row.error
    crispin = by_slug(writer, user, "crispin-vandermolen-qz")
    assert crispin is not None
    assert crispin.emails == []
    assert crispin.current_company == "Tessellate Robotics"


def test_a_header_with_trailing_whitespace_maps_like_any_other(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    assert run.mapping_json["Location"] == "location"
    assert "Location " not in run.mapping_json


# --- candidates -------------------------------------------------------------


def test_a_name_and_company_match_is_a_candidate_not_a_match(writer: Session, user: User) -> None:
    seeded = seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    rows, _ = import_runs.list_rows(writer, user, run.id)
    row = {row.row_number: row for row in rows}[ROW_CANDIDATE]

    assert row.resolution is ImportResolution.CANDIDATE
    assert row.candidate_ids_json == [seeded["barnaby"].id]
    assert row.contact_id is None


def test_commit_refuses_to_run_while_a_candidate_is_undecided(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    with pytest.raises(import_runs.UndecidedCandidates) as raised:
        import_runs.commit(writer, user, run.id)
    assert raised.value.row_numbers == (ROW_CANDIDATE,)


def test_a_merge_into_decision_enriches_the_contact_it_names(writer: Session, user: User) -> None:
    seeded = seed_existing(writer, user)
    barnaby = seeded["barnaby"]
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    import_runs.commit(writer, user, run.id, decisions={ROW_CANDIDATE: MergeInto(barnaby.id)})

    assert barnaby.current_title == "Senior Wobbler"
    rows, _ = import_runs.list_rows(writer, user, run.id)
    row = {row.row_number: row for row in rows}[ROW_CANDIDATE]
    assert row.resolution is ImportResolution.MATCHED
    assert row.contact_id == barnaby.id
    assert row.decision_json == {"kind": "merge_into", "contact_id": barnaby.id}


def test_a_create_new_decision_makes_a_second_contact(writer: Session, user: User) -> None:
    seeded = seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    import_runs.commit(writer, user, run.id, decisions={ROW_CANDIDATE: CreateNew()})

    twins = list(
        writer.scalars(
            scoped(user, Contact).where(Contact.last_name == "Fitzmaurice").order_by(Contact.id)
        )
    )
    assert len(twins) == 2
    assert twins[0].id == seeded["barnaby"].id
    assert twins[1].current_title == "Senior Wobbler"


# --- the undecided policy (#136) ----------------------------------------------


def test_the_create_new_policy_decides_the_undecided_rows_in_the_commit(
    writer: Session, user: User
) -> None:
    seeded = seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    committed = import_runs.commit(
        writer, user, run.id, undecided=import_runs.UndecidedPolicy.CREATE_NEW
    )

    twins = list(writer.scalars(scoped(user, Contact).where(Contact.last_name == "Fitzmaurice")))
    assert sorted(twin.id for twin in twins)[0] == seeded["barnaby"].id
    assert len(twins) == 2
    assert committed.skipped_count == 0
    rows, _ = import_runs.list_rows(writer, user, run.id)
    row = {row.row_number: row for row in rows}[ROW_CANDIDATE]
    assert row.resolution is ImportResolution.CREATED
    assert row.decision_json == {"kind": "create_new", "contact_id": None}
    assert row.candidate_ids_json == [seeded["barnaby"].id]


def test_a_recorded_decision_wins_over_the_policy(writer: Session, user: User) -> None:
    seeded = seed_existing(writer, user)
    barnaby = seeded["barnaby"]
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    import_runs.commit(
        writer,
        user,
        run.id,
        decisions={ROW_CANDIDATE: MergeInto(barnaby.id)},
        undecided=import_runs.UndecidedPolicy.CREATE_NEW,
    )

    assert barnaby.current_title == "Senior Wobbler"
    twins = writer.scalars(scoped(user, Contact).where(Contact.last_name == "Fitzmaurice"))
    assert [twin.id for twin in twins] == [barnaby.id]


def test_the_policy_leaves_a_row_that_stopped_being_a_candidate_to_import_normally(
    writer: Session, user: User
) -> None:
    """The window #136 closes: the row was a candidate when the draft was read, and is a
    plain new contact by commit time. It must import as one, not be handed a decision
    ``identity.apply()`` refuses for anything but a candidate and be skipped for it.
    """
    seeded = seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    writer.delete(seeded["barnaby"])
    writer.flush()

    committed = import_runs.commit(
        writer, user, run.id, undecided=import_runs.UndecidedPolicy.CREATE_NEW
    )

    rows, _ = import_runs.list_rows(writer, user, run.id)
    row = {row.row_number: row for row in rows}[ROW_CANDIDATE]
    assert row.resolution is ImportResolution.CREATED
    assert row.error is None
    assert row.decision_json is None
    assert committed.skipped_count == 0


def test_the_skip_policy_skips_and_counts_the_undecided_rows(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    committed = import_runs.commit(writer, user, run.id, undecided=import_runs.UndecidedPolicy.SKIP)

    rows, _ = import_runs.list_rows(writer, user, run.id)
    row = {row.row_number: row for row in rows}[ROW_CANDIDATE]
    assert row.resolution is ImportResolution.SKIPPED
    assert row.decision_json is None
    assert "waiting for a decision" in (row.error or "")
    assert committed.skipped_count >= 1


def test_a_decision_naming_a_row_the_run_does_not_have_is_an_error(
    writer: Session, user: User
) -> None:
    run = import_runs.create_run(writer, user, filename="sample.csv", content=NINE_COLUMN)
    with pytest.raises(import_runs.UnknownRow):
        import_runs.set_decisions(writer, user, run.id, {99: CreateNew()})


# --- preview ----------------------------------------------------------------


def test_preview_shows_the_first_rows_without_writing_anything(writer: Session, user: User) -> None:
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)

    previewed = import_runs.preview(writer, user, run.id)

    assert [row.resolution for row in previewed] == [
        ImportResolution.MATCHED,
        ImportResolution.MATCHED,
        ImportResolution.CANDIDATE,
        ImportResolution.CREATED,
        ImportResolution.MATCHED,
        ImportResolution.CREATED,
    ]
    assert previewed[ROW_MATCHED - 1].contact_id == fern.id
    assert previewed[ROW_MATCHED - 1].matched_by == "public_id"
    titles = {change.field: change for change in previewed[ROW_MATCHED - 1].changes}
    assert titles["current_title"].before == "Kite Apprentice"
    assert titles["current_title"].after == "Head of Kites"
    assert not titles["current_title"].refused
    # Nothing was kept: the preview resolved and applied inside a rolled-back savepoint.
    assert fern.current_title == "Kite Apprentice"
    assert by_slug(writer, user, "imogen-thistlewhite-qz") is None
    assert import_runs.get_run(writer, user, run.id).status is ImportStatus.DRAFT


def test_preview_names_the_field_a_manual_override_refuses(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    previewed = import_runs.preview(writer, user, run.id)
    refused = previewed[ROW_REFUSED - 1].refused

    assert [change.field for change in refused] == ["headline"]
    assert refused[0].before == "Tinsmith, retired"
    assert refused[0].after == "Tinsmith to the gentry"
    assert refused[0].kept_source == ContactSource.MANUAL.value


def test_preview_stops_at_its_limit(writer: Session, user: User) -> None:
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    assert len(import_runs.preview(writer, user, run.id, limit=2)) == 2


# --- runs, counts, and presets ----------------------------------------------


def test_a_draft_run_counts_the_whole_file_and_writes_nothing_else(
    writer: Session, user: User
) -> None:
    seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="people.csv", content=LINKEDHELPER)

    assert run.status is ImportStatus.DRAFT
    assert run.total_rows == 6
    assert (run.matched_count, run.created_count, run.candidate_count) == (3, 2, 1)
    assert run.filename == "people.csv"
    assert by_slug(writer, user, "imogen-thistlewhite-qz") is None


def test_the_nine_column_preset_is_detected_from_the_header(writer: Session, user: User) -> None:
    run = import_runs.create_run(writer, user, filename="export.csv", content=NINE_COLUMN)
    assert run.preset == "nine-column"
    assert run.mapping_json["Current Job Title"] == "current_title"
    assert run.mapping_json["CityState"] == "location"


def test_a_run_cannot_be_committed_twice(writer: Session, user: User) -> None:
    run_id = import_sample(writer, user, content=NINE_COLUMN)
    with pytest.raises(import_runs.RunNotDraft):
        import_runs.commit(writer, user, run_id, undecided=import_runs.UndecidedPolicy.SKIP)


# --- orphaned drafts: listing and deleting (#90) -----------------------------


def test_list_runs_narrows_by_status(writer: Session, user: User) -> None:
    committed_id = import_sample(writer, user, content=NINE_COLUMN)
    draft = import_runs.create_run(writer, user, filename="second.csv", content=NINE_COLUMN)

    drafts, draft_total = import_runs.list_runs(writer, user, status=ImportStatus.DRAFT)
    assert draft_total == 1
    assert [run.id for run in drafts] == [draft.id]

    committed, committed_total = import_runs.list_runs(writer, user, status=ImportStatus.COMMITTED)
    assert committed_total == 1
    assert [run.id for run in committed] == [committed_id]

    every_run, every_total = import_runs.list_runs(writer, user)
    assert every_total == 2
    assert {run.id for run in every_run} == {committed_id, draft.id}


def test_delete_run_removes_a_draft_and_its_rows(writer: Session, user: User) -> None:
    run = import_runs.create_run(writer, user, filename="orphan.csv", content=NINE_COLUMN)
    run_id = run.id
    assert run.total_rows == 3

    import_runs.delete_run(writer, user, run_id)

    with pytest.raises(import_runs.RunNotFound):
        import_runs.get_run(writer, user, run_id)
    remaining_rows = writer.scalars(scoped(user, ImportRow).where(ImportRow.run_id == run_id)).all()
    assert list(remaining_rows) == []


def test_delete_run_refuses_a_committed_run(writer: Session, user: User) -> None:
    run_id = import_sample(writer, user, content=NINE_COLUMN)
    with pytest.raises(import_runs.RunNotDraft):
        import_runs.delete_run(writer, user, run_id)
    # Refused, not half-deleted: the run and its rows are still there.
    assert import_runs.get_run(writer, user, run_id).status is ImportStatus.COMMITTED


def test_delete_run_refuses_a_rolled_back_run(writer: Session, user: User) -> None:
    run_id = import_sample(writer, user, content=NINE_COLUMN)
    import_runs.rollback(writer, user, run_id)
    with pytest.raises(import_runs.RunNotDraft):
        import_runs.delete_run(writer, user, run_id)


def test_delete_run_needs_a_writer_session(session: Session, user_in: User) -> None:
    with pytest.raises(RuntimeError, match="writer session"):
        import_runs.delete_run(session, user_in, 1)


def test_a_saved_preset_maps_a_file_the_built_ins_do_not(writer: Session, user: User) -> None:
    content = "Given,Family,Works At\nHortensia,Blennerhassett,Tarnish & Sons\n"
    import_runs.save_preset(
        writer,
        user,
        "our-crm",
        {"Given": "first_name", "Family": "last_name", "Works At": "current_company"},
    )
    run = import_runs.create_run(
        writer, user, filename="ours.csv", content=content, preset_name="our-crm"
    )

    assert run.preset == "our-crm"
    assert run.mapping_json == {
        "Given": "first_name",
        "Family": "last_name",
        "Works At": "current_company",
    }
    assert import_runs.saved_presets(writer, user)["our-crm"]["Given"] == "first_name"
    import_runs.delete_preset(writer, user, "our-crm")
    assert import_runs.saved_presets(writer, user) == {}


def test_a_saved_preset_may_not_shadow_a_built_in_one(writer: Session, user: User) -> None:
    with pytest.raises(import_runs.DuplicatePreset):
        import_runs.save_preset(writer, user, "nine-column", {"A": "first_name"})


def test_an_explicit_mapping_overrides_the_preset(writer: Session, user: User) -> None:
    run = import_runs.create_run(
        writer,
        user,
        filename="export.csv",
        content=NINE_COLUMN,
        preset_name="nine-column",
        mapping={"CityState": "headline", "Phone Number": ""},
    )
    assert run.mapping_json["CityState"] == "headline"
    assert "Phone Number" not in run.mapping_json


# --- one user cannot see or undo another's work -----------------------------


def test_one_user_cannot_see_or_roll_back_another_users_run(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        alice = factories.make_user(session)
        bob = factories.make_user(session)
        seed_existing(session, alice)
        run_id = import_sample(session, alice)
        alice_contacts = len(list(session.scalars(scoped(alice, Contact))))

        assert import_runs.list_runs(session, bob)[0] == []
        assert import_runs.list_runs(session, alice)[1] == 1
        with pytest.raises(import_runs.RunNotFound):
            import_runs.get_run(session, bob, run_id)
        with pytest.raises(import_runs.RunNotFound):
            import_runs.list_rows(session, bob, run_id)
        with pytest.raises(import_runs.RunNotFound):
            import_runs.rollback(session, bob, run_id)
        with pytest.raises(import_runs.RunNotFound):
            import_runs.preview(session, bob, run_id)
        with pytest.raises(import_runs.RunNotFound):
            import_runs.delete_run(session, bob, run_id)

        # Alice's run is untouched, and Bob's own presets are his alone.
        assert import_runs.get_run(session, alice, run_id).status is ImportStatus.COMMITTED
        assert len(list(session.scalars(scoped(alice, Contact)))) == alice_contacts
        import_runs.save_preset(session, alice, "hers", {"Given": "first_name"})
        assert import_runs.saved_presets(session, bob) == {}


def test_an_import_never_reaches_another_users_contact(
    session_factory: sessionmaker[Session],
) -> None:
    """Alice's file names Bob's contact by slug; it makes her own instead."""
    with session_scope(session_factory, write=True) as session:
        alice = factories.make_user(session)
        bob = factories.make_user(session)
        seed_existing(session, bob)
        bob_fern = by_slug(session, bob, "fern-oglethorpe-qz")
        assert bob_fern is not None

        import_sample(session, alice)
        alice_fern = by_slug(session, alice, "fern-oglethorpe-qz")

        assert alice_fern is not None
        assert alice_fern.id != bob_fern.id
        assert bob_fern.current_title == "Kite Apprentice"
        everyone = list(
            session.scalars(
                unscoped(select(Contact).where(Contact.li_public_id == "fern-oglethorpe-qz"))
            )
        )
        assert len(everyone) == 2


# --- a writer session is required -------------------------------------------


@pytest.fixture
def user_in(session: Session) -> User:
    return factories.make_user(session)


def test_creating_a_run_needs_a_writer_session(session: Session, user_in: User) -> None:
    with pytest.raises(RuntimeError, match="writer session"):
        import_runs.create_run(session, user_in, filename="x.csv", content=NINE_COLUMN)


def test_every_row_keeps_the_observed_at_of_its_run(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    started = datetime.now(UTC)
    run_id = import_sample(writer, user)
    run = import_runs.get_run(writer, user, run_id)
    assert run.committed_at is not None and run.committed_at >= started
