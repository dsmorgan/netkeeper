"""netkeeper.crm.archive: the sample archive becoming contacts and interactions (P1-03).

The counts asserted here are the sample's, counted by hand from the fixture
files, which is what "imports with correct counts" means for this item. The
sample is invented from end to end; see ``tests/test_linkedin_archive.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.archive import (
    INVITATION_SUMMARY,
    SUMMARY_MAX_CHARS,
    ArchiveImport,
    import_archive,
)
from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.provenance import SOURCE_RANK, revert_to_synced, set_manual_field
from netkeeper.db import session_scope
from netkeeper.linkedin.archive import Archive, open_archive
from netkeeper.models import Contact, ContactSource, Interaction, InteractionKind, User
from netkeeper.scoping import scoped

FIXTURES = Path(__file__).parent / "fixtures" / "archive"
OBSERVED = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def archive() -> Iterator[Archive]:
    with open_archive(FIXTURES) as opened:
        yield opened


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller of the importer must use (CLAUDE.md)."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _run(session: Session, user: User, archive: Archive) -> ArchiveImport:
    return import_archive(session, user, archive, observed_at=OBSERVED)


def _contacts(session: Session, user: User) -> dict[str, Contact]:
    return {
        contact.li_public_id or contact.first_name: contact
        for contact in session.scalars(scoped(user, Contact))
    }


def _all_contacts(session: Session, user: User) -> list[Contact]:
    return list(session.scalars(scoped(user, Contact)))


def _interactions(session: Session, user: User) -> list[Interaction]:
    return sorted(session.scalars(scoped(user, Interaction)), key=lambda row: (row.at, row.id))


# --- counts -----------------------------------------------------------------


def test_the_sample_imports_with_the_expected_counts(
    writer: Session, user: User, archive: Archive
) -> None:
    report = _run(writer, user, archive)

    connections = report.connections
    assert connections.rows == 9
    assert connections.created == 7  # six slugs, plus the row that carries only a name
    assert connections.updated == 1  # the duplicate of Ada
    assert connections.skipped == 1  # the row with neither a name nor a URL
    assert connections.needs_review == 0
    assert connections.with_email == 2  # the duplicate carries the same address
    assert connections.undated == 2  # one empty cell, one unreadable

    messages = report.messages
    assert report.owner_public_id == "nettie-keeperton"
    assert report.owner_by == "traffic"
    assert messages.rows == 11
    assert messages.conversations == 6
    assert messages.attributed == 3
    assert messages.no_counterpart == 1  # nobody signed conv-bb
    assert messages.group_threads == 1
    assert messages.unknown_contact == 1  # the stranger is not a connection
    assert messages.no_owner == 0
    assert messages.attributed + 3 == messages.conversations
    assert messages.added == 6
    assert messages.outbound == 2
    assert messages.inbound == 4
    assert messages.undated == 1
    assert messages.already_present == 0

    invitations = report.invitations
    assert invitations.rows == 6
    assert invitations.added == 2
    assert invitations.unknown_contact == 1
    assert invitations.no_counterpart == 1
    assert invitations.undirected == 1
    assert invitations.undated == 1
    assert invitations.already_present == 0

    assert len(_contacts(writer, user)) == 7
    assert len(_interactions(writer, user)) == 8


def test_observed_at_defaults_to_the_export_time(
    writer: Session, user: User, tmp_path: Path
) -> None:
    copy = tmp_path / "export"
    copy.mkdir()
    for source in FIXTURES.iterdir():
        (copy / source.name).write_bytes(source.read_bytes())
    with open_archive(copy) as opened:
        report = import_archive(writer, user, opened)
        assert report.observed_at == opened.exported_at


def test_a_naive_observed_at_is_refused(writer: Session, user: User, archive: Archive) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        import_archive(writer, user, archive, observed_at=datetime(2024, 6, 1, 12, 0))


def test_a_read_only_session_is_refused(
    session: Session, archive: Archive, session_factory: sessionmaker[Session]
) -> None:
    with session_scope(session_factory, write=True) as setup:
        user_id = factories.make_user(setup).id
    owner = session.get(User, user_id)
    assert owner is not None
    with pytest.raises(RuntimeError, match="writer session"):
        import_archive(session, owner, archive)


# --- contacts ---------------------------------------------------------------


def test_contacts_carry_the_archive_source_and_the_rows_values(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    ada = _contacts(writer, user)["ada-fictional"]
    assert ada.source is ContactSource.ARCHIVE
    assert (ada.first_name, ada.last_name) == ("Ada", "Fictional")
    assert (ada.current_company, ada.current_title) == ("Fictional Works", "Staff Engineer")
    assert ada.connected_on == date(2019, 3, 12)
    assert ada.li_url == "https://www.linkedin.com/in/ada-fictional/"
    assert ada.field_sources["first_name"] == ContactSource.ARCHIVE.value


def test_email_is_set_only_where_the_export_carried_one(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    contacts = _contacts(writer, user)
    assert [row.email for row in contacts["ada-fictional"].emails] == ["ada@example.test"]
    assert all(
        not contact.emails for slug, contact in contacts.items() if slug != "ada-fictional"
    ), "an empty Email Address cell must not become an empty address"


def test_a_two_digit_year_and_a_missing_one_both_land(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    contacts = _contacts(writer, user)
    assert contacts["dee-notional"].connected_on == date(2017, 7, 9)
    assert contacts["cyd-invented"].connected_on is None


def test_a_row_with_only_a_name_still_becomes_a_contact(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    fen = _contacts(writer, user)["Fen"]
    assert fen.li_public_id is None
    assert fen.current_company == "Pretend Co"


def test_the_duplicate_row_updates_rather_than_doubles(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    slugs = [contact.li_public_id for contact in _all_contacts(writer, user)]
    assert slugs.count("ada-fictional") == 1


def test_one_user_s_import_is_invisible_to_another(
    writer: Session, user: User, archive: Archive
) -> None:
    other = factories.make_user(writer)
    _run(writer, user, archive)
    assert _all_contacts(writer, other) == []
    assert list(writer.scalars(scoped(other, Interaction))) == []


# --- interactions -----------------------------------------------------------


def test_messages_become_directed_interactions_under_the_archive_source(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    ada = _contacts(writer, user)["ada-fictional"]
    rows = [row for row in _interactions(writer, user) if row.contact_id == ada.id]
    assert [row.kind for row in rows] == [
        InteractionKind.LI_OUT,
        InteractionKind.LI_IN,
        InteractionKind.LI_OUT,
    ]
    assert all(row.source is ContactSource.ARCHIVE for row in rows)
    assert rows[0].at == datetime(2023, 5, 1, 14, 22, 10, tzinfo=UTC)
    assert rows[1].summary is not None and "Reply with no profile URL" in rows[1].summary


def test_invitations_become_interactions_that_say_they_are_invitations(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    contacts = _contacts(writer, user)
    invitations = [
        row
        for row in _interactions(writer, user)
        if row.summary is not None and row.summary.startswith(INVITATION_SUMMARY)
    ]
    assert len(invitations) == 2
    outgoing = next(row for row in invitations if row.kind is InteractionKind.LI_OUT)
    assert outgoing.contact_id == contacts["cyd-invented"].id
    assert outgoing.at == datetime(2021, 5, 12, 15, 14, tzinfo=UTC)
    incoming = next(row for row in invitations if row.kind is InteractionKind.LI_IN)
    assert incoming.contact_id == contacts["bo-placeholder"].id
    assert incoming.summary == INVITATION_SUMMARY  # that one carried no note


def test_messages_never_create_a_contact(writer: Session, user: User, archive: Archive) -> None:
    _run(writer, user, archive)
    assert "hal-stranger" not in _contacts(writer, user)
    assert "yon-outsider" not in _contacts(writer, user)


def test_a_file_that_says_it_twice_writes_it_twice(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    eli = _contacts(writer, user)["eli-imaginary"]
    rows = [row for row in _interactions(writer, user) if row.contact_id == eli.id]
    assert len(rows) == 2
    assert rows[0].at == rows[1].at


def test_last_contacted_comes_from_the_outbound_rows(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    contacts = _contacts(writer, user)
    assert contacts["ada-fictional"].last_contacted_at == datetime(2023, 5, 3, 9, 30, tzinfo=UTC)
    # Eli only ever wrote in; nothing outbound, so nothing to record.
    assert contacts["eli-imaginary"].last_contacted_at is None


def test_a_long_body_is_trimmed(writer: Session, user: User, tmp_path: Path) -> None:
    long_body = "x" * (SUMMARY_MAX_CHARS + 500)
    export = tmp_path / "export"
    export.mkdir()
    (export / "Connections.csv").write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Fictional,https://www.linkedin.com/in/ada-fictional,,Works,Eng,12 Mar 2019\n",
        encoding="utf-8",
    )
    (export / "messages.csv").write_text(
        "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,"
        "RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER\n"
        f"c1,,Nettie Keeperton,https://www.linkedin.com/in/nettie-keeperton,Ada Fictional,"
        f'https://www.linkedin.com/in/ada-fictional,2023-05-01 14:22:10 UTC,,"{long_body}",INBOX\n'
        "c2,,Nettie Keeperton,https://www.linkedin.com/in/nettie-keeperton,Ada Fictional,"
        "https://www.linkedin.com/in/ada-fictional,2023-05-02 14:22:10 UTC,,short,INBOX\n",
        encoding="utf-8",
    )
    with open_archive(export) as opened:
        # Two conversations, both with the same person: the traffic cannot say
        # which of the two is the owner, so the caller does.
        import_archive(
            writer, user, opened, observed_at=OBSERVED, owner_public_id="nettie-keeperton"
        )
    summaries = [row.summary for row in _interactions(writer, user)]
    assert summaries[0] is not None
    assert len(summaries[0]) == SUMMARY_MAX_CHARS
    assert summaries[0].endswith("…")


# --- re-import --------------------------------------------------------------


def test_a_second_run_adds_nothing(writer: Session, user: User, archive: Archive) -> None:
    first = _run(writer, user, archive)
    before = [(row.contact_id, row.kind, row.at) for row in _interactions(writer, user)]

    second = _run(writer, user, archive)

    assert second.connections.created == 0
    # Eight rows carry someone; seven of them name a profile URL (Ada twice) and
    # match on it again. The eighth names only a person and a company, which
    # spec 8.2 makes a candidate and never a match, so it waits for review
    # instead of being written a second time.
    assert second.connections.updated == 7
    assert second.connections.needs_review == 1
    assert second.messages.added == 0
    assert second.messages.already_present == first.messages.added
    assert second.invitations.added == 0
    assert second.invitations.already_present == first.invitations.added
    assert len(_contacts(writer, user)) == 7
    assert [(row.contact_id, row.kind, row.at) for row in _interactions(writer, user)] == before


def test_a_second_run_does_not_swallow_a_hand_entered_interaction(
    writer: Session, user: User, archive: Archive
) -> None:
    """An identical manual row must neither block the import nor be consumed by it."""
    _run(writer, user, archive)
    ada = _contacts(writer, user)["ada-fictional"]
    by_hand = add_interaction(
        writer,
        user,
        ada.id,
        InteractionKind.LI_OUT,
        datetime(2023, 5, 1, 14, 22, 10, tzinfo=UTC),
        "Typed in by a person at the same instant.",
    )
    report = _run(writer, user, archive)
    assert report.messages.added == 0
    assert writer.get(Interaction, by_hand.id) is not None
    assert len([row for row in _interactions(writer, user) if row.contact_id == ada.id]) == 4


def test_a_row_with_no_profile_url_is_held_for_review_rather_than_duplicated(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    _run(writer, user, archive)
    assert [contact.first_name for contact in _all_contacts(writer, user)].count("Fen") == 1


def test_a_third_run_still_adds_nothing(writer: Session, user: User, archive: Archive) -> None:
    _run(writer, user, archive)
    _run(writer, user, archive)
    count = len(_interactions(writer, user))
    _run(writer, user, archive)
    assert len(_interactions(writer, user)) == count


# --- provenance -------------------------------------------------------------


def test_archive_ranks_below_manual_and_sync() -> None:
    """The rank this importer relies on, asserted rather than assumed."""
    assert SOURCE_RANK["manual"] > SOURCE_RANK["sync"] > SOURCE_RANK["archive"]
    assert SOURCE_RANK["archive"] > SOURCE_RANK["csv"]


def test_a_manual_edit_survives_a_reimport(writer: Session, user: User, archive: Archive) -> None:
    _run(writer, user, archive)
    ada = _contacts(writer, user)["ada-fictional"]
    set_manual_field(ada, "current_company", "Somewhere Else")
    set_manual_field(ada, "preferred_name", "Addie")
    writer.flush()

    _run(writer, user, archive)

    ada = _contacts(writer, user)["ada-fictional"]
    assert ada.current_company == "Somewhere Else"
    assert ada.preferred_name == "Addie"
    assert ada.field_sources["current_company"] == ContactSource.MANUAL.value
    # The archive still said what it saw, so the edit can be undone.
    assert ada.synced_values["current_company"]["value"] == "Fictional Works"
    assert ada.synced_values["current_company"]["source"] == ContactSource.ARCHIVE.value


def test_the_edit_reverts_to_what_the_archive_reported(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    ada = _contacts(writer, user)["ada-fictional"]
    set_manual_field(ada, "current_company", "Somewhere Else")
    writer.flush()
    _run(writer, user, archive)

    revert_to_synced(ada, "current_company")
    writer.flush()

    assert ada.current_company == "Fictional Works"
    assert ada.field_sources["current_company"] == ContactSource.ARCHIVE.value


def test_an_archive_row_does_not_overwrite_a_sync(
    writer: Session, user: User, archive: Archive
) -> None:
    contact = factories.make_contact(
        writer,
        user,
        li_public_id="ada-fictional",
        first_name="Ada",
        last_name="Fictional",
        current_company="What The Sync Saw",
        source=ContactSource.SYNC,
        field_sources={"current_company": ContactSource.SYNC.value},
    )
    report = _run(writer, user, archive)
    assert report.connections.updated == 2  # the sync's contact, then the duplicate row
    writer.refresh(contact)
    assert contact.current_company == "What The Sync Saw"
    assert contact.synced_values["current_company"]["value"] == "Fictional Works"
