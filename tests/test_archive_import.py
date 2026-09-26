"""netkeeper.crm.archive: the sample archive becoming contacts and interactions (P1-03).

The counts asserted here are the sample's, counted by hand from the fixture
files, which is what "imports with correct counts" means for this item. The
sample is invented from end to end; see ``tests/test_linkedin_archive.py``.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import import_runs
from netkeeper.crm.archive import (
    SUMMARY_MAX_CHARS,
    ArchiveImport,
    _html_to_text,
    _note_unfamiliar_messages,
    import_archive,
    report_json,
)
from netkeeper.crm.interactions import INVITATION_SUMMARY, add_interaction
from netkeeper.crm.provenance import SOURCE_RANK, revert_to_synced, set_manual_field
from netkeeper.crm.tags import contact_tags, create_rule, create_tag, list_rules
from netkeeper.db import session_scope
from netkeeper.linkedin.archive import Archive, open_archive
from netkeeper.models import (
    Contact,
    ContactPosition,
    ContactSource,
    ContactTag,
    ImportResolution,
    ImportSourceKind,
    ImportStatus,
    Interaction,
    InteractionKind,
    RuleField,
    User,
    UserPosition,
)
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
    assert messages.rows == 13
    assert messages.conversations == 7
    assert messages.attributed == 4
    assert messages.no_counterpart == 1  # nobody signed conv-bb
    assert messages.group_threads == 1
    assert messages.unknown_contact == 1  # the stranger is not a connection
    assert messages.no_owner == 0
    assert messages.attributed + 3 == messages.conversations
    assert messages.added == 8
    assert messages.outbound == 3
    assert messages.inbound == 5
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

    positions = report.positions
    assert positions.rows == 4
    assert positions.created == 3  # the fourth row names neither a company nor a title
    assert positions.updated == 0
    assert positions.skipped == 1
    assert positions.undated == 1  # "Placeholder Ltd" carries neither date

    assert len(_contacts(writer, user)) == 7
    assert len(_interactions(writer, user)) == 10
    # Decision: the archive gives Company and Position with no dates, so it
    # writes the scalars under provenance and no position history at all.
    assert list(writer.scalars(scoped(user, ContactPosition))) == []


def test_the_rules_run_over_what_the_import_touched(
    writer: Session, user: User, archive: Archive
) -> None:
    """#64: an imported address book is tagged when the import returns, not when a button is.

    And only what the import touched: a contact that was already there and is not
    in the file is left exactly as it was, so a re-import of one file cannot
    quietly reconcile the whole address book.
    """
    engineers = create_tag(writer, user, "engineering")
    create_rule(writer, user, engineers.id, RuleField.TITLE, r"\bengineer\b")
    untouched = factories.make_contact(
        writer, user, li_public_id="not-in-the-file", current_title="Staff Engineer"
    )

    report = _run(writer, user, archive)

    # The duplicate row of Ada resolves to the contact the first one created, so
    # a person repeated in the file is examined once, not once per row.
    assert report.tagging.contacts == report.connections.created
    tagged = {
        row.contact_id
        for row in writer.scalars(scoped(user, ContactTag).where(ContactTag.tag_id == engineers.id))
    }
    ada = _contacts(writer, user)["ada-fictional"]
    assert tagged == {ada.id}, "the contact the file named, and nobody else"
    assert untouched.id not in tagged


def test_an_import_seeds_the_default_rules_before_it_runs_them(
    writer: Session, user: User, archive: Archive
) -> None:
    """`netkeeper import archive` on a database the server has never started on.

    Nothing else seeds the defaults on that path, and rules nobody has seeded
    tag nobody, which is the cold-start this item exists to prevent (#64).
    """
    assert list_rules(writer, user) == [], "no rules before the import"
    report = _run(writer, user, archive)
    assert list_rules(writer, user), "the import seeded the default rule set"
    assert report.tagging.added > 0
    ada = _contacts(writer, user)["ada-fictional"]
    assert "engineering" in {row.tag.name for row in contact_tags(writer, user, ada.id)}


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


def test_html_message_bodies_become_plain_text(writer: Session, user: User, tmp_path: Path) -> None:
    """LinkedIn InMail arrives as HTML; the stored summary is plain text (#75)."""
    export = tmp_path / "export"
    export.mkdir()
    (export / "Connections.csv").write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Fictional,https://www.linkedin.com/in/ada-fictional,,Works,Eng,12 Mar 2019\n",
        encoding="utf-8",
    )
    body = (
        "<p class='spinmail-quill-editor'>Hello &amp; welcome</p>"
        "<p class='spinmail-quill-editor'>Second line, cut off mid tag <a href='foo"
    )
    (export / "messages.csv").write_text(
        "CONVERSATION ID,CONVERSATION TITLE,FROM,SENDER PROFILE URL,TO,"
        "RECIPIENT PROFILE URLS,DATE,SUBJECT,CONTENT,FOLDER\n"
        f"c1,,Nettie Keeperton,https://www.linkedin.com/in/nettie-keeperton,Ada Fictional,"
        f'https://www.linkedin.com/in/ada-fictional,2023-05-01 14:22:10 UTC,,"{body}",INBOX\n',
        encoding="utf-8",
    )
    with open_archive(export) as opened:
        import_archive(
            writer, user, opened, observed_at=OBSERVED, owner_public_id="nettie-keeperton"
        )
    summary = _interactions(writer, user)[0].summary
    assert summary == "Hello & welcome\nSecond line, cut off mid tag"
    assert "<" not in summary and ">" not in summary


def _imported_summary(writer: Session, user: User, tmp_path: Path, body: str) -> str | None:
    """Import a one-message archive carrying ``body`` and return the resulting summary."""
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
        f'https://www.linkedin.com/in/ada-fictional,2023-05-01 14:22:10 UTC,,"{body}",INBOX\n',
        encoding="utf-8",
    )
    with open_archive(export) as opened:
        import_archive(
            writer, user, opened, observed_at=OBSERVED, owner_public_id="nettie-keeperton"
        )
    return _interactions(writer, user)[0].summary


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Forwarding from Ada <ada@example.test> for you.",
            "Forwarding from Ada <ada@example.test> for you.",
        ),
        ("the placeholder is <name> here", "the placeholder is <name> here"),
        ("compare a <b and c > d", "compare a <b and c > d"),
    ],
)
def test_plain_text_with_angle_brackets_is_not_mistaken_for_markup(
    writer: Session, user: User, tmp_path: Path, body: str, expected: str
) -> None:
    """B3 (pre-merge review of #127): a message that was never HTML in the first place --
    an email address in angle brackets, a "<name>" placeholder, coincidental text that
    happens to start with a short tag-shaped word -- must not be silently deleted."""
    assert _imported_summary(writer, user, tmp_path, body) == expected


def test_entity_encoded_markup_is_still_stripped_not_stored_literally(
    writer: Session, user: User, tmp_path: Path
) -> None:
    """B3: unescaping happens as the parser reads plain data and is never re-examined as a
    tag within the same pass, so a body that spells a real tag with entities
    (``&lt;p&gt;``) used to decode into a literal ``<p>...</p>`` sitting in the stored
    summary -- markup surviving the exact importer whose "done when" is that none does."""
    summary = _imported_summary(
        writer, user, tmp_path, "She wrote &lt;p&gt;hello&lt;/p&gt; in the box"
    )
    assert summary == "She wrote\nhello\nin the box"
    assert summary is not None
    assert "<" not in summary and ">" not in summary


def test_a_script_element_is_folded_away_and_keeps_its_text(
    writer: Session, user: User, tmp_path: Path
) -> None:
    """Verification of #127 found these two stored as literal markup.

    ``script`` and ``style`` are the two tags nobody types as prose and the two
    whose source text would most alarm whoever read the column, so they are
    folded like any other known tag. Their text is kept rather than deleted
    with them: an unclosed ``<script`` puts the parser into CDATA mode, so
    deleting content would take the rest of the message with it.
    """
    assert _imported_summary(writer, user, tmp_path, "<script>alert(1)</script>Real body") == (
        "alert(1)Real body"
    )
    # The rest against the converter itself: one import per directory.
    assert _html_to_text("&lt;script&gt;alert(1)&lt;/script&gt;") == "alert(1)"
    assert _html_to_text("<script>alert(1)") == "alert(1)"
    assert _html_to_text("<style>p { color: red }</style>After") == "p { color: red }After"


def test_an_image_only_message_has_no_summary(writer: Session, user: User, tmp_path: Path) -> None:
    body = "<img src='https://example.test/x.png'>"
    assert _imported_summary(writer, user, tmp_path, body) is None


def test_html_to_text_is_idempotent(writer: Session, user: User) -> None:
    """B3: applying the function to its own output must be a no-op, on the realistic real
    markup shape and on the entity-encoded and plain-angle-bracket cases above -- otherwise
    the migration backfill (0010), which cannot know whether a row already went through
    this, could keep changing a summary every time it is re-applied."""
    samples = [
        "<p class='spinmail-quill-editor'>Hello &amp; welcome</p>",
        "She wrote &lt;p&gt;hello&lt;/p&gt; in the box",
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "Forwarding from Ada <ada@example.test> for you.",
        "the placeholder is <name> here",
        "compare a <b and c > d",
    ]
    for sample in samples:
        once = _html_to_text(sample)
        twice = _html_to_text(once)
        assert once == twice, sample


def test_a_long_plain_text_body_is_cut_on_a_word_boundary(
    writer: Session, user: User, tmp_path: Path
) -> None:
    words = "lorem ipsum dolor sit amet consectetur " * 100
    assert len(words) > SUMMARY_MAX_CHARS
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
        f'https://www.linkedin.com/in/ada-fictional,2023-05-01 14:22:10 UTC,,"{words}",INBOX\n',
        encoding="utf-8",
    )
    with open_archive(export) as opened:
        import_archive(
            writer, user, opened, observed_at=OBSERVED, owner_public_id="nettie-keeperton"
        )
    summary = _interactions(writer, user)[0].summary
    assert summary is not None
    assert summary.endswith("…")
    plain = summary.removesuffix("…")
    assert not plain.endswith(" ")  # rstripped after the cut
    assert len(plain) < SUMMARY_MAX_CHARS - 1  # backed off the hard cap to land on a word
    assert words.strip()[: len(plain)] == plain  # a prefix of the original, not mangled
    assert words.strip()[len(plain)] == " "  # and the cut itself landed on whitespace


# --- positions ----------------------------------------------------------


def test_positions_become_the_users_own_job_history(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    rows = {row.company: row for row in writer.scalars(scoped(user, UserPosition)) if row.company}
    assert set(rows) == {"Fictional Works, Inc.", "Notional Group", "Placeholder Ltd"}
    assert rows["Fictional Works, Inc."].title == "Founding Engineer"
    assert rows["Fictional Works, Inc."].started_on == date(2018, 1, 1)
    assert rows["Fictional Works, Inc."].ended_on == date(2019, 12, 1)
    assert rows["Fictional Works, Inc."].is_current is False
    # No Finished On: current.
    assert rows["Notional Group"].started_on == date(2020, 6, 1)
    assert rows["Notional Group"].ended_on is None
    assert rows["Notional Group"].is_current is True
    # Neither date at all: not current, and nothing crashes over the missing pair.
    assert rows["Placeholder Ltd"].started_on is None
    assert rows["Placeholder Ltd"].ended_on is None
    assert rows["Placeholder Ltd"].is_current is False
    assert all(row.source is ContactSource.ARCHIVE for row in rows.values())


def test_a_position_re_import_does_not_duplicate(
    writer: Session, user: User, archive: Archive
) -> None:
    _run(writer, user, archive)
    before = len(list(writer.scalars(scoped(user, UserPosition))))

    second = _run(writer, user, archive)

    assert second.positions.created == 0
    assert second.positions.updated == 3
    assert len(list(writer.scalars(scoped(user, UserPosition)))) == before


def test_positions_are_scoped_to_the_user(writer: Session, user: User, archive: Archive) -> None:
    other = factories.make_user(writer)
    _run(writer, user, archive)
    assert list(writer.scalars(scoped(other, UserPosition))) == []


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
    second = _run(writer, user, archive)
    assert [contact.first_name for contact in _all_contacts(writer, user)].count("Fen") == 1
    # It reappears for review on every run, which is the cost of spec 8.2 never
    # letting a name and a company be a match. Pinned so the behavior is a
    # decision rather than a surprise.
    assert second.connections.needs_review == 1
    # The row keeps who it might be, so a review can offer them (#217, M28).
    assert second.run_id is not None
    (held,) = [
        row
        for row in import_runs.get_run(writer, user, second.run_id).rows
        if row.resolution is ImportResolution.CANDIDATE
    ]
    (fen,) = [contact for contact in _all_contacts(writer, user) if contact.first_name == "Fen"]
    assert held.candidate_ids_json == [fen.id]
    assert held.contact_id is None


def test_a_no_op_reimport_does_not_restamp_every_contact(
    writer: Session, user: User, archive: Archive
) -> None:
    """Runs two and three are byte-identical, so nothing should look edited.

    ``synced_values`` is a ``MutableDict``: writing an equal entry into it still
    marks the row dirty and fires ``onupdate``, which on a real archive would
    move ``updated_at`` on every contact for an import that changed nothing.
    """
    _run(writer, user, archive)
    writer.flush()
    before = {contact.id: contact.updated_at for contact in _all_contacts(writer, user)}

    _run(writer, user, archive)
    writer.flush()

    assert {contact.id: contact.updated_at for contact in _all_contacts(writer, user)} == before


def test_a_comma_in_a_display_name_does_not_cost_a_conversation(
    writer: Session, user: User, archive: Archive
) -> None:
    """Regression: "Dee Notional, PhD" in ``TO`` used to read as two recipients,
    which made the conversation a group thread and dropped both its messages.
    """
    report = _run(writer, user, archive)
    assert report.messages.group_threads == 1  # the one real group thread, and only it
    dee = _contacts(writer, user)["dee-notional"]
    rows = [row for row in _interactions(writer, user) if row.contact_id == dee.id]
    assert [row.kind for row in rows] == [InteractionKind.LI_OUT, InteractionKind.LI_IN]


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


# --- the import is a run, and it rolls back (#132) ----------------------------


def test_an_archive_import_is_recorded_as_a_committed_run(
    writer: Session, user: User, archive: Archive
) -> None:
    report = _run(writer, user, archive)

    assert report.run_id is not None
    run = import_runs.get_run(writer, user, report.run_id)
    assert run.source_kind is ImportSourceKind.ARCHIVE
    assert run.status is ImportStatus.COMMITTED
    assert run.committed_at is not None
    assert (run.total_rows, run.created_count, run.matched_count) == (9, 7, 1)
    assert (run.skipped_count, run.candidate_count) == (1, 0)
    assert run.report_json is not None
    assert run.report_json["connections"]["rows"] == 9
    assert run.report_json["messages"]["added"] == report.messages.added
    assert run.report_json["invitations"]["added"] == report.invitations.added
    assert run.report_json["owner_public_id"] == "nettie-keeperton"
    written = {row.id for row in _interactions(writer, user)}
    assert run.created_json is not None
    assert set(run.created_json["interactions"]) == written

    rows, total = import_runs.list_rows(writer, user, run.id)
    assert total == 9
    resolutions = [row.resolution for row in rows]
    assert resolutions.count(ImportResolution.CREATED) == 7
    assert resolutions.count(ImportResolution.MATCHED) == 1
    assert resolutions.count(ImportResolution.SKIPPED) == 1
    assert all(row.raw_json.keys() >= {"First Name", "Last Name", "URL"} for row in rows)
    listed, _ = import_runs.list_runs(writer, user)
    assert [listed_run.id for listed_run in listed] == [run.id]


def test_rolling_back_an_archive_run_removes_what_it_created(
    writer: Session, user: User, archive: Archive
) -> None:
    report = _run(writer, user, archive)
    assert report.run_id is not None

    result = import_runs.rollback(writer, user, report.run_id)

    assert result.contacts_deleted == 7
    assert _all_contacts(writer, user) == []
    assert _interactions(writer, user) == []
    run = import_runs.get_run(writer, user, report.run_id)
    assert run.status is ImportStatus.ROLLED_BACK


def test_rolling_back_an_archive_run_restores_a_contact_it_enriched(
    writer: Session, user: User, archive: Archive
) -> None:
    """A contact that was there first keeps its old values and loses the run's interactions."""
    ada = factories.make_contact(
        writer,
        user,
        li_urn=None,
        li_public_id="ada-fictional",
        first_name="Ada",
        last_name="Fictional",
        current_company=None,
        current_title=None,
    )
    hand_written = add_interaction(writer, user, ada.id, InteractionKind.NOTE, OBSERVED, "met")
    writer.flush()
    report = _run(writer, user, archive)
    assert report.run_id is not None
    assert ada.current_company == "Fictional Works"
    assert len([row for row in _interactions(writer, user) if row.contact_id == ada.id]) > 1

    import_runs.rollback(writer, user, report.run_id)

    # Read through a fresh query: mypy keeps ``ada``'s narrowing from before the
    # rollback and would call an ``is None`` on it unreachable.
    restored = writer.scalars(scoped(user, Contact).where(Contact.id == ada.id)).one()
    assert restored.current_company is None
    assert restored.current_title is None
    assert [row.id for row in _interactions(writer, user)] == [hand_written.id]
    assert list(_contacts(writer, user)) == ["ada-fictional"]


def test_an_archive_imported_again_after_its_rollback_lands_again(
    writer: Session, user: User, archive: Archive
) -> None:
    first = _run(writer, user, archive)
    assert first.run_id is not None
    import_runs.rollback(writer, user, first.run_id)

    second = _run(writer, user, archive)

    assert second.connections.created == 7
    assert second.messages.added == first.messages.added
    assert second.invitations.added == first.invitations.added


def test_the_first_of_two_imports_of_one_archive_rolls_back_after_the_second(
    writer: Session, user: User, archive: Archive
) -> None:
    """The second import changed nothing, so it neither supersedes nor counts as a loss."""
    first = _run(writer, user, archive)
    second = _run(writer, user, archive)
    assert first.run_id is not None and second.run_id is not None
    assert second.connections.created == 0

    import_runs.rollback(writer, user, second.run_id)
    import_runs.rollback(writer, user, first.run_id)

    assert _all_contacts(writer, user) == []


# --- a messages-shaped member under another name (#74) -------------------------


def _sample_with(tmp_path: Path, extra: dict[str, Path]) -> Path:
    """The sample directory, plus fixture files copied in under other names."""
    root = tmp_path / "export"
    shutil.copytree(FIXTURES, root)
    for name, source in extra.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return root


def test_the_sample_names_no_unfamiliar_message_file(
    writer: Session, user: User, archive: Archive
) -> None:
    report = _run(writer, user, archive)
    assert report.unfamiliar_message_files == []
    assert report_json(report)["unfamiliar_message_files"] == []


def test_an_unknown_assistant_log_is_imported_but_named(
    writer: Session, user: User, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A fourth assistant log reads as messages; the report says so instead of hiding it.

    An assistant has no profile URL, so its conversations never become
    interactions: on its own, as here, the log cannot even say whose archive it
    is (``no_owner``); beside traffic that names the owner they would land in
    ``no_counterpart``. Either way the counts grow and nothing is written, as
    #74 describes. The warning is what makes that visible.
    """
    root = _sample_with(tmp_path, {"interview_prep_messages.csv": FIXTURES / "guide_messages.csv"})
    with (
        caplog.at_level(logging.WARNING, logger="netkeeper.crm.archive"),
        open_archive(root) as opened,
    ):
        report = _run(writer, user, opened)
    assert report.unfamiliar_message_files == ["interview_prep_messages.csv"]
    assert report_json(report)["unfamiliar_message_files"] == ["interview_prep_messages.csv"]
    assert report.messages.conversations == 8  # the sample's seven, and the log's one
    assert report.messages.no_owner == 1  # the log's, read as a table of its own
    assert report.messages.added == 8  # the sample's, and nothing more
    assert "interview_prep_messages.csv" in caplog.text
    assert "An assistant's chat log" not in caplog.text  # never a body in the log


def test_the_familiar_name_in_a_subdirectory_or_another_case_is_not_named(
    writer: Session, user: User, tmp_path: Path
) -> None:
    root = tmp_path / "export"
    shutil.copytree(FIXTURES, root, ignore=shutil.ignore_patterns("messages.csv"))
    (root / "inbox").mkdir()
    shutil.copyfile(FIXTURES / "messages.csv", root / "inbox" / "MESSAGES.CSV")
    with open_archive(root) as opened:
        report = _run(writer, user, opened)
    assert report.messages.rows == 13  # the sample's table, found where it moved
    assert report.unfamiliar_message_files == []


@pytest.mark.parametrize(
    ("name", "named"),
    [
        ("export\\Messages.csv", False),
        ("export\\inbox\\MESSAGES.CSV", False),
        ("export\\interview_prep_messages.csv", True),
        ("messages.csv\\my inbox.csv", True),
    ],
)
def test_a_backslash_is_a_separator_when_naming_a_messages_file(name: str, named: bool) -> None:
    """A zip written on Windows can name its members with backslashes: the base
    name is what is compared, whichever separator the member uses (#222, M14)."""
    report = ArchiveImport(observed_at=OBSERVED)
    _note_unfamiliar_messages(name, report)
    assert report.unfamiliar_message_files == ([name] if named else [])


def test_a_renamed_message_history_is_imported_and_named(
    writer: Session, user: User, tmp_path: Path
) -> None:
    """A renamed table still imports (the reader goes by header), and is still reported."""
    root = tmp_path / "export"
    shutil.copytree(FIXTURES, root, ignore=shutil.ignore_patterns("messages.csv"))
    shutil.copyfile(FIXTURES / "messages.csv", root / "my inbox.csv")
    with open_archive(root) as opened:
        report = _run(writer, user, opened)
    assert report.messages.added == 8
    assert report.unfamiliar_message_files == ["my inbox.csv"]
