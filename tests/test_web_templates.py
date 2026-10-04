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
from netkeeper.models import CampaignStatus, TemplateChannel, User, UserKind

CSRF = {"X-Netkeeper-Client": "1"}
EMAIL = {"name": "reconnect", "channel": "email", "subject": "Hi {{ first_name }}",
         "body": "Hi {{ first_name }}, Ada here."}  # fmt: skip


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
    created = await _create(client, subject=None, body="{{ step.__class__ }}")
    assert {i["rule"] for i in created["lint"]} == {
        "missing_subject",
        "unsafe_attribute",
        "no_contact_field",
    }


async def test_a_me_field_is_a_removed_field_lint_error(client: httpx.AsyncClient) -> None:
    """#320, #342: the save goes through, and lint says what changed for each ``me.*``."""
    created = await _create(client, body="{{ first_name }} {{ me.podcast }} {{ me.city }}")
    assert [(i["rule"], i["field"]) for i in created["lint"]] == [
        ("removed_field", "me.podcast"),
        ("removed_field", "me.city"),
    ]
    assert all("were removed" in i["message"] for i in created["lint"])


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


def _use(app: FastAPI, template_id: int, status: CampaignStatus) -> None:
    """A campaign in ``status`` whose one step sends template ``template_id``."""
    with session_scope(_factory(app), write=True) as session:
        user = session.scalars(select(User)).one()
        campaign = factories.make_campaign(session, user, status=status)
        campaign.steps[0].template_id = template_id


def _in_use(rows: list[dict[str, Any]], *names: str) -> dict[str, bool]:
    """``in_use`` by name, for ``names`` only: the campaign factory adds templates of its own."""
    return {row["name"]: row["in_use"] for row in rows if row["name"] in names}


async def test_in_use_says_which_templates_a_campaign_past_draft_sends(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    sent = await _create(client, name="sent")
    drafted = await _create(client, name="drafted")
    free = await _create(client, name="free")
    assert (sent["in_use"], drafted["in_use"], free["in_use"]) == (False, False, False)
    _use(running_app, sent["id"], CampaignStatus.ACTIVE)
    _use(running_app, drafted["id"], CampaignStatus.DRAFT)

    listed = (await client.get("/api/v1/templates")).json()
    assert _in_use(listed, "sent", "drafted", "free") == {
        "drafted": False,
        "free": False,
        "sent": True,
    }
    for row, expected in ((sent, True), (drafted, False), (free, False)):
        assert (await client.get(f"/api/v1/templates/{row['id']}")).json()["in_use"] is expected


async def test_in_use_moves_to_the_old_version_when_an_edit_makes_a_new_one(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    created = await _create(client)
    _use(running_app, created["id"], CampaignStatus.PAUSED)
    url = f"/api/v1/templates/{created['id']}"

    patched = (await client.patch(url, json={"body": "Hey {{ first_name }}"}, headers=CSRF)).json()
    assert (patched["previous_id"], patched["in_use"]) == (created["id"], False)
    old = (await client.get(url)).json()
    assert (old["current"], old["in_use"]) == (False, True)
    listed = (await client.get("/api/v1/templates")).json()
    assert _in_use(listed, EMAIL["name"]) == {EMAIL["name"]: False}

    # The new version is not in use, so the next edit changes it in place.
    again = f"/api/v1/templates/{patched['id']}"
    edited = (await client.patch(again, json={"body": "Yo {{ first_name }}"}, headers=CSRF)).json()
    assert (edited["id"], edited["in_use"]) == (patched["id"], False)


async def test_in_use_ignores_another_users_campaign(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    created = await _create(client)
    with session_scope(_factory(running_app), write=True) as session:
        stranger = factories.make_user(session, kind=UserKind.HOSTED)
        campaign = factories.make_campaign(session, stranger)
        # A cross-user reference the service never makes; the scoped query must ignore it.
        campaign.steps[0].template_id = created["id"]
    listed = (await client.get("/api/v1/templates")).json()
    assert _in_use(listed, EMAIL["name"]) == {EMAIL["name"]: False}
    assert (await client.get(f"/api/v1/templates/{created['id']}")).json()["in_use"] is False


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
                "line": 1,
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
    assert (await client.post("/api/v1/templates/lint", json=EMAIL)).status_code == 403


async def test_lint_of_unsaved_text_matches_what_a_save_would_store(
    client: httpx.AsyncClient,
) -> None:
    """P3-10: the editor lints as you type, so an error shows before you save."""
    draft = {"channel": "email", "subject": "   ", "body": "{% for x in y %}{% endfor %}"}
    response = await client.post("/api/v1/templates/lint", json=draft, headers=CSRF)
    assert response.status_code == 200, response.text
    linted = response.json()
    assert [(i["rule"], i["part"], i["field"]) for i in linted] == [
        ("missing_subject", "subject", None),
        ("unsupported", "body", "for"),
        ("no_contact_field", "body", None),
    ]
    assert linted[1]["message"].startswith("line 1: ")
    assert (await client.get("/api/v1/templates")).json() == []  # nothing was saved

    saved = await _create(client, **draft)
    assert saved["lint"] == linted

    clean = {"channel": "linkedin", "body": "Hi {{ first_name }}"}
    assert (await client.post("/api/v1/templates/lint", json=clean, headers=CSRF)).json() == []


async def test_lint_of_unsaved_text_refuses_what_a_save_would(client: httpx.AsyncClient) -> None:
    for draft in (
        {"channel": "fax", "body": "x"},
        {"channel": "email", "subject": "s" * 501, "body": "x"},
        {"channel": "email", "body": "b" * 20_001},
    ):
        response = await client.post("/api/v1/templates/lint", json=draft, headers=CSRF)
        assert response.status_code == 422, draft


# --- the editor's field list and lint lines (#344) ---------------------------------------


async def test_merge_fields_without_a_contact_are_invented_placeholders(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/templates/merge-fields")
    assert response.status_code == 200, response.text
    listed = response.json()
    assert listed["contact_id"] is None
    by_name = {f["name"]: f for f in listed["fields"]}
    assert list(by_name)[:2] == ["first_name", "last_name"]
    assert by_name["first_name"] == {
        "name": "first_name",
        "group": "contact",
        "description": by_name["first_name"]["description"],
        "insert": "first_name",
        "example": "Alex",
        "example_source": "placeholder",
    }
    assert by_name["previous_send_date"]["insert"] == "previous_send_date | ago"
    assert {f["example_source"] for f in listed["fields"]} == {"placeholder"}
    assert all(f["example"] and f["description"] for f in listed["fields"])


async def test_merge_fields_with_a_contact_show_its_values_and_no_me_fields(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        user = session.scalars(select(User)).one()
        contact_id = factories.make_contact(
            session, user, preferred_name="Bo", current_company="Fixture Co", location=None
        ).id
    response = await client.get("/api/v1/templates/merge-fields", params={"contact_id": contact_id})
    assert response.status_code == 200, response.text
    listed = response.json()
    assert listed["contact_id"] == contact_id
    shown = {f["name"]: (f["example"], f["example_source"]) for f in listed["fields"]}
    assert shown["first_name"] == ("Bo", "contact")
    assert shown["company"] == ("Fixture Co", "contact")
    assert shown["location"] == (None, "contact")  # renders empty for this contact
    assert not [name for name in shown if name.startswith("me")]  # #320, #342
    assert shown["campaign.name"] == ("Example campaign", "placeholder")
    assert shown["personal_line"][1] == "placeholder"


async def test_merge_fields_for_another_users_or_no_contact_is_404(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    with session_scope(_factory(running_app), write=True) as session:
        other = factories.make_user(session, kind=UserKind.HOSTED)
        their_contact = factories.make_contact(session, other, preferred_name="Theirs").id
    for contact_id in (their_contact, their_contact + 999):
        response = await client.get(
            "/api/v1/templates/merge-fields", params={"contact_id": contact_id}
        )
        assert response.status_code == 404
        assert "Theirs" not in response.text


async def test_lint_reports_the_line_of_each_finding(client: httpx.AsyncClient) -> None:
    draft = {
        "channel": "email",
        "subject": "Hi {{ frist_name }}",
        "body": "Hi {{ first_name }}\n\n{{ compnay }}\nsee http:/broken",
    }
    response = await client.post("/api/v1/templates/lint", json=draft, headers=CSRF)
    assert response.status_code == 200, response.text
    assert [(i["part"], i["field"], i["line"]) for i in response.json()] == [
        ("subject", "frist_name", 1),
        ("body", "compnay", 3),
        ("body", "http:/broken", 4),
    ]
