"""``/settings/config`` (#343): settings edited in the web UI instead of config.toml."""

from __future__ import annotations

from dataclasses import replace
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import make_session_factory, session_scope
from netkeeper.models import User
from netkeeper.scoping import install_scope_guard
from netkeeper.services import ui_settings
from netkeeper.services.scheduled_runs import ServeExtractor
from netkeeper.services.settings_kv import set_setting
from netkeeper.services.users import ensure_local_user
from netkeeper.web.app import create_app

CSRF = {"X-Netkeeper-Client": "1"}
URL = "/api/v1/settings/config"
PREFILLS = "linkedin.budget.li_prefills_per_day"


def _field(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(f for f in body["fields"] if f["key"] == key)


async def _put(client: httpx.AsyncClient, values: dict[str, Any]) -> httpx.Response:
    return await client.put(URL, json={"values": values}, headers=CSRF)


async def test_every_field_answers_its_default_with_its_source(client: httpx.AsyncClient) -> None:
    body = (await client.get(URL)).json()
    assert body["config_path"] is None
    prefills = _field(body, PREFILLS)
    assert {k: prefills[k] for k in ("value", "default", "source", "maximum", "warn_above")} == {
        "value": 15,
        "default": 15,
        "source": "default",
        "maximum": 50,
        "warn_above": 20,
    }
    auto = _field(body, "campaigns.linkedin_auto_send")
    assert auto["editable"] is False and auto["locked_reason"]
    assert all(f["key"] for f in body["fields"])


async def test_a_saved_value_is_in_force_on_the_next_request(client: httpx.AsyncClient) -> None:
    saved = await _put(client, {PREFILLS: 25, "linkedin.budget.profile_visits_per_day": 120})
    assert saved.status_code == 200, saved.text
    prefills = _field(saved.json(), PREFILLS)
    assert (prefills["value"], prefills["source"]) == (25, "ui")
    assert prefills["notes"][0].startswith("LinkedIn prefills are set to 25 a day")

    # The rest of the API reads the new values at once, with no restart.
    budget = (await client.get("/api/v1/linkedin/budget")).json()
    limits = {b["action"]: b["day"]["limit"] for b in budget["budgets"]}
    assert limits["li_prefills"] == 25
    assert budget["risk_warning"].startswith("Profile visits are set to 120 a day")
    posture = (await client.get("/api/v1/posture")).json()
    notes = [n for p in posture["protections"] for n in p["notes"]]
    assert any(n.startswith("LinkedIn prefills are set to 25 a day") for n in notes)

    reset = await _put(client, {PREFILLS: None})
    assert _field(reset.json(), PREFILLS)["source"] == "default"


@pytest.mark.parametrize(
    "values",
    [
        {PREFILLS: 51},
        {PREFILLS: 0},
        {"linkedin.budget.profile_visits_per_day": 251},
        {"campaigns.mailbox_daily_cap": 401},
        {"linkedin.active_hours": ["08:00", "08:00"]},
        {"campaigns.linkedin_auto_send": True},
        {"web.port": 1},
    ],
)
async def test_a_refused_value_answers_422_and_stores_nothing(
    client: httpx.AsyncClient, values: dict[str, Any]
) -> None:
    response = await _put(client, {"backup.keep": 3, **values})
    assert response.status_code == 422, response.text
    body = (await client.get(URL)).json()
    assert _field(body, "backup.keep")["source"] == "default"
    assert _field(body, "campaigns.linkedin_auto_send")["value"] is False


async def test_an_empty_put_is_refused(client: httpx.AsyncClient) -> None:
    assert (await _put(client, {})).status_code == 422


async def test_a_write_without_the_csrf_header_is_refused(client: httpx.AsyncClient) -> None:
    assert (await client.put(URL, json={"values": {PREFILLS: 20}})).status_code == 403


async def test_a_key_config_toml_sets_is_locked(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    base = running_app.state.settings
    running_app.state.settings = replace(
        base,
        source_path=Path("/etc/netkeeper.toml"),
        file_keys=frozenset({PREFILLS}),
        linkedin=replace(
            base.linkedin, budget=replace(base.linkedin.budget, li_prefills_per_day=10)
        ),
    )
    refused = await _put(client, {PREFILLS: 30})
    assert refused.status_code == 422
    assert "the file wins" in refused.json()["detail"]
    field = _field((await client.get(URL)).json(), PREFILLS)
    assert (field["value"], field["source"], field["editable"]) == (10, "file", False)
    assert "/etc/netkeeper.toml" in field["locked_reason"]
    limits = (await client.get("/api/v1/linkedin/budget")).json()["budgets"]
    assert {b["action"]: b["day"]["limit"] for b in limits}["li_prefills"] == 10


async def test_the_serve_read_values_apply_without_a_restart(client: httpx.AsyncClient) -> None:
    """#464: serve reads the reply interval and the active hours per user, as it runs."""
    saved = await _put(client, {"campaigns.reply_poll_minutes": 3})
    field = _field(saved.json(), "campaigns.reply_poll_minutes")
    assert (field["applies"], field["restart_pending"]) == ("now", False)
    assert "no restart" in field["applies_note"]


async def test_each_user_sees_and_changes_only_their_own(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other_id = factories.make_user(session).id
    assert (await _put(client, {PREFILLS: 30})).status_code == 200
    with acting_as(running_app, other_id):
        assert _field((await client.get(URL)).json(), PREFILLS)["value"] == 15
        assert (await _put(client, {PREFILLS: 40})).status_code == 200
    assert _field((await client.get(URL)).json(), PREFILLS)["value"] == 30
    with session_scope(factory) as session:
        other = session.get(User, other_id)
        assert other is not None
        assert ui_settings.stored(session, other) == {PREFILLS: 40}


class _Stopped:
    """What the recorded scheduler hands back: nothing to stop."""

    executor = None
    scheduler = None

    def stop(self) -> None:
        return None


class _Monitor:
    instances: list[_Monitor] = []

    def __init__(self, *args: Any, interval_s: float, **kwargs: Any) -> None:
        self.interval_s = interval_s
        self.interval_for = kwargs["interval_for"]
        self.instances.append(self)

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def test_serve_starts_its_scheduler_and_polls_from_each_users_settings_page(
    app: FastAPI, bare_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#343, #464: the scheduler's hours, the monitor's interval and the reply interval
    are each user's own page values, not the first local user's. ``app`` only prepares
    ``bare_engine`` (migrated); the app under test is built here."""
    engine = bare_engine
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        set_setting(session, user, "config.linkedin.active_hours", ["07:15", "19:45"])
        set_setting(session, user, "config.campaigns.reply_poll_minutes", 3)
        other = factories.make_user(session)
        set_setting(session, other, "config.linkedin.active_hours", ["10:00", "16:00"])
        set_setting(session, other, "config.campaigns.reply_poll_minutes", 30)
        user_id, other_id = user.id, other.id
    hours: list[Any] = []

    def scheduler(*args: Any, active_hours: Any = None, **kwargs: Any) -> _Stopped:
        hours.append(active_hours)
        return _Stopped()

    monkeypatch.setattr("netkeeper.web.app.start_serve_scheduler", scheduler)
    monkeypatch.setattr("netkeeper.web.app.MailboxMonitor", _Monitor)
    _Monitor.instances = []
    served = create_app(Settings(), engine=engine, extractor=ServeExtractor(executor=_no_executor))
    async with served.router.lifespan_context(served):
        with session_scope(factory) as session:
            users = {uid: session.get(User, uid) for uid in (user_id, other_id)}
            for each in users.values():
                assert each is not None
                session.expunge(each)
        (provider,) = hours
        assert provider(users[user_id]) == (time(7, 15), time(19, 45))
        assert provider(users[other_id]) == (time(10, 0), time(16, 0))
        (monitor,) = _Monitor.instances
        assert (monitor.interval_for(user_id), monitor.interval_for(other_id)) == (180, 1800)
        sender = served.state.campaign_engine.sender
        assert sender.replies_every_of(user_id) == timedelta(minutes=3)
        assert sender.replies_every_of(other_id) == timedelta(minutes=30)


def _no_executor(factory: Any, bus: Any) -> Any:
    raise AssertionError("the recorded scheduler never builds an executor")


async def test_the_auto_send_budget_warning_reads_the_settings_page(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """#343: with auto-send on in the file, the step options' #447 warning follows the
    page's li_messages_auto value, not the file's."""
    base = running_app.state.settings
    running_app.state.settings = replace(
        base, campaigns=replace(base.campaigns, linkedin_auto_send=True)
    )
    url = "/api/v1/campaigns/linkedin/options"
    assert (await client.get(url)).json()["auto_send_warning"] is None  # 15, the default
    assert (await _put(client, {"linkedin.budget.li_messages_auto_per_day": 25})).status_code == 200
    warning = (await client.get(url)).json()["auto_send_warning"]
    assert warning is not None and warning.startswith(
        "Auto-sent LinkedIn messages are set to 25 a day"
    )
