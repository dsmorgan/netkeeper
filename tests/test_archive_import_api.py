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

import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.linkedin import archive as linkedin_archive
from netkeeper.models import Contact, Interaction, User
from netkeeper.scoping import scoped_count
from netkeeper.web.api import imports as imports_api

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


# --- the CLI and the API agree -------------------------------------------------


async def test_the_response_shape_has_room_for_a_field_this_endpoint_does_not_fill(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A crude but cheap guard against #117's future field breaking existing clients:
    every key already promised is still there, and the response is a plain
    object a new key can be added to (not, say, a fixed-length array).
    """
    response = await _upload(client, _zipped(tmp_path))
    body = response.json()
    assert isinstance(body, dict)
    assert {"connections", "messages", "invitations", "ignored_files"} <= body.keys()


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
    detail = response.json()["detail"]
    assert "random.zip" in detail
    assert "Connections.csv" in detail


async def test_garbage_bytes_answer_422_not_500(client: httpx.AsyncClient) -> None:
    response = await _upload(client, b"this is not a zip file at all")
    assert response.status_code == 422, response.text


async def test_the_upload_size_guard_answers_422_before_opening_anything(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imports_api, "ARCHIVE_MAX_UPLOAD_BYTES", 50)
    response = await _upload(client, _zipped(tmp_path))
    assert response.status_code == 422, response.text
    assert "upload limit" in response.json()["detail"]


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
    assert "compresses" in response.json()["detail"]


async def test_a_path_traversing_member_is_refused_with_a_422_not_a_500(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("../../etc/passwd.csv", "First Name,Last Name,URL,Connected On\n")
    response = await _upload(client, path.read_bytes())
    assert response.status_code == 422, response.text
    assert "unsafe path" in response.json()["detail"]
