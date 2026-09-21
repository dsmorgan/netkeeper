"""netkeeper.linkedin.archive and .conversations: reading an export, with no database.

The sample archive under ``tests/fixtures/archive`` is hand-written and entirely
invented, as every fixture in this repository is (CLAUDE.md). It is shaped like
a real export on purpose: the preamble before the connections header, one row
with an email address and the rest without, a two-digit year, a date in no
recognizable form, a duplicate connection, a conversation whose profile URL
appears on one row only, a conversation where the other party never appears with
a URL at all, a group thread, and both directions of invitation.
"""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from netkeeper.linkedin.archive import (
    Archive,
    ArchiveFormatError,
    ArchiveKind,
    ArchiveMember,
    InvitationDirection,
    MessageRow,
    open_archive,
    parse_connected_on,
    parse_invitation_date,
    parse_message_date,
    public_id_from_url,
)
from netkeeper.linkedin.conversations import Owner, detect_owner, group

FIXTURES = Path(__file__).parent / "fixtures" / "archive"
OWNER = "nettie-keeperton"


def _member(archive: Archive, kind: ArchiveKind) -> ArchiveMember:
    return next(member for member in archive.members if member.kind is kind)


def _zipped(tmp_path: Path) -> Path:
    """The fixture directory packed into a zip, members in subdirectories and all."""
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for source in sorted(FIXTURES.iterdir()):
            zf.write(source, arcname=source.name)
        zf.writestr("Jobs/Saved Jobs.csv", "Company Name,Job Title\nMade Up Co,Some Role\n")
    return path


def _message(
    conversation_id: str,
    sender: str,
    sender_public_id: str | None,
    recipients: tuple[str, ...] = (),
    recipient_public_ids: tuple[str, ...] = (),
    *,
    row_number: int = 1,
) -> MessageRow:
    return MessageRow(
        row_number=row_number,
        conversation_id=conversation_id,
        conversation_title=None,
        sender=sender,
        sender_url=None,
        sender_public_id=sender_public_id,
        recipients=recipients,
        recipient_urls=(),
        recipient_public_ids=recipient_public_ids,
        sent_at=datetime(2024, 1, 1, tzinfo=UTC),
        subject=None,
        content="body",
        folder="INBOX",
    )


# --- dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12 Mar 2019", date(2019, 3, 12)),
        ("09 Jul 17", date(2017, 7, 9)),
        ("", None),
        (None, None),
        ("2019-03-12", None),
        ("32 Frob 2020", None),
    ],
)
def test_connected_on_reads_both_year_forms(text: str | None, expected: date | None) -> None:
    assert parse_connected_on(text) == expected


@pytest.mark.parametrize(
    "text",
    ["2023-05-01 14:22:10 UTC", "2023-05-01 14:22:10", "  2023-05-01 14:22:10 UTC  "],
)
def test_message_dates_are_utc_with_or_without_the_suffix(text: str) -> None:
    assert parse_message_date(text) == datetime(2023, 5, 1, 14, 22, 10, tzinfo=UTC)


def test_invitation_dates_are_month_first_and_read_as_utc() -> None:
    assert parse_invitation_date("5/12/21, 3:14 PM") == datetime(2021, 5, 12, 15, 14, tzinfo=UTC)
    assert parse_invitation_date("not a date") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/in/Ada-Fictional", "ada-fictional"),
        ("https://www.linkedin.com/in/ada%2Dfictional/", "ada-fictional"),
        ("https://www.linkedin.com/company/fictional-works", None),
        ("", None),
        (None, None),
    ],
)
def test_public_id_comes_from_the_profile_url(url: str | None, expected: str | None) -> None:
    assert public_id_from_url(url) == expected


# --- opening ----------------------------------------------------------------


def test_a_directory_yields_the_three_tables() -> None:
    with open_archive(FIXTURES) as archive:
        assert [(member.name, member.kind) for member in archive.members] == [
            ("Connections.csv", ArchiveKind.CONNECTIONS),
            ("messages.csv", ArchiveKind.MESSAGES),
            ("Invitations.csv", ArchiveKind.INVITATIONS),
        ]


def test_the_assistant_chat_logs_are_not_conversations() -> None:
    """guide_messages.csv carries the messages header but is not messages with people."""
    with open_archive(FIXTURES) as archive:
        assert all("guide" not in member.name for member in archive.members)


def test_a_zip_reads_the_same_as_the_directory(tmp_path: Path) -> None:
    with open_archive(_zipped(tmp_path)) as archive:
        assert [member.kind for member in archive.members] == [
            ArchiveKind.CONNECTIONS,
            ArchiveKind.MESSAGES,
            ArchiveKind.INVITATIONS,
        ]
        assert archive.exported_at is not None
        rows = list(archive.connections(_member(archive, ArchiveKind.CONNECTIONS)))
    assert len(rows) == 9


def test_a_zip_arrives_as_a_stream_too(tmp_path: Path) -> None:
    data = _zipped(tmp_path).read_bytes()
    with open_archive(io.BytesIO(data), filename="upload.zip") as archive:
        assert archive.name == "upload.zip"
        assert len(archive.members) == 3


def test_one_csv_opens_on_its_own() -> None:
    with open_archive(FIXTURES / "Connections.csv") as archive:
        (member,) = archive.members
        assert member.kind is ArchiveKind.CONNECTIONS
        assert len(list(archive.connections(member))) == 9


def test_naming_an_assistant_log_reads_it_anyway() -> None:
    """The name filter is for scans; asking for one file is an explicit request."""
    with open_archive(FIXTURES / "guide_messages.csv") as archive:
        (member,) = archive.members
        assert member.kind is ArchiveKind.MESSAGES


def test_a_table_can_be_read_twice() -> None:
    with open_archive(FIXTURES) as archive:
        member = _member(archive, ArchiveKind.CONNECTIONS)
        first = list(archive.connections(member))
        second = list(archive.connections(member))
    assert first == second


def test_a_file_that_is_no_table_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "Notes.csv"
    path.write_text("Date,Note\n2024-01-01,Something\n", encoding="utf-8")
    with pytest.raises(ArchiveFormatError, match=r"Notes\.csv"), open_archive(path):
        pass


def test_a_directory_with_no_table_is_refused(tmp_path: Path) -> None:
    (tmp_path / "Skills.csv").write_text("Name\nMaking things up\n", encoding="utf-8")
    with (
        pytest.raises(ArchiveFormatError, match=r"no Connections\.csv"),
        open_archive(tmp_path),
    ):
        pass


def test_a_table_missing_a_needed_column_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "Connections.csv"
    path.write_text("First Name,Last Name,Connected On\nAda,Fictional,12 Mar 2019\n", "utf-8")
    with pytest.raises(ArchiveFormatError, match="missing column"), open_archive(path):
        pass


# --- connections ------------------------------------------------------------


def test_the_preamble_is_skipped_and_rows_keep_their_line(tmp_path: Path) -> None:
    with open_archive(FIXTURES) as archive:
        rows = list(archive.connections(_member(archive, ArchiveKind.CONNECTIONS)))
    assert [row.row_number for row in rows] == [1, 2, 3, 4, 5, 6, 8, 9, 10]
    assert rows[0].first_name == "Ada"


def test_an_export_without_a_preamble_reads_too(tmp_path: Path) -> None:
    path = tmp_path / "Connections.csv"
    path.write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Fictional,https://www.linkedin.com/in/ada-fictional,,Works,Eng,12 Mar 2019\n",
        encoding="utf-8",
    )
    with open_archive(path) as archive:
        (row,) = archive.connections(archive.members[0])
    assert row.public_id == "ada-fictional"


def test_email_is_set_only_where_the_export_carried_one() -> None:
    with open_archive(FIXTURES) as archive:
        rows = list(archive.connections(_member(archive, ArchiveKind.CONNECTIONS)))
    assert rows[0].email == "ada@example.test"  # lowercased from "Ada@Example.test"
    assert [row.email for row in rows[1:4]] == [None, None, None]
    assert sum(1 for row in rows if row.email is not None) == 2  # the duplicate carries it too


def test_missing_cells_are_none_not_empty_strings() -> None:
    with open_archive(FIXTURES) as archive:
        rows = list(archive.connections(_member(archive, ArchiveKind.CONNECTIONS)))
    eli = next(row for row in rows if row.public_id == "eli-imaginary")
    assert (eli.company, eli.position) == (None, None)
    fen = next(row for row in rows if row.first_name == "Fen")
    assert (fen.url, fen.public_id, fen.connected_on) == (None, None, None)


def test_an_unreadable_date_is_none_and_says_where(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="netkeeper.linkedin.archive"),
        open_archive(FIXTURES) as archive,
    ):
        rows = list(archive.connections(_member(archive, ArchiveKind.CONNECTIONS)))
    assert next(row for row in rows if row.first_name == "Fen").connected_on is None
    assert "Connections.csv row 8" in caplog.text
    assert "Frob" not in caplog.text  # the cell itself never reaches the log


def test_an_empty_date_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="netkeeper.linkedin.archive"),
        open_archive(FIXTURES) as archive,
    ):
        rows = list(archive.connections(_member(archive, ArchiveKind.CONNECTIONS)))
    assert next(row for row in rows if row.public_id == "cyd-invented").connected_on is None
    assert "row 3" not in caplog.text


# --- invitations ------------------------------------------------------------


def test_invitations_carry_direction_and_both_parties() -> None:
    with open_archive(FIXTURES) as archive:
        rows = list(archive.invitations(_member(archive, ArchiveKind.INVITATIONS)))
    assert [row.direction for row in rows] == [
        InvitationDirection.OUTGOING,
        InvitationDirection.INCOMING,
        InvitationDirection.INCOMING,
        InvitationDirection.OUTGOING,
        None,  # "PENDING" is neither
        InvitationDirection.INCOMING,
    ]
    assert rows[0].invitee_public_id == "cyd-invented"
    assert rows[1].inviter_public_id == "bo-placeholder"
    assert rows[3].invitee_public_id is None
    assert rows[5].sent_at is None


# --- conversations ----------------------------------------------------------


def test_the_owner_is_the_profile_in_every_conversation() -> None:
    with open_archive(FIXTURES) as archive:
        threads = group(archive.messages(_member(archive, ArchiveKind.MESSAGES)))
    assert threads.owner is not None
    assert threads.owner.public_id == OWNER
    assert threads.owner.by == "traffic"
    assert threads.owner.names == frozenset({"nettie keeperton"})


def test_a_conversation_is_attributed_from_the_one_row_that_names_the_other_party() -> None:
    with open_archive(FIXTURES) as archive:
        threads = group(archive.messages(_member(archive, ArchiveKind.MESSAGES)))
    conversation = next(c for c in threads.conversations if c.conversation_id == "conv-aa")
    assert conversation.counterpart_public_id == "ada-fictional"
    assert len(conversation.messages) == 3
    assert [message.by for message in conversation.messages] == ["url", "name", "name"]
    assert [message.outbound for message in conversation.messages] == [True, False, True]


def test_a_conversation_the_other_party_never_signed_is_reported() -> None:
    with open_archive(FIXTURES) as archive:
        threads = group(archive.messages(_member(archive, ArchiveKind.MESSAGES)))
    assert threads.skipped_for("no_counterpart") == 1
    assert all(c.conversation_id != "conv-bb" for c in threads.conversations)


def test_a_group_thread_is_skipped_not_split() -> None:
    with open_archive(FIXTURES) as archive:
        threads = group(archive.messages(_member(archive, ArchiveKind.MESSAGES)))
    assert threads.skipped_for("group") == 1
    assert all(c.conversation_id != "conv-cc" for c in threads.conversations)


def test_every_conversation_is_accounted_for() -> None:
    with open_archive(FIXTURES) as archive:
        threads = group(archive.messages(_member(archive, ArchiveKind.MESSAGES)))
    assert threads.total_conversations == 6
    assert len(threads.conversations) == 4
    assert threads.rows == 11
    assert threads.undated_rows == 1


def test_a_row_addressed_to_several_makes_a_group_thread_even_without_urls() -> None:
    rows = [
        _message("one", "Nettie Keeperton", OWNER, ("Ada Fictional", "Bo Placeholder")),
        _message("two", "Nettie Keeperton", OWNER, ("Ada Fictional",), ("ada-fictional",)),
        _message("three", "Nettie Keeperton", OWNER, ("Cyd Invented",), ("cyd-invented",)),
    ]
    threads = group(rows)
    assert threads.skipped_for("group") == 1
    assert {c.conversation_id for c in threads.conversations} == {"two", "three"}


def test_display_names_never_match_across_conversations() -> None:
    """Two people called the same thing stay two people."""
    rows = [
        _message("one", "Nettie Keeperton", OWNER, ("Sam Doubled",), ("sam-doubled-1",)),
        _message("one", "Sam Doubled", None),
        _message("two", "Nettie Keeperton", OWNER, ("Sam Doubled",), ("sam-doubled-2",)),
        _message("two", "Sam Doubled", None),
    ]
    threads = group(rows)
    assert {c.counterpart_public_id for c in threads.conversations} == {
        "sam-doubled-1",
        "sam-doubled-2",
    }
    assert all(not message.outbound for c in threads.conversations for message in c.messages[1:])


def test_one_conversation_alone_does_not_name_an_owner() -> None:
    rows = [
        _message("one", "Nettie Keeperton", OWNER, ("Ada Fictional",), ("ada-fictional",)),
        _message("one", "Ada Fictional", "ada-fictional", ("Nettie Keeperton",), (OWNER,)),
    ]
    threads = group(rows)
    assert threads.owner is None
    assert threads.conversations == ()
    assert threads.skipped_for("no_owner") == 1


def test_the_owner_can_be_given_when_the_traffic_is_silent() -> None:
    rows = [
        _message("one", "Nettie Keeperton", OWNER, ("Ada Fictional",), ("ada-fictional",)),
        _message("one", "Ada Fictional", "ada-fictional", ("Nettie Keeperton",), (OWNER,)),
    ]
    threads = group(rows, owner=OWNER)
    assert threads.owner == Owner(OWNER, frozenset({"nettie keeperton"}), by="given")
    (conversation,) = threads.conversations
    assert conversation.counterpart_public_id == "ada-fictional"


def test_the_profile_name_breaks_a_tie() -> None:
    rows = [
        _message("one", "Nettie Keeperton", OWNER, ("Ada Fictional",), ("ada-fictional",)),
        _message("two", "Ada Fictional", "ada-fictional", ("Nettie Keeperton",), (OWNER,)),
    ]
    assert detect_owner(rows) is None
    owner = detect_owner(rows, profile_name="Nettie Keeperton")
    assert owner is not None
    assert (owner.public_id, owner.by) == (OWNER, "profile-name")


def test_no_messages_at_all_name_no_owner() -> None:
    threads = group([])
    assert threads.owner is None
    assert threads.total_conversations == 0
