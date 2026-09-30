"""Adding a contact by hand (#303): the service, ``POST /contacts``, and ``netkeeper contacts add``.

The same dedup and checks an import runs: a contact the email or the LinkedIn URL
finds is never created twice, whatever the case or the URL's spelling; a name,
company, and surname match is a duplicate unless the caller says to add anyway.
The two-user test lives in ``tests/isolation/test_isolation.py``.

Every name, address, and URL here is invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.crm import lists as list_service
from netkeeper.crm import tags as tag_service
from netkeeper.crm.contacts import merge_contacts
from netkeeper.crm.filters import parse_filter
from netkeeper.crm.importer import is_email_address
from netkeeper.crm.new_contact import Duplicate, Invalid, NewContact, create_contact
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Contact,
    ContactAlias,
    ContactSource,
    ContactTag,
    ListKind,
    ListMember,
    TagSource,
    User,
    UserKind,
)
from netkeeper.scoping import install_scope_guard, scoped, scoped_count
from netkeeper.services.users import ensure_local_user

CSRF = {"X-Netkeeper-Client": "1"}


# --- the service ------------------------------------------------------------


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def test_creates_a_manual_contact_with_its_address_and_linkedin_identity(
    writer: Session, user: User
) -> None:
    contact = create_contact(
        writer,
        user,
        NewContact(
            first_name="  Wren ",
            last_name="Halloway",
            email=" Wren.Halloway@Example.TEST ",
            current_company="Brindle Works",
            current_title="Product Designer",
            li_url="linkedin.com/in/Wren-Halloway-Fake/?trk=x",
        ),
    )
    assert contact.id is not None
    assert (contact.first_name, contact.last_name) == ("Wren", "Halloway")
    assert contact.preferred_name == "Wren"
    assert contact.current_company == "Brindle Works"
    assert contact.current_title == "Product Designer"
    # The URL is read the way an import reads it: the slug, lowercased, and the canonical URL.
    assert contact.li_public_id == "wren-halloway-fake"
    assert contact.li_url == "https://www.linkedin.com/in/wren-halloway-fake/"
    assert contact.li_urn is None
    # Provenance: manual, as the first source and for every field given (spec 10.5).
    assert contact.source is ContactSource.MANUAL
    assert set(contact.field_sources) == {
        "li_public_id",
        "li_url",
        "first_name",
        "last_name",
        "current_title",
        "current_company",
    }
    assert set(contact.field_sources.values()) == {"manual"}
    assert contact.synced_values == {}
    [email] = contact.emails
    assert email.email == "wren.halloway@example.test"
    assert email.is_primary
    assert email.source is ContactSource.MANUAL


def test_a_last_name_alone_is_enough(writer: Session, user: User) -> None:
    contact = create_contact(writer, user, NewContact(last_name="Okafor"))
    assert (contact.first_name, contact.last_name) == ("", "Okafor")
    assert contact.emails == []
    assert contact.li_url is None


@pytest.mark.parametrize(
    ("new", "field"),
    [
        (NewContact(), "first_name"),
        (NewContact(first_name="  ", last_name=""), "first_name"),
        (NewContact(first_name="Ada", email="not-an-address"), "email"),
        (NewContact(first_name="Ada", email="ada@localhost"), "email"),
        (NewContact(first_name="Ada", email="a@x.test, b@y.test"), "email"),
        (NewContact(first_name="Ada", email="Ada <ada@x.test>"), "email"),
        (NewContact(first_name="Ada", li_url="https://example.test/in/ada"), "li_url"),
        (NewContact(first_name="Ada", li_url="https://www.linkedin.com/company/x"), "li_url"),
        (NewContact(first_name="A" * 201), "first_name"),
        (NewContact(first_name="Ada", current_company="C" * 301), "current_company"),
    ],
)
def test_refuses_a_value_that_does_not_hold_up(
    writer: Session, user: User, new: NewContact, field: str
) -> None:
    with pytest.raises(Invalid) as caught:
        create_contact(writer, user, new)
    assert caught.value.field == field
    assert writer.scalar(scoped_count(user, Contact)) == 0


def test_the_email_check_is_the_importers() -> None:
    assert is_email_address("ada@example.test")
    assert not is_email_address("ada@localhost")
    assert not is_email_address("ada example.test")


def test_a_duplicate_by_email_ignores_case_and_spaces(writer: Session, user: User) -> None:
    existing = factories.make_contact(writer, user, emails=["ada.quill@example.test"])
    with pytest.raises(Duplicate) as caught:
        create_contact(
            writer, user, NewContact(first_name="Someone", email="  ADA.Quill@Example.Test")
        )
    assert caught.value.contact_id == existing.id
    assert caught.value.matched_by == "email"
    assert caught.value.archived is False
    assert writer.scalar(scoped_count(user, Contact)) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/ada-quill-fake/",
        "HTTPS://WWW.LINKEDIN.COM/in/ADA-Quill-Fake",
        "linkedin.com/in/ada-quill-fake?utm_source=share",
        "https://uk.linkedin.com/in/ada%2Dquill%2Dfake",
    ],
)
def test_a_duplicate_by_linkedin_url_however_it_is_spelled(
    writer: Session, user: User, url: str
) -> None:
    existing = factories.make_contact(writer, user, li_public_id="ada-quill-fake")
    with pytest.raises(Duplicate) as caught:
        create_contact(writer, user, NewContact(first_name="Ada", li_url=url))
    assert caught.value.contact_id == existing.id
    assert caught.value.matched_by == "linkedin"


def test_a_duplicate_by_an_old_slug(writer: Session, user: User) -> None:
    existing = factories.make_contact(writer, user, li_public_id="ada-new-fake")
    existing.aliases.append(
        ContactAlias(
            user_id=user.id,
            li_public_id="ada-old-fake",
            source=ContactSource.SYNC,
            observed_at=existing.created_at,
        )
    )
    writer.flush()
    with pytest.raises(Duplicate) as caught:
        create_contact(
            writer,
            user,
            NewContact(first_name="Ada", li_url="https://www.linkedin.com/in/ada-old-fake"),
        )
    assert caught.value.contact_id == existing.id
    assert caught.value.matched_by == "linkedin"


def test_a_duplicate_names_an_archived_contact_as_archived(writer: Session, user: User) -> None:
    existing = factories.make_contact(writer, user, emails=["gone@example.test"])
    existing.archived_at = existing.created_at
    writer.flush()
    with pytest.raises(Duplicate) as caught:
        create_contact(writer, user, NewContact(first_name="X", email="gone@example.test"))
    assert caught.value.contact_id == existing.id
    assert caught.value.archived is True


def test_a_duplicate_of_a_merged_away_contact_names_the_survivor(
    writer: Session, user: User
) -> None:
    loser = factories.make_contact(writer, user, emails=["merged@example.test"])
    survivor = factories.make_contact(writer, user)
    merge_contacts(writer, user, survivor.id, loser.id)
    with pytest.raises(Duplicate) as caught:
        create_contact(writer, user, NewContact(first_name="X", email="merged@example.test"))
    assert caught.value.contact_id == survivor.id


def test_a_name_and_company_match_is_a_duplicate_unless_allowed(
    writer: Session, user: User
) -> None:
    existing = factories.make_contact(
        writer, user, first_name="Ada", last_name="Quill", current_company="Blueleaf"
    )
    new = NewContact(first_name="ada", last_name="QUILL", current_company=" blueleaf ")
    with pytest.raises(Duplicate) as caught:
        create_contact(writer, user, new)
    assert caught.value.matched_by == "name"
    assert caught.value.contact_ids == (existing.id,)

    added = create_contact(writer, user, new, allow_name_match=True)
    assert added.id != existing.id
    assert writer.scalar(scoped_count(user, Contact)) == 2


def test_allow_name_match_never_lets_an_email_duplicate_through(
    writer: Session, user: User
) -> None:
    factories.make_contact(writer, user, emails=["ada@example.test"])
    with pytest.raises(Duplicate) as caught:
        create_contact(
            writer,
            user,
            NewContact(first_name="Ada", email="ada@example.test"),
            allow_name_match=True,
        )
    assert caught.value.matched_by == "email"


def test_the_new_contact_takes_its_tags_and_list_and_the_rules_run(
    writer: Session, user: User
) -> None:
    friends = tag_service.create_tag(writer, user, "friends")
    speakers = tag_service.create_tag(writer, user, "speakers")
    first_100 = list_service.create_list(writer, user, "First 100", ListKind.STATIC)
    contact = create_contact(
        writer,
        user,
        NewContact(
            first_name="Ada",
            current_title="Staff Software Engineer",
            tag_ids=(friends.id, speakers.id, friends.id),
            list_id=first_100.id,
        ),
    )
    assignments = {
        row.tag_id: row.source
        for row in writer.scalars(
            scoped(user, ContactTag).where(ContactTag.contact_id == contact.id)
        )
    }
    engineering = tag_service.find_tag(writer, user, "engineering")
    assert engineering is not None
    assert assignments == {
        friends.id: TagSource.MANUAL,
        speakers.id: TagSource.MANUAL,
        # The default rules, seeded on the way, tagged the title as an import commit would.
        engineering.id: TagSource.RULE,
    }
    members = writer.scalars(
        scoped(user, ListMember).where(ListMember.list_id == first_100.id)
    ).all()
    assert [member.contact_id for member in members] == [contact.id]


def test_a_missing_tag_or_list_or_a_smart_list_is_refused_before_anything_is_written(
    writer: Session, user: User
) -> None:
    smart = list_service.create_list(
        writer,
        user,
        "Engineers",
        ListKind.SMART,
        filter=parse_filter(
            {"where": {"op": "contains", "field": "current_title", "value": "engineer"}}
        ),
    )
    for new, field in (
        (NewContact(first_name="Ada", tag_ids=(999,)), "tag_ids"),
        (NewContact(first_name="Ada", list_id=999), "list_id"),
        (NewContact(first_name="Ada", list_id=smart.id), "list_id"),
    ):
        with pytest.raises(Invalid) as caught:
            create_contact(writer, user, new)
        assert caught.value.field == field
    assert writer.scalar(scoped_count(user, Contact)) == 0


def test_another_users_contacts_tags_and_lists_are_not_seen(writer: Session, user: User) -> None:
    other = factories.make_user(writer, kind=UserKind.HOSTED)
    factories.make_contact(writer, other, emails=["shared@example.test"], li_public_id="shared")
    their_tag = tag_service.create_tag(writer, other, "theirs")
    their_list = list_service.create_list(writer, other, "Theirs", ListKind.STATIC)

    # The same address and URL are no duplicate of someone else's contact.
    mine = create_contact(
        writer,
        user,
        NewContact(
            first_name="Ada",
            email="shared@example.test",
            li_url="https://www.linkedin.com/in/shared",
        ),
    )
    assert mine.user_id == user.id
    with pytest.raises(Invalid):
        create_contact(writer, user, NewContact(first_name="B", tag_ids=(their_tag.id,)))
    with pytest.raises(Invalid):
        create_contact(writer, user, NewContact(first_name="B", list_id=their_list.id))


def test_needs_a_writer_session(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        user = factories.make_user(session)
        with pytest.raises(RuntimeError, match="writer"):
            create_contact(session, user, NewContact(first_name="Ada"))


# --- the API ----------------------------------------------------------------


@pytest.fixture
def factory(running_app: FastAPI) -> sessionmaker[Session]:
    made: sessionmaker[Session] = running_app.state.session_factory
    return made


@pytest.fixture
def owner(factory: sessionmaker[Session]) -> User:
    with session_scope(factory) as session:
        found = session.scalars(
            select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
        ).first()
        assert found is not None
        return found


async def _create(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post("/api/v1/contacts", json=body, headers=CSRF)


async def test_post_contacts_creates_and_answers_the_contact(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    with session_scope(factory, write=True) as session:
        user = session.get(User, owner.id)
        assert user is not None
        tag_id = tag_service.create_tag(session, user, "friends").id
        list_id = list_service.create_list(session, user, "Keep warm", ListKind.STATIC).id

    response = await _create(
        client,
        first_name="Wren",
        last_name="Halloway",
        email="Wren@Example.test",
        current_company="Brindle Works",
        current_title="Gardener",
        li_url="https://www.linkedin.com/in/wren-fake/",
        tag_ids=[tag_id],
        list_id=list_id,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["first_name"] == "Wren"
    assert body["source"] == "manual"
    assert body["li_url"] == "https://www.linkedin.com/in/wren-fake/"
    assert [email["email"] for email in body["emails"]] == ["wren@example.test"]
    assert body["field_sources"]["current_company"] == "manual"

    tags = (await client.get(f"/api/v1/contacts/{body['id']}/tags")).json()
    assert [(tag["tag_id"], tag["source"]) for tag in tags] == [(tag_id, "manual")]
    members = (await client.get(f"/api/v1/lists/{list_id}/members")).json()
    assert [row["id"] for row in members["items"]] == [body["id"]]


async def test_post_contacts_answers_409_with_the_existing_contact(
    client: httpx.AsyncClient, factory: sessionmaker[Session], owner: User
) -> None:
    with session_scope(factory, write=True) as session:
        user = session.get(User, owner.id)
        assert user is not None
        existing = factories.make_contact(
            session,
            user,
            first_name="Ada",
            last_name="Quill",
            current_company="Blueleaf",
            li_public_id="ada-quill-fake",
            emails=["ada@example.test"],
        ).id

    by_email = await _create(client, first_name="New", email="ADA@example.test")
    assert by_email.status_code == 409
    assert by_email.json() == {
        "detail": "duplicate",
        "contact_id": existing,
        "contact_ids": [existing],
        "matched_by": "email",
        "archived": False,
    }
    by_url = await _create(client, first_name="New", li_url="www.linkedin.com/in/ADA-QUILL-FAKE/")
    assert by_url.status_code == 409
    assert by_url.json()["matched_by"] == "linkedin"
    by_name = await _create(client, first_name="Ada", last_name="Quill", current_company="Blueleaf")
    assert by_name.status_code == 409
    assert by_name.json()["matched_by"] == "name"
    anyway = await _create(
        client,
        first_name="Ada",
        last_name="Quill",
        current_company="Blueleaf",
        allow_name_match=True,
    )
    assert anyway.status_code == 201

    with session_scope(factory) as session:
        user = session.get(User, owner.id)
        assert user is not None
        assert session.scalar(scoped_count(user, Contact)) == 2


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "first_name"),
        ({"first_name": "Ada", "email": "nope"}, "email"),
        ({"first_name": "Ada", "li_url": "https://example.test/ada"}, "li_url"),
        ({"first_name": "Ada", "tag_ids": [424242]}, "tag_ids"),
        ({"first_name": "Ada", "list_id": 424242}, "list_id"),
    ],
)
async def test_post_contacts_names_the_field_it_refuses(
    client: httpx.AsyncClient, body: dict[str, Any], field: str
) -> None:
    response = await _create(client, **body)
    assert response.status_code == 422, response.text
    [problem] = response.json()["detail"]
    assert problem["loc"] == ["body", field]
    assert problem["msg"]


async def test_post_contacts_refuses_an_unknown_field(client: httpx.AsyncClient) -> None:
    response = await _create(client, first_name="Ada", headline="not taken here")
    assert response.status_code == 422


async def test_post_contacts_needs_the_csrf_header(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/contacts", json={"first_name": "Ada"})
    assert response.status_code == 403


# --- the CLI ----------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    made = make_session_factory(engine)
    install_scope_guard(made)
    with session_scope(made, write=True) as session:
        ensure_local_user(session)
    yield made
    engine.dispose()


def test_cli_contacts_add_creates_with_tags_and_list(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        tag_service.create_tag(session, user, "friends")
        list_service.create_list(session, user, "Keep warm", ListKind.STATIC)

    result = CliRunner().invoke(
        cli,
        [
            "contacts",
            "add",
            "--first-name",
            "Wren",
            "--last-name",
            "Halloway",
            "--email",
            "wren@example.test",
            "--company",
            "Brindle Works",
            "--title",
            "Gardener",
            "--linkedin",
            "https://www.linkedin.com/in/wren-fake",
            "--tag",
            "Friends",
            "--list",
            "Keep warm",
        ],
    )
    assert result.exit_code == 0, result.output
    with session_scope(cli_db) as session:
        user = ensure_local_user(session)
        contact = session.scalars(scoped(user, Contact)).one()
        assert result.stdout == f"added contact {contact.id}: Wren Halloway\n"
        assert contact.source is ContactSource.MANUAL
        assert contact.li_public_id == "wren-fake"
        assert [tag.name for tag in contact.tags] == ["friends"]
        assert session.scalar(scoped_count(user, ListMember)) == 1


def test_cli_contacts_add_refuses_a_duplicate_and_a_bad_value(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        existing = factories.make_contact(
            session,
            user,
            first_name="Ada",
            last_name="Quill",
            current_company="Blueleaf",
            emails=["ada@example.test"],
        ).id

    runner = CliRunner()
    dup = runner.invoke(
        cli, ["contacts", "add", "--first-name", "X", "--email", "ADA@example.test"]
    )
    assert dup.exit_code == 1
    assert f"error: already a contact: {existing}, matched by email" in dup.stderr

    by_name = ["contacts", "add", "--first-name", "Ada", "--last-name", "Quill"]
    by_name += ["--company", "Blueleaf"]
    refused = runner.invoke(cli, by_name)
    assert refused.exit_code == 1
    assert "--allow-name-match" in refused.stderr
    assert runner.invoke(cli, [*by_name, "--allow-name-match"]).exit_code == 0

    bad = runner.invoke(cli, ["contacts", "add", "--email", "ada2@example.test"])
    assert bad.exit_code == 1
    assert "error: first_name: a contact needs a first name or a last name" in bad.stderr

    no_tag = runner.invoke(cli, ["contacts", "add", "--first-name", "Z", "--tag", "nope"])
    assert no_tag.exit_code == 1
    assert "error: no tag 'nope'" in no_tag.stderr

    with session_scope(cli_db) as session:
        user = ensure_local_user(session)
        assert session.scalar(scoped_count(user, Contact)) == 2
