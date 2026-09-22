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
import json
import logging
import struct
import subprocess
import sys
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from netkeeper.linkedin import archive as linkedin_archive
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
# What ADR 0005 keeps out of the extractor. "sqlalchemy" is not in the ADR's
# words but follows from them: importing the ORM is how the models arrive.
FORBIDDEN_IMPORTS = ("netkeeper.models", "netkeeper.crm", "netkeeper.db", "sqlalchemy")


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
    recipient_urls: tuple[str, ...] | None = None,
) -> MessageRow:
    """A message row. ``recipient_urls`` defaults to one URL per recipient public id."""
    return MessageRow(
        row_number=row_number,
        conversation_id=conversation_id,
        conversation_title=None,
        sender=sender,
        sender_url=None,
        sender_public_id=sender_public_id,
        recipients=recipients,
        recipient_urls=(
            recipient_urls
            if recipient_urls is not None
            else tuple(f"https://www.linkedin.com/in/{slug}" for slug in recipient_public_ids)
        ),
        recipient_public_ids=recipient_public_ids,
        sent_at=datetime(2024, 1, 1, tzinfo=UTC),
        subject=None,
        content="body",
        folder="INBOX",
    )


# --- the boundary -----------------------------------------------------------


def test_the_extractor_loads_no_models_and_no_session() -> None:
    """ADR 0005: nothing under ``linkedin/`` imports the ORM or opens a session.

    Asserted in a subprocess, because this test module has the whole package
    imported already and would see every one of these in ``sys.modules``
    whatever the extractor did. A plain grep would miss a transitive import,
    which is the way this invariant actually breaks.
    """
    script = (
        "import sys, json\n"
        "import netkeeper.linkedin.archive, netkeeper.linkedin.conversations\n"
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).parent.parent,
    )
    loaded = json.loads(result.stdout)
    leaked = [
        name
        for name in loaded
        for forbidden in FORBIDDEN_IMPORTS
        if name == forbidden or name.startswith(f"{forbidden}.")
    ]
    assert leaked == [], f"netkeeper/linkedin/ pulled in {leaked}"


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


def test_the_fixture_holds_only_the_files_it_is_meant_to() -> None:
    """A real export has thirty-odd more members, several holding the owner's own
    address and phone number. Only these five belong here, and only these five
    are hand-written, so anything else appearing is a real export leaking in.
    """
    assert sorted(path.name for path in FIXTURES.iterdir()) == [
        "Connections.csv",
        "Invitations.csv",
        "Profile.csv",
        "guide_messages.csv",
        "messages.csv",
    ]


def test_a_directory_yields_the_tables_in_reading_order() -> None:
    with open_archive(FIXTURES) as archive:
        assert [(member.name, member.kind) for member in archive.members] == [
            ("Connections.csv", ArchiveKind.CONNECTIONS),
            ("messages.csv", ArchiveKind.MESSAGES),
            ("Invitations.csv", ArchiveKind.INVITATIONS),
            ("Profile.csv", ArchiveKind.PROFILE),
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
            ArchiveKind.PROFILE,
        ]
        assert archive.exported_at is not None
        rows = list(archive.connections(_member(archive, ArchiveKind.CONNECTIONS)))
    assert len(rows) == 9


def test_a_zip_arrives_as_a_stream_too(tmp_path: Path) -> None:
    data = _zipped(tmp_path).read_bytes()
    with open_archive(io.BytesIO(data), filename="upload.zip") as archive:
        assert archive.name == "upload.zip"
        assert len(archive.members) == 4


def test_the_zip_names_the_table_it_does_not_read(tmp_path: Path) -> None:
    """``_zipped`` adds one member with a header this reader does not recognize."""
    with open_archive(_zipped(tmp_path)) as archive:
        assert archive.ignored == ("Jobs/Saved Jobs.csv",)


def test_a_directory_names_the_tables_it_does_not_read(tmp_path: Path) -> None:
    for source in FIXTURES.iterdir():
        (tmp_path / source.name).write_bytes(source.read_bytes())
    (tmp_path / "Skills.csv").write_text("Name\nMade up\n", encoding="utf-8")
    with open_archive(tmp_path) as archive:
        assert archive.ignored == ("Skills.csv",)


def test_a_doubly_named_zip_extension_still_opens_normally(tmp_path: Path) -> None:
    """A real export's own filename ends ``.zip.zip`` (checked by hand against one);
    nothing here keys off the name, only the bytes, so the doubled extension
    changes nothing about how it opens.
    """
    zipped = _zipped(tmp_path)
    doubled = zipped.with_name("export.zip.zip")
    zipped.rename(doubled)
    with open_archive(doubled) as archive:
        assert len(archive.members) == 4


def test_a_zip_nested_inside_a_zip_is_refused_not_unwrapped(tmp_path: Path) -> None:
    """Checked by hand against a real export: its own ``.zip.zip`` name is just a
    doubled extension, not an actual nested zip — the CSVs sit directly in the one
    zip a person downloads. A genuinely nested zip (someone re-zipping their own
    download, say) is refused rather than silently unwrapped a level, since real
    exports never need that and guessing how far to unwrap is its own hazard —
    but the refusal names the nested zip and says what to do about it, rather
    than sending someone looking for a ``Connections.csv`` that is one level
    down (issue #124 review finding 5).
    """
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.write(FIXTURES / "Connections.csv", arcname="Connections.csv")
    outer = tmp_path / "export.zip.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, arcname="export.zip")
    with pytest.raises(ArchiveFormatError) as excinfo, open_archive(outer):
        pass
    message = str(excinfo.value)
    assert "no Connections.csv" in message
    assert "contains another zip (export.zip)" in message
    assert "extract it and upload the file inside" in message


def test_a_zip_with_several_members_and_no_table_gets_the_plain_message(
    tmp_path: Path,
) -> None:
    """The nested-zip hint only fires for a lone nested zip; several unreadable
    members (none of them the LinkedIn tables) get the ordinary message, not a
    guess about which one might be a zip to extract.
    """
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Skills.csv", "Name\nMade up\n")
        zf.writestr("inner-a.zip", b"not really a zip")
        zf.writestr("inner-b.zip", b"not really a zip either")
    with pytest.raises(ArchiveFormatError) as excinfo, open_archive(path):
        pass
    assert "contains another zip" not in str(excinfo.value)


# --- zip guards (P1-20) ------------------------------------------------------


def test_too_many_members_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linkedin_archive, "MAX_MEMBERS", 2)
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for index in range(3):
            zf.writestr(f"Skills{index}.csv", "Name\nMade up\n")
    with pytest.raises(ArchiveFormatError, match="member limit"), open_archive(path):
        pass


def test_too_many_files_in_a_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linkedin_archive, "MAX_MEMBERS", 2)
    for index in range(3):
        (tmp_path / f"Skills{index}.csv").write_text("Name\nMade up\n", encoding="utf-8")
    with pytest.raises(ArchiveFormatError, match="file limit"), open_archive(tmp_path):
        pass


def test_the_uncompressed_total_over_the_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linkedin_archive, "MAX_TOTAL_UNCOMPRESSED_BYTES", 100)
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Connections.csv", "First Name\n" + "a" * 60 + "\n")
        zf.writestr("Skills.csv", "Name\n" + "b" * 60 + "\n")
    with pytest.raises(ArchiveFormatError, match="uncompressed"), open_archive(path):
        pass


def test_the_directory_total_over_the_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linkedin_archive, "MAX_TOTAL_UNCOMPRESSED_BYTES", 100)
    (tmp_path / "Connections.csv").write_text("First Name\n" + "a" * 60 + "\n", encoding="utf-8")
    (tmp_path / "Skills.csv").write_text("Name\n" + "b" * 60 + "\n", encoding="utf-8")
    with pytest.raises(ArchiveFormatError, match="across its files"), open_archive(tmp_path):
        pass


def test_a_high_compression_ratio_is_refused_as_a_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linkedin_archive, "MAX_COMPRESSION_RATIO", 10)
    monkeypatch.setattr(linkedin_archive, "COMPRESSION_RATIO_FLOOR_BYTES", 100)
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Skills.csv", "0" * 100_000, compress_type=zipfile.ZIP_DEFLATED)
    with pytest.raises(ArchiveFormatError, match="compresses"), open_archive(path):
        pass


def test_ordinary_csv_text_is_nowhere_near_the_compression_ratio_limit(tmp_path: Path) -> None:
    """Real CSV text, actually deflated, does not approach the default ratio limit."""
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Connections.csv", (FIXTURES / "Connections.csv").read_bytes())
        zf.writestr("messages.csv", (FIXTURES / "messages.csv").read_bytes())
    with open_archive(path) as archive:
        assert len(archive.members) == 2


@pytest.mark.parametrize(
    "member_name", ["/etc/passwd.csv", "../../etc/passwd.csv", "a/../../b.csv", "C:/win.csv"]
)
def test_a_member_path_that_would_escape_the_archive_is_refused(
    tmp_path: Path, member_name: str
) -> None:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member_name, "First Name,Last Name,URL,Connected On\n")
    with pytest.raises(ArchiveFormatError, match="unsafe path"), open_archive(path):
        pass


def test_a_directory_bounds_a_huge_non_csv_file_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory path sums every file toward the total, not only the
    ``.csv``-looking ones, matching the zip path (issue #124 review finding 7).
    """
    monkeypatch.setattr(linkedin_archive, "MAX_TOTAL_UNCOMPRESSED_BYTES", 100)
    (tmp_path / "Connections.csv").write_text("First Name\na\n", encoding="utf-8")
    (tmp_path / "attachment.bin").write_bytes(b"x" * 200)
    with pytest.raises(ArchiveFormatError, match="across its files"), open_archive(tmp_path):
        pass


# --- a damaged or hostile zip does not become a 500 (issue #124 review finding 1) --


def _zip_with_false_declared_size(tmp_path: Path, content: bytes, declared: int) -> Path:
    """A zip whose one member's declared uncompressed size does not match its real one.

    Built by finding the real size as a 4-byte little-endian integer in the
    zip's own bytes — it appears in both the local file header and the
    central directory record — and overwriting every occurrence. ``zipfile``
    will not write a lie like this through its normal API (``writestr``
    always sets ``file_size`` from the real data), which is exactly why a
    reader has to survive one arriving some other way: a truncated download,
    or a deliberately crafted file.
    """
    path = tmp_path / "liar.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Connections.csv", content)
    raw = bytearray(buf.getvalue())
    real = struct.pack("<I", len(content))
    lie = struct.pack("<I", declared)
    index = 0
    replaced = 0
    while (index := raw.find(real, index)) != -1:
        raw[index : index + 4] = lie
        index += 4
        replaced += 1
    assert replaced >= 2, "expected the real size in both the local and central headers"
    path.write_bytes(bytes(raw))
    return path


def test_a_declared_size_that_understates_the_real_one_is_refused_not_a_traceback(
    tmp_path: Path,
) -> None:
    """Every guard here reads only declared metadata, so a lying header passes
    all of them; the lie is caught when the CRC no longer matches what was
    actually decompressed — an ``ArchiveFormatError``, not the bare
    ``zipfile.BadZipFile`` that used to escape uncaught.
    """
    content = (
        b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        + b"A,B,https://example.invalid/in/x,,C,D,01 Jan 2020\n" * 2000
    )
    path = _zip_with_false_declared_size(tmp_path, content, declared=500)
    with (
        pytest.raises(ArchiveFormatError, match="damaged inside the zip"),
        open_archive(path) as archive,
    ):
        for member in archive.members:
            if member.kind is ArchiveKind.CONNECTIONS:
                list(archive.connections(member))


def test_flipped_bytes_in_the_deflate_stream_are_refused_not_a_traceback(
    tmp_path: Path,
) -> None:
    """An ordinary damaged download — not a crafted one — hits the same path:
    a real declared size, a compressed stream corrupted in transit.
    """
    content = (
        b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        + b"A,B,https://example.invalid/in/x,,C,D,01 Jan 2020\n" * 2000
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Connections.csv", content)
    raw = bytearray(buf.getvalue())
    # Flip a run of bytes strictly inside the compressed data: after the local
    # file header and its filename, comfortably before the central directory
    # (which a flip landing in would break the zip itself, rather than the
    # file it holds — a different failure than the one this test is for).
    local_header_end = 30 + len(b"Connections.csv")
    central_dir_start = raw.find(b"PK\x01\x02")
    assert central_dir_start != -1
    compressed_region = central_dir_start - local_header_end
    assert compressed_region > 400, "fixture too small to flip safely inside it"
    start = local_header_end + compressed_region // 2
    for offset in range(200):
        raw[start + offset] ^= 0xFF
    path = tmp_path / "corrupt-deflate.zip"
    path.write_bytes(bytes(raw))
    with (
        pytest.raises(ArchiveFormatError, match="damaged inside the zip"),
        open_archive(path) as archive,
    ):
        for member in archive.members:
            if member.kind is ArchiveKind.CONNECTIONS:
                list(archive.connections(member))


def _zip_with_encrypted_member(tmp_path: Path) -> Path:
    """A zip whose one member's General Purpose Bit Flag claims encryption.

    Not genuinely encrypted content — the stdlib ``zipfile`` module cannot
    write that — only enough to reach the check that reads the flag before
    ``zf.open`` ever tries to decrypt anything.
    """
    path = tmp_path / "encrypted.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Connections.csv",
            "First Name,Last Name,URL,Connected On\nA,B,https://example.invalid/in/x,01 Jan 2020\n",
        )
    raw = bytearray(buf.getvalue())
    local_index = raw.find(b"PK\x03\x04")
    assert local_index != -1
    raw[local_index + 6] |= 0x01
    central_index = raw.find(b"PK\x01\x02")
    assert central_index != -1
    raw[central_index + 8] |= 0x01
    path.write_bytes(bytes(raw))
    return path


def test_an_encrypted_member_is_refused_with_a_clean_message(tmp_path: Path) -> None:
    """The message names the file, not a ``repr(ZipInfo(...))`` (nit from the review)."""
    path = _zip_with_encrypted_member(tmp_path)
    with pytest.raises(ArchiveFormatError) as excinfo, open_archive(path):
        pass
    message = str(excinfo.value)
    assert "encrypted" in message
    assert "ZipInfo" not in message


# --- the shipped guard values, not a monkeypatched stand-in (review finding 4) --


def test_the_guard_values_themselves_are_the_ones_that_were_reasoned_about() -> None:
    """The numbers, pinned — not the boundary logic, which the tests below cover.

    Every other guard test sizes its fixture from the live constant, so it
    stays true however the constant moves: raising ``MAX_MEMBERS`` to a million
    keeps them all green while the guard stops guarding anything. These are the
    values each constant's own comment argues for, against a real export of
    about 1.4 MB across thirty-odd members. Changing one is a decision, so it
    changes this line too, and the comment beside the constant with it.
    """
    assert linkedin_archive.MAX_MEMBER_BYTES == 512 * 1024 * 1024
    assert linkedin_archive.MAX_TOTAL_UNCOMPRESSED_BYTES == 64 * 1024 * 1024
    assert linkedin_archive.MAX_MEMBERS == 5000
    assert linkedin_archive.MAX_COMPRESSION_RATIO == 100
    assert linkedin_archive.COMPRESSION_RATIO_FLOOR_BYTES == 8 * 1024 * 1024


def test_the_member_size_cap_is_enforced_at_its_shipped_value() -> None:
    info = zipfile.ZipInfo(filename="messages.csv")
    info.file_size = linkedin_archive.MAX_MEMBER_BYTES + 1
    info.compress_size = 1000
    with pytest.raises(ArchiveFormatError, match="byte limit"):
        linkedin_archive._guard_zip_members([info], "test.zip")


def test_the_total_uncompressed_cap_is_enforced_at_its_shipped_value() -> None:
    info = zipfile.ZipInfo(filename="Skills.csv")
    info.file_size = linkedin_archive.MAX_TOTAL_UNCOMPRESSED_BYTES + 1
    # Comfortably under the ratio cap, so it is the total-size guard being
    # tested here, not the ratio guard tripping first on the same member.
    info.compress_size = info.file_size // 2
    with pytest.raises(ArchiveFormatError, match="uncompressed"):
        linkedin_archive._guard_zip_members([info], "test.zip")


def test_the_member_count_cap_is_enforced_at_its_shipped_value() -> None:
    infos = []
    for index in range(linkedin_archive.MAX_MEMBERS + 1):
        info = zipfile.ZipInfo(filename=f"f{index}.csv")
        info.file_size = 0
        info.compress_size = 0
        infos.append(info)
    with pytest.raises(ArchiveFormatError, match="member limit"):
        linkedin_archive._guard_zip_members(infos, "test.zip")


def test_the_compression_ratio_cap_is_enforced_at_its_shipped_value() -> None:
    info = zipfile.ZipInfo(filename="Skills.csv")
    info.file_size = linkedin_archive.COMPRESSION_RATIO_FLOOR_BYTES + 1
    info.compress_size = info.file_size // (linkedin_archive.MAX_COMPRESSION_RATIO + 1)
    with pytest.raises(ArchiveFormatError, match="compresses"):
        linkedin_archive._guard_zip_members([info], "test.zip")


def test_a_member_at_the_compression_ratio_floor_is_not_judged_by_ratio() -> None:
    """At or below the floor, no ratio — however extreme — trips the guard."""
    info = zipfile.ZipInfo(filename="Skills.csv")
    info.file_size = linkedin_archive.COMPRESSION_RATIO_FLOOR_BYTES
    info.compress_size = 1
    linkedin_archive._guard_zip_members([info], "test.zip")  # does not raise


def test_a_real_deflated_member_over_the_shipped_ratio_and_floor_is_refused(
    tmp_path: Path,
) -> None:
    """No monkeypatch: an actual zip, actually deflated, past the shipped values."""
    path = tmp_path / "bomb.zip"
    payload = b"0" * (linkedin_archive.COMPRESSION_RATIO_FLOOR_BYTES + 1)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Skills.csv", payload)
    with pytest.raises(ArchiveFormatError, match="compresses"), open_archive(path):
        pass


def test_declared_member_count_matches_a_small_real_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "x")
        zf.writestr("b.csv", "y")
    assert linkedin_archive._declared_member_count(buf) == 2


def test_a_zip_declaring_more_members_than_the_shipped_cap_is_refused_cheaply() -> None:
    """No monkeypatch: built with one more member than ``MAX_MEMBERS`` actually
    allows, and refused before :class:`zipfile.ZipFile` parses a single entry.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for index in range(linkedin_archive.MAX_MEMBERS + 1):
            zf.writestr(str(index), b"")
    with pytest.raises(ArchiveFormatError, match="member limit"), open_archive(buf):
        pass


def test_declared_member_count_is_not_fooled_by_an_understated_entries_field() -> None:
    """``zipfile`` itself does not trust the entry-count field either: it reads
    central-directory records until it has consumed the declared *byte size*.
    Understating the count field while leaving the byte size honest must not
    buy a smaller answer than the byte size alone already implies.
    """
    buf = io.BytesIO()
    real_count = 200
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for index in range(real_count):
            zf.writestr(f"f{index}", b"")
    raw = bytearray(buf.getvalue())
    eocd_index = raw.rfind(b"PK\x05\x06")
    assert eocd_index != -1
    real_entries = struct.unpack_from("<H", raw, eocd_index + 10)[0]
    assert real_entries == real_count
    struct.pack_into("<H", raw, eocd_index + 10, 1)  # lie: claim only one entry
    count = linkedin_archive._declared_member_count(io.BytesIO(bytes(raw)))
    assert count >= real_count


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


# --- the owner's own profile ------------------------------------------------


def test_the_profile_names_the_archive_owner() -> None:
    with open_archive(FIXTURES) as archive:
        assert archive.owner_name() == "Nettie Keeperton"


def test_an_archive_without_a_profile_names_nobody(tmp_path: Path) -> None:
    (tmp_path / "Connections.csv").write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Fictional,https://www.linkedin.com/in/ada-fictional,,Works,Eng,12 Mar 2019\n",
        encoding="utf-8",
    )
    with open_archive(tmp_path) as archive:
        assert archive.owner_name() is None


def test_a_profile_on_its_own_is_not_an_archive(tmp_path: Path) -> None:
    """It names the owner and holds nothing to import, so there is nothing to open."""
    path = tmp_path / "Profile.csv"
    path.write_text(
        "First Name,Last Name,Headline\nNettie,Keeperton,Keeps a made-up network warm\n",
        encoding="utf-8",
    )
    with pytest.raises(ArchiveFormatError), open_archive(path):
        pass


def test_a_directory_of_only_a_profile_is_refused(tmp_path: Path) -> None:
    (tmp_path / "Profile.csv").write_text(
        "First Name,Last Name,Headline\nNettie,Keeperton,Keeps a made-up network warm\n",
        encoding="utf-8",
    )
    with (
        pytest.raises(ArchiveFormatError, match=r"no Connections\.csv"),
        open_archive(tmp_path),
    ):
        pass


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
    assert threads.total_conversations == 7
    assert len(threads.conversations) == 5
    assert threads.rows == 13
    assert threads.undated_rows == 1


def test_several_names_and_no_urls_at_all_make_a_group_thread() -> None:
    """With no URL list to check against, the names are all there is to go on."""
    rows = [
        _message("one", "Nettie Keeperton", OWNER, ("Ada Fictional", "Bo Placeholder")),
        _message("two", "Nettie Keeperton", OWNER, ("Ada Fictional",), ("ada-fictional",)),
        _message("three", "Nettie Keeperton", OWNER, ("Cyd Invented",), ("cyd-invented",)),
    ]
    threads = group(rows)
    assert threads.skipped_for("group") == 1
    assert {c.conversation_id for c in threads.conversations} == {"two", "three"}


def test_a_comma_in_a_display_name_does_not_invent_a_second_recipient() -> None:
    """``TO`` splits on commas and credentials carry them; the URL list is the truth.

    Regression: counting names alone turned every "Firstname Lastname, PhD"
    thread into a group chat and dropped the conversation whole.
    """
    rows = [
        _message(
            "one",
            "Nettie Keeperton",
            OWNER,
            ("Dee Notional", "PhD"),
            ("dee-notional",),
        ),
        _message("one", "Dee Notional, PhD", "dee-notional", ("Nettie Keeperton",), (OWNER,)),
    ]
    threads = group(rows, owner=OWNER)
    assert threads.skipped_for("group") == 0
    (conversation,) = threads.conversations
    assert conversation.counterpart_public_id == "dee-notional"
    assert [message.outbound for message in conversation.messages] == [True, False]


def test_a_comma_in_a_name_survives_the_whole_read() -> None:
    """End to end from the fixture, because the split happens in the parser."""
    with open_archive(FIXTURES) as archive:
        threads = group(archive.messages(_member(archive, ArchiveKind.MESSAGES)))
    conversation = next(c for c in threads.conversations if c.conversation_id == "conv-gg")
    assert conversation.counterpart_public_id == "dee-notional"
    assert len(conversation.messages) == 2


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


def test_the_profile_name_settles_a_tie_end_to_end() -> None:
    """The path :func:`netkeeper.crm.archive.import_archive` takes for a small archive."""
    rows = [
        _message("one", "Nettie Keeperton", OWNER, ("Ada Fictional",), ("ada-fictional",)),
        _message("two", "Ada Fictional", "ada-fictional", ("Nettie Keeperton",), (OWNER,)),
    ]
    assert group(rows).owner is None
    threads = group(rows, profile_name="Nettie Keeperton")
    assert threads.owner is not None
    assert (threads.owner.public_id, threads.owner.by) == (OWNER, "profile-name")
    assert len(threads.conversations) == 2


def test_no_messages_at_all_name_no_owner() -> None:
    threads = group([])
    assert threads.owner is None
    assert threads.total_conversations == 0
