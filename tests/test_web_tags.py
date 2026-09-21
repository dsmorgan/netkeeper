"""The tags and auto-tag rules API (spec 14.1): CRUD, tagging, rules, run, preview, CSRF."""

from __future__ import annotations

from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.tags import DEFAULT_FIELDS, DEFAULT_PATTERNS
from netkeeper.db import session_scope
from netkeeper.models import User

CSRF = {"X-Netkeeper-Client": "1"}
DEFAULT_RULE_COUNT = len(DEFAULT_PATTERNS) * len(DEFAULT_FIELDS)


@pytest.fixture
def contact_ids(running_app: FastAPI) -> list[int]:
    """Three contacts of the local user: a VP, a founder, and a barista."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        titles = ["VP of Engineering", "Founder", "Barista"]
        return [
            factories.make_contact(
                session, user, current_title=title, headline=None, current_company=None
            ).id
            for title in titles
        ]


async def _tags_by_name(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    response = await client.get("/api/v1/tags")
    assert response.status_code == 200
    return {tag["name"]: tag for tag in response.json()}


async def test_defaults_are_seeded_at_startup(client: httpx.AsyncClient) -> None:
    tags = await _tags_by_name(client)
    assert set(tags) == {name for name, _ in DEFAULT_PATTERNS}
    assert all(tag["kind"] == "auto" and tag["contact_count"] == 0 for tag in tags.values())
    rules = await client.get("/api/v1/autotag-rules")
    assert rules.status_code == 200
    body = rules.json()
    assert len(body) == DEFAULT_RULE_COUNT
    assert [rule["position"] for rule in body] == list(range(DEFAULT_RULE_COUNT))
    assert body[0]["tag_id"] == tags["c-suite"]["id"]
    assert body[0]["field"] == "title" and body[1]["field"] == "headline"


async def test_state_changing_routes_need_the_csrf_header(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("POST", "/api/v1/tags"),
        ("PATCH", "/api/v1/tags/1"),
        ("DELETE", "/api/v1/tags/1"),
        ("POST", "/api/v1/contacts/1/tags"),
        ("DELETE", "/api/v1/contacts/1/tags/1"),
        ("POST", "/api/v1/autotag-rules"),
        ("POST", "/api/v1/autotag-rules/run"),
        ("POST", "/api/v1/autotag-rules/preview"),
        ("POST", "/api/v1/autotag-rules/reorder"),
        ("POST", "/api/v1/autotag-rules/1/run"),
        ("PATCH", "/api/v1/autotag-rules/1"),
        ("DELETE", "/api/v1/autotag-rules/1"),
    ]:
        response = await client.request(method, path, json={})
        assert response.status_code == 403, (method, path)


async def test_tag_crud(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/tags", json={"name": " Warm ", "color": "#AABBCC"}, headers=CSRF
    )
    assert created.status_code == 201, created.text
    tag = created.json()
    assert {k: tag[k] for k in ("name", "color", "kind", "contact_count")} == {
        "name": "Warm",
        "color": "#aabbcc",
        "kind": "manual",
        "contact_count": 0,
    }
    assert (
        await client.post("/api/v1/tags", json={"name": "warm"}, headers=CSRF)
    ).status_code == 409
    for bad in ({"name": ""}, {"name": "x", "color": "red"}, {"name": "x", "kind": "bogus"}):
        assert (await client.post("/api/v1/tags", json=bad, headers=CSRF)).status_code == 422, bad
    assert (
        await client.post("/api/v1/tags", json={"name": "   "}, headers=CSRF)
    ).status_code == 422

    url = f"/api/v1/tags/{tag['id']}"
    renamed = await client.patch(url, json={"name": "Hot"}, headers=CSRF)
    assert renamed.status_code == 200 and renamed.json()["color"] == "#aabbcc"
    cleared = await client.patch(url, json={"color": None}, headers=CSRF)
    assert cleared.status_code == 200 and cleared.json()["color"] is None
    assert cleared.json()["name"] == "Hot"
    assert (await client.patch(url, json={"name": "VP"}, headers=CSRF)).status_code == 409
    assert (await client.patch(url, json={"color": "nope"}, headers=CSRF)).status_code == 422
    assert "Hot" in await _tags_by_name(client)

    assert (await client.delete(url, headers=CSRF)).status_code == 204
    assert (await client.delete(url, headers=CSRF)).status_code == 404
    assert (await client.patch(url, json={"name": "x"}, headers=CSRF)).status_code == 404
    assert "Hot" not in await _tags_by_name(client)


async def test_tag_and_untag_a_contact(client: httpx.AsyncClient, contact_ids: list[int]) -> None:
    vp_id = (await _tags_by_name(client))["vp"]["id"]
    contact_id = contact_ids[2]
    url = f"/api/v1/contacts/{contact_id}/tags"
    created = await client.post(url, json={"tag_id": vp_id}, headers=CSRF)
    assert created.status_code == 201, created.text
    body = created.json()
    assert {k: body[k] for k in ("contact_id", "tag_id", "source", "rule_id")} == {
        "contact_id": contact_id,
        "tag_id": vp_id,
        "source": "manual",
        "rule_id": None,
    }
    again = await client.post(url, json={"tag_id": vp_id}, headers=CSRF)
    assert again.status_code == 201 and again.json()["id"] == body["id"]
    assert (await _tags_by_name(client))["vp"]["contact_count"] == 1
    assert (await client.post(url, json={"tag_id": vp_id + 1000}, headers=CSRF)).status_code == 404
    assert (
        await client.post("/api/v1/contacts/999999/tags", json={"tag_id": vp_id}, headers=CSRF)
    ).status_code == 404

    assert (await client.delete(f"{url}/{vp_id}", headers=CSRF)).status_code == 204
    assert (await client.delete(f"{url}/{vp_id}", headers=CSRF)).status_code == 404
    assert (await _tags_by_name(client))["vp"]["contact_count"] == 0


async def test_rules_crud_reorder_run_and_preview(
    client: httpx.AsyncClient, contact_ids: list[int]
) -> None:
    vp_id = (await _tags_by_name(client))["vp"]["id"]
    _, founder_contact, barista = contact_ids

    preview = await client.post(
        "/api/v1/autotag-rules/preview",
        json={"field": "title", "pattern": r"founder|barista"},
        headers=CSRF,
    )
    assert preview.status_code == 200
    assert preview.json() == {"count": 2, "contact_ids": [founder_contact, barista], "timeouts": 0}
    bad = await client.post(
        "/api/v1/autotag-rules/preview", json={"field": "title", "pattern": "("}, headers=CSRF
    )
    assert bad.status_code == 422
    assert "regular expression" in bad.text and bad.json()["detail"][0]["loc"] == [
        "body",
        "pattern",
    ]

    run = await client.post("/api/v1/autotag-rules/run", headers=CSRF)
    assert run.status_code == 200
    assert run.json() == {"contacts": 3, "added": 3, "removed": 0, "updated": 0, "timeouts": 0}
    tags = await _tags_by_name(client)
    assert (tags["vp"]["contact_count"], tags["engineering"]["contact_count"]) == (1, 1)
    assert tags["founder"]["contact_count"] == 1

    created = await client.post(
        "/api/v1/autotag-rules",
        json={"tag_id": vp_id, "field": "title", "pattern": r"\bbarista\b", "enabled": False},
        headers=CSRF,
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert (rule["position"], rule["enabled"]) == (DEFAULT_RULE_COUNT, False)
    for bad_body in (
        {"tag_id": vp_id, "field": "title", "pattern": "("},
        {"tag_id": vp_id, "field": "nope", "pattern": "x"},
        {"tag_id": vp_id, "field": "title", "pattern": ""},
    ):
        response = await client.post("/api/v1/autotag-rules", json=bad_body, headers=CSRF)
        assert response.status_code == 422, bad_body
    missing_tag = await client.post(
        "/api/v1/autotag-rules",
        json={"tag_id": vp_id + 1000, "field": "title", "pattern": "x"},
        headers=CSRF,
    )
    assert missing_tag.status_code == 404

    url = f"/api/v1/autotag-rules/{rule['id']}"
    single = await client.post(f"{url}/run", headers=CSRF)
    assert single.status_code == 200 and single.json()["added"] == 0  # disabled: matches nothing
    enabled = await client.patch(url, json={"enabled": True}, headers=CSRF)
    assert enabled.status_code == 200 and enabled.json()["enabled"] is True
    assert (await client.patch(url, json={"pattern": "["}, headers=CSRF)).status_code == 422
    single = await client.post(f"{url}/run", headers=CSRF)
    assert single.json() == {"contacts": 3, "added": 1, "removed": 0, "updated": 0, "timeouts": 0}
    assert (await _tags_by_name(client))["vp"]["contact_count"] == 2

    reordered = await client.post(
        "/api/v1/autotag-rules/reorder", json={"rule_ids": [rule["id"]]}, headers=CSRF
    )
    assert reordered.status_code == 200
    positions = [(r["id"], r["position"]) for r in reordered.json()]
    assert positions[0] == (rule["id"], 0) and len(positions) == DEFAULT_RULE_COUNT + 1
    assert (
        await client.post(
            "/api/v1/autotag-rules/reorder", json={"rule_ids": [999999]}, headers=CSRF
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/autotag-rules/reorder",
            json={"rule_ids": [rule["id"], rule["id"]]},
            headers=CSRF,
        )
    ).status_code == 422

    assert (await client.delete(url, headers=CSRF)).status_code == 204
    assert (await client.delete(url, headers=CSRF)).status_code == 404
    assert (await client.post(f"{url}/run", headers=CSRF)).status_code == 404
    listed = await client.get("/api/v1/autotag-rules")
    assert len(listed.json()) == DEFAULT_RULE_COUNT
    # The deleted rule's assignment goes at the next full run.
    run = await client.post("/api/v1/autotag-rules/run", headers=CSRF)
    assert run.json() == {"contacts": 3, "added": 0, "removed": 1, "updated": 0, "timeouts": 0}


async def test_removing_an_auto_tag_keeps_it_off_until_added_by_hand(
    client: httpx.AsyncClient, contact_ids: list[int]
) -> None:
    vp_contact = contact_ids[0]
    vp_id = (await _tags_by_name(client))["vp"]["id"]
    await client.post("/api/v1/autotag-rules/run", headers=CSRF)
    assert (await _tags_by_name(client))["vp"]["contact_count"] == 1
    url = f"/api/v1/contacts/{vp_contact}/tags"
    assert (await client.delete(f"{url}/{vp_id}", headers=CSRF)).status_code == 204
    run = await client.post("/api/v1/autotag-rules/run", headers=CSRF)
    assert run.json()["added"] == 0
    assert (await _tags_by_name(client))["vp"]["contact_count"] == 0
    manual = await client.post(url, json={"tag_id": vp_id}, headers=CSRF)
    assert manual.status_code == 201 and manual.json()["source"] == "manual"
    run = await client.post("/api/v1/autotag-rules/run", headers=CSRF)
    assert run.json() == {"contacts": 3, "added": 0, "removed": 0, "updated": 0, "timeouts": 0}
    assert (await _tags_by_name(client))["vp"]["contact_count"] == 1
