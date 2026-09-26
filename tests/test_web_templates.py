"""The templates API (spec 8.5, 11.1; item P3-03): CRUD, lint, preview, CSRF, other users."""

from __future__ import annotations

from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import templates as service
from netkeeper.db import session_scope
from netkeeper.models import TemplateChannel, User, UserKind

CSRF = {"X-Netkeeper-Client": "1"}
EMAIL = {"name": "reconnect", "channel": "email", "subject": "Hi {{ first_name }}",
         "body": "Hi {{ first_name }}, {{ me.name }} here."}  # fmt: skip


def _factory(app: FastAPI) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = app.state.session_factory
    return factory


async def _create(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/templates", json={**EMAIL, **fields}, headers=CSRF)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_create_list_get_update_delete(client: httpx.AsyncClient) -> None:
    created = await _create(client)
    assert {k: created[k] for k in ("name", "channel", "subject", "body", "version")} == {
        **EMAIL,
        "version": 1,
    }
    assert (created["previous_id"], created["current"], created["lint"]) == (None, True, [])
    url = f"/api/v1/templates/{created['id']}"

    listed = (await client.get("/api/v1/templates")).json()
    assert [row["id"] for row in listed] == [created["id"]]
    assert (await client.get(url)).json() == created

    patched = await client.patch(url, json={"body": "Yo {{ nickname }}"}, headers=CSRF)
    assert patched.status_code == 200, patched.text
    assert patched.json()["id"] == created["id"] and patched.json()["subject"] == EMAIL["subject"]
    assert [(i["rule"], i["severity"], i["part"], i["field"]) for i in patched.json()["lint"]] == [
        ("undefined_variable", "error", "body", "nickname"),
        ("no_contact_field", "error", "body", None),
    ]

    cleared = await client.patch(url, json={"subject": None}, headers=CSRF)
    assert cleared.json()["subject"] is None
    assert "missing_subject" in {i["rule"] for i in cleared.json()["lint"]}

    assert (await client.delete(url, headers=CSRF)).status_code == 204
    assert (await client.get(url)).status_code == 404
    assert (await client.get("/api/v1/templates")).json() == []


async def test_lint_errors_are_reported_not_refused(client: httpx.AsyncClient) -> None:
    created = await _create(client, subject=None, body="{{ me.__class__ }}")
    assert {i["rule"] for i in created["lint"]} == {
        "missing_subject",
        "unsafe_attribute",
        "no_contact_field",
    }


async def test_me_fields_come_from_the_config(client: httpx.AsyncClient) -> None:
    """The test app runs on default settings, so [me] has only the five standard keys."""
    created = await _create(client, body="{{ first_name }} {{ me.podcast }} {{ me.city }}")
    assert [i["field"] for i in created["lint"]] == ["me.podcast"]


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"name": ""}, 422),
        ({"name": "   "}, 422),  # the service's check, after trimming
        ({"channel": "fax"}, 422),
        ({"subject": "s" * 501}, 422),
        ({"body": "b" * 20_001}, 422),
    ],
)
async def test_values_that_cannot_be_stored(
    client: httpx.AsyncClient, fields: dict[str, Any], status: int
) -> None:
    response = await client.post("/api/v1/templates", json={**EMAIL, **fields}, headers=CSRF)
    assert response.status_code == status, response.text


async def test_duplicate_names_conflict(client: httpx.AsyncClient) -> None:
    await _create(client)
    response = await client.post("/api/v1/templates", json=EMAIL, headers=CSRF)
    assert response.status_code == 409
    other = await _create(client, name="other")
    renamed = await client.patch(
        f"/api/v1/templates/{other['id']}", json={"name": EMAIL["name"]}, headers=CSRF
    )
    assert renamed.status_code == 409


async def test_an_edit_of_a_template_in_use_answers_with_the_new_version(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await _create(client)
    monkeypatch.setattr(service, "is_in_use", lambda _session, _user, row: row.id == created["id"])
    url = f"/api/v1/templates/{created['id']}"
    patched = (await client.patch(url, json={"body": "Hey {{ first_name }}"}, headers=CSRF)).json()
    assert patched["id"] != created["id"]
    assert (patched["version"], patched["previous_id"], patched["current"]) == (
        2,
        created["id"],
        True,
    )

    old = (await client.get(url)).json()
    assert (old["body"], old["current"]) == (EMAIL["body"], False)
    assert [row["id"] for row in (await client.get("/api/v1/templates")).json()] == [patched["id"]]
    assert (await client.patch(url, json={"body": "x"}, headers=CSRF)).status_code == 409
    assert (await client.delete(url, headers=CSRF)).status_code == 409
    newest = f"/api/v1/templates/{patched['id']}"
    assert (await client.delete(newest, headers=CSRF)).status_code == 409  # still in use


async def test_a_concurrent_edit_of_a_version_in_use_is_409_not_500(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await _create(client)
    monkeypatch.setattr(service, "is_in_use", lambda _session, _user, row: row.id == created["id"])
    url = f"/api/v1/templates/{created['id']}"
    first = await client.patch(url, json={"body": "Hey {{ first_name }}"}, headers=CSRF)
    assert first.status_code == 200
    # A second edit that read the row before the first one's insert saw no successor.
    monkeypatch.setattr(service, "is_superseded", lambda _session, _user, _row: False)
    second = await client.patch(url, json={"body": "Yo {{ first_name }}"}, headers=CSRF)
    assert second.status_code == 409
    assert "replaced by another edit" in second.json()["detail"]


async def test_preview(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    created = await _create(client, body="Hi {{ first_name }} at {{ company }}.")
    with session_scope(_factory(running_app), write=True) as session:
        user = session.scalars(select(User)).one()
        contact_id = factories.make_contact(
            session, user, preferred_name="Bo", current_company=None
        ).id
    response = await client.get(
        f"/api/v1/templates/{created['id']}/preview", params={"contact_id": contact_id}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "subject": "Hi Bo",
        "body": "Hi Bo at .",
        "issues": [
            {
                "rule": "missing_value",
                "severity": "warning",
                "part": "body",
                "message": "`company` has no value here, so it renders empty",
                "field": "company",
            }
        ],
    }
    missing = await client.get(
        f"/api/v1/templates/{created['id']}/preview", params={"contact_id": contact_id + 999}
    )
    assert missing.status_code == 404


async def test_preview_of_a_sandbox_escape_is_422(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    created = await _create(client, body="{{ first_name }}{{ ''.__class__ }}")
    with session_scope(_factory(running_app), write=True) as session:
        user = session.scalars(select(User)).one()
        contact_id = factories.make_contact(session, user).id
    response = await client.get(
        f"/api/v1/templates/{created['id']}/preview", params={"contact_id": contact_id}
    )
    assert response.status_code == 422
    assert "_ are refused" in response.json()["detail"]


async def test_another_users_template_and_contact_are_404(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    mine = await _create(client)
    with session_scope(_factory(running_app), write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        theirs = service.create_template(
            session,
            other,
            name="theirs",
            channel=TemplateChannel.LINKEDIN,
            subject=None,
            body="{{ first_name }}",
            me_keys=(),
        ).id
        their_contact = factories.make_contact(session, other).id
    url = f"/api/v1/templates/{theirs}"
    assert (await client.get(url)).status_code == 404
    assert (await client.patch(url, json={"body": "x"}, headers=CSRF)).status_code == 404
    assert (await client.delete(url, headers=CSRF)).status_code == 404
    assert (
        await client.get(f"{url}/preview", params={"contact_id": their_contact})
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/templates/{mine['id']}/preview", params={"contact_id": their_contact}
        )
    ).status_code == 404


async def test_state_changing_routes_need_the_csrf_header(client: httpx.AsyncClient) -> None:
    created = await _create(client)
    url = f"/api/v1/templates/{created['id']}"
    assert (await client.post("/api/v1/templates", json=EMAIL)).status_code == 403
    assert (await client.patch(url, json={"body": "x"})).status_code == 403
    assert (await client.delete(url)).status_code == 403
