"""``POST /api/v1/imports/archive``: the LinkedIn export zip, uploaded (P1-20).

The fixture archive under ``tests/fixtures/archive`` is the same hand-written,
entirely invented sample ``tests/test_archive_import.py`` and
``tests/test_linkedin_archive.py`` use (see their module docstrings for what is
in it); the counts asserted here are the ones those modules already pin by
hand, so a drift between the CLI's pipeline and this endpoint's use of it shows
up as a mismatch against numbers already trusted elsewhere. The zip-level
guards themselves (member count, total size, compression ratio, member paths)
are exercised in ``tests/test_linkedin_archive.py``; what is checked here is
that this endpoint surfaces a guard failure as ``422``, not a ``500``.
"""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import factories
import httpx
import pytest
from fastapi import FastAPI, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.linkedin import archive as linkedin_archive
from netkeeper.linkedin.archive import ArchiveRefusalCode
from netkeeper.models import Contact, Interaction, User
from netkeeper.scoping import scoped_count
from netkeeper.web.api import imports as imports_api
from netkeeper.web.errors import ApiError

CSRF = {"X-Netkeeper-Client": "1"}
FIXTURES = Path(__file__).parent / "fixtures" / "archive"

KNOWN_CONNECTIONS = {
    "rows": 9,
    "created": 7,
    "updated": 1,
    "needs_review": 0,
    "skipped": 1,
    "with_email": 2,
    "undated": 2,
}
KNOWN_MESSAGES = {
    "rows": 13,
    "conversations": 7,
    "attributed": 4,
    "no_counterpart": 1,
    "group_threads": 1,
    "unknown_contact": 1,
    "no_owner": 0,
    "added": 8,
    "already_present": 0,
    "undated": 1,
    "outbound": 3,
    "inbound": 5,
}
KNOWN_INVITATIONS = {
    "rows": 6,
    "added": 2,
    "already_present": 0,
    "unknown_contact": 1,
    "no_counterpart": 1,
    "undated": 1,
    "undirected": 1,
}


def _zipped(dest: Path, *, name: str = "export.zip", extra: dict[str, str] | None = None) -> bytes:
    """The fixture archive directory packed into a zip, as a LinkedIn export arrives."""
    path = dest / name
    with zipfile.ZipFile(path, "w") as zf:
        for source in sorted(FIXTURES.iterdir()):
            zf.write(source, arcname=source.name)
        for member_name, content in (extra or {}).items():
            zf.writestr(member_name, content)
    return path.read_bytes()


async def _upload(
    client: httpx.AsyncClient, data: bytes, *, filename: str = "export.zip"
) -> httpx.Response:
    return await client.post(
        "/api/v1/imports/archive",
        headers=CSRF,
        files={"file": (filename, data, "application/zip")},
    )


def _counts(session_factory: sessionmaker[Session]) -> tuple[int, int]:
    with session_scope(session_factory) as session:
        user = session.scalars(select(User)).one()
        contacts = session.scalar(scoped_count(user, Contact))
        interactions = session.scalar(scoped_count(user, Interaction))
    return contacts or 0, interactions or 0


# --- the happy path -----------------------------------------------------------


async def test_the_upload_reports_the_known_counts(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path
) -> None:
    response = await _upload(client, _zipped(tmp_path))
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["filename"] == "export.zip"
    assert body["owner_public_id"] == "nettie-keeperton"
    assert body["owner_by"] == "traffic"
    assert body["connections"] == KNOWN_CONNECTIONS
    assert body["messages"] == KNOWN_MESSAGES
    assert body["invitations"] == KNOWN_INVITATIONS
    assert body["ignored_files"] == []

    contacts, interactions = _counts(running_app.state.session_factory)
    assert contacts == 7
    assert interactions == 10


async def test_the_upload_is_a_run_in_the_history_and_rolls_back(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path
) -> None:
    """#132: the import that populated the database can be shown and undone."""
    response = await _upload(client, _zipped(tmp_path, extra={"Skills.csv": "Name\nMade up\n"}))
    assert response.status_code == 201, response.text
    run_id = response.json()["run_id"]
    assert isinstance(run_id, int)

    history = (await client.get("/api/v1/imports")).json()
    assert [run["id"] for run in history["items"]] == [run_id]
    run = (await client.get(f"/api/v1/imports/{run_id}")).json()
    assert run["source_kind"] == "archive"
    assert run["status"] == "committed"
    assert run["filename"] == "export.zip"
    assert run["created_count"] == KNOWN_CONNECTIONS["created"]
    assert run["archive"]["connections"] == KNOWN_CONNECTIONS
    assert run["archive"]["messages"] == KNOWN_MESSAGES
    assert run["archive"]["invitations"] == KNOWN_INVITATIONS
    assert run["archive"]["ignored_files"] == ["Skills.csv"]
    rows = (await client.get(f"/api/v1/imports/{run_id}/rows")).json()
    assert rows["total"] == KNOWN_CONNECTIONS["rows"]

    undone = await client.post(f"/api/v1/imports/{run_id}/rollback", headers=CSRF)
    assert undone.status_code == 200, undone.text
    assert undone.json()["contacts_deleted"] == KNOWN_CONNECTIONS["created"]
    assert _counts(running_app.state.session_factory) == (0, 0)


async def test_a_table_this_importer_does_not_read_is_named_as_ignored(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """``Positions.csv`` lands here too, until #117 adds it — this just proves any
    unrecognized table does, using a made-up one so the test does not depend on
    #117's shape.
    """
    data = _zipped(tmp_path, extra={"Skills.csv": "Name\nMade up\n"})
    response = await _upload(client, data)
    assert response.status_code == 201, response.text
    assert response.json()["ignored_files"] == ["Skills.csv"]


async def test_a_messages_shaped_file_under_another_name_is_named(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """#74: a fourth assistant log imports as message history, and the result says so,
    both on the upload's answer and on the run the history keeps."""
    guide = (FIXTURES / "guide_messages.csv").read_text(encoding="utf-8")
    data = _zipped(tmp_path, extra={"interview_prep_messages.csv": guide})
    response = await _upload(client, data)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["unfamiliar_message_files"] == ["interview_prep_messages.csv"]
    assert body["messages"]["added"] == KNOWN_MESSAGES["added"]
    run = (await client.get(f"/api/v1/imports/{body['run_id']}")).json()
    assert run["archive"]["unfamiliar_message_files"] == ["interview_prep_messages.csv"]


async def test_the_sample_names_no_unfamiliar_message_file(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    response = await _upload(client, _zipped(tmp_path))
    assert response.status_code == 201, response.text
    assert response.json()["unfamiliar_message_files"] == []


async def test_reimporting_the_same_archive_adds_nothing(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path
) -> None:
    data = _zipped(tmp_path)
    first = await _upload(client, data)
    assert first.status_code == 201, first.text

    second = await _upload(client, data)
    assert second.status_code == 201, second.text
    body = second.json()

    assert body["connections"]["created"] == 0
    assert body["connections"]["updated"] == KNOWN_CONNECTIONS["created"]
    assert body["connections"]["needs_review"] == 1
    assert body["messages"]["added"] == 0
    assert body["messages"]["already_present"] == KNOWN_MESSAGES["added"]
    assert body["invitations"]["added"] == 0
    assert body["invitations"]["already_present"] == KNOWN_INVITATIONS["added"]

    contacts, interactions = _counts(running_app.state.session_factory)
    assert contacts == 7
    assert interactions == 10


# --- refusing a bad upload ------------------------------------------------------


async def test_the_csrf_header_is_required(client: httpx.AsyncClient, tmp_path: Path) -> None:
    response = await client.post(
        "/api/v1/imports/archive",
        files={"file": ("export.zip", _zipped(tmp_path), "application/zip")},
    )
    assert response.status_code == 403


async def test_a_zip_that_is_not_a_linkedin_export_answers_422_naming_the_file(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "random.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Skills.csv", "Name\nMade up\n")
    response = await _upload(client, path.read_bytes(), filename="random.zip")
    assert response.status_code == 422, response.text
    body = response.json()
    assert "random.zip" in body["detail"]
    assert "Connections.csv" in body["detail"]
    assert body["code"] == ArchiveRefusalCode.WRONG_ARCHIVE.value


async def test_garbage_bytes_answer_422_not_500(client: httpx.AsyncClient) -> None:
    response = await _upload(client, b"this is not a zip file at all")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == ArchiveRefusalCode.NOT_A_ZIP.value


async def test_the_upload_size_guard_answers_422_before_opening_anything(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imports_api, "ARCHIVE_MAX_UPLOAD_BYTES", 50)
    response = await _upload(client, _zipped(tmp_path))
    assert response.status_code == 422, response.text
    body = response.json()
    assert "byte limit" in body["detail"]
    assert body["code"] == ArchiveRefusalCode.TOO_LARGE.value


async def test_a_zip_bomb_is_refused_with_a_422_not_a_500(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linkedin_archive, "MAX_COMPRESSION_RATIO", 10)
    monkeypatch.setattr(linkedin_archive, "COMPRESSION_RATIO_FLOOR_BYTES", 100)
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Skills.csv", "0" * 100_000, compress_type=zipfile.ZIP_DEFLATED)
    response = await _upload(client, path.read_bytes(), filename="bomb.zip")
    assert response.status_code == 422, response.text
    body = response.json()
    assert "compresses" in body["detail"]
    assert body["code"] == ArchiveRefusalCode.COMPRESSION_RATIO_TOO_HIGH.value


async def test_a_path_traversing_member_is_refused_with_a_422_not_a_500(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("../../etc/passwd.csv", "First Name,Last Name,URL,Connected On\n")
    response = await _upload(client, path.read_bytes())
    assert response.status_code == 422, response.text
    body = response.json()
    assert "unsafe path" in body["detail"]
    assert body["code"] == ArchiveRefusalCode.UNSAFE_MEMBER_PATH.value


async def test_a_damaged_download_is_refused_with_a_422_not_a_500(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The live-server failure the review opened with: a zip whose header lies
    about a member's uncompressed size passes every declared-metadata guard,
    and used to escape as a bare 500 when the lie was discovered mid-read.
    An ordinary damaged download shapes the same way — not only a crafted one.
    """
    content = (FIXTURES / "Connections.csv").read_bytes() * 50
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Connections.csv", content)
    raw = bytearray(buf.getvalue())
    real = struct.pack("<I", len(content))
    lie = struct.pack("<I", 500)
    index = 0
    while (index := raw.find(real, index)) != -1:
        raw[index : index + 4] = lie
        index += 4
    response = await _upload(client, bytes(raw), filename="liar.zip")
    assert response.status_code == 422, response.text
    body = response.json()
    assert "damaged inside the zip" in body["detail"]
    assert body["code"] == ArchiveRefusalCode.DAMAGED.value


def test_the_upload_cap_is_the_value_that_was_reasoned_about() -> None:
    """The number, pinned. The test below sizes its upload from the constant, so
    it stays green if the cap is raised to something that bounds nothing; this
    is the line that has to change when the cap does."""
    assert imports_api.ARCHIVE_MAX_UPLOAD_BYTES == 200 * 1024 * 1024


async def test_the_upload_size_guard_is_enforced_at_its_shipped_value(
    session_factory: sessionmaker[Session],
) -> None:
    """No monkeypatch, and no need to transfer 200 MiB to prove it: ``file.size``
    is a plain attribute Starlette has already computed by the time a handler
    runs (it accumulates it as the multipart parser writes each chunk), so the
    real cap is exercised by setting that attribute directly on a stand-in
    upload — the same value an actual oversized transfer would leave behind —
    rather than by sending one.
    """
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        upload = UploadFile(
            file=io.BytesIO(b""),
            size=imports_api.ARCHIVE_MAX_UPLOAD_BYTES + 1,
            filename="huge.zip",
        )
        with pytest.raises(ApiError) as excinfo:
            imports_api.import_archive(user=user, session=session, file=upload)
    assert excinfo.value.status_code == 422
    assert "byte limit" in excinfo.value.body["detail"]
    assert excinfo.value.body["code"] == ArchiveRefusalCode.TOO_LARGE.value


# --- every refusal carries a code, not just most of them ---------------------
#
# The wizard built on top of this endpoint (P1-21) keys off ``code``, not the
# words in ``detail`` — substring-matching another layer's prose is exactly
# what broke it against this endpoint's own message changes. The guarantee
# that matters is not "some refusals carry a code" but "every one does", so
# this builds one real scenario per :class:`ArchiveRefusalCode` member and
# then asserts the set of codes exercised is the whole enum: a new refusal
# added without a scenario here fails this test, not just a code review.


def _garbage_bytes() -> bytes:
    return b"this is not a zip file at all"


def _wrong_archive_zip_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "wrong.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Skills.csv", "Name\nMade up\n")
    return path.read_bytes()


def _nested_zip_bytes(tmp_path: Path) -> bytes:
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("Connections.csv", "First Name,Last Name,URL,Connected On\n")
    outer = tmp_path / "export.zip.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, arcname="export.zip")
    return outer.read_bytes()


def _encrypted_zip_bytes() -> bytes:
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
    return bytes(raw)


def _damaged_zip_bytes() -> bytes:
    content = (FIXTURES / "Connections.csv").read_bytes() * 20
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Connections.csv", content)
    raw = bytearray(buf.getvalue())
    real = struct.pack("<I", len(content))
    lie = struct.pack("<I", 50)
    index = 0
    while (index := raw.find(real, index)) != -1:
        raw[index : index + 4] = lie
        index += 4
    return bytes(raw)


def _malformed_table_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # A Connections table missing the "URL" column its reader needs.
        zf.writestr("Connections.csv", "First Name,Last Name,Connected On\nA,B,01 Jan 2020\n")
    return buf.getvalue()


def _too_many_members_zip_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for index in range(3):
            zf.writestr(f"f{index}.csv", "Name\nMade up\n")
    return path.read_bytes()


def _compression_ratio_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Skills.csv", "0" * 100_000)
    return buf.getvalue()


def _unsafe_member_path_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/passwd.csv", "First Name,Last Name,URL,Connected On\n")
    return buf.getvalue()


async def _assert_refusal(client: httpx.AsyncClient, data: bytes, code: ArchiveRefusalCode) -> None:
    response = await _upload(client, data, filename=f"{code.value}.zip")
    assert response.status_code == 422, f"{code}: expected 422, got {response.status_code}"
    body = response.json()
    assert body["code"] == code.value, f"{code}: response carried {body!r}"
    assert body["detail"], f"{code}: detail was empty"


async def test_every_refusal_code_is_reachable_and_present_on_the_response(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    seen: set[ArchiveRefusalCode] = set()

    async def check(data: bytes, code: ArchiveRefusalCode) -> None:
        await _assert_refusal(client, data, code)
        seen.add(code)

    await check(_garbage_bytes(), ArchiveRefusalCode.NOT_A_ZIP)
    await check(_wrong_archive_zip_bytes(tmp_path), ArchiveRefusalCode.WRONG_ARCHIVE)
    await check(_nested_zip_bytes(tmp_path), ArchiveRefusalCode.NESTED_ZIP)
    await check(_encrypted_zip_bytes(), ArchiveRefusalCode.ENCRYPTED)
    await check(_damaged_zip_bytes(), ArchiveRefusalCode.DAMAGED)
    await check(_malformed_table_zip_bytes(), ArchiveRefusalCode.MALFORMED_TABLE)
    await check(_unsafe_member_path_zip_bytes(), ArchiveRefusalCode.UNSAFE_MEMBER_PATH)

    # These three need a guard patched small enough for ordinary test-sized
    # data to trip it; scoped to just their own upload so one does not also
    # catch another scenario's zip before it reaches the code being tested.
    with pytest.MonkeyPatch.context() as m:
        m.setattr(linkedin_archive, "MAX_MEMBERS", 2)
        await check(_too_many_members_zip_bytes(tmp_path), ArchiveRefusalCode.TOO_MANY_MEMBERS)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(linkedin_archive, "MAX_COMPRESSION_RATIO", 10)
        m.setattr(linkedin_archive, "COMPRESSION_RATIO_FLOOR_BYTES", 100)
        await check(_compression_ratio_zip_bytes(), ArchiveRefusalCode.COMPRESSION_RATIO_TOO_HIGH)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(imports_api, "ARCHIVE_MAX_UPLOAD_BYTES", 50)
        await check(_zipped(tmp_path), ArchiveRefusalCode.TOO_LARGE)

    assert seen == set(ArchiveRefusalCode), (
        f"no scenario covers {set(ArchiveRefusalCode) - seen}; "
        "every refusal code needs one, not just most"
    )


# --- a damaged central directory (#138) -----------------------------------------


def _corrupt_central_directory(data: bytes, start: int, length: int = 100) -> bytes:
    """``data`` with ``length`` bytes XOR 0xFF from ``start`` bytes into its central directory.

    Every local file header and every member's bytes are left intact, as in the
    reproduction #138 opened with.
    """
    raw = bytearray(data)
    eocd = raw.rfind(b"PK\x05\x06")
    size, offset = struct.unpack_from("<II", raw, eocd + 12)
    begin = offset + start
    for index in range(begin, min(begin + length, offset + size)):
        raw[index] ^= 0xFF
    return bytes(raw)


@pytest.mark.parametrize(
    ("start", "said"),
    [
        # The #138 reproduction: a garbled name length makes one record swallow the
        # three after it. zipfile opened the rest, and the import answered 201 with
        # Connections and Invitations only; messages.csv was simply gone.
        (154, "declares 6 files, but only 3 could be read"),
        # The last record's name garbled, lengths intact: the count still matches,
        # but "messages.csv" no longer ends in .csv and was skipped as noise.
        (336, "1 of its 6 file names no longer match the files they point at"),
        # A garbled name in a record flagged UTF-8: a UnicodeDecodeError, and a 500.
        (7, "the zip's directory is damaged"),
    ],
)
async def test_a_damaged_central_directory_is_refused_rather_than_read_partly(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path, start: int, said: str
) -> None:
    response = await _upload(client, _corrupt_central_directory(_zipped(tmp_path), start))

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == ArchiveRefusalCode.DAMAGED.value
    assert said in body["detail"]
    assert _counts(running_app.state.session_factory) == (0, 0)


async def test_no_corruption_of_the_central_directory_imports_partly_or_answers_500(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path
) -> None:
    """Sweep the damage across the whole directory: every upload either imports in
    full or is refused with a 422 and a code. None imports some tables and drops the
    rest, and none escapes as a 500 (a garbled UTF-8 name used to).
    """
    pristine = _zipped(tmp_path)
    eocd = pristine.rfind(b"PK\x05\x06")
    (size,) = struct.unpack_from("<I", pristine, eocd + 12)
    outcomes: set[int | str] = set()
    for start in range(0, size - 20, 7):
        response = await _upload(client, _corrupt_central_directory(pristine, start))
        if response.status_code == 201:
            body = response.json()
            assert body["connections"] == KNOWN_CONNECTIONS, f"partial import at {start}"
            assert body["messages"]["rows"] == KNOWN_MESSAGES["rows"], f"partial at {start}"
            assert body["invitations"]["rows"] == KNOWN_INVITATIONS["rows"], f"partial at {start}"
            outcomes.add(201)
        else:
            assert response.status_code == 422, f"{response.status_code} at {start}"
            outcomes.add(response.json()["code"])
    assert ArchiveRefusalCode.DAMAGED.value in outcomes
