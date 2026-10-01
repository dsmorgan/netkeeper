"""``/gmail-setup``: the guided Gmail setup's progress (#302)."""

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services import gmail_setup
from netkeeper.services.settings_kv import set_setting

CSRF = {"X-Netkeeper-Client": "1"}
EMPTY = {
    "project_id": None,
    "sender_email": None,
    "done": [],
    "steps": ["project", "gmail_api", "branding", "test_user", "client_created", "published"],
}


async def _put(client: httpx.AsyncClient, **body: object) -> httpx.Response:
    return await client.put("/api/v1/gmail-setup", json=body, headers=CSRF)


def test_the_steps_are_pinned() -> None:
    assert gmail_setup.MANUAL_STEPS == (
        "project",
        "gmail_api",
        "branding",
        "test_user",
        "client_created",
        "published",
    )
    assert gmail_setup.SETTING_KEY == "gmail.setup"


async def test_nothing_is_set_up_at_first(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/gmail-setup")
    assert response.status_code == 200
    assert response.json() == EMPTY


async def test_progress_is_stored_normalized_and_in_order(client: httpx.AsyncClient) -> None:
    response = await _put(
        client,
        project_id="  Netkeeper-ab12cd ",
        sender_email=" me@example.com ",
        done=["test_user", "project", "project"],
    )
    assert response.status_code == 200, response.text
    expected = {
        **EMPTY,
        "project_id": "netkeeper-ab12cd",
        "sender_email": "me@example.com",
        "done": ["project", "test_user"],
    }
    assert response.json() == expected
    assert (await client.get("/api/v1/gmail-setup")).json() == expected


async def test_a_put_replaces_and_can_clear(client: httpx.AsyncClient) -> None:
    await _put(client, project_id="netkeeper-ab12cd", done=["project"])
    response = await _put(client, project_id="", sender_email=None, done=[])
    assert response.json() == EMPTY


async def test_a_put_needs_the_csrf_header(client: httpx.AsyncClient) -> None:
    response = await client.put("/api/v1/gmail-setup", json={"project_id": "netkeeper-ab12cd"})
    assert response.status_code == 403
    assert (await client.get("/api/v1/gmail-setup")).json() == EMPTY


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"project_id": "short"}, "6 to 30"),
        ({"project_id": "1netkeeper"}, "starting with a letter"),
        ({"project_id": "netkeeper-"}, "not ending with a hyphen"),
        ({"project_id": "net keeper"}, "lowercase"),
        ({"project_id": "netkeeper;rm-rf"}, "lowercase"),
        ({"project_id": "a" * 31}, "6 to 30"),
        ({"sender_email": "not-an-address"}, "email address"),
        ({"done": ["project", "launch_rockets"]}, "launch_rockets"),
    ],
)
async def test_unusable_values_are_refused(
    client: httpx.AsyncClient, body: dict[str, object], fragment: str
) -> None:
    response = await _put(client, **body)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert (await client.get("/api/v1/gmail-setup")).json() == EMPTY


async def test_each_user_sees_only_their_own(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session)
        gmail_setup.save(
            session,
            other,
            gmail_setup.GmailSetup(
                project_id="their-project", sender_email="them@example.com", done=("project",)
            ),
        )
        other_id = other.id
    assert (await client.get("/api/v1/gmail-setup")).json() == EMPTY

    await _put(client, project_id="my-project", done=["branding"])
    with session_scope(factory) as session:
        other = session.get_one(User, other_id)
        assert gmail_setup.load(session, other) == gmail_setup.GmailSetup(
            project_id="their-project", sender_email="them@example.com", done=("project",)
        )


async def test_a_stored_value_that_no_longer_fits_degrades(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        set_setting(
            session,
            user,
            gmail_setup.SETTING_KEY,
            {"project_id": "BAD ID!", "sender_email": 7, "done": ["project", "retired_step"]},
        )
    body = (await client.get("/api/v1/gmail-setup")).json()
    assert body == {**EMPTY, "done": ["project"]}

    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        set_setting(session, user, gmail_setup.SETTING_KEY, "not a dict")
    assert (await client.get("/api/v1/gmail-setup")).json() == EMPTY
