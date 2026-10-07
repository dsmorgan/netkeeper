import asyncio
import importlib
import json
import logging
import re
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import Engine, delete, select
from typer.testing import CliRunner

from netkeeper import __version__, migrations
from netkeeper.cli import app as cli
from netkeeper.config import LinkedInSettings, Settings
from netkeeper.db import make_session_factory
from netkeeper.models import User, UserKind
from netkeeper.web.app import API_PREFIX, create_app, discover_routers, openapi_json
from netkeeper.worker import dev_app, serve_app

REPO_ROOT = Path(__file__).resolve().parents[1]
API_PATHS = {
    "/api/v1/health",
    "/api/v1/gmail-setup",
    "/api/v1/gmail/activity",
    "/api/v1/me",
    "/api/v1/me/positions",
    "/api/v1/me/positions/{position_id}",
    "/api/v1/events",
    "/api/v1/tasks/ping",
    "/api/v1/tasks/{task_id}",
    "/api/v1/contacts",
    "/api/v1/contacts/query",
    "/api/v1/contacts/stats",
    "/api/v1/dashboard/next-fires",
    "/api/v1/dashboard/changed-jobs",
    "/api/v1/dashboard/inbound",
    "/api/v1/do-not-send",
    "/api/v1/do-not-send/{entry_id}",
    "/api/v1/contacts/bulk",
    "/api/v1/contacts/bulk/count",
    "/api/v1/contacts/{contact_id}",
    "/api/v1/contacts/{contact_id}/revert-field",
    "/api/v1/contacts/{contact_id}/archive",
    "/api/v1/contacts/{contact_id}/unarchive",
    "/api/v1/contacts/{contact_id}/confirm",
    "/api/v1/contacts/{contact_id}/reject",
    "/api/v1/contacts/{contact_id}/merge",
    "/api/v1/contacts/{contact_id}/merge/preview",
    "/api/v1/contacts/{contact_id}/duplicates",
    "/api/v1/contacts/{contact_id}/emails",
    "/api/v1/contacts/{contact_id}/emails/{email_id}",
    "/api/v1/contacts/{contact_id}/phones",
    "/api/v1/contacts/{contact_id}/phones/{phone_id}",
    "/api/v1/contacts/{contact_id}/links",
    "/api/v1/contacts/{contact_id}/links/{link_id}",
    "/api/v1/contacts/{contact_id}/interactions",
    "/api/v1/contacts/{contact_id}/timeline",
    "/api/v1/contacts/{contact_id}/notes",
    "/api/v1/interactions/{interaction_id}",
    "/api/v1/linkedin/runs",
    "/api/v1/linkedin/runs/{run_id}",
    "/api/v1/linkedin/runs/{run_id}/cancel",
    "/api/v1/linkedin/runs/{run_id}/pause",
    "/api/v1/linkedin/runs/{run_id}/resume",
    "/api/v1/linkedin/runs/{run_id}/contacts",
    "/api/v1/linkedin/runs/{run_id}/diagnostics",
    "/api/v1/linkedin/budget",
    "/api/v1/linkedin/heat",
    "/api/v1/linkedin/heat/clear",
    "/api/v1/linkedin/inbox/acknowledge",
    "/api/v1/linkedin/pins",
    "/api/v1/linkedin/pins/{contact_id}",
    "/api/v1/linkedin/schedule",
    "/api/v1/linkedin/schedule/arm",
    "/api/v1/linkedin/schedule/disarm",
    "/api/v1/linkedin/schedule/pause",
    "/api/v1/linkedin/schedule/unpause",
    "/api/v1/linkedin/status",
    "/api/v1/linkedin/session-flag/clear",
    "/api/v1/linkedin/browser",
    "/api/v1/linkedin/browser/health",
    "/api/v1/tags",
    "/api/v1/tags/{tag_id}",
    "/api/v1/contacts/{contact_id}/tags",
    "/api/v1/contacts/{contact_id}/tags/{tag_id}",
    "/api/v1/autotag-rules",
    "/api/v1/autotag-rules/{rule_id}",
    "/api/v1/autotag-rules/{rule_id}/run",
    "/api/v1/autotag-rules/reorder",
    "/api/v1/autotag-rules/run",
    "/api/v1/autotag-rules/preview",
    "/api/v1/imports",
    "/api/v1/imports/inspect",
    "/api/v1/imports/archive",
    "/api/v1/imports/presets",
    "/api/v1/imports/presets/{name}",
    "/api/v1/imports/{run_id}",
    "/api/v1/imports/{run_id}/rows",
    "/api/v1/imports/{run_id}/preview",
    "/api/v1/imports/{run_id}/commit",
    "/api/v1/imports/{run_id}/rollback",
    "/api/v1/exports",
    "/api/v1/lists",
    "/api/v1/lists/{list_id}",
    "/api/v1/lists/{list_id}/members",
    "/api/v1/lists/{list_id}/members/{contact_id}",
    "/api/v1/views",
    "/api/v1/views/{view_id}",
    "/api/v1/triage/next",
    "/api/v1/triage/decisions",
    "/api/v1/triage/undo",
    "/api/v1/triage/contacts/{contact_id}",
    "/api/v1/triage/contacts/{contact_id}/preferred-name",
    "/api/v1/triage/suggestions",
    "/api/v1/triage/suggestions/{key}/contacts",
    "/api/v1/triage/suggestions/{key}/apply",
    "/api/v1/poll-status",
    "/api/v1/poll-status/gmail-replies/check-now",
    "/api/v1/posture",
    "/api/v1/settings/self-contact",
    "/api/v1/settings/sending-hours",
    "/api/v1/campaigns",
    "/api/v1/campaigns/{campaign_id}",
    "/api/v1/campaigns/{campaign_id}/activate",
    "/api/v1/campaigns/{campaign_id}/archive",
    "/api/v1/campaigns/{campaign_id}/delete-plan",
    "/api/v1/campaigns/{campaign_id}/end",
    "/api/v1/campaigns/{campaign_id}/enroll",
    "/api/v1/campaigns/{campaign_id}/enrollments",
    "/api/v1/campaigns/{campaign_id}/pause",
    "/api/v1/campaigns/{campaign_id}/results",
    "/api/v1/campaigns/{campaign_id}/resume",
    "/api/v1/campaigns/{campaign_id}/start",
    "/api/v1/campaigns/{campaign_id}/start-options",
    "/api/v1/campaigns/{campaign_id}/steps/{step_id}/schedule",
    "/api/v1/campaigns/{campaign_id}/unarchive",
    "/api/v1/campaigns/linkedin/ready",
    "/api/v1/campaigns/linkedin/waiting",
    "/api/v1/campaigns/linkedin/options",
    "/api/v1/campaigns/linkedin/prefill",
    "/api/v1/campaigns/linkedin/messages/{message_id}/check",
    "/api/v1/campaigns/linkedin/messages/{message_id}/discard",
    "/api/v1/campaigns/{campaign_id}/review",
    "/api/v1/campaigns/{campaign_id}/review/guards",
    "/api/v1/campaigns/{campaign_id}/review/lint",
    "/api/v1/campaigns/{campaign_id}/review/start",
    "/api/v1/campaigns/{campaign_id}/review/steps/{step_id}",
    "/api/v1/campaigns/{campaign_id}/review/steps/{step_id}/approve",
    "/api/v1/campaigns/{campaign_id}/review/steps/{step_id}/messages/approve",
    "/api/v1/campaigns/{campaign_id}/review/test-send",
    "/api/v1/inbox",
    "/api/v1/inbox/{message_id}/handled",
    "/api/v1/mailboxes",
    "/api/v1/mailboxes/oauth/callback",
    "/api/v1/mailboxes/oauth/client",
    "/api/v1/mailboxes/oauth/start",
    "/api/v1/mailboxes/status",
    "/api/v1/mailboxes/{mailbox_id}/arm",
    "/api/v1/mailboxes/{mailbox_id}/check",
    "/api/v1/mailboxes/{mailbox_id}/disarm",
    "/api/v1/mailboxes/{mailbox_id}/disconnect",
    "/api/v1/templates",
    "/api/v1/templates/lint",
    "/api/v1/templates/merge-fields",
    "/api/v1/templates/{template_id}",
    "/api/v1/templates/{template_id}/preview",
}


# --- routes -----------------------------------------------------------------


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_me_returns_the_local_user(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/me")
    assert response.status_code == 200
    assert response.json() == {
        "id": 1,
        "kind": "local",
        "display_name": None,
        "email": None,
        "timezone": LinkedInSettings().timezone,
    }


async def test_me_without_a_local_user_is_a_clear_500(
    client: httpx.AsyncClient, bare_engine: Engine
) -> None:
    with make_session_factory(bare_engine)() as session:
        session.execute(delete(User))
        session.commit()
    response = await client.get("/api/v1/me")
    assert response.status_code == 500
    assert "netkeeper db upgrade" in response.json()["detail"]


# --- lifespan ---------------------------------------------------------------


async def test_lifespan_migrates_and_creates_the_user(
    running_app: FastAPI, bare_engine: Engine
) -> None:
    assert migrations.current_revision(bare_engine) == migrations.head_revision()
    with make_session_factory(bare_engine)() as session:
        users = list(session.scalars(select(User)))
    assert [user.kind for user in users] == [UserKind.LOCAL]

    state = running_app.state
    assert state.engine is bare_engine
    assert state.settings == Settings()
    assert state.bus.subscriber_count == 0
    assert state.tasks.get("nope") is None
    assert type(state.auth).__name__ == "LocalSingleUser"


async def test_startup_is_idempotent(app: FastAPI, bare_engine: Engine) -> None:
    async with app.router.lifespan_context(app):
        pass
    async with app.router.lifespan_context(app):
        pass
    with make_session_factory(bare_engine)() as session:
        assert len(list(session.scalars(select(User)))) == 1


async def test_shutdown_cancels_running_tasks(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        runner = app.state.tasks
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        info = runner.submit("forever", forever, user_id=1)
        await started.wait()
    done = runner.get(info.id)
    assert done is not None
    assert done.status == "failed"
    assert done.error == "cancelled"


def test_dev_app_sets_up_logging_then_builds_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --reload worker imports this factory without going through the CLI callback."""
    monkeypatch.chdir(tmp_path)  # no config.toml in reach
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        app = dev_app()
        assert isinstance(app, FastAPI)
        assert any(handler.get_name() == "netkeeper" for handler in root.handlers)
    finally:
        root.handlers[:] = before


@pytest.mark.parametrize(("per_day", "warns"), [(101, True), (100, False)])
def test_serve_app_logs_the_profile_visit_risk_once_above_100_a_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    per_day: int,
    warns: bool,
) -> None:
    """#318: serve's startup says so once at WARNING; building the app attaches nothing."""
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    base = Settings()
    settings = replace(
        base,
        linkedin=replace(
            base.linkedin, budget=replace(base.linkedin.budget, profile_visits_per_day=per_day)
        ),
    )
    with caplog.at_level(logging.WARNING, logger="netkeeper.worker"):
        serve_app(settings)
    risk = [
        record
        for record in caplog.records
        if record.name == "netkeeper.worker" and "Profile visits are set to" in record.getMessage()
    ]
    if warns:
        assert len(risk) == 1 and risk[0].levelno == logging.WARNING
        assert risk[0].getMessage().startswith("Profile visits are set to 101 a day")
    else:
        assert risk == []


@pytest.mark.parametrize("field", ["li_prefills_per_day", "li_messages_auto_per_day"])
@pytest.mark.parametrize(("per_day", "warns"), [(21, True), (20, False)])
def test_serve_app_logs_the_linkedin_message_risk_once_above_20_a_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    field: str,
    per_day: int,
    warns: bool,
) -> None:
    """#447: serve's startup logs a message budget above 20 a day once, per budget."""
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    base = Settings()
    settings = replace(
        base,
        linkedin=replace(base.linkedin, budget=replace(base.linkedin.budget, **{field: per_day})),
    )
    with caplog.at_level(logging.WARNING, logger="netkeeper.worker"):
        serve_app(settings)
    risk = [
        r
        for r in caplog.records
        if r.name == "netkeeper.worker" and "a day, above 20 a day" in r.getMessage()
    ]
    if warns:
        assert len(risk) == 1 and risk[0].levelno == logging.WARNING
        assert risk[0].getMessage().startswith(("LinkedIn prefills are", "Auto-sent LinkedIn"))
    else:
        assert risk == []


# --- router discovery -------------------------------------------------------


def test_known_api_modules_are_discovered() -> None:
    assert [name for name, _ in discover_routers()] == [
        "autotag_rules",
        "campaign_review",
        "campaigns",
        "contacts",
        "dashboard",
        "do_not_send",
        "events",
        "exports",
        "gmail",
        "gmail_setup",
        "health",
        "imports",
        "inbox",
        "interactions",
        "linkedin",
        "linkedin_steps",
        "lists",
        "mailboxes",
        "me",
        "poll_status",
        "positions",
        "posture",
        "self_contact",
        "sending_hours",
        "tags",
        "tasks",
        "templates",
        "triage",
    ]


def test_discovery_finds_routers_in_any_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = tmp_path / "fake_api"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    (package_dir / "alpha.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/alpha')\n"
        "def alpha() -> dict[str, str]:\n"
        "    return {}\n"
    )
    (package_dir / "beta.py").write_text("router = 'not a router'\n")
    (package_dir / "gamma.py").write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        found = discover_routers(importlib.import_module("fake_api"))
    finally:
        for name in [n for n in sys.modules if n == "fake_api" or n.startswith("fake_api.")]:
            del sys.modules[name]
    assert [name for name, _ in found] == ["alpha"]
    assert isinstance(found[0][1], APIRouter)
    route = found[0][1].routes[0]
    assert isinstance(route, APIRoute)
    assert route.path == "/alpha"


def test_every_discovered_router_is_mounted_under_the_prefix(app: FastAPI) -> None:
    # The SPA catch-all is a route too, but include_in_schema=False keeps it out.
    paths = set(app.openapi()["paths"])
    assert paths == API_PATHS
    assert all(path.startswith(API_PREFIX) for path in paths)


def test_operation_ids_are_snake_case(app: FastAPI) -> None:
    schema = app.openapi()
    ids = [op["operationId"] for path in schema["paths"].values() for op in path.values()]
    assert ids
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", op_id) for op_id in ids), ids
    assert len(ids) == len(set(ids))


# --- frontend ---------------------------------------------------------------


def _static_client(app: FastAPI) -> httpx.AsyncClient:
    # No lifespan: the static routes do not touch the database.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def test_serves_the_built_frontend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bare_engine: Engine
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>netkeeper</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(dist))
    app = create_app(Settings(), engine=bare_engine)

    async with _static_client(app) as client:
        for path in ("/", "/contacts", "/contacts/42", "/settings"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith("text/html")
            assert response.text == "<!doctype html><title>netkeeper</title>"

        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console.log(1)"
        assert "javascript" in asset.headers["content-type"]

        favicon = await client.get("/favicon.svg")
        assert favicon.status_code == 200
        assert favicon.text == "<svg/>"

        assert (await client.get("/missing.png")).status_code == 404
        assert (await client.get("/assets/missing.js")).status_code == 404

        # Never shadow the API: an unknown API path is a JSON 404, not the SPA page.
        missing_api = await client.get("/api/v1/nope")
        assert missing_api.status_code == 404
        assert missing_api.json() == {"detail": "Not Found"}
        assert (await client.get("/api")).status_code == 404

        assert (await client.get("/docs")).status_code == 200


async def test_not_built_page_when_the_build_is_missing(app: FastAPI) -> None:
    async with _static_client(app) as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "not built" in response.text
        assert "make build-ui" in response.text
        assert "no-dist" in response.text

        assert (await client.get("/contacts")).status_code == 200
        assert (await client.get("/api/v1/nope")).status_code == 404
        assert (await client.get("/assets/app.js")).status_code == 404


# --- openapi export ---------------------------------------------------------


def test_openapi_export_writes_the_app_schema(tmp_path: Path) -> None:
    out = tmp_path / "schema" / "openapi.json"
    result = CliRunner().invoke(cli, ["openapi", "export", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert str(out) in result.stdout

    text = out.read_text()
    assert text.endswith("\n")
    assert text == openapi_json(create_app(Settings()))
    parsed = json.loads(text)
    assert list(parsed) == sorted(parsed)
    assert parsed["info"] == {
        "title": "netkeeper",
        "version": __version__,
        "description": "Keep your professional network warm.",
    }
    assert set(parsed["paths"]) == API_PATHS


def test_committed_frontend_schema_is_current() -> None:
    """`make gen-client` must be rerun when the API changes; CI diffs this too."""
    committed = (REPO_ROOT / "frontend" / "openapi.json").read_text()
    assert committed == openapi_json(create_app(Settings()))


async def test_built_frontend_never_serves_files_outside_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bare_engine: Engine
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>netkeeper</title>")
    (tmp_path / "secret.txt").write_text("OUTSIDE-DIST-MARKER")
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(dist))
    app = create_app(Settings(), engine=bare_engine)

    attempts = (
        "/..%2fsecret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/assets/../../secret.txt",
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "//secret.txt",
    )
    async with _static_client(app) as client:
        for path in attempts:
            response = await client.get(path)
            # A rejected path is a 404; a path that normalizes to an SPA route gets the
            # index page. Either way nothing outside dist is ever served.
            served_index = response.text == "<!doctype html><title>netkeeper</title>"
            assert response.status_code in (400, 404) or served_index, path
            assert "OUTSIDE-DIST-MARKER" not in response.text, path
