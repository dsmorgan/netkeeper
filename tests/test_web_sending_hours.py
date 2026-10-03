"""``/settings/sending-hours`` (#338): the global sending hours, read and written."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

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
