"""``/mailboxes``: the Gmail OAuth flow and mailbox health over the API (#244, P3-01)."""

import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import factories
import httpx
import pytest
from fastapi import FastAPI
from gmail_fakes import CLIENT_ID, CLIENT_SECRET, FAKE_EMAIL, FakeGoogle, MemoryKeyring
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import CampaignSettings, Settings
from netkeeper.db import DATABASE_FILENAME, session_scope
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.events import EventBus, Subscription
from netkeeper.services.scheduled_runs import ServeExtractor
from netkeeper.web.app import create_app

CSRF = {"X-Netkeeper-Client": "1"}
CALLBACK = "http://127.0.0.1/api/v1/mailboxes/oauth/callback"


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


async def _set_client(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/api/v1/mailboxes/oauth/client",
        json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text


async def _start(client: httpx.AsyncClient, mailbox_id: int | None = None) -> str:
    response = await client.post(
        "/api/v1/mailboxes/oauth/start", json={"mailbox_id": mailbox_id}, headers=CSRF
    )
    assert response.status_code == 200, response.text
    url: str = response.json()["authorization_url"]
    return url


async def _follow(client: httpx.AsyncClient, redirect: str) -> dict[str, str]:
    """Be the browser Google sends back: no CSRF header, just the navigation."""
    parts = urlsplit(redirect)
    response = await client.get(f"{parts.path}?{parts.query}")
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/settings?")
    return _query(location)


async def _connect(
    client: httpx.AsyncClient, fake: FakeGoogle, email: str = FAKE_EMAIL
) -> dict[str, str]:
    return await _follow(client, fake.consent(await _start(client), email=email))


async def _status(client: httpx.AsyncClient) -> dict[str, object]:
    response = await client.get("/api/v1/mailboxes/status")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def _drain(bus: EventBus, subscription: Subscription) -> list[tuple[str, dict[str, object]]]:
    bus.unsubscribe(subscription)
    events = []
    while not subscription._queue.empty():
        event = subscription._queue.get_nowait()
        if event is not None:
            events.append((event.type, event.data))
    return events


# --- the client ---------------------------------------------------------------------


async def test_nothing_is_set_up_at_first(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    assert running_app.state.mailbox_monitor is None  # only serve polls
    assert await _status(client) == {
        "client_configured": False,
        "client_id": None,
        "mailboxes": [],
        "reauth_required": False,
    }
    assert (await client.get("/api/v1/mailboxes")).json() == []


async def test_the_client_goes_to_the_keychain(
    client: httpx.AsyncClient, memory_keyring: MemoryKeyring
) -> None:
    response = await client.put(
        "/api/v1/mailboxes/oauth/client",
        json={"client_id": f" {CLIENT_ID} ", "client_secret": CLIENT_SECRET},
        headers=CSRF,
    )
    assert response.status_code == 200
    status = await _status(client)
    assert response.json() == status
    assert (status["client_configured"], status["client_id"]) == (True, CLIENT_ID)
    [(key, value)] = memory_keyring.entries.items()
    assert key[1].endswith("/gmail/oauth_client")
    assert CLIENT_SECRET in value


async def test_a_bad_client_id_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.put(
        "/api/v1/mailboxes/oauth/client",
        json={"client_id": "my-project", "client_secret": CLIENT_SECRET},
        headers=CSRF,
    )
    assert response.status_code == 422
    assert "apps.googleusercontent.com" in response.json()["detail"]


async def test_a_locked_keychain_answers_503(
    client: httpx.AsyncClient, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.broken = True
    response = await client.put(
        "/api/v1/mailboxes/oauth/client",
        json={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        headers=CSRF,
    )
    assert response.status_code == 503
    assert CLIENT_SECRET not in response.text


async def test_starting_without_a_client_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/mailboxes/oauth/start", json={}, headers=CSRF)
    assert response.status_code == 409
    assert "gmail-setup" in response.json()["detail"]


# --- the flow -----------------------------------------------------------------------


async def test_authorizing_connects_the_mailbox_and_tells_the_page(
    client: httpx.AsyncClient, running_app: FastAPI, fake_google: FakeGoogle
) -> None:
    bus: EventBus = running_app.state.bus
    subscription = bus.subscribe()
    await _set_client(client)
    url = await _start(client)
    assert url.startswith(f"{fake_google.base}/auth?")
    assert _query(url)["redirect_uri"] == CALLBACK

    assert await _connect(client, fake_google, "Sender@Example.com") == {"gmail": "connected"}

    status = await _status(client)
    mailboxes: list[dict[str, object]] = status["mailboxes"]  # type: ignore[assignment]
    [mailbox] = mailboxes
    assert mailbox["email"] == "sender@example.com"
    assert mailbox["status"] == "ok"
    assert mailbox["daily_cap"] == 80
    assert status["reauth_required"] is False
    assert _drain(bus, subscription) == [
        ("mailbox.status", {"mailbox_id": mailbox["id"], "status": "ok", "reason": None})
    ]


async def test_the_daily_cap_comes_from_the_config(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_google: FakeGoogle,
) -> None:
    app = _app(
        bare_engine,
        tmp_path,
        _migrated_template,
        monkeypatch,
        Settings(campaigns=CampaignSettings(mailbox_daily_cap=25)),
    )
    async with app.router.lifespan_context(app), _client(app) as client:
        await _set_client(client)
        await _connect(client, fake_google)
        [mailbox] = (await client.get("/api/v1/mailboxes")).json()
    assert mailbox["daily_cap"] == 25


async def test_a_state_is_good_for_one_answer(
    client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    await _set_client(client)
    redirect = fake_google.consent(await _start(client))
    assert await _follow(client, redirect) == {"gmail": "connected"}
    assert await _follow(client, redirect) == {"gmail": "error", "reason": "state_mismatch"}


async def test_an_unknown_state_stores_nothing(
    client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    await _set_client(client)
    redirect = fake_google.consent(await _start(client))
    parts = urlsplit(redirect)
    query = _query(redirect)
    forged = f"{parts.path}?state=forged&code={query['code']}"
    assert await _follow(client, f"http://127.0.0.1{forged}") == {
        "gmail": "error",
        "reason": "state_mismatch",
    }
    assert ("/token", "authorization_code") not in fake_google.requests
    assert (await _status(client))["mailboxes"] == []


async def test_pressing_cancel_on_googles_page_says_so(
    client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    await _set_client(client)
    redirect = fake_google.deny(await _start(client))
    assert await _follow(client, redirect) == {"gmail": "error", "reason": "access_denied"}


async def test_a_disabled_gmail_api_is_named(
    client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    await _set_client(client)
    fake_google.profile_status = 403
    assert await _connect(client, fake_google) == {
        "gmail": "error",
        "reason": "gmail_api_refused",
    }
    assert (await _status(client))["mailboxes"] == []


async def test_a_second_account_is_refused_while_one_is_connected(
    client: httpx.AsyncClient, fake_google: FakeGoogle, memory_keyring: MemoryKeyring
) -> None:
    await _set_client(client)
    await _connect(client, fake_google, "one@example.com")
    assert await _connect(client, fake_google, "two@example.com") == {
        "gmail": "error",
        "reason": "other_mailbox_connected",
    }
    assert len(memory_keyring.entries) == 2  # the client and the first token only


# --- health -------------------------------------------------------------------------


async def test_a_revoked_token_shows_as_reauth_required_after_one_check(
    client: httpx.AsyncClient, running_app: FastAPI, fake_google: FakeGoogle
) -> None:
    """Spec 11.5: revoke, one poll, and the page's banner has what it needs."""
    await _set_client(client)
    await _connect(client, fake_google)
    [mailbox] = (await client.get("/api/v1/mailboxes")).json()
    bus: EventBus = running_app.state.bus
    subscription = bus.subscribe()
    fake_google.revoke_all()

    response = await client.post(f"/api/v1/mailboxes/{mailbox['id']}/check", headers=CSRF)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["status_reason"]) == (
        "reauth_required",
        "invalid_grant",
    )
    assert (await _status(client))["reauth_required"] is True
    assert _drain(bus, subscription) == [
        (
            "mailbox.status",
            {"mailbox_id": mailbox["id"], "status": "reauth_required", "reason": "invalid_grant"},
        )
    ]


async def test_an_answer_cut_off_part_way_is_not_a_server_error(
    client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    """The check answers the mailbox unchanged, and the callback names the outcome (#256)."""
    await _set_client(client)
    await _connect(client, fake_google)
    [mailbox] = (await client.get("/api/v1/mailboxes")).json()
    fake_google.truncate = True
    response = await client.post(f"/api/v1/mailboxes/{mailbox['id']}/check", headers=CSRF)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    url = await _start(client, mailbox["id"])
    assert await _follow(client, fake_google.consent(url)) == {
        "gmail": "error",
        "reason": "unavailable",
    }


async def test_reauthorizing_preselects_the_account_and_heals_it(
    client: httpx.AsyncClient, fake_google: FakeGoogle
) -> None:
    await _set_client(client)
    await _connect(client, fake_google)
    [mailbox] = (await client.get("/api/v1/mailboxes")).json()
    fake_google.revoke_all()
    await client.post(f"/api/v1/mailboxes/{mailbox['id']}/check", headers=CSRF)

    url = await _start(client, mailbox["id"])
    assert _query(url)["login_hint"] == FAKE_EMAIL
    assert await _follow(client, fake_google.consent(url)) == {"gmail": "connected"}
    status = await _status(client)
    assert status["reauth_required"] is False
    healed_list: list[dict[str, object]] = status["mailboxes"]  # type: ignore[assignment]
    [healed] = healed_list
    assert (healed["id"], healed["status"]) == (mailbox["id"], "ok")


async def test_disconnecting_disables_and_forgets(
    client: httpx.AsyncClient, fake_google: FakeGoogle, memory_keyring: MemoryKeyring
) -> None:
    await _set_client(client)
    await _connect(client, fake_google)
    [mailbox] = (await client.get("/api/v1/mailboxes")).json()
    response = await client.post(f"/api/v1/mailboxes/{mailbox['id']}/disconnect", headers=CSRF)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["status_reason"]) == (
        "disabled",
        "disconnected",
    )
    assert [key for key in memory_keyring.entries if "mailbox" in key[1]] == []


@pytest.mark.parametrize("action", ["check", "disconnect"])
async def test_another_users_mailbox_is_404(
    client: httpx.AsyncClient, running_app: FastAPI, action: str
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session)
        mailbox = mailbox_service.connect(session, other, "them@example.com", "rt", daily_cap=80)
    response = await client.post(f"/api/v1/mailboxes/{mailbox.id}/{action}", headers=CSRF)
    assert response.status_code == 404
    start = await client.post(
        "/api/v1/mailboxes/oauth/start", json={"mailbox_id": mailbox.id}, headers=CSRF
    )
    assert start.status_code == 404
    status = await _status(client)
    assert (status["mailboxes"], status["reauth_required"]) == ([], False)


# --- the app ------------------------------------------------------------------------


def _app(
    engine: Engine,
    tmp_path: Path,
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    **kwargs: object,
) -> FastAPI:
    (tmp_path / "bare").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, tmp_path / "bare" / DATABASE_FILENAME)
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    return create_app(settings, engine=engine, **kwargs)  # type: ignore[arg-type]


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def test_the_app_sends_the_flow_where_it_is_told(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGoogle()
    fake.start()
    try:
        app = _app(
            bare_engine, tmp_path, _migrated_template, monkeypatch, Settings(), gmail=fake.endpoints
        )
        async with app.router.lifespan_context(app), _client(app) as client:
            await _set_client(client)
            assert await _connect(client, fake) == {"gmail": "connected"}
    finally:
        fake.stop()


@pytest.mark.parametrize(("minutes", "seconds"), [(7, 420), (0, 60)])
async def test_only_serve_polls_the_mailboxes(
    bare_engine: Engine,
    tmp_path: Path,
    _migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    minutes: int,
    seconds: int,
) -> None:
    """Every reply_poll_minutes, and a nonsense 0 polls every minute rather than failing."""
    extractor = ServeExtractor(executor=lambda factory, bus: object())  # type: ignore[arg-type,return-value]
    served = _app(
        bare_engine,
        tmp_path,
        _migrated_template,
        monkeypatch,
        Settings(campaigns=CampaignSettings(reply_poll_minutes=minutes)),
        extractor=extractor,
    )
    async with served.router.lifespan_context(served):
        monitor = served.state.mailbox_monitor
        assert isinstance(monitor, mailbox_service.MailboxMonitor)
        assert monitor._interval_s == seconds
        assert monitor._task is not None and not monitor._task.done()
    assert monitor._task is None
