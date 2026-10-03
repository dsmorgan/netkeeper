"""``/settings/sending-hours`` (#338): the global sending hours, read and written."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import User, UserKind
from netkeeper.services import sending_hours
from netkeeper.services.settings_kv import set_setting

CSRF = {"X-Netkeeper-Client": "1"}
URL = "/api/v1/settings/sending-hours"


async def test_the_default_is_monday_to_friday_nine_to_five(client: httpx.AsyncClient) -> None:
    body = (await client.get(URL)).json()
    assert {k: body[k] for k in ("enabled", "days", "start", "end")} == {
        "enabled": True,
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "start": "09:00",
        "end": "17:00",
    }
    assert body["summary"] == "Mon to Fri, 09:00 to 17:00"
    assert body["timezone"]
    assert body["readable"] is True


async def test_new_hours_are_stored_and_read_back(client: httpx.AsyncClient) -> None:
    new = {"enabled": True, "days": ["Sat", "Tue"], "start": "08:30", "end": "12:00"}
    saved = await client.put(URL, json=new, headers=CSRF)
    assert saved.status_code == 200, saved.text
    assert saved.json()["days"] == ["Tue", "Sat"]
    assert saved.json()["summary"] == "Tue, Sat, 08:30 to 12:00"
    assert (await client.get(URL)).json()["days"] == ["Tue", "Sat"]

    off = {"enabled": False, "days": ["Mon"], "start": "09:00", "end": "17:00"}
    assert (await client.put(URL, json=off, headers=CSRF)).json()["summary"] == "any time"


@pytest.mark.parametrize(
    "body",
    [
        {"enabled": True, "days": [], "start": "09:00", "end": "17:00"},
        {"enabled": True, "days": ["Funday"], "start": "09:00", "end": "17:00"},
        {"enabled": True, "days": ["Mon"], "start": "17:00", "end": "09:00"},
        {"enabled": True, "days": ["Mon"], "start": "09:00", "end": "09:00"},
        {"enabled": True, "days": ["Mon"], "start": "9:00", "end": "17:00"},
        {"enabled": True, "days": ["Mon"], "start": "09:00", "end": "24:00"},
        {"days": ["Mon"], "start": "09:00", "end": "17:00"},
    ],
)
async def test_hours_that_cannot_be_used_are_refused(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.put(URL, json=body, headers=CSRF)
    assert response.status_code == 422, response.text
    assert (await client.get(URL)).json()["summary"] == "Mon to Fri, 09:00 to 17:00"


async def test_a_write_without_the_csrf_header_is_refused(client: httpx.AsyncClient) -> None:
    body = {"enabled": False, "days": ["Mon"], "start": "09:00", "end": "17:00"}
    assert (await client.put(URL, json=body)).status_code == 403
    assert (await client.get(URL)).json()["enabled"] is True


async def test_an_unreadable_stored_value_answers_the_defaults_and_a_save_repairs_it(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """#338 review S2: the Settings page can always show the form and fix the value."""
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()
        set_setting(session, user, sending_hours.KEY, {"enabled": "yes"})
    broken = await client.get(URL)
    assert broken.status_code == 200
    assert broken.json()["readable"] is False
    assert broken.json()["summary"] == "Mon to Fri, 09:00 to 17:00"
    body = {"enabled": True, "days": ["Mon", "Tue"], "start": "10:00", "end": "16:00"}
    assert (await client.put(URL, json=body, headers=CSRF)).status_code == 200
    fixed = (await client.get(URL)).json()
    assert fixed["readable"] is True and fixed["summary"] == "Mon, Tue, 10:00 to 16:00"
