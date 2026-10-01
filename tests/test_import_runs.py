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

from netkeeper.crm import identity, import_runs, triage
from netkeeper.crm.identity import CreateNew, MergeInto
from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.lists import add_members, create_list
from netkeeper.crm.provenance import may_overwrite, set_manual_field
from netkeeper.crm.tags import create_rule, create_tag, tag_contact
from netkeeper.db import mark_for_write, session_scope
from netkeeper.models import (
    CampaignStatus,
    Contact,
    ContactEmail,
    ContactMet,
    ContactPhone,
    ContactSource,
    ContactTag,
    Enrollment,
    EnrollmentStatus,
    ImportResolution,
    ImportRow,
    ImportRun,
    ImportStatus,
    Interaction,
    InteractionKind,
    ListKind,
    RuleField,
    TemplateChannel,
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


def test_the_runs_to_undo_first_are_named_newest_first(writer: Session, user: User) -> None:
    """#217 item 2 (M2): with two later runs over the same field, the newest comes first."""
    seeded = seed_existing(writer, user)
    first = import_sample(writer, user)
    second = import_sample(writer, user, content=SECOND_FILE)
    third = import_sample(writer, user, content=SECOND_FILE.replace("Kite Director", "Kite Tsar"))
    assert seeded["fern"].current_title == "Kite Tsar"

    with pytest.raises(import_runs.RunSuperseded) as raised:
        import_runs.rollback(writer, user, first)

    assert raised.value.run_ids == (third, second)
    assert f"import run(s) {third}, {second}" in str(raised.value)


def test_a_later_run_that_only_refreshed_the_ledger_still_supersedes(
    writer: Session, user: User
) -> None:
    """#217 item 2 (M14): the same value, observed again, is a ``synced`` change only.

    Undoing the first run would put back the ``synced_values`` entry from before
    it, and the later run's rollback would then put back the first run's entry,
    which nothing would back any more.
    """
    seeded = seed_existing(writer, user)
    fern = seeded["fern"]
    first = import_sample(writer, user)
    same = SECOND_FILE.replace("Kite Director", fern.current_title or "")
    later = import_sample(writer, user, content=same)
    (row,) = import_runs.get_run(writer, user, later).rows
    assert row.changes_json is not None
    assert "current_title" not in row.changes_json["fields"]
    assert "current_title" not in row.changes_json["sources"]
    assert "current_title" in row.changes_json["synced"]

    with pytest.raises(import_runs.RunSuperseded) as raised:
        import_runs.rollback(writer, user, first)

    assert raised.value.run_ids == (later,)


@pytest.mark.parametrize("kind", ["sources", "synced"])
def test_a_later_run_that_recorded_only_a_source_or_ledger_entry_supersedes(
    writer: Session, user: User, kind: str
) -> None:
    """#217 item 2 (M14): each of the row's three records counts as writing the field.

    A run that changes a field's source changes its ledger entry too, so a row
    recording only a source cannot come from an import today; the later row is
    narrowed to one record by hand, so the check does not depend on which.
    """
    seeded = seed_existing(writer, user)
    first = import_sample(writer, user)
    later = import_sample(writer, user, content=SECOND_FILE)
    (row,) = import_runs.get_run(writer, user, later).rows
    assert row.changes_json is not None
    synced = row.changes_json["synced"]["current_title"]
    row.changes_json = {
        **row.changes_json,
        "fields": {},
        "sources": {"current_title": ContactSource.CSV.value} if kind == "sources" else {},
        "synced": {"current_title": synced} if kind == "synced" else {},
    }
    writer.flush()

    with pytest.raises(import_runs.RunSuperseded) as raised:
        import_runs.rollback(writer, user, first)

    assert raised.value.run_ids == (later,)
    assert raised.value.contact_ids == (seeded["fern"].id,)


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


# --- campaign rows on a created contact (#242) -------------------------------------


@pytest.mark.parametrize("force", [False, True])
def test_a_campaign_message_on_a_created_contact_refuses_the_rollback_even_with_force(
    writer: Session, user: User, force: bool
) -> None:
    """A message is never deleted (spec 8); without this the delete hit its FK and a 500."""
    seeded = seed_existing(writer, user)
    run_id, imogen = _created_imogen_after(writer, user)
    campaign = factories.make_campaign(writer, user)
    enrollment = factories.make_enrollment(writer, campaign, imogen, current_step=1)
    factories.make_message(writer, enrollment)

    with pytest.raises(import_runs.CreatedContactsMessaged) as raised:
        import_runs.rollback(writer, user, run_id, force=force)

    assert raised.value.contact_ids == (imogen.id,)
    assert raised.value.messages == 1
    assert "1 campaign message" in str(raised.value)
    assert "with force or without" in str(raised.value)
    # Refused before the first write.
    assert import_runs.get_run(writer, user, run_id).status is ImportStatus.COMMITTED
    assert seeded["fern"].current_title == "Head of Kites"
    assert writer.get(Contact, imogen.id) is not None


def test_a_message_naming_a_created_contact_only_through_its_enrollment_refuses(
    writer: Session, user: User
) -> None:
    """Deleting the contact cascades to the enrollment, which the message's FK keeps."""
    seed_existing(writer, user)
    run_id, imogen = _created_imogen_after(writer, user)
    elsewhere = factories.make_contact(writer, user)
    campaign = factories.make_campaign(writer, user)
    enrollment = factories.make_enrollment(writer, campaign, imogen, current_step=1)
    message = factories.make_message(writer, enrollment)
    message.contact_id = elsewhere.id  # a row the service never writes, but the FK allows
    writer.flush()

    with pytest.raises(import_runs.CreatedContactsMessaged) as raised:
        import_runs.rollback(writer, user, run_id, force=True)

    assert raised.value.contact_ids == (imogen.id,)


def test_a_message_naming_a_created_contact_itself_refuses_whoever_its_enrollment_names(
    writer: Session, user: User
) -> None:
    """The other half: the message's own ``contact_id`` is the created contact (#242 review)."""
    seed_existing(writer, user)
    run_id, imogen = _created_imogen_after(writer, user)
    elsewhere = factories.make_contact(writer, user)
    campaign = factories.make_campaign(writer, user)
    enrollment = factories.make_enrollment(writer, campaign, elsewhere, current_step=1)
    message = factories.make_message(writer, enrollment)
    message.contact_id = imogen.id  # a row the service never writes, but the FK allows
    writer.flush()

    with pytest.raises(import_runs.CreatedContactsMessaged) as raised:
        import_runs.rollback(writer, user, run_id, force=True)

    assert raised.value.contact_ids == (imogen.id,)
    assert raised.value.messages == 1


def test_every_message_and_every_messaged_contact_is_counted(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run_id, imogen = _created_imogen_after(writer, user)
    crispin = by_slug(writer, user, "crispin-vandermolen-qz")
    assert crispin is not None
    campaign = factories.make_campaign(
        writer, user, channels=(TemplateChannel.EMAIL, TemplateChannel.EMAIL)
    )
    to_imogen = factories.make_enrollment(writer, campaign, imogen, current_step=2)
    factories.make_message(writer, to_imogen, position=1)
    factories.make_message(writer, to_imogen, position=2)
    factories.make_message(writer, factories.make_enrollment(writer, campaign, crispin))

    with pytest.raises(import_runs.CreatedContactsMessaged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.contact_ids == tuple(sorted([imogen.id, crispin.id]))
    assert raised.value.messages == 3
    assert "2 contact(s) this run created" in str(raised.value)
    assert "3 campaign messages" in str(raised.value)


def test_messages_are_refused_before_a_merge_is(writer: Session, user: User) -> None:
    """A merge could be undone; a message never can, so it is the refusal to report."""
    seed_existing(writer, user)
    run_id, imogen = _created_imogen_after(writer, user)
    older = _older_record(writer, user)
    identity.merge(writer, user, imogen.id, older.id)
    campaign = factories.make_campaign(writer, user)
    factories.make_message(writer, factories.make_enrollment(writer, campaign, imogen))

    with pytest.raises(import_runs.CreatedContactsMessaged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.contact_ids == (imogen.id,)


def test_another_users_message_is_never_counted(writer: Session, user: User) -> None:
    seed_existing(writer, user)
    run_id, _ = _created_imogen_after(writer, user)
    stranger = factories.make_user(writer)
    theirs = factories.make_contact(writer, stranger)
    campaign = factories.make_campaign(writer, stranger)
    factories.make_message(writer, factories.make_enrollment(writer, campaign, theirs))

    result = import_runs.rollback(writer, user, run_id)

    assert result.contacts_deleted >= 1


def test_an_enrollment_with_nothing_sent_is_counted_and_force_deletes_it(
    writer: Session, user: User
) -> None:
    """A place in a campaign is something a person did; it goes only with force."""
    seed_existing(writer, user)
    run_id, imogen = _created_imogen_after(writer, user)
    imogen_id = imogen.id
    campaign = factories.make_campaign(writer, user, status=CampaignStatus.DRAFT)
    factories.make_enrollment(writer, campaign, imogen, status=EnrollmentStatus.PENDING)

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    acquired = raised.value.acquired
    assert acquired.enrollments == 1
    assert acquired.interactions == acquired.list_memberships == acquired.edited_contacts == 0
    assert "1 campaign enrollment" in str(raised.value)

    result = import_runs.rollback(writer, user, run_id, force=True)

    assert result.contacts_deleted >= 1
    assert writer.scalars(scoped(user, Contact).where(Contact.id == imogen_id)).first() is None
    assert writer.scalars(scoped(user, Enrollment)).first() is None


def _created_imogen_after(writer: Session, user: User) -> tuple[int, Contact]:
    """Like :func:`_created_imogen`, for a caller that seeded the existing contacts itself."""
    run_id = import_sample(writer, user)
    imogen = writer.scalars(scoped(user, Contact).where(Contact.last_name == "Thistlewhite")).one()
    return run_id, imogen


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


def test_a_triage_decision_on_a_created_contact_is_counted(writer: Session, user: User) -> None:
    """#217 item 2 (M8)."""
    run_id, imogen = _created_imogen(writer, user)
    triage.decide(writer, user, imogen.id, ContactMet.MET, at=NOW)
    triage.decide(writer, user, imogen.id, ContactMet.NOT_MET, at=NOW)
    writer.flush()

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.acquired.triage_decisions == 2
    assert "2 triage decisions" in str(raised.value)


@pytest.mark.parametrize(
    ("attribute", "value"), [("met", ContactMet.MET), ("do_not_contact", True)]
)
def test_met_or_do_not_contact_on_a_created_contact_counts_as_an_edit(
    writer: Session, user: User, attribute: str, value: object
) -> None:
    """#217 item 2 (M7): each of a person's own marks is an edit on its own."""
    run_id, imogen = _created_imogen(writer, user)
    setattr(imogen, attribute, value)
    writer.flush()

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    acquired = raised.value.acquired
    assert acquired.edited_contacts == 1
    assert acquired.triage_decisions == acquired.enriched_contacts == 0


def _sync_imogen(writer: Session, user: User, imogen: Contact) -> None:
    """A sync that fills in the URN, headline and location: no job field, so no snapshot."""
    incoming = identity.IncomingContact(
        source=ContactSource.SYNC,
        observed_at=datetime.now(UTC),  # after the run's own observation
        li_urn="urn:li:fsd_profile:ACoAAImogenQZ",
        li_public_id=imogen.li_public_id,
        headline="Brine whisperer",
        location="Farland",
    )
    identity.apply(writer, user, incoming, identity.resolve(writer, user, incoming), snapshot=False)
    writer.flush()


def test_a_sync_that_enriched_a_created_contact_is_counted(writer: Session, user: User) -> None:
    """#217 item 1: a rollback never deletes what an enrichment added later."""
    run_id, imogen = _created_imogen(writer, user)
    _sync_imogen(writer, user, imogen)
    assert imogen.li_urn == "urn:li:fsd_profile:ACoAAImogenQZ"

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    acquired = raised.value.acquired
    assert imogen.id in acquired.contact_ids
    assert acquired.enriched_contacts == 1
    assert acquired.children == acquired.edited_contacts == acquired.later_imports == 0
    assert "1 contact another source filled in since" in str(raised.value)
    assert import_runs.get_run(writer, user, run_id).status is ImportStatus.COMMITTED
    assert writer.get(Contact, imogen.id) is not None


def test_a_synced_value_the_live_column_refused_still_counts(writer: Session, user: User) -> None:
    """A sync's observation is kept in ``synced_values`` even where the column refused it."""
    run_id, imogen = _created_imogen(writer, user)
    set_manual_field(imogen, "headline", "Pickles, mostly")
    writer.flush()
    incoming = identity.IncomingContact(
        source=ContactSource.SYNC,
        observed_at=datetime.now(UTC),  # after the run's own observation
        li_public_id=imogen.li_public_id,
        headline="Brine whisperer",
    )
    identity.apply(writer, user, incoming, identity.resolve(writer, user, incoming), snapshot=False)
    writer.flush()
    assert imogen.headline == "Pickles, mostly"
    assert imogen.field_sources["headline"] == ContactSource.MANUAL.value
    assert imogen.synced_values["headline"]["source"] == ContactSource.SYNC.value

    with pytest.raises(import_runs.CreatedContactsChanged) as raised:
        import_runs.rollback(writer, user, run_id)

    assert raised.value.acquired.enriched_contacts == 1
    assert raised.value.acquired.edited_contacts == 1


def test_the_runs_own_source_is_not_counted_as_enrichment(writer: Session, user: User) -> None:
    """Every field a CSV run fills is recorded as ``csv``; that alone is no reason to refuse."""
    run_id, imogen = _created_imogen(writer, user)
    assert set(imogen.field_sources.values()) == {ContactSource.CSV.value}
    assert {entry["source"] for entry in imogen.synced_values.values()} == {ContactSource.CSV.value}

    result = import_runs.rollback(writer, user, run_id)

    assert result.contacts_deleted >= 1


def test_force_deletes_a_contact_a_sync_enriched(writer: Session, user: User) -> None:
    run_id, imogen = _created_imogen(writer, user)
    imogen_id = imogen.id
    _sync_imogen(writer, user, imogen)

    import_runs.rollback(writer, user, run_id, force=True)

    assert writer.scalars(scoped(user, Contact).where(Contact.id == imogen_id)).first() is None


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


# --- the name-and-company duplicate warning (#228) --------------------------

_NAME_MAPPING = {"First Name": "first_name", "Last Name": "last_name", "Company": "current_company"}


def _run_of(session: Session, user: User, content: str) -> ImportRun:
    """Draft ``content`` with :data:`_NAME_MAPPING` and commit it, deciding every candidate new.

    ``CREATE_NEW`` is what makes two rows with no identifier at all land as two
    contacts rather than one (spec 8.2 step 4 never folds them together) --
    exactly the shape the warning is about.
    """
    run = import_runs.create_run(
        session, user, filename="dupes.csv", content=content, mapping=_NAME_MAPPING
    )
    return import_runs.commit(
        session, user, run.id, undecided=import_runs.UndecidedPolicy.CREATE_NEW
    )


def test_two_rows_with_the_same_name_and_company_warn_as_one_group(
    writer: Session, user: User
) -> None:
    content = "First Name,Last Name,Company\nJordan,Vance,Acme Inc\nJordan,Vance,Acme Inc\n"
    committed = _run_of(writer, user, content)

    assert committed.created_count == 2
    groups = import_runs.duplicate_groups(committed)
    assert len(groups) == 1
    (group,) = groups
    assert [contact.row_number for contact in group.contacts] == [1, 2]
    contact_ids = {contact.contact_id for contact in group.contacts}
    assert len(contact_ids) == 2
    assert committed.report_json == {
        "duplicate_groups": [
            {
                "contacts": [
                    {"contact_id": group.contacts[0].contact_id, "row_number": 1},
                    {"contact_id": group.contacts[1].contact_id, "row_number": 2},
                ]
            }
        ]
    }


def test_three_rows_with_the_same_name_and_company_warn_as_one_group_of_three(
    writer: Session, user: User
) -> None:
    content = (
        "First Name,Last Name,Company\n"
        "Priya,Natarajan,Quill and Ink Press\n"
        "Priya,Natarajan,Quill and Ink Press\n"
        "Priya,Natarajan,Quill and Ink Press\n"
    )
    committed = _run_of(writer, user, content)

    assert committed.created_count == 3
    groups = import_runs.duplicate_groups(committed)
    assert len(groups) == 1
    (group,) = groups
    assert [contact.row_number for contact in group.contacts] == [1, 2, 3]
    assert len({contact.contact_id for contact in group.contacts}) == 3


def test_the_warning_ignores_case_and_surrounding_whitespace(writer: Session, user: User) -> None:
    """Candidate matching is case-insensitive and trimmed (spec 8.2 step 4); so is this."""
    content = (
        "First Name,Last Name,Company\n"
        "Priya,Natarajan,Quill and Ink Press\n"
        " priya , NATARAJAN ,  quill and ink press  \n"
    )
    committed = _run_of(writer, user, content)

    assert committed.created_count == 2
    groups = import_runs.duplicate_groups(committed)
    assert len(groups) == 1
    assert [contact.row_number for contact in groups[0].contacts] == [1, 2]


def test_no_warning_for_rows_with_a_different_company(writer: Session, user: User) -> None:
    content = "First Name,Last Name,Company\nJordan,Vance,Acme Inc\nJordan,Vance,Globex LLC\n"
    committed = _run_of(writer, user, content)

    assert committed.created_count == 2
    assert import_runs.duplicate_groups(committed) == []


def test_no_warning_for_rows_sharing_a_name_but_not_a_company(writer: Session, user: User) -> None:
    """The item's own done-when case: a name alone is never enough to group (spec 8.2 step 4)."""
    content = "First Name,Last Name,Company\nJordan,Vance,Acme Inc\nJordan,Vance,\n"
    committed = _run_of(writer, user, content)

    assert committed.created_count == 2
    assert import_runs.duplicate_groups(committed) == []


def test_no_warning_when_neither_row_has_a_company(writer: Session, user: User) -> None:
    """Two rows that share a name and *neither* carries a company do not group either.

    This is not provable by two rows disagreeing (the case above): it would
    also pass if :func:`~netkeeper.crm.identity.name_company_key` stopped
    requiring a company at all and fell back to, say, an empty string for a
    missing one -- both rows would then carry the *same* fallback key and group
    regardless. Two new contacts, both company-less, is the shape that
    actually exercises that guard.
    """
    content = "First Name,Last Name,Company\nJordan,Vance,\nJordan,Vance,\n"
    committed = _run_of(writer, user, content)

    assert committed.created_count == 2
    assert import_runs.duplicate_groups(committed) == []


def test_no_warning_when_the_rows_name_different_people(writer: Session, user: User) -> None:
    """Two rows that each carry their *own* profile URL are two different people, not a
    duplicate: an identical identifier would have matched the first row's contact outright,
    before name and company were ever consulted, so two *different* identifiers on rows that
    land in the same name-and-company group prove the file itself told them apart. Calling a
    merge between them "safe" would be wrong, so the group is left out entirely (#228) --
    rather than kept with softer wording, since there is no ambiguity here to warn about.
    """
    mapping = {**_NAME_MAPPING, "Profile Url": "li_url"}
    content = (
        "First Name,Last Name,Company,Profile Url\n"
        "Jordan,Vance,Acme Inc,https://www.linkedin.com/in/jordan-vance-aa/\n"
        "Jordan,Vance,Acme Inc,https://www.linkedin.com/in/jordan-vance-bb/\n"
    )
    run = import_runs.create_run(
        writer, user, filename="dupes.csv", content=content, mapping=mapping
    )
    committed = import_runs.commit(
        writer, user, run.id, undecided=import_runs.UndecidedPolicy.CREATE_NEW
    )

    assert committed.created_count == 2
    assert import_runs.duplicate_groups(committed) == []


def test_a_group_is_kept_when_only_one_of_its_rows_carries_an_identifier(
    writer: Session, user: User
) -> None:
    """One row with its own profile URL, next to one with none, is still this file's
    ambiguity: the identified row's contact is a specific, known person, but the
    identifier-less row could still be that same person for all the file says -- unlike
    two rows that each name someone different, nothing here rules that out.
    """
    mapping = {**_NAME_MAPPING, "Profile Url": "li_url"}
    content = (
        "First Name,Last Name,Company,Profile Url\n"
        "Jordan,Vance,Acme Inc,https://www.linkedin.com/in/jordan-vance-aa/\n"
        "Jordan,Vance,Acme Inc,\n"
    )
    run = import_runs.create_run(
        writer, user, filename="dupes.csv", content=content, mapping=mapping
    )
    committed = import_runs.commit(
        writer, user, run.id, undecided=import_runs.UndecidedPolicy.CREATE_NEW
    )

    assert committed.created_count == 2
    groups = import_runs.duplicate_groups(committed)
    assert len(groups) == 1
    assert [contact.row_number for contact in groups[0].contacts] == [1, 2]


def test_no_warning_when_only_one_new_contact_matches_an_existing_one(
    writer: Session, user: User
) -> None:
    """A row that matches an existing contact is not itself new, so a lone twin never groups."""
    seed_existing(writer, user)
    run = import_runs.create_run(writer, user, filename="sample.csv", content=LINKEDHELPER)
    committed = import_runs.commit(writer, user, run.id, decisions={ROW_CANDIDATE: CreateNew()})

    # Barnaby's twin (ROW_CANDIDATE) is the only new contact that shares a name and
    # company with anybody -- and the contact it shares one with, Barnaby himself,
    # already existed before this commit, so there is no second *new* contact to
    # pair it with.
    assert import_runs.duplicate_groups(committed) == []


def test_no_groups_leaves_report_json_unset(writer: Session, user: User) -> None:
    content = "First Name,Last Name,Company\nJordan,Vance,Acme Inc\n"
    committed = _run_of(writer, user, content)

    assert committed.report_json is None
    assert import_runs.duplicate_groups(committed) == []


def test_duplicate_groups_is_empty_for_a_draft_run(writer: Session, user: User) -> None:
    content = "First Name,Last Name,Company\nJordan,Vance,Acme Inc\nJordan,Vance,Acme Inc\n"
    run = import_runs.create_run(
        writer, user, filename="dupes.csv", content=content, mapping=_NAME_MAPPING
    )

    assert import_runs.duplicate_groups(run) == []


def test_duplicate_groups_is_empty_once_the_run_is_rolled_back(writer: Session, user: User) -> None:
    """Both contacts a group named are deleted by the rollback, so the warning stops too.

    ``report_json`` itself keeps the group as history -- :func:`_duplicate_groups_json`
    is not re-run and nothing deletes the key -- only what :func:`duplicate_groups` hands
    back changes, because a run that cannot be rolled back twice has nothing left to warn
    anyone away from merging.
    """
    content = "First Name,Last Name,Company\nJordan,Vance,Acme Inc\nJordan,Vance,Acme Inc\n"
    committed = _run_of(writer, user, content)
    assert len(import_runs.duplicate_groups(committed)) == 1
    stored = committed.report_json

    rolled_back = import_runs.rollback(writer, user, committed.id)

    assert rolled_back.contacts_deleted == 2
    assert import_runs.duplicate_groups(committed) == []
    assert committed.report_json == stored  # history, not deleted


def test_duplicate_warning_words_the_total_and_the_rollback_cost() -> None:
    assert import_runs.duplicate_warning([]) is None
    group = import_runs.DuplicateGroup(
        (
            import_runs.DuplicateContact(contact_id=5, row_number=1),
            import_runs.DuplicateContact(contact_id=6, row_number=2),
        )
    )
    warning = import_runs.duplicate_warning([group])
    assert warning is not None
    assert warning.startswith(
        "2 new contacts share a name and company with another row in this file."
    )
    assert "no longer be rolled back" in warning


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
