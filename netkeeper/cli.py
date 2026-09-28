"""Command-line entry point: serve, db, config, backup, openapi, tags, import/export, gmail,
campaigns, version."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final, Literal, NoReturn
from zoneinfo import ZoneInfo

import typer
import uvicorn
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from netkeeper import __version__, migrations
from netkeeper.campaigns import gmail_oauth
from netkeeper.campaigns.render import me_fields
from netkeeper.config import ConfigError, Settings, load_settings, render_toml
from netkeeper.crm import import_runs
from netkeeper.crm.archive import ArchiveImport, import_archive
from netkeeper.crm.archive_check import open_checked_archive
from netkeeper.crm.contacts import ContactStats, contact_stats
from netkeeper.crm.exports import ExportError, ExportFormat, ExportPreset, export_stream
from netkeeper.crm.filters import FilterError, FilterTree, SortKey, parse_filter, parse_sort
from netkeeper.crm.lists import ListCount, find_list, list_lists, list_views, member_counts
from netkeeper.crm.tags import ensure_default_rules, list_tags, run_rules
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.activity_lock import SINGLE_ACCOUNT_KEY, account_key
from netkeeper.linkedin.archive import ArchiveFormatError
from netkeeper.linkedin.browser import (
    CHROME_PROFILE_DIRNAME,
    ActivityLocks,
    AttachBrowserProvider,
    BrowserError,
)
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.preflight import LoginState, PreflightReport
from netkeeper.linkedin.preflight import preflight as run_preflight
from netkeeper.linkedin.rehearse import NotANeutralSite, Rehearsal, serve_replica
from netkeeper.linkedin.rehearse import rehearse as run_rehearsal
from netkeeper.linkedin.rehearse import render as render_rehearsal
from netkeeper.logging_setup import setup_logging
from netkeeper.models import (
    EnrollmentStatus,
    ImportResolution,
    ImportRun,
    ImportStatus,
    Mailbox,
    MailboxArm,
    MailboxStatus,
    StepMode,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
    UserKind,
)
from netkeeper.paths import CONFIG_ENV, data_dir
from netkeeper.scoping import install_scope_guard
from netkeeper.services import (
    campaign_review,
    enrich_plan,
    keychain,
    route_breaker,
    runs,
    simulate_campaign,
)
from netkeeper.services import campaigns as campaign_service
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.backup import (
    BACKUPS_DIRNAME,
    BackupError,
    create_backup,
    list_backups,
    prune_backups,
)
from netkeeper.services.browser_launch import cdp_port, chrome_launch_command, remote_host_note
from netkeeper.services.events import EventBus
from netkeeper.services.linkedin_accounts import (
    account_id_for,
    arm_scheduled_runs,
    disarm_scheduled_runs,
    ensure_account,
    find_account,
)
from netkeeper.services.linkedin_session import clear_session_flag, session_flag
from netkeeper.services.pacing import profiles as pacing_profiles
from netkeeper.services.posture import SessionProbe, posture
from netkeeper.services.posture import render as render_posture
from netkeeper.services.simulate_campaign import DEFAULT_SCHEDULE_DAYS
from netkeeper.services.simulate_run import DEFAULT_DAYS as DEFAULT_SIMULATION_DAYS
from netkeeper.services.simulate_run import DEFAULT_SEED as DEFAULT_SIMULATION_SEED
from netkeeper.services.simulate_run import DEFAULT_THROTTLES as DEFAULT_SIMULATION_THROTTLES
from netkeeper.services.simulate_run import InvalidSimulation, run_simulation
from netkeeper.services.simulate_run import render as render_simulation
from netkeeper.services.users import ensure_local_user
from netkeeper.web.app import create_app, openapi_json
from netkeeper.worker import BrowserWorker, serve_app

log = logging.getLogger(__name__)

APP_FACTORY = "netkeeper.worker:dev_app"

app = typer.Typer(help="netkeeper: keep your professional network warm.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect the resolved configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Create and migrate the database.", no_args_is_help=True)
openapi_app = typer.Typer(help="Work with the API schema.", no_args_is_help=True)
# No help= here: the group description comes from backup_group's docstring so that
# `netkeeper backup --help` also carries the note about the default subcommand.
backup_app = typer.Typer(invoke_without_command=True)
tags_app = typer.Typer(help="Tags and auto-tag rules.", no_args_is_help=True)
lists_app = typer.Typer(
    help="Static lists, smart lists, and saved table views.", no_args_is_help=True
)
import_app = typer.Typer(
    help="Bring contacts in from a LinkedIn archive or a CSV.", no_args_is_help=True
)
contacts_app = typer.Typer(help="Inspect your contacts.", no_args_is_help=True)
browser_app = typer.Typer(
    help="The Chrome netkeeper attaches to (it never starts one).", no_args_is_help=True
)
linkedin_app = typer.Typer(
    help="LinkedIn runs by hand, their history, cancel, and scheduled-run arming.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(openapi_app, name="openapi")
app.add_typer(backup_app, name="backup")
app.add_typer(tags_app, name="tags")
app.add_typer(lists_app, name="lists")
app.add_typer(import_app, name="import")
app.add_typer(contacts_app, name="contacts")
app.add_typer(browser_app, name="browser")
app.add_typer(linkedin_app, name="linkedin")
gmail_app = typer.Typer(
    help="Connect the Gmail account campaigns send from (docs/gmail-setup.md).",
    no_args_is_help=True,
)
app.add_typer(gmail_app, name="gmail")
campaigns_app = typer.Typer(
    help="Campaigns: create, enroll, review status, activate through the review gate, pause.",
    no_args_is_help=True,
)
app.add_typer(campaigns_app, name="campaigns")


@dataclass(frozen=True, slots=True)
class CliState:
    """Global options, stored on the Typer context for subcommands to read."""

    config: Path | None = None


@dataclass(frozen=True, slots=True)
class _PathsBlock:
    data_dir: str
    config: str


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Config file to use instead of $NETKEEPER_CONFIG and the default search.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """netkeeper command group."""
    ctx.obj = CliState(config=config)
    setup_logging()


def _load_settings_or_exit(state: CliState) -> Settings:
    try:
        return load_settings(state.config)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _display_url(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="Interface to bind (default: web.host from the config).",
            show_default=False,
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port", help="Port to bind (default: web.port from the config).", show_default=False
        ),
    ] = None,
    reload: Annotated[
        bool, typer.Option("--reload", help="Restart when the code changes (development).")
    ] = False,
) -> None:
    """Run the web server: the API, the event stream, and the built frontend."""
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    bind_host = settings.web.host if host is None else host
    bind_port = settings.web.port if port is None else port
    # log_config=None keeps uvicorn's records on the root logger set up in main().
    if reload:
        # The reloader imports the factory in a worker process it restarts on change,
        # so the app and its lifespan are built there and only there; dev_app() sets
        # up logging there too. --config travels through the environment because
        # that process does not see our arguments.
        if state.config is not None:
            os.environ[CONFIG_ENV] = str(state.config.expanduser().resolve())
        uvicorn.run(
            APP_FACTORY, factory=True, reload=True, host=bind_host, port=bind_port, log_config=None
        )
        return
    uvicorn.run(serve_app(settings), host=bind_host, port=bind_port, log_config=None)


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the resolved settings as TOML, followed by a paths table."""
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    source = "defaults" if settings.source_path is None else str(settings.source_path)
    paths = _PathsBlock(data_dir=str(data_dir()), config=source)
    typer.echo(render_toml(settings), nl=False)
    typer.echo()
    typer.echo(render_toml(paths, table="paths"), nl=False)


@db_app.command("upgrade")
def db_upgrade(ctx: typer.Context) -> None:
    """Apply pending migrations, then make sure the local user exists."""
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    url = database_url()
    engine = make_engine(url)
    try:
        migrations.upgrade(engine)
        with session_scope(make_session_factory(engine), write=True) as session:
            user = ensure_local_user(session, settings=settings)
        revision = migrations.current_revision(engine)
    finally:
        engine.dispose()
    typer.echo(f"{_display_url(url)}: at revision {revision}, local user id {user.id}")


@db_app.command("current")
def db_current() -> None:
    """Print the revision the database is at, and the newest one available."""
    engine = make_engine(database_url())
    try:
        current = migrations.current_revision(engine)
    finally:
        engine.dispose()
    typer.echo(f"current: {current or '(none)'}")
    typer.echo(f"head:    {migrations.head_revision() or '(none)'}")


@db_app.command("revision")
def db_revision(
    message: Annotated[
        str, typer.Option("--message", "-m", help="What the migration does, in a few words.")
    ],
) -> None:
    """Autogenerate a migration by diffing the models against the database (for developers)."""
    engine = make_engine(database_url())
    try:
        written = migrations.create_revision(engine, message)
    finally:
        engine.dispose()
    for path in written:
        typer.echo(str(path))


@openapi_app.command("export")
def openapi_export(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option("--out", help="File to write the JSON schema to.")],
) -> None:
    """Write the OpenAPI schema as JSON with sorted keys (the input to `make gen-client`)."""
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(openapi_json(create_app(settings)), encoding="utf-8")
    typer.echo(f"wrote {out}")


@browser_app.command("launch")
def browser_launch(
    ctx: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print the port, profile, and CDP URL as JSON instead (for scripts/chrome.sh).",
        ),
    ] = False,
) -> None:
    """Print the command that starts Chrome with a debug port. netkeeper never runs it.

    netkeeper attaches to a browser you run; it does not own one. A browser netkeeper
    started would be a second device on your LinkedIn account, and that is what gets
    accounts restricted, so this command prints a command for you to run (ADR 0002).

    The command itself comes from :mod:`netkeeper.services.browser_launch` --
    the same module ``GET /api/v1/linkedin/browser`` uses (P2-12) -- so there is
    exactly one place that knows how to build it; nothing here restates the logic.
    """
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    cdp_url = settings.linkedin.cdp_url
    profile = data_dir() / CHROME_PROFILE_DIRNAME
    if as_json:
        # What `make chrome` reads, so it starts Chrome on the port `serve` and
        # `preflight` will attach to, not on a guess. `remote` is the reason the
        # printed command would not work on this machine, or null.
        typer.echo(
            json.dumps(
                {
                    "cdp_url": cdp_url,
                    "port": cdp_port(cdp_url),
                    "profile": str(profile),
                    "remote": remote_host_note(cdp_url),
                }
            )
        )
        return
    typer.echo("netkeeper attaches to a Chrome you start yourself. It never starts one.")
    typer.echo("Run this in a terminal (again whenever that Chrome is not running):")
    typer.echo()
    for line in chrome_launch_command(cdp_port(cdp_url), profile):
        typer.echo(f"  {line}")
    typer.echo()
    typer.echo("Then, in that window:")
    typer.echo("  - log in to LinkedIn once;")
    typer.echo("  - use it for your own LinkedIn browsing too, so your activity and")
    typer.echo("    netkeeper's share one session and one fingerprint.")
    typer.echo()
    typer.echo(
        "Chrome 136 and later refuse --remote-debugging-port on the default profile\n"
        "directory, so the separate --user-data-dir above is required."
    )
    note = remote_host_note(cdp_url)
    if note is not None:
        typer.echo()
        typer.echo(f"note: {note}")
    typer.echo()
    typer.echo("From a checkout, `make chrome` (scripts/chrome.sh) runs that command for you,")
    typer.echo("waits for the port, and clears a profile lock a crashed Chrome left behind.")
    typer.echo()
    typer.echo(f"Check it with: netkeeper preflight   (attaches to {cdp_url})")


@app.command()
def preflight(ctx: typer.Context) -> None:
    """Check the sidecar: the attach, the LinkedIn session, and the browser fingerprint.

    Everything it reports comes from your own machine. It opens one blank tab on the
    Chrome you started, reads a few navigator properties and the names of the LinkedIn
    cookies already in that profile, and closes the tab again. It visits no website,
    and it never reads or prints a cookie value. Exits non-zero when a job could not
    run right now.

    It takes the same per-account activity lock every netkeeper process takes before
    attaching, so while `netkeeper serve` or another command holds the browser, this
    reports which process holds it and exits non-zero instead of attaching alongside.

    A run that finds a live session clears `linkedin.session_flag` if a login wall
    had set it: logging back in to the netkeeper Chrome profile is the actual fix
    for that condition, and this is where the fix gets noticed (#154). A checkpoint
    flag is different -- a live session cookie is not proof a checkpoint is
    resolved -- so it is left in place, with a line saying so and naming
    `netkeeper linkedin clear-flag` (#168 review, F1).
    """
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    provider = AttachBrowserProvider(settings.linkedin.cdp_url)
    report = asyncio.run(run_preflight(provider, _browser_lock_key()))
    for line in _clear_session_flag_after_login(report):
        typer.echo(line)
    for line in _preflight_lines(report):
        typer.echo(line)
    if not report.ok:
        raise typer.Exit(code=1)


def _clear_session_flag_after_login(report: PreflightReport) -> list[str]:
    """The other half of `linkedin.session_flag` (#144): a live session can clear it.

    Only `LoginState.LOGGED_IN` does anything at all. `LoginState.NO_SESSION` is
    not treated as its opposite: preflight's cookie-jar read is weaker evidence
    than LinkedIn actually answering with a login wall (`classify.Outcome.LOGGED_OUT`),
    which is the whole reason #145 kept the two enums from comparing equal, so a
    missing cookie must never *set* the flag here either -- only a job that gets an
    answer from LinkedIn does that (spec 9.7, `services.linkedin_session.flag_session`).

    **Only a `LoggedOut` flag is auto-cleared (#168 review, F1, decided
    conservatively).** `li_at` being present is not proof a `Checkpoint` has been
    resolved -- a checkpoint leaves `li_at` in place, so clearing it on that
    evidence alone would send the next job straight back into an open checkpoint
    (spec 9.7). A `Checkpoint` flag is left exactly as it was, with one line saying
    why, and is only ever cleared by hand, with `netkeeper linkedin clear-flag`. (A
    later `Ok` classification could plausibly also clear it; that is out of scope
    here, deliberately, and left for a follow-up once a real job classifies
    responses at all.)

    **The flag is read once, in a read session, before anything writes (#170 item
    6).** Preflight never contacts LinkedIn -- it only reads this Chrome profile's
    own cookie jar (spec 9.1) -- so a `Checkpoint` report, which writes nothing,
    has no business taking the write lock just to look at a value a running
    `serve` might be waiting to write itself. Only a `LoggedOut` flag goes on to
    open a writer, and even then it re-reads the flag first and clears it only if
    it is still the exact flag this function already saw: a job that raised a new
    flag, or a `netkeeper linkedin clear-flag` that ran, in the gap between the two
    sessions must not be undone by a write this function decided on stale evidence.

    `linkedin/preflight.py` may not open a database session (spec 9.10, ADR 0005),
    so the clearing happens here, in the CLI -- the one place both the browser
    report and the database are reachable. Returns the line(s) this command should
    print, so the CLI body stays a plain "compute, then echo" and this stays
    testable without capturing stdout.

    A fresh install (before `netkeeper db upgrade`) must see none of this: preflight
    answers from the browser alone and always has, and `make_engine` creates the
    data directory and an empty sqlite file as a side effect of merely being called
    (spec 15) -- so the sqlite file's existence is checked *first*, without ever
    opening an engine, and a missing file returns with nothing printed and nothing
    created (#168 review, F4). Once a database is genuinely there, only "no such
    table" (the schema itself is missing -- also a fresh, pre-`db upgrade` install)
    and no local user row are silent; anything else `OperationalError` can mean --
    locked, read-only, a disk I/O error -- is logged at WARNING and printed as one
    line, and still exits 0: a failure to clear an advisory flag is not a reason to
    fail the command that just told you the browser and the session are fine.
    """
    if report.login is not LoginState.LOGGED_IN:
        return []
    url = database_url()
    parsed_url = make_url(url)
    sqlite_file = parsed_url.database
    is_sqlite_file = parsed_url.get_backend_name() == "sqlite" and sqlite_file not in (
        None,
        ":memory:",
    )
    if is_sqlite_file and sqlite_file is not None and not Path(sqlite_file).exists():
        return []
    engine = make_engine(url)
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:  # read: this command may not write yet
            user = session.scalars(
                select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
            ).first()
            flag = None if user is None else session_flag(session, user)
        if flag is None:
            return []
        if flag.outcome is Outcome.CHECKPOINT:
            return [
                "leaving the checkpoint session flag in place: a live session cookie is"
                " not proof the checkpoint is resolved. Once you have opened LinkedIn in"
                " the netkeeper Chrome profile and confirmed the account is healthy, clear"
                " it with `netkeeper linkedin clear-flag`"
            ]
        if flag.outcome is not Outcome.LOGGED_OUT:
            return []
        with session_scope(factory, write=True) as session:
            user = session.scalars(
                select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
            ).first()
            if user is None or session_flag(session, user) != flag:
                # Gone, or changed, since the read above: clear only the flag this
                # function actually saw, never whatever is there now.
                return []
            clear_session_flag(session, user)
        return [
            "cleared the logged-out session flag: this Chrome profile's cookie jar"
            " shows a live LinkedIn session again"
        ]
    except OperationalError as exc:
        if _is_missing_schema(exc):
            log.debug("no schema to clear the session flag in yet: %s", exc)
            return []
        log.warning("could not clear the session flag: %s", exc)
        return [f"could not clear the session flag: {exc.orig or exc}"]
    finally:
        engine.dispose()


def _is_missing_schema(exc: OperationalError) -> bool:
    """Whether ``exc`` is sqlite's "no such table", the fresh-install case that stays silent."""
    return "no such table" in str(exc.orig or exc).lower()


@linkedin_app.command("clear-flag")
def linkedin_clear_flag(
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Clear `linkedin.session_flag` by hand -- the way back from a checkpoint.

    `netkeeper preflight` clears the flag itself once it finds a live session, but
    only for a `LoggedOut` flag: the cookie jar showing a live session again is
    real evidence the login wall is gone. It never does that for a `Checkpoint`
    flag, because a live `li_at` cookie is not proof a checkpoint has been solved
    (spec 9.7) -- so that one is only ever cleared here, by hand, once you have
    opened LinkedIn in the netkeeper Chrome profile yourself and confirmed the
    account looks healthy.

    Asks for confirmation first, naming what is being cleared and when it was
    raised; `--yes` skips the prompt for a script.

    **The write lock is not held across the prompt (#170 item 1).** The flag is
    read in its own read session, before anything asks for confirmation, so a
    `netkeeper serve` running at the same time never sees "database is locked"
    for however long a person takes to answer. Only after the prompt returns (or
    `--yes` skips it) does this open a writer, and even then it re-reads the flag
    first and clears it only if it is still the exact flag this command showed --
    a job that raised a new flag while the prompt was on screen must not have its
    flag cleared by an answer given about a different one.
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:  # read: no write lock while we prompt
            user = _local_user_or_exit(session)
            flag = session_flag(session, user)
        if flag is None:
            typer.echo("no session flag is set")
            return
        if not yes:
            confirmed = typer.confirm(
                f"clear the {flag.outcome.value} session flag (raised"
                f" {flag.flagged_at:%Y-%m-%d %H:%M UTC} at {flag.url or '/'})?"
            )
            if not confirmed:
                typer.echo("cancelled: the flag is unchanged")
                raise typer.Exit(code=1)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            if session_flag(session, user) != flag:
                typer.echo("the session flag changed while waiting for an answer; not clearing it")
                raise typer.Exit(code=1)
            clear_session_flag(session, user)
    finally:
        engine.dispose()
    typer.echo("session flag cleared")


def _preflight_lines(report: PreflightReport) -> list[str]:
    """The report as lines: a field table, then anything wrong, then the verdict."""
    rows: list[tuple[str, str]] = [
        ("cdp url", report.cdp_url),
        ("attached", "yes" if report.attached else "no"),
    ]
    if report.attached:
        rows.append(("browser", report.browser_version or "unknown"))
        rows.append(("contexts", str(report.context_count)))
        rows.append(("session", _session_cell(report)))
    fingerprint = report.fingerprint
    if fingerprint is not None:
        rows.extend(
            [
                ("user agent", fingerprint.user_agent or "-"),
                ("platform", fingerprint.platform or "-"),
                ("languages", ", ".join(fingerprint.languages) or "-"),
                ("timezone", fingerprint.timezone or "-"),
                ("cpu cores", str(fingerprint.hardware_concurrency)),
                ("webdriver", "true" if fingerprint.webdriver else "false"),
                ("plugins", str(fingerprint.plugin_count)),
            ]
        )
    lines = _format_table(("FIELD", "VALUE"), rows).splitlines()
    lines.extend(f"problem: {problem}" for problem in report.problems)
    lines.extend(f"warning: {warning}" for warning in report.warnings)
    lines.append("ready" if report.ok else "not ready")
    return lines


def _session_cell(report: PreflightReport) -> str:
    """The session line: state, the cookie names found, and when the session expires.

    Cookie names only. A value never reaches this function.
    """
    words = {
        LoginState.LOGGED_IN: "logged in",
        LoginState.NO_SESSION: "no LinkedIn session",
        LoginState.UNKNOWN: "unknown",
    }
    cell = words[report.login]
    if report.session_cookies:
        cell += f" ({', '.join(report.session_cookies)})"
    if report.session_expires_at is not None:
        cell += f", expires {report.session_expires_at:%Y-%m-%d %H:%M UTC}"
    return cell


def _session_probe(report: PreflightReport) -> SessionProbe:
    """A preflight report reduced to what a posture report may say about it.

    The adapter lives here rather than in ``services.posture`` because that
    module may not import ``linkedin.preflight``: a module under ``services/``
    that reaches the browser is how browser work ends up inside a request
    handler, and ``tests/test_browser_safety.py`` fails the build for it.

    Cookie *names* cross this line and cookie values do not -- they cannot,
    because preflight never read one and :class:`SessionProbe` has no field to
    put one in (spec 9.1, CLAUDE.md).
    """
    logged_in: dict[LoginState, bool | None] = {
        LoginState.LOGGED_IN: True,
        LoginState.NO_SESSION: False,
        LoginState.UNKNOWN: None,
    }
    return SessionProbe(
        attached=report.attached,
        logged_in=logged_in[report.login],
        browser_version=report.browser_version,
        cookie_names=report.session_cookies,
        problems=report.problems,
    )


@app.command("posture")
def posture_command(
    ctx: typer.Context,
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="Attach to Chrome and read the LinkedIn session, as `netkeeper preflight`"
            " does. --no-probe answers from the database alone and reports the session as"
            " unknown.",
        ),
    ] = True,
    account: Annotated[
        int | None,
        typer.Option(
            help="LinkedIn account id the budgets and heat belong to. Defaults to the"
            " local user's account."
        ),
    ] = None,
) -> None:
    """Every protection the LinkedIn extractor has, and a warning for anything that is off.

    This is the "is it safe to run?" answer in one place: attach-only browsing,
    the active window and where now falls in it, the warm-up ramp and today's
    budget, every per-day and per-week counter against its limit and against
    spec 9.6's hard max, the heat score with when it was last raised and when
    runs resume, the weekend multiplier, LinkedIn auto-send, and the session
    flag a checkpoint or a login wall raises.

    Reads only: it takes no write lock and changes nothing. Exits non-zero when
    anything warned, so it can gate a script as well as inform a person. It
    attaches to Chrome to check the LinkedIn session, the same way `netkeeper
    preflight` does; --no-probe skips that and reports the session as unknown
    rather than assuming it is fine.
    """
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    provider = _provider(settings)
    session_probe = (
        _session_probe(asyncio.run(run_preflight(provider, _browser_lock_key()))) if probe else None
    )
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            report = posture(
                session,
                user,
                account if account is not None else account_id_for(session, user),
                now=datetime.now(UTC),
                settings=settings,
                # The provider this command would actually run with, not the
                # class attribute: an instance that reported a different mode
                # is exactly what this row exists to catch, and reading the
                # class makes the check tautological.
                browser_mode=provider.mode,
                probe=session_probe,
            )
    finally:
        engine.dispose()
    typer.echo(render_posture(report), nl=False)
    if not report.ok:
        raise typer.Exit(code=1)


DEFAULT_REHEARSAL_VISITS = 3


@app.command("rehearse")
def rehearse_command(
    ctx: typer.Context,
    visits: Annotated[
        int, typer.Option(help="How many profile visits to rehearse.")
    ] = DEFAULT_REHEARSAL_VISITS,
    seed: Annotated[
        int | None,
        typer.Option(
            help="Seed the pacing plan, to repeat a rehearsal exactly.", show_default=False
        ),
    ] = None,
    scale: Annotated[
        float,
        typer.Option(
            help="Divide every wait by this. 1.0 waits exactly what a run would; anything"
            " else is marked as scaled in the log.",
        ),
    ] = 1.0,
    site: Annotated[
        str | None,
        typer.Option(
            help="A loopback replica you started yourself. Without it, netkeeper starts"
            " one and shuts it down afterwards.",
            show_default=False,
        ),
    ] = None,
    log: Annotated[
        Path | None,
        typer.Option(help="Also write the request log here.", show_default=False),
    ] = None,
) -> None:
    """Rehearse enrichment against a neutral loopback site, and print every request.

    Watch what netkeeper would do before it does it anywhere real. It attaches
    to your Chrome exactly as a job does, follows the genuine pacing plan --
    the same scroll deltas, the same dwell, the same lognormal waits, the same
    bursts -- against a profile-shaped page served on this machine's loopback,
    and records every request the tab made: method, status, kind, timing, path.
    The pacing is the one in your config file, not a demo default, so what you
    watch is what a live run would do.

    Each visit is enrichment's: open the profile, scroll it, scroll back to the
    top, pause, and click Contact info once. The requests in the log are the
    page's own -- the profile page, a card it loads as it is scrolled, the
    overlay it loads when Contact info is clicked. netkeeper sends none of them;
    it reads their answers (ADR 0006). The connections sync, which only scrolls,
    is watched in the browser smoke suite's loopback replica.

    It never touches LinkedIn. The site must be a loopback url, a linkedin.com
    host is refused by name, and a rehearsal that somehow reached one raises
    instead of reporting. Nothing here launches a browser (ADR 0002).
    """
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    chosen_seed = int(datetime.now(UTC).timestamp()) if seed is None else seed
    replica: AbstractContextManager[str] = (
        nullcontext(site) if site is not None else serve_replica()
    )
    # The pacing the owner configured, not the pacing module's defaults. The
    # two are equal out of the box, which is exactly why passing them is worth
    # doing: a rehearsal showing 25-second medians against a config asking for
    # 5 would be a fidelity claim that is false, and fidelity is the whole
    # point of rehearsing.
    pacing = pacing_profiles(settings.linkedin.pacing)
    try:
        with replica as base:
            rehearsal = asyncio.run(
                run_rehearsal(
                    _provider(settings),
                    account=_browser_lock_key(),
                    site=base,
                    visits=visits,
                    seed=chosen_seed,
                    time_scale=scale,
                    delay=pacing.delay,
                    burst=pacing.burst,
                )
            )
    except (NotANeutralSite, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except BrowserError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _report_rehearsal(rehearsal, log)


def _report_rehearsal(rehearsal: Rehearsal, log: Path | None) -> None:
    text = render_rehearsal(rehearsal)
    typer.echo(text, nl=False)
    if log is not None:
        log.write_text(text, encoding="utf-8")
        typer.echo(f"wrote the request log to {log}")


def _browser_lock_key() -> str:
    """The activity-lock key of the local user's LinkedIn account (#169 F).

    Every browser path in every process has to meet at the same lock file, and
    `netkeeper serve` keys its runs by the account row. So a command that
    attaches (preflight, posture's probe, rehearse) reads the row too, without
    writing anything: no database yet (a fresh install, before `db upgrade`), no
    local user, or no account row all mean account 1, the first user's, which
    is also the row every existing install has (migration 0011).
    """
    url = database_url()
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and (
        parsed.database in (None, ":memory:") or not Path(parsed.database or "").exists()
    ):
        return SINGLE_ACCOUNT_KEY
    engine = make_engine(url)
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = session.scalars(
                select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
            ).first()
            if user is None:
                return SINGLE_ACCOUNT_KEY
            return account_key(account_id_for(session, user))
    except (OperationalError, ProgrammingError) as exc:
        # SQLite says a missing table or column is operational; PostgreSQL says it
        # is a programming error. Either means a schema this cannot read yet.
        log.debug("no account row to key the browser lock by (%s); using account 1", exc)
        return SINGLE_ACCOUNT_KEY
    finally:
        engine.dispose()


def _provider(settings: Settings) -> AttachBrowserProvider:
    """The one provider there is, pointed at the configured debug port (ADR 0002).

    Its lock registry co-claims the legacy lock with the local user's account
    (#169 F, #175 review F10), the same account `_browser_lock_key` names.
    """
    return AttachBrowserProvider(
        settings.linkedin.cdp_url, locks=ActivityLocks(legacy_partner=_browser_lock_key())
    )


@app.command("simulate")
def simulate_command(
    ctx: typer.Context,
    days: Annotated[
        int | None,
        typer.Option(
            help=f"How many simulated days to replay. Default {DEFAULT_SIMULATION_DAYS},"
            f" or {DEFAULT_SCHEDULE_DAYS} with --campaign.",
            show_default=False,
        ),
    ] = None,
    throttles: Annotated[
        int,
        typer.Option(help="How many Throttled outcomes to inject, at points drawn from --seed."),
    ] = DEFAULT_SIMULATION_THROTTLES,
    seed: Annotated[
        int,
        typer.Option(help="Seeds the throttle placement. The same seed always repeats."),
    ] = DEFAULT_SIMULATION_SEED,
    campaign: Annotated[
        int | None,
        typer.Option(
            "--campaign",
            help="Replay this campaign's schedule instead: its steps, delays, send window"
            " and caps, for its audience, sending nothing.",
            show_default=False,
        ),
    ] = None,
    start: Annotated[
        datetime | None,
        typer.Option(
            help="With --campaign: when the replay starts (ISO 8601, in your time zone"
            " unless it says). Default: now.",
            formats=["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"],
            show_default=False,
        ),
    ] = None,
) -> None:
    """What would N simulated days do to your account? Answered without doing any of it.

    Replays a deterministic virtual clock against your config's budgets, pacing,
    heat, active hours, and timezone -- never against your real data. This command
    builds its own throwaway SQLite database in a temporary directory, seeded cold
    (no counters carried over from a real run), and deletes it again before
    returning. It attaches to no browser and makes no request to linkedin.com or
    anywhere else.

    For every simulated day it shows the warm-up ramp, the weekend damping, how
    many of the derived profile-visit budget were actually used, the heat score
    and whether browser jobs were skipped, and how many of the injected throttles
    landed that day, plus how many times each scheduled job kind fired. Each
    injected throttle is fed through the real heat-raising call, so its
    consequences -- heat rising, delays' cooldown multiplier stretching, the
    per-run budget shrinking, and, with enough of them close together, the skip
    threshold tripping -- are the genuine ones a live run would see, not a
    picture of them.

    With `--campaign ID` it replays that campaign's schedule instead: its steps,
    delays, modes, send window, holidays, and caps, for as many synthetic contacts
    as it has live enrollments (or, with nobody enrolled yet, as its list or filter
    holds), from step 1 as if activated at `--start`. It reads the campaign from
    your database and changes nothing there; the replay runs on the real engine
    tick in a scratch database, deleted afterwards, and sends nothing. It shows the
    sends per local day and step.
    """
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    if campaign is not None:
        _simulate_campaign(campaign, settings=settings, days=days, start=start, seed=seed)
        if throttles:
            typer.echo("note: --throttles applies to LinkedIn runs; ignored with --campaign")
        return
    if start is not None:
        typer.echo("error: --start applies only with --campaign", err=True)
        raise typer.Exit(code=1)
    try:
        report = asyncio.run(
            run_simulation(
                days=DEFAULT_SIMULATION_DAYS if days is None else days,
                throttles=throttles,
                seed=seed,
                settings=settings,
            )
        )
    except InvalidSimulation as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(render_simulation(report), nl=False)


def _simulate_campaign(
    campaign_id: int, *, settings: Settings, days: int | None, start: datetime | None, seed: int
) -> None:
    """`simulate --campaign`: read the campaign's shape, replay it in a scratch database."""
    with _campaign_db() as factory, session_scope(factory) as session:
        user = _local_user_or_exit(session)
        try:
            shape = simulate_campaign.campaign_shape(session, user, campaign_id, settings=settings)
        except simulate_campaign.InvalidSchedule as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    zone = ZoneInfo(shape.timezone)
    begin = datetime.now(UTC) if start is None else start
    if begin.tzinfo is None:
        begin = begin.replace(tzinfo=zone)
    try:
        report = simulate_campaign.simulate_schedule(
            shape,
            settings=settings,
            start=begin.astimezone(UTC),
            days=simulate_campaign.DEFAULT_SCHEDULE_DAYS if days is None else days,
            seed=seed,
        )
    except simulate_campaign.InvalidSchedule as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(simulate_campaign.render_schedule(report), nl=False)


# --- LinkedIn runs: start by hand, watch, cancel, arm the schedule (P2-10) -----------

schedule_app = typer.Typer(
    help="Whether scheduled LinkedIn runs may fire. Every install starts disarmed.",
    no_args_is_help=True,
)
linkedin_app.add_typer(schedule_app, name="schedule")


@linkedin_app.command("sync")
def linkedin_sync(
    ctx: typer.Context,
    full: Annotated[
        bool,
        typer.Option(
            "--full/--incremental",
            help="Page the whole connections list (--full) or stop at the first page of"
            " connections already known (--incremental, the default).",
        ),
    ] = False,
) -> None:
    """Run one connections sync now, in this terminal, and wait for it.

    This visits LinkedIn: it attaches to the Chrome you started, takes the same
    per-account browser lock `netkeeper serve` takes, opens your connections
    page, scrolls it, and reads the connections the page itself loads. It sends
    no request of its own. It stays within today's page budget and follows the
    same pacing, heat, and session-flag rules a scheduled run has. It works while
    scheduled runs are disarmed: this is how the first supervised run is done.
    Ctrl-C stops it; what it read is kept.
    """
    kind = SyncRunKind.CONNECTIONS_FULL if full else SyncRunKind.CONNECTIONS_INCREMENTAL
    _run_by_hand(ctx, kind)


@linkedin_app.command("enrich")
def linkedin_enrich(
    ctx: typer.Context,
    max_visits: Annotated[
        int | None,
        typer.Option(
            "--max-visits",
            min=1,
            help="Visit at most this many profiles. It only lowers today's budget; it"
            " never raises it.",
            show_default=False,
        ),
    ] = None,
    resume: Annotated[
        int | None,
        typer.Option(
            "--resume",
            help="Resume an aborted enrichment run's remaining plan, in its order.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Run one enrichment now, in this terminal, and wait for it.

    This visits LinkedIn profiles: each visit is a real page view, a scroll, and
    one click on Contact info, read from what the page loads and sends nothing of
    its own (ADR 0006), paced like a person, within today's warm-up-ramped,
    weekend-damped, heat-shrunk budget (`netkeeper posture` shows it). Pinned
    contacts go first. It works while scheduled runs are disarmed. Ctrl-C stops
    it between profiles; `--resume <run id>` picks up what it left.
    """
    _run_by_hand(ctx, SyncRunKind.ENRICH, max_visits=max_visits, resume=resume)


def _run_by_hand(
    ctx: typer.Context,
    kind: SyncRunKind,
    *,
    max_visits: int | None = None,
    resume: int | None = None,
) -> None:
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            user_id = user.id
            try:
                account_id = ensure_account(session, user).id
                runs.refuse_if_flagged_or_hot(
                    session, user, account_id, now=datetime.now(UTC), settings=settings.linkedin
                )
                if resume is not None:
                    run = enrich_plan.start_resume(
                        session, user, resume, now=datetime.now(UTC), max_visits=max_visits
                    )
                else:
                    run = runs.create_run(
                        session,
                        user,
                        kind,
                        trigger=SyncRunTrigger.MANUAL,
                        now=datetime.now(UTC),
                        max_visits=max_visits,
                    )
            except (
                runs.RunError,
                runs.HeatSkipped,
                runs.SessionFlagged,
                enrich_plan.PlanNotFound,
                enrich_plan.PlanFinished,
            ) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            run_id = run.id
        typer.echo(
            f"run {run_id} ({kind.value}) started; `netkeeper linkedin cancel {run_id}` stops it"
        )
        bus = EventBus()
        worker = BrowserWorker(_provider(settings), factory, settings.linkedin, bus=bus)
        asyncio.run(_execute_printing(worker, bus, run_id, user_id))
        with session_scope(factory) as session:
            finished = runs.get_run(session, _local_user_or_exit(session), run_id)
            lines = _run_lines(finished)
            failed = finished.status is SyncRunStatus.FAILED
    finally:
        engine.dispose()
    for line in lines:
        typer.echo(line)
    if failed:
        raise typer.Exit(code=1)


async def _execute_printing(
    worker: BrowserWorker, bus: EventBus, run_id: int, user_id: int
) -> None:
    subscription = bus.subscribe()

    async def show() -> None:
        async for event in subscription:
            if event.type == "run.progress":
                counts = ", ".join(
                    f"{key} {value}"
                    for key, value in event.data.items()
                    if key != "run_id" and value is not None
                )
                typer.echo(f"  {counts}")

    printer = asyncio.create_task(show())
    try:
        await worker.execute(run_id, user_id)
    finally:
        bus.unsubscribe(subscription)
        await printer


def _run_lines(run: SyncRun) -> list[str]:
    """A run as a field table: kind, status, how it ended, its counts."""
    derived = runs.view(run)
    rows: list[tuple[str, str]] = [
        ("run", str(run.id)),
        ("kind", run.kind.value),
        ("trigger", run.trigger.value),
        ("status", run.status.value),
        ("started", f"{run.started_at:%Y-%m-%d %H:%M UTC}"),
        ("ended", "-" if run.completed_at is None else f"{run.completed_at:%Y-%m-%d %H:%M UTC}"),
        ("stopped by", run.stop_reason or "-"),
    ]
    if derived.planned is not None:
        rows.append(("plan", f"{derived.completed or 0} of {derived.planned} done"))
    if run.max_visits is not None:
        rows.append(("max visits", str(run.max_visits)))
    if run.resume_of_id is not None:
        rows.append(("resumes", f"run {run.resume_of_id}"))
    for key, value in sorted((run.counts_json or {}).items()):
        if isinstance(value, dict):
            value = ", ".join(f"{k} {v}" for k, v in value.items() if v is not None) or "-"
        rows.append((key.replace("_", " "), "-" if value is None else str(value)))
    if derived.aging_refused is not None:
        rows.append(("aging refused", derived.aging_refused))
    if run.notes:
        rows.append(("notes", run.notes))
    if run.error:
        rows.append(("error", run.error))
    return _format_table(("FIELD", "VALUE"), rows).splitlines()


@linkedin_app.command("runs")
def linkedin_runs(
    limit: Annotated[int, typer.Option(min=1, max=200, help="How many runs to list.")] = 20,
) -> None:
    """List recent LinkedIn runs, newest first. Reads only."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            rows, total = runs.list_runs(session, user, limit=limit)
            table = [
                (
                    str(run.id),
                    run.kind.value,
                    run.trigger.value,
                    run.status.value,
                    f"{run.started_at:%Y-%m-%d %H:%M}",
                    run.stop_reason or "-",
                )
                for run in rows
            ]
    finally:
        engine.dispose()
    if not table:
        typer.echo("no runs yet")
        return
    typer.echo(
        _format_table(("RUN", "KIND", "TRIGGER", "STATUS", "STARTED (UTC)", "STOPPED BY"), table),
        nl=False,
    )
    if total > len(table):
        typer.echo(f"({total - len(table)} older runs not shown)")


@linkedin_app.command("run")
def linkedin_run(run_id: Annotated[int, typer.Argument(help="The run to show.")]) -> None:
    """Show one run: how far it got, how it ended, and its counts. Reads only."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            try:
                lines = _run_lines(runs.get_run(session, user, run_id))
            except runs.RunNotFound as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    for line in lines:
        typer.echo(line)


@linkedin_app.command("cancel")
def linkedin_cancel(run_id: Annotated[int, typer.Argument(help="The run to stop.")]) -> None:
    """Ask a running run to stop at its next check (between pages or profiles).

    Works across processes: the flag is on the run's row, so this stops a run
    `netkeeper serve` or another terminal is doing. The run ends `aborted` and
    keeps what it completed.
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                run = runs.request_cancel(session, user, run_id, now=datetime.now(UTC))
            except (runs.RunNotFound, runs.RunFinished) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            left_behind = run.status is SyncRunStatus.FAILED
    finally:
        engine.dispose()
    if left_behind:
        typer.echo(
            f"run {run_id} was left running by a process that is gone (nothing holds its"
            " browser lock); marked it failed"
        )
        return
    typer.echo(f"asked run {run_id} to stop; it stops at its next check")


@schedule_app.command("status")
def linkedin_schedule_status() -> None:
    """Whether scheduled LinkedIn runs are armed, and the counts that skip them. Reads only."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            account = find_account(session, user)
            armed_at = None if account is None else account.scheduled_runs_armed_at
            account_id = account_id_for(session, user)
            route = route_breaker.state(session, user, account_id)
            lost = route_breaker.answer_lost_states(session, user, account_id)
    finally:
        engine.dispose()
    if armed_at is None:
        typer.echo(
            "disarmed: `netkeeper serve` fires no scheduled LinkedIn run. Runs are the ones"
            " you start (`netkeeper linkedin sync`, `netkeeper linkedin enrich`)."
        )
    else:
        typer.echo(f"armed since {armed_at:%Y-%m-%d %H:%M UTC}: scheduled runs fire when due")
    typer.echo(_streak_line("route-changed breaker", "route_changed", route))
    for kind, streak in lost.items():
        typer.echo(_streak_line("answer-lost limit", "answer_lost", streak, kind=kind))


def _streak_line(
    name: str,
    stop_reason: str,
    streak: route_breaker.BreakerState,
    *,
    kind: SyncRunKind | None = None,
) -> str:
    """One line of `schedule status` for a streak that skips scheduled connections runs."""
    runs_of = "connections" if kind is None else kind.value
    if kind is not None:
        name = f"{name} ({kind.value})"
    if not streak.readable:
        return (
            f"{name}: stored state unreadable, treated as tripped; scheduled connections"
            " runs are skipped (`netkeeper linkedin schedule reset-breaker`)"
        )
    counted = f"{streak.count} of {streak.threshold} {stop_reason} {runs_of} runs in a row"
    if streak.tripped:
        return (
            f"{name}: tripped, {counted}; scheduled connections runs are skipped"
            " (`netkeeper linkedin schedule reset-breaker`)"
        )
    return f"{name}: {counted}"


@schedule_app.command("arm")
def linkedin_schedule_arm(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Let `netkeeper serve` run LinkedIn jobs on its own schedule.

    Until this, serve's scheduler keeps due times and fires nothing. Once armed,
    the weekly full sync (due soon after arming if it has never run), the daily
    incremental sync, and enrichment run on their own, within your budgets and
    active hours. Arm only after a supervised run by hand has gone well.
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        # Read, then ask, then write: the prompt waits on a person, and a writer
        # session held across it would lock out `serve` for as long (#175 review, F4).
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            account = find_account(session, user)
            already = account is not None and account.scheduled_runs_armed_at is not None
        if already:
            typer.echo("scheduled LinkedIn runs are already armed")
            return
        if not yes and not typer.confirm(
            "arm scheduled LinkedIn runs? netkeeper serve will then visit LinkedIn on its"
            " own schedule, without you starting each run"
        ):
            typer.echo("cancelled: scheduled runs stay disarmed")
            raise typer.Exit(code=1)
        with session_scope(factory, write=True) as session:
            # Arming is idempotent, so a change between the read and here is harmless.
            arm_scheduled_runs(session, _local_user_or_exit(session), now=datetime.now(UTC))
    finally:
        engine.dispose()
    typer.echo("scheduled LinkedIn runs armed; `netkeeper linkedin schedule disarm` undoes it")


@schedule_app.command("disarm")
def linkedin_schedule_disarm() -> None:
    """Stop scheduled LinkedIn runs from firing. A run already going is not stopped."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            disarm_scheduled_runs(session, _local_user_or_exit(session))
    finally:
        engine.dispose()
    typer.echo("scheduled LinkedIn runs disarmed")


@schedule_app.command("reset-breaker")
def linkedin_schedule_reset_breaker(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Clear the route-changed breaker and the answer-lost limit so scheduled
    connections runs can fire again.

    Two connections runs in a row (full or incremental, by hand or by
    schedule) ending `route_changed` trip it (#189 item 1): a wall served in
    place at the connections url raises no heat and sets no session flag, so
    this breaker is the only thing that stops a scheduled sync from loading it
    again at every interval. `netkeeper posture` shows the count. A manual run
    (`netkeeper linkedin sync`) that reaches a natural end clears it the same
    way, without this command -- that is how you check whether the wall is
    still there.

    Three runs in a row of one connections kind (full or incremental) ending
    `answer_lost` trip the answer-lost limit (#199) the same way; this clears
    every count. A manual run of that kind that completes with nothing lost
    clears its kind's count too.
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            account_id = account_id_for(session, user)
            current = route_breaker.state(session, user, account_id)
            lost = route_breaker.answer_lost_states(session, user, account_id)
        if (
            current.readable
            and current.count == 0
            and all(s.readable and s.count == 0 for s in lost.values())
        ):
            typer.echo(
                "neither the route-changed breaker nor the answer-lost limit has a count;"
                " nothing to reset"
            )
            return
        # A corrupt row reads as tripped (fail closed), and posture tells the
        # person to run this command, so this command must be able to clear it
        # (#191 review N1). It clears the answer-lost limit too (#199).
        counts = ", ".join(
            [
                f"{current.count} `route_changed`"
                if current.readable
                else "route_changed unreadable",
                *(
                    f"{s.count} `answer_lost` {kind.value}"
                    if s.readable
                    else f"answer_lost {kind.value} unreadable"
                    for kind, s in lost.items()
                ),
            ]
        )
        question = (
            f"reset the route-changed breaker and the answer-lost limit ({counts} connections"
            " run(s) in a row)? scheduled connections runs will be allowed to fire again"
            if current.readable and all(s.readable for s in lost.values())
            else f"a stored breaker state is unreadable, so it reads as tripped ({counts})."
            " reset them all? scheduled connections runs will be allowed to fire again"
        )
        if not yes and not typer.confirm(question):
            typer.echo("cancelled: the breaker stays as it is")
            raise typer.Exit(code=1)
        with session_scope(factory, write=True) as session:
            route_breaker.reset(session, _local_user_or_exit(session), account_id)
    finally:
        engine.dispose()
    typer.echo(
        "route-changed breaker and answer-lost limit reset; scheduled connections runs may"
        " fire again when due"
    )


@backup_app.callback()
def backup_group(ctx: typer.Context) -> None:
    """Snapshot the database into the data directory's backups/, and list the snapshots.

    `netkeeper backup` on its own is `netkeeper backup create`.
    """
    if ctx.invoked_subcommand is None:
        _backup_create(ctx)


@backup_app.command("create")
def backup_create(ctx: typer.Context) -> None:
    """Snapshot the database with VACUUM INTO, then prune to the newest backup.keep files."""
    _backup_create(ctx)


@backup_app.command("list")
def backup_list() -> None:
    """List the backups in the data directory's backups/, newest first."""
    directory = data_dir() / BACKUPS_DIRNAME
    backups = list_backups(directory)
    if not backups:
        typer.echo(f"no backups in {directory}")
        return
    now = datetime.now(UTC)
    rows = [
        (info.path.name, _human_size(info.size), _human_age(now - info.created_at))
        for info in backups
    ]
    typer.echo(_format_table(("NAME", "SIZE", "AGE"), rows), nl=False)


def _backup_create(ctx: typer.Context) -> None:
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    keep = settings.backup.keep
    if keep < 1:
        typer.echo(f"error: backup.keep must be at least 1, got {keep}", err=True)
        raise typer.Exit(code=1)
    directory = data_dir() / BACKUPS_DIRNAME
    try:
        written = create_backup(database_url(), directory)
    except BackupError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    removed = prune_backups(directory, keep)
    typer.echo(f"wrote {written} ({_human_size(written.stat().st_size)})")
    noun = "backup" if len(removed) == 1 else "backups"
    typer.echo(f"pruned {len(removed)} older {noun} (keeping the newest {keep})")


@tags_app.command("list")
def tags_list() -> None:
    """List the local user's tags with kind, color, and how many contacts carry each."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            tags = list_tags(session, user)
    finally:
        engine.dispose()
    if not tags:
        typer.echo("no tags")
        return
    rows = [
        (row.tag.name, row.tag.kind.value, row.tag.color or "-", str(row.contact_count))
        for row in tags
    ]
    typer.echo(_format_table(("NAME", "KIND", "COLOR", "CONTACTS"), rows), nl=False)


@tags_app.command("run-rules")
def tags_run_rules() -> None:
    """Apply every enabled auto-tag rule to every live contact (seeding the defaults first)."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            seeded = ensure_default_rules(session, user)
            result = run_rules(session, user)
    finally:
        engine.dispose()
    if seeded:
        typer.echo(f"seeded {len(seeded)} default rules")
    line = (
        f"{result.contacts} contacts: {result.added} tags added, {result.removed} removed, "
        f"{result.updated} re-credited"
    )
    if result.timeouts:
        line += f", {result.timeouts} searches timed out (treated as no match; see the log)"
    typer.echo(line)


@lists_app.command("list")
def lists_list() -> None:
    """List the local user's lists with kind and how many contacts are in each right now."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            rows = list_lists(session, user)
            counts = member_counts(session, user, rows)
    finally:
        engine.dispose()
    if not rows:
        typer.echo("no lists")
        return
    table = [(row.name, row.kind.value, _member_cell(counts.get(row.id))) for row in rows]
    typer.echo(_format_table(("NAME", "KIND", "MEMBERS"), table), nl=False)


def _member_cell(count: ListCount | None) -> str:
    """The MEMBERS cell: the number, or why there is no number for that one list."""
    if count is None:
        return "0"
    return "broken" if count.broken else str(count.count)


@lists_app.command("views")
def lists_views() -> None:
    """List the local user's saved table views with their columns."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            rows = list_views(session, user)
    finally:
        engine.dispose()
    if not rows:
        typer.echo("no saved views")
        return
    table = [(row.name, ", ".join(row.columns)) for row in rows]
    typer.echo(_format_table(("NAME", "COLUMNS"), table), nl=False)


# --- import -------------------------------------------------------------


@import_app.command("archive")
def import_archive_cmd(
    path: Annotated[
        Path,
        typer.Argument(
            help="A LinkedIn data export: a zip, an unpacked directory, or one CSV on its own.",
            exists=True,
            readable=True,
        ),
    ],
) -> None:
    """Import a LinkedIn archive: connections, messages, and invitations (netkeeper.crm.archive)."""
    try:
        with open_checked_archive(path) as archive:
            engine = make_engine(database_url())
            try:
                factory = make_session_factory(engine)
                install_scope_guard(factory)
                with session_scope(factory, write=True) as session:
                    user = _local_user_or_exit(session)
                    report = import_archive(session, user, archive)
                    summary = _archive_report(report)
            finally:
                engine.dispose()
    except ArchiveFormatError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(summary)


def _archive_report(report: ArchiveImport) -> str:
    c, p = report.connections, report.positions
    m, i, t = report.messages, report.invitations, report.tagging
    unfamiliar = tuple(
        f"warning: read {name} as message history, but it is not messages.csv; "
        "if it is a LinkedIn assistant chat log, its conversations are counted above "
        "but added nothing"
        for name in report.unfamiliar_message_files
    )
    return "\n".join(
        (
            f"connections: {c.rows} rows, {c.created} created, {c.updated} updated, "
            f"{c.skipped} skipped, {c.needs_review} needs review",
            f"positions: {p.rows} rows, {p.created} created, {p.updated} updated, "
            f"{p.unchanged} unchanged, {p.skipped} skipped",
            f"messages: {m.rows} rows in {m.conversations} conversations "
            f"({m.attributed} attributed, {m.no_counterpart} no counterparty, "
            f"{m.group_threads} group, {m.unknown_contact} not a contact); "
            f"{m.added} interactions added",
            f"invitations: {i.rows} rows; {i.added} interactions added",
            f"auto-tag rules: {t.contacts} contacts examined, {t.added} tags added, "
            f"{t.removed} removed",
            *unfamiliar,
            f"recorded as import run {report.run_id}; undo it with "
            f"`netkeeper import rollback {report.run_id}`",
        )
    )


# Candidates are ambiguous on purpose (spec 8.2 step 4): "merge" cannot be a
# blanket flag because merging is which-contact, not whether-to-merge, and
# Candidate.contact_ids is ascending by id, not ranked by confidence, so
# merging into contact_ids[0] would be merging into whichever row happens to
# be oldest, not the one that is actually the same person. Only the two
# decisions that are safe in bulk are offered: skip (nothing happens) and new
# (a distinct contact that can still be merged by hand later). The default,
# with neither flag, is to refuse and say which rows need a look.
# Not a PEP 695 `type` alias: Typer's parameter introspection does not chase
# TypeAliasType through to the Literal it wraps (RuntimeError: "Type not yet
# supported"), so this stays a plain assignment, as netkeeper.crm.exports does
# for ExportFormat and ExportPreset.
OnCandidate = Literal["new", "skip"]


@import_app.command("csv")
def import_csv_cmd(
    path: Annotated[
        Path,
        typer.Argument(help="The CSV file to import.", exists=True, dir_okay=False, readable=True),
    ],
    preset: Annotated[
        str | None,
        typer.Option("--preset", help="A built-in preset name, or one you saved. Default: detect."),
    ] = None,
    mapping: Annotated[
        str | None,
        typer.Option(
            "--mapping", help='A column mapping as JSON, e.g. \'{"Company": "current_company"}\'.'
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Resolve the whole file and report, leaving a draft run behind."
        ),
    ] = False,
    on_candidate: Annotated[
        OnCandidate | None,
        typer.Option(
            "--on-candidate",
            help="How to resolve a row that matches more than one contact: "
            "new (a separate contact) or skip (leave it out). Applies to every ambiguous "
            "row in the file at once, not one at a time, and to each row on its own: with "
            "new, a person listed twice in the file becomes two new contacts. "
            "Omitted: refuse and say which rows.",
        ),
    ] = None,
) -> None:
    """Import a CSV: read it into a draft run, then commit it (netkeeper.crm.import_runs).

    Reading the file and committing it are two separate transactions, the way
    ``POST /imports`` and ``POST /imports/{id}/commit`` are two separate
    requests: a draft always lands, even when this goes on to refuse the
    commit, so "decide them in the app" or a later ``--on-candidate`` names a
    run that is actually there to open or resume.
    """
    with _reporting_lock_races():
        parsed_mapping = _mapping_or_exit(mapping)
        content = path.read_bytes()
        draft = _create_draft(path.name, content, preset, parsed_mapping)
        lines = [_run_report("draft", draft)]
        if dry_run:
            typer.echo("\n".join(lines))
            return
        if draft.candidate_rows and on_candidate is None:
            _refuse_undecided(draft.id, draft.candidate_rows)
        committed = _commit_run(draft.id, on_candidate)
        lines.append(_run_report("committed", committed))
        typer.echo("\n".join(lines))


@dataclass(frozen=True, slots=True)
class _RunSnapshot:
    """The fields of an ``ImportRun`` the CLI reports, read while its session was open."""

    id: int
    filename: str
    preset: str | None
    total_rows: int
    matched_count: int
    created_count: int
    candidate_count: int
    skipped_count: int
    tagged_contacts: int = 0
    tags_added: int = 0
    candidate_rows: tuple[int, ...] = ()


def _snapshot_of(run: ImportRun, *, candidate_rows: tuple[int, ...] = ()) -> _RunSnapshot:
    return _RunSnapshot(
        id=run.id,
        filename=run.filename,
        preset=run.preset,
        total_rows=run.total_rows,
        matched_count=run.matched_count,
        created_count=run.created_count,
        candidate_count=run.candidate_count,
        skipped_count=run.skipped_count,
        tagged_contacts=run.tagged_contacts,
        tags_added=run.tags_added,
        candidate_rows=candidate_rows,
    )


def _create_draft(
    filename: str, content: bytes, preset: str | None, mapping: dict[str, str] | None
) -> _RunSnapshot:
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                run = import_runs.create_run(
                    session,
                    user,
                    filename=filename,
                    content=content,
                    preset_name=preset,
                    mapping=mapping,
                )
            # EmptyFile, MalformedCsv, InvalidMapping, and UnknownPreset are
            # netkeeper.crm.importer.CsvImportError, not ImportRunError: reading
            # the file and running it as an import are different failure modes,
            # exactly as they are two different exception groups in
            # web/api/imports.py's translate_errors().
            except (import_runs.ImportRunError, import_runs.CsvImportError) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            candidates = tuple(
                row.row_number for row in run.rows if row.resolution is ImportResolution.CANDIDATE
            )
            return _snapshot_of(run, candidate_rows=candidates)
    finally:
        engine.dispose()


def _commit_run(run_id: int, on_candidate: OnCandidate | None) -> _RunSnapshot:
    """Commit ``run_id`` in its own engine and transaction, under ``on_candidate``.

    The policy goes to ``commit()`` itself (#136), so the rows it applies to are
    the ones that commit finds undecided, in the same transaction that decides
    them; nothing is read off the draft's stored resolution. Without a policy a
    still-undecided row refuses the commit, naming the run to finish.
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                run = import_runs.commit(
                    session, user, run_id, undecided=_UNDECIDED_POLICY[on_candidate]
                )
            except import_runs.UndecidedCandidates as exc:
                _refuse_undecided(run_id, exc.row_numbers)
            # commit() does not raise CsvImportError today, but catching it here
            # too keeps this in step with web/api/imports.py's translate_errors(),
            # which covers it on every route: the asymmetry between the two
            # exception groups is exactly what _create_draft got bitten by once.
            except (import_runs.ImportRunError, import_runs.CsvImportError) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            return _snapshot_of(run)
    finally:
        engine.dispose()


_UNDECIDED_POLICY: Final[dict[OnCandidate | None, import_runs.UndecidedPolicy]] = {
    None: import_runs.UndecidedPolicy.REFUSE,
    "skip": import_runs.UndecidedPolicy.SKIP,
    "new": import_runs.UndecidedPolicy.CREATE_NEW,
}


@contextmanager
def _reporting_lock_races() -> Iterator[None]:
    """Wrap a writing import command's whole body: a lost SQLite write-lock race must
    not surface as a raw traceback. ``import csv``, ``resume``, and ``rm`` each open one
    or more writer sessions, and any of them can lose a race to a concurrent writer -- these are
    destructive or state-changing commands, not the read-mostly ones.
    """
    try:
        yield
    except OperationalError as exc:
        typer.echo(f"error: the database is busy ({exc.orig}); try again in a moment", err=True)
        raise typer.Exit(code=1) from exc


def _mapping_or_exit(raw: str | None) -> dict[str, str] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: --mapping is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        typer.echo('error: --mapping must be a JSON object of {"header": "field"}', err=True)
        raise typer.Exit(code=1)
    return {str(key): value for key, value in data.items()}


def _refuse_undecided(run_id: int, row_numbers: Sequence[int]) -> NoReturn:
    shown = ", ".join(str(number) for number in row_numbers[:10])
    more = "" if len(row_numbers) <= 10 else f" and {len(row_numbers) - 10} more"
    typer.echo(
        f"error: {len(row_numbers)} row(s) match more than one contact and have no decision "
        f"(rows {shown}{more}); decide them in the app (import run {run_id}), or finish this "
        f"run with `netkeeper import resume {run_id} --on-candidate new` (or `--on-candidate "
        "skip`) -- re-running `import csv` would read the file into a second draft and leave "
        f"run {run_id} behind as an orphan",
        err=True,
    )
    raise typer.Exit(code=1)


def _run_report(label: str, run: _RunSnapshot) -> str:
    report = (
        f"{label} run {run.id}: {run.total_rows} rows from {run.filename!r} "
        f"({run.preset or 'custom mapping'}); {run.matched_count} matched, "
        f"{run.created_count} created, {run.candidate_count} candidate(s), "
        f"{run.skipped_count} skipped"
    )
    # The rules run at commit, over the contacts the commit wrote (#64), so a
    # draft has nothing to report here.
    if run.tagged_contacts:
        report += f"; {run.tags_added} tags added over {run.tagged_contacts} contacts"
    return report


@import_app.command("resume")
def import_resume_cmd(
    run_id: Annotated[
        int, typer.Argument(help="A draft import run to finish, left by --dry-run or a refusal.")
    ],
    on_candidate: Annotated[
        OnCandidate | None,
        typer.Option(
            "--on-candidate",
            help="How to resolve a row that matches more than one contact: "
            "new (a separate contact) or skip (leave it out). Applies to every row still "
            "undecided in the run at once, not one at a time, and to each row on its own: with "
            "new, a person listed twice in the file becomes two new contacts. "
            "Omitted: refuse and say which rows.",
        ),
    ] = None,
) -> None:
    """Finish a draft run without reading its file again (netkeeper.crm.import_runs).

    The CLI counterpart of the web wizard's "Finish this import": it commits the
    named run in place, so a run a refused commit named -- or one left by
    ``--dry-run`` -- is the one that gets finished, not a second draft next to
    it (#90). ``netkeeper import runs --status draft`` lists the candidates.

    Unlike ``import csv``, where the read and the commit are the same instant,
    time passes before a resume: a row this run once saw as a candidate may by
    now be an outright match or a plain new contact, because someone decided
    it another way, or a colliding contact was merged, edited, or deleted.
    ``commit()`` re-resolves every row against the database as it is *now*
    (``import_runs.py``) and applies ``--on-candidate`` to the rows it finds
    undecided in that same transaction (#136), rather than trusting the
    resolution the draft recorded when it was read.
    """
    with _reporting_lock_races():
        committed = _commit_run(run_id, on_candidate)
    typer.echo(_run_report("committed", committed))


@import_app.command("rollback")
def import_rollback_cmd(
    run_id: Annotated[int, typer.Argument(help="The import run to undo.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Roll back even though contacts the run created have gained interactions, "
            "tags, lists, campaign enrollments or edits since; they are deleted with them. "
            "Never overrides a campaign message, a merge or a later run that wrote over "
            "this one.",
        ),
    ] = False,
) -> None:
    """Undo a committed import run: delete what it created, restore what it enriched.

    Refused, with nothing undone, when a contact the run created has a campaign
    message (#242), when a merge has drawn in a contact the run
    created, when a later run wrote over fields it wrote (roll that one back
    first), or when contacts it created have gained things since, which
    ``--force`` deletes anyway (#78).
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                result = import_runs.rollback(session, user, run_id, force=force)
            # Same defensive symmetry as _commit_run: rollback() does not raise
            # CsvImportError today, but the except clause matches
            # translate_errors() rather than assuming it never will.
            except (import_runs.ImportRunError, import_runs.CsvImportError) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            summary = (
                f"run {result.run_id}: {result.contacts_deleted} contact(s) deleted, "
                f"{result.contacts_restored} restored, {result.fields_restored} field(s) "
                f"put back, {result.children_deleted} child row(s) removed"
            )
    finally:
        engine.dispose()
    typer.echo(summary)


@import_app.command("runs")
def import_runs_cmd(
    status: Annotated[
        ImportStatus | None,
        typer.Option("--status", help="Only runs in this state, e.g. draft. Omitted: every run."),
    ] = None,
) -> None:
    """List import runs, newest first.

    A draft left by ``--dry-run`` or a refused commit stays here -- narrow with
    ``--status draft`` -- until it is finished with ``import resume`` or
    removed with ``import rm`` (#90).
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            runs, total = import_runs.list_runs(session, user, status=status, limit=200)
    finally:
        engine.dispose()
    if not runs:
        typer.echo("no import runs" if status is None else f"no {status.value} import runs")
        return
    rows = [(str(run.id), run.filename, run.status.value, str(run.total_rows)) for run in runs]
    typer.echo(_format_table(("ID", "FILENAME", "STATUS", "ROWS"), rows), nl=False)
    if total > len(runs):
        typer.echo(f"...and {total - len(runs)} more; narrow with --status")


@import_app.command("rm")
def import_rm_cmd(
    run_id: Annotated[
        int, typer.Argument(help="A draft import run to delete. A committed run is refused.")
    ],
) -> None:
    """Delete a draft run and its rows (#90). Refuses a committed or rolled-back run."""
    with _reporting_lock_races():
        engine = make_engine(database_url())
        try:
            factory = make_session_factory(engine)
            install_scope_guard(factory)
            with session_scope(factory, write=True) as session:
                user = _local_user_or_exit(session)
                try:
                    import_runs.delete_run(session, user, run_id)
                except (import_runs.ImportRunError, import_runs.CsvImportError) as exc:
                    typer.echo(f"error: {exc}", err=True)
                    raise typer.Exit(code=1) from exc
        finally:
            engine.dispose()
    typer.echo(f"deleted import run {run_id}")


# --- export ---------------------------------------------------------------


@app.command("export")
def export_cmd(
    preset: Annotated[ExportPreset, typer.Option("--preset")] = "full",
    output_format: Annotated[ExportFormat, typer.Option("--format")] = "json",
    headerless: Annotated[
        bool, typer.Option("--headerless", help="Drop the CSV header row (ignored otherwise).")
    ] = False,
    spreadsheet_safe: Annotated[
        bool,
        typer.Option(
            "--spreadsheet-safe",
            help=(
                "Quote CSV cells a spreadsheet would run as formulas. Safe to open in a "
                "spreadsheet, not safe to re-import (ignored otherwise)."
            ),
        ),
    ] = False,
    filter_: Annotated[
        str | None,
        typer.Option("--filter", help="A FilterTree (spec 10.4) as JSON. Omitted: every contact."),
    ] = None,
    sort: Annotated[
        str | None,
        typer.Option("--sort", help="A list of SortKey as JSON. Omitted: id ascending."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="File to write to. Omitted: stdout.")
    ] = None,
) -> None:
    """Export contacts as CSV, JSON, or vCard: the same presets as ``GET /exports``."""
    tree = _filter_or_exit(filter_)
    sort_keys = _sort_or_exit(sort)
    now = datetime.now(UTC)
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            try:
                body = export_stream(
                    session,
                    user,
                    preset=preset,
                    output_format=output_format,
                    headerless=headerless,
                    spreadsheet_safe=spreadsheet_safe,
                    tree=tree,
                    sort=sort_keys,
                    now=now,
                )
            except FilterError as exc:
                # Compiling is where a predicate the language parses but the
                # compiler will not take shows up, and export_stream() does it
                # before it yields anything (#95). Same message the API's 422
                # carries, rather than a traceback over a half-written file.
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            except ExportError as exc:
                # macos-contacts asked for as CSV or JSON: refused before --out is opened.
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            if out is not None:
                with out.open("w", encoding="utf-8", newline="") as handle:
                    for chunk in body:
                        handle.write(chunk)
            else:
                for chunk in body:
                    typer.echo(chunk, nl=False)
    finally:
        engine.dispose()
    if out is not None:
        typer.echo(f"wrote {out}")


def _filter_or_exit(raw: str | None) -> FilterTree:
    if raw is None:
        return FilterTree()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: --filter is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        return parse_filter(data)
    except FilterError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _sort_or_exit(raw: str | None) -> list[SortKey]:
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: --sort is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        return parse_sort(data)
    except FilterError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# --- campaigns (P3-13) --------------------------------------------------------
#
# Each command mirrors a `/campaigns` route. Activation goes through the review gate
# (`campaign_review.activate`) and nothing else: this module never names the engine's
# gate token, and `tests/test_cli_campaigns.py` checks that it doesn't.


@contextmanager
def _campaign_db() -> Iterator[sessionmaker[Session]]:
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        yield factory
    finally:
        engine.dispose()


@contextmanager
def _campaign_errors() -> Iterator[None]:
    """A refusal from the campaign services, as `error: ...` and exit 1."""
    try:
        yield
    except (campaign_service.CampaignError, campaign_review.ReviewError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _counts_cell(counts: Mapping[EnrollmentStatus, int]) -> str:
    shown = [f"{n} {status.value}" for status in EnrollmentStatus if (n := counts.get(status))]
    return ", ".join(shown) or "none"


def _missing_lines(missing: Sequence[campaign_review.Missing]) -> list[str]:
    lines = []
    for m in missing:
        where = ""
        if m.enrollment_ids:
            where = " (enrollments " + ", ".join(str(i) for i in m.enrollment_ids) + ")"
        elif m.step_positions:
            where = " (steps " + ", ".join(str(p) for p in m.step_positions) + ")"
        lines.append(f"  - {m.requirement}: {m.detail}{where}")
    return lines


def _when(at: datetime | None) -> str:
    return "-" if at is None else f"{at:%Y-%m-%d %H:%M UTC}"


def _list_id_or_exit(session: Session, user: User, which: str | None) -> int | None:
    """The id of the user's list named by its name or id."""
    if which is None:
        return None
    found = find_list(session, user, which)
    if found is not None:
        return found.id
    if which.isdigit():
        return int(which)  # the service refuses an id that is not the user's
    typer.echo(f"error: no list {which!r}", err=True)
    raise typer.Exit(code=1)


def _audience_filter_or_exit(raw: str | None) -> FilterTree | None:
    return None if raw is None else _filter_or_exit(raw)


def _step_or_exit(raw: str) -> campaign_service.StepSpec:
    """``TEMPLATE_ID[:DELAY_DAYS[:MODE]]``."""
    parts = raw.split(":")
    try:
        if not 1 <= len(parts) <= 3:
            raise ValueError
        template_id = int(parts[0])
        delay = int(parts[1]) if len(parts) > 1 and parts[1] else None
        mode = StepMode(parts[2]) if len(parts) > 2 else None
    except ValueError as exc:
        modes = ", ".join(m.value for m in StepMode)
        typer.echo(
            f"error: --step {raw!r}: expected TEMPLATE_ID[:DELAY_DAYS[:MODE]], MODE one of {modes}",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    return campaign_service.StepSpec(template_id=template_id, delay_days=delay, mode=mode)


@campaigns_app.command("list")
def campaigns_list() -> None:
    """List the campaigns, newest first, with their enrollments by status (GET /campaigns)."""
    with _campaign_db() as factory, session_scope(factory) as session:
        user = _local_user_or_exit(session)
        rows = [
            (
                str(row.campaign.id),
                row.campaign.name,
                row.campaign.status.value,
                str(row.steps),
                _counts_cell(row.enrollments),
            )
            for row in campaign_service.list_campaigns(session, user)
        ]
    if not rows:
        typer.echo("no campaigns; create one with `netkeeper campaigns create`")
        return
    typer.echo(_format_table(("ID", "NAME", "STATUS", "STEPS", "ENROLLMENTS"), rows), nl=False)


@campaigns_app.command("status")
def campaigns_status(
    ctx: typer.Context,
    campaign_id: Annotated[int, typer.Argument(help="The campaign's ID.")],
) -> None:
    """Show one campaign: its steps, enrollments, next fire, and review (GET /campaigns/{id}).

    For a draft or reviewing campaign it lists what activation still needs.
    """
    settings = _load_settings_or_exit(ctx.ensure_object(CliState))
    with _campaign_db() as factory, session_scope(factory) as session, _campaign_errors():
        user = _local_user_or_exit(session)
        detail = campaign_service.campaign_status(
            session, user, campaign_id, me=me_fields(settings.me), now=datetime.now(UTC)
        )
        c = detail.campaign
        lines = [
            f"campaign {c.id}: {c.name}",
            f"status: {c.status.value}",
            f"mailbox: {detail.mailbox_email or '-'}",
            f"daily cap: {'config' if c.daily_cap is None else c.daily_cap}",
            f"enrollments: {_counts_cell(detail.enrollments)}",
            f"next fire: {_when(detail.next_action_at)}",
        ]
        steps = [
            (
                str(s.step.position),
                s.step.channel.value,
                s.step.mode.value,
                f"+{s.step.delay_days}d",
                s.step.condition.value,
                "yes" if s.step.same_thread else "no",
                f"{s.template_name} v{s.template_version}",
                str(s.fired),
                str(s.sent),
            )
            for s in detail.steps
        ]
        missing = detail.missing
        status = c.status
    typer.echo("\n".join(lines))
    typer.echo(
        _format_table(
            (
                "STEP",
                "CHANNEL",
                "MODE",
                "DELAY",
                "CONDITION",
                "THREAD",
                "TEMPLATE",
                "FIRED",
                "SENT",
            ),
            steps,
        ),
        nl=False,
    )
    if status in campaign_service.REVIEWABLE:
        if missing:
            typer.echo("review: activation still needs")
            typer.echo("\n".join(_missing_lines(missing)))
        else:
            typer.echo("review: complete; `netkeeper campaigns activate` can activate it")


@campaigns_app.command("create")
def campaigns_create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The campaign's name, also its Gmail label.")],
    step: Annotated[
        list[str],
        typer.Option(
            "--step",
            help="A step, in order: TEMPLATE_ID[:DELAY_DAYS[:MODE]]. Repeat for each step."
            " Defaults: the first at once, the rest 7 days on; draft for email, prefill for"
            " LinkedIn.",
        ),
    ],
    mailbox: Annotated[
        str | None,
        typer.Option("--mailbox", help="The mailbox's address or ID. Needed for an email step."),
    ] = None,
    list_name: Annotated[
        str | None, typer.Option("--list", help="The audience: a list's name or ID.")
    ] = None,
    filter_json: Annotated[
        str | None, typer.Option("--filter", help="The audience: a filter, as JSON.")
    ] = None,
    daily_cap: Annotated[
        int | None, typer.Option(help="The campaign's own daily cap. Default: the config's.")
    ] = None,
) -> None:
    """Create a draft campaign (POST /campaigns). Nobody is enrolled until `enroll`."""
    settings = _load_settings_or_exit(ctx.ensure_object(CliState))
    steps = [_step_or_exit(raw) for raw in step]
    audience = _audience_filter_or_exit(filter_json)
    with _campaign_db() as factory, session_scope(factory, write=True) as session:
        user = _local_user_or_exit(session)
        mailbox_id = None if mailbox is None else _mailbox_or_exit(session, user, mailbox).id
        list_id = _list_id_or_exit(session, user, list_name)
        with _campaign_errors():
            campaign = campaign_service.create_campaign(
                session,
                user,
                name=name,
                steps=steps,
                settings=settings,
                mailbox_id=mailbox_id,
                list_id=list_id,
                filter=audience,
                daily_cap=daily_cap,
            )
        line = f"created draft campaign {campaign.id} {campaign.name!r} with {len(steps)} steps"
    typer.echo(line)


@campaigns_app.command("enroll")
def campaigns_enroll(
    campaign_id: Annotated[int, typer.Argument(help="The campaign's ID.")],
    list_name: Annotated[
        str | None,
        typer.Option("--list", help="Set the audience to this list (name or ID) first."),
    ] = None,
    filter_json: Annotated[
        str | None, typer.Option("--filter", help="Set the audience to this filter first (JSON).")
    ] = None,
    contact: Annotated[
        list[int] | None, typer.Option("--contact", help="Also enroll this contact ID.")
    ] = None,
) -> None:
    """Enroll the audience as pending, through the guards (POST /campaigns/{id}/enroll).

    Only a draft or reviewing campaign takes anyone. `--list` or `--filter` sets the
    audience first, on a draft only.
    """
    audience = _audience_filter_or_exit(filter_json)
    with _campaign_db() as factory, session_scope(factory, write=True) as session:
        user = _local_user_or_exit(session)
        list_id = _list_id_or_exit(session, user, list_name)
        with _campaign_errors():
            outcome = campaign_service.enroll(
                session,
                user,
                campaign_id,
                now=datetime.now(UTC),
                list_id=list_id,
                filter=audience,
                contact_ids=contact or (),
            )
    typer.echo(
        f"campaign {campaign_id}: {outcome.enrolled} enrolled, {outcome.already} already in,"
        f" {outcome.excluded} excluded"
    )
    typer.echo(outcome.summary)


@campaigns_app.command("activate")
def campaigns_activate(
    ctx: typer.Context,
    campaign_id: Annotated[int, typer.Argument(help="The campaign's ID.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Activate a reviewed campaign through the review gate (POST /campaigns/{id}/activate).

    Refused, with the list of what is missing, unless every review requirement is
    recorded and current: sampled and searched previews approved, a test send of
    each email step, a clean lint, and the guard summary acknowledged. Once active,
    `netkeeper serve` fires its steps on an armed mailbox.
    """
    settings = _load_settings_or_exit(ctx.ensure_object(CliState))
    me = me_fields(settings.me)
    with _campaign_db() as factory:
        # Read, then ask, then write: a writer held across the prompt would lock out serve.
        with session_scope(factory) as session, _campaign_errors():
            user = _local_user_or_exit(session)
            campaign = campaign_review.get_campaign(session, user, campaign_id)
            name = campaign.name
            gaps = campaign_review.missing(session, user, campaign, me=me, now=datetime.now(UTC))
        if gaps:
            _refuse_activation(campaign_id, gaps)
        if not yes and not typer.confirm(
            f"activate campaign {campaign_id} {name!r}? `netkeeper serve` fires its steps"
            " from the next tick on an armed mailbox"
        ):
            typer.echo(f"cancelled: campaign {campaign_id} stays in review")
            raise typer.Exit(code=1)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                campaign_review.activate(
                    session, user, campaign_id, settings=settings, me=me, now=datetime.now(UTC)
                )
            except campaign_review.ReviewIncomplete as exc:
                _refuse_activation(campaign_id, exc.missing, cause=exc)
            except campaign_review.ReviewError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
    typer.echo(f"campaign {campaign_id} {name!r} is active")


def _refuse_activation(
    campaign_id: int,
    missing: Sequence[campaign_review.Missing],
    *,
    cause: BaseException | None = None,
) -> NoReturn:
    typer.echo(
        f"error: campaign {campaign_id} cannot be activated; the review still needs:", err=True
    )
    typer.echo("\n".join(_missing_lines(missing)), err=True)
    raise typer.Exit(code=1) from cause


@campaigns_app.command("pause")
def campaigns_pause(
    campaign_id: Annotated[int, typer.Argument(help="The campaign's ID.")],
) -> None:
    """Pause an active campaign (POST /campaigns/{id}/pause).

    Nothing fires until `resume`; each enrollment keeps its state and due time. A step
    already handed to Gmail is not recalled.
    """
    with (
        _campaign_db() as factory,
        session_scope(factory, write=True) as session,
        _campaign_errors(),
    ):
        user = _local_user_or_exit(session)
        campaign_service.pause(session, user, campaign_id)
    typer.echo(f"campaign {campaign_id} paused")


@campaigns_app.command("resume")
def campaigns_resume(
    campaign_id: Annotated[int, typer.Argument(help="The campaign's ID.")],
) -> None:
    """Resume a paused campaign (POST /campaigns/{id}/resume).

    A step that came due meanwhile fires at the next chance, one at a time and spaced
    as usual.
    """
    with (
        _campaign_db() as factory,
        session_scope(factory, write=True) as session,
        _campaign_errors(),
    ):
        user = _local_user_or_exit(session)
        campaign_service.resume(session, user, campaign_id)
    typer.echo(f"campaign {campaign_id} resumed")


# --- contacts ---------------------------------------------------------------


@contacts_app.command("stats")
def contacts_stats() -> None:
    """Triage progress: how many contacts are met, not met, skipped, or untriaged, and more."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            stats = contact_stats(session, user)
    finally:
        engine.dispose()
    typer.echo(_format_table(("METRIC", "COUNT"), _stats_rows(stats)), nl=False)
    typer.echo(
        f"total ({stats.total}) counts live contacts only: not archived, not merged away. "
        "archived and merged away are separate counts of what total leaves out."
    )


def _stats_rows(stats: ContactStats) -> list[tuple[str, str]]:
    fields = (
        ("total", stats.total),
        ("met", stats.met),
        ("not met", stats.not_met),
        ("skipped", stats.skipped),
        ("untriaged", stats.untriaged),
        ("archived", stats.archived),
        ("merged away", stats.merged_away),
        ("with email", stats.with_email),
        ("with phone", stats.with_phone),
        ("tagged", stats.tagged),
        ("tagged by rule", stats.tagged_by_rule),
    )
    return [(name, str(count)) for name, count in fields]


# --- gmail (P3-01) -----------------------------------------------------------------

#: How long ``gmail login`` waits for Google's redirect.
GMAIL_LOGIN_TIMEOUT_S: Final = 300


@gmail_app.command("client")
def gmail_client(
    path: Annotated[
        Path,
        typer.Argument(
            help="The client JSON the Cloud console downloads for a Desktop app client."
        ),
    ],
) -> None:
    """Store the OAuth client (ID and secret) in the Keychain."""
    client = _client_file_or_exit(path)
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
        _keychain_or_exit(lambda: mailbox_service.save_client(user, client))
    finally:
        engine.dispose()
    typer.echo(f"stored OAuth client {client.client_id}")


@gmail_app.command("login")
def gmail_login(
    ctx: typer.Context,
    client_file: Annotated[
        Path | None,
        typer.Option(
            "--client-file", help="Store this client JSON first (as `gmail client` does)."
        ),
    ] = None,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Seconds to wait for Google's redirect.", min=1)
    ] = GMAIL_LOGIN_TIMEOUT_S,
) -> None:
    """Authorize Gmail: print Google's URL, wait for its redirect, store the token."""
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    client = None if client_file is None else _client_file_or_exit(client_file)
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            live = mailbox_service.live_mailbox(session, user)
            hint = None if live is None else live.email
        if client is not None:
            stored = client
            _keychain_or_exit(lambda: mailbox_service.save_client(user, stored))
        else:
            client = _keychain_or_exit(lambda: mailbox_service.load_client(user.id))
            if client is None:
                typer.echo(
                    "error: no OAuth client is stored; pass --client-file or run"
                    " `netkeeper gmail client <file>` (docs/gmail-setup.md)",
                    err=True,
                )
                raise typer.Exit(code=1)
        email, refresh_token = _authorize_or_exit(client, hint, timeout)
        try:
            with session_scope(factory, write=True) as session:
                user = _local_user_or_exit(session)
                mailbox = mailbox_service.connect(
                    session,
                    user,
                    email,
                    refresh_token,
                    daily_cap=settings.campaigns.mailbox_daily_cap,
                )
                line = (
                    f"connected {mailbox.email} (mailbox {mailbox.id}, cap {mailbox.daily_cap}/day)"
                )
        except mailbox_service.OtherMailboxConnected as exc:
            typer.echo(f"error: {exc} (`netkeeper gmail disconnect {exc.connected}`)", err=True)
            raise typer.Exit(code=1) from exc
        except keychain.KeychainUnavailable as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.echo(line)


@gmail_app.command("status")
def gmail_status() -> None:
    """List the mailboxes and their health, without asking Google."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            rows = [_mailbox_row(row) for row in mailbox_service.list_mailboxes(session, user)]
        client = _keychain_or_exit(lambda: mailbox_service.load_client(user.id))
    finally:
        engine.dispose()
    typer.echo(f"client: {'not set' if client is None else client.client_id}")
    if not rows:
        typer.echo("no mailboxes; run `netkeeper gmail login`")
        return
    typer.echo(
        _format_table(("ID", "EMAIL", "STATUS", "REASON", "CAP", "CHECKED", "ARMED"), rows),
        nl=False,
    )


@gmail_app.command("check")
def gmail_check() -> None:
    """Refresh every connected mailbox's token now, as `serve` does every poll."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory) as session:
            _local_user_or_exit(session)
        results = mailbox_service.poll_mailboxes(factory)
    finally:
        engine.dispose()
    if not results:
        typer.echo("no connected mailboxes to check")
        return
    for result in results:
        reason = "" if result.reason is None else f" ({result.reason})"
        typer.echo(f"mailbox {result.mailbox_id}: {result.status.value}{reason}")
    if any(result.status is not MailboxStatus.OK for result in results):
        raise typer.Exit(code=1)


@gmail_app.command("disconnect")
def gmail_disconnect(
    email: Annotated[str, typer.Argument(help="The mailbox's address.")],
) -> None:
    """Forget a mailbox's token and disable it. Campaigns that named it keep the row."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            wanted = email.strip().lower()
            found = [
                row for row in mailbox_service.list_mailboxes(session, user) if row.email == wanted
            ]
            if not found:
                typer.echo(f"error: no mailbox {wanted}", err=True)
                raise typer.Exit(code=1)
            try:
                mailbox_service.disconnect(session, user, found[0])
            except keychain.KeychainUnavailable as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    typer.echo(f"disconnected {wanted}")


@gmail_app.command("arm")
def gmail_arm(
    mailbox: Annotated[str, typer.Argument(help="The mailbox's address or ID.")],
    send: Annotated[
        bool,
        typer.Option(
            "--send",
            help="The second step: let send steps go out, not only drafts. Needs the mailbox"
            " armed for drafts and one of its drafts found by its Message-ID.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Let `netkeeper serve` use this mailbox for campaign steps: drafts first.

    Armed, every campaign step on the mailbox becomes a Gmail draft you send by hand,
    `send` steps included. `--send` is the separate step that lets send steps go out
    on their own; it is refused until a draft made there has been found by its
    Message-ID (`netkeeper gmail status`). `netkeeper gmail disarm` undoes both.
    """
    mode = MailboxArm.SEND if send else MailboxArm.DRAFT
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        # Read, then ask, then write: a writer held across the prompt would lock out serve.
        with session_scope(factory) as session:
            user = _local_user_or_exit(session)
            row = _mailbox_or_exit(session, user, mailbox)
            email, mailbox_id, current = row.email, row.id, row.arm
        if current is mode:
            typer.echo(f"{email} is already armed for {mode.value}")
            return
        question = (
            f"let netkeeper serve send campaign email from {email} with no one pressing Send?"
            if send
            else f"let netkeeper serve make campaign drafts in {email}? You send each by hand"
        )
        if not yes and not typer.confirm(question):
            typer.echo(f"cancelled: {email} stays as it was")
            raise typer.Exit(code=1)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            row = mailbox_service.get_mailbox(session, user, mailbox_id)
            try:
                mailbox_service.arm(
                    session, user, row, mode, by=_cli_actor(), now=datetime.now(UTC)
                )
            except mailbox_service.ArmRefused as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    if send:
        typer.echo(f"{email} armed for send: send steps go out from the next tick")
    else:
        typer.echo(
            f"{email} armed for drafts: every step is a draft from the next tick;"
            " `netkeeper gmail disarm` undoes it"
        )


@gmail_app.command("disarm")
def gmail_disarm(
    mailbox: Annotated[str, typer.Argument(help="The mailbox's address or ID.")],
) -> None:
    """Stop `netkeeper serve` using this mailbox: nothing more is claimed from the next tick.

    A step already handed to Gmail is not recalled."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            row = _mailbox_or_exit(session, user, mailbox)
            mailbox_service.disarm(session, user, row)
            email = row.email
    finally:
        engine.dispose()
    typer.echo(f"{email} disarmed")


def _authorize_or_exit(
    client: gmail_oauth.OAuthClient, hint: str | None, timeout: int
) -> tuple[str, str]:
    """Run the loopback flow; the account's address and its refresh token."""
    with gmail_oauth.LoopbackReceiver() as receiver:
        authorization = gmail_oauth.begin(client, receiver.redirect_uri, login_hint=hint)
        _present_authorization_url(authorization.url)
        try:
            answer = receiver.wait(timeout)
        except TimeoutError as exc:
            typer.echo(f"error: no answer from Google within {timeout} s; run it again", err=True)
            raise typer.Exit(code=1) from exc
    if not secrets.compare_digest(answer.get("state", ""), authorization.state):
        typer.echo("error: the answer does not match this login (state); run it again", err=True)
        raise typer.Exit(code=1)
    if "error" in answer:
        typer.echo(f"error: Google answered {answer['error']!r}; nothing was stored", err=True)
        raise typer.Exit(code=1)
    try:
        grant = gmail_oauth.exchange_code(client, authorization, answer.get("code", ""))
        return gmail_oauth.fetch_email(grant.access_token), grant.refresh_token
    except gmail_oauth.OAuthError as exc:
        typer.echo(f"error: {exc} ({exc.code})", err=True)
        raise typer.Exit(code=1) from exc


def _present_authorization_url(url: str) -> None:
    """Show the URL. netkeeper never opens a browser itself (ADR 0002); the person does."""
    typer.echo("Open this URL in the browser signed in to the Gmail account, and allow access:")
    typer.echo()
    typer.echo(f"  {url}")
    typer.echo()
    typer.echo("Waiting for Google to send you back here...")


def _client_file_or_exit(path: Path) -> gmail_oauth.OAuthClient:
    try:
        return gmail_oauth.parse_client_file(path.expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        typer.echo(f"error: cannot read {path}: {exc.strerror}", err=True)
        raise typer.Exit(code=1) from exc
    except gmail_oauth.ClientConfigError as exc:
        typer.echo(f"error: {path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _keychain_or_exit[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except keychain.KeychainUnavailable as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _mailbox_row(row: Mailbox) -> tuple[str, ...]:
    checked = "-" if row.checked_at is None else row.checked_at.strftime("%Y-%m-%d %H:%M UTC")
    return (
        str(row.id),
        row.email,
        row.status.value,
        row.status_reason or "-",
        str(row.daily_cap),
        checked,
        _armed_cell(row),
    )


def _armed_cell(row: Mailbox) -> str:
    """``no``, or the mode, since when and by whom (#277)."""
    if row.arm is None or row.armed_at is None:
        return "no"
    since = row.send_armed_at or row.armed_at
    return f"{row.arm.value} since {since:%Y-%m-%d %H:%M UTC} by {row.armed_by or '?'}"


def _mailbox_or_exit(session: Session, user: User, which: str) -> Mailbox:
    """The user's mailbox named by its address or its id."""
    wanted = which.strip().lower()
    for row in mailbox_service.list_mailboxes(session, user):
        if row.email == wanted or str(row.id) == wanted:
            return row
    typer.echo(f"error: no mailbox {wanted}", err=True)
    raise typer.Exit(code=1)


def _cli_actor() -> str:
    """Who armed it, from the terminal: the login name."""
    try:
        return f"cli ({getpass.getuser()})"
    except (OSError, KeyError):  # no login name to be had
        return "cli"


def _local_user_or_exit(session: Session) -> User:
    user = session.scalars(
        select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
    ).first()
    if user is None:
        typer.echo("error: no local user exists; run `netkeeper db upgrade` first", err=True)
        raise typer.Exit(code=1)
    return user


def _format_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    """Left-aligned columns separated by two spaces, one line per row, newline-terminated."""
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    lines: list[str] = []
    for row in (headers, *rows):
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        lines.append("  ".join(cells).rstrip())
    return "".join(f"{line}\n" for line in lines)


def _human_size(size: int) -> str:
    if size < 1000:
        return f"{size} B"
    value = size / 1000
    for unit in ("kB", "MB", "GB"):
        if value < 1000:
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} TB"


def _human_age(age: timedelta) -> str:
    seconds = int(age.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


if __name__ == "__main__":
    app()
