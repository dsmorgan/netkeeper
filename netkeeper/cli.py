"""Command-line entry point: serve, db, config, backup, openapi, tags, import/export, version."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit

import typer
import uvicorn
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from netkeeper import __version__, migrations
from netkeeper.config import ConfigError, Settings, load_settings, render_toml
from netkeeper.crm import import_runs
from netkeeper.crm.archive import ArchiveImport, import_archive
from netkeeper.crm.contacts import ContactStats, contact_stats
from netkeeper.crm.exports import ExportFormat, ExportPreset, export_stream
from netkeeper.crm.filters import FilterError, FilterTree, SortKey, parse_filter, parse_sort
from netkeeper.crm.identity import CreateNew
from netkeeper.crm.lists import ListCount, list_lists, list_views, member_counts
from netkeeper.crm.tags import ensure_default_rules, list_tags, run_rules
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.archive import ArchiveFormatError, open_archive
from netkeeper.linkedin.browser import CHROME_PROFILE_DIRNAME, AttachBrowserProvider
from netkeeper.linkedin.preflight import LoginState, PreflightReport
from netkeeper.linkedin.preflight import preflight as run_preflight
from netkeeper.logging_setup import setup_logging
from netkeeper.models import ImportResolution, ImportRun, ImportStatus, User, UserKind
from netkeeper.paths import CONFIG_ENV, data_dir
from netkeeper.scoping import install_scope_guard
from netkeeper.services.backup import (
    BACKUPS_DIRNAME,
    BackupError,
    create_backup,
    list_backups,
    prune_backups,
)
from netkeeper.services.users import ensure_local_user
from netkeeper.web.app import create_app, openapi_json

APP_FACTORY = "netkeeper.web.app:dev_app"

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
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(openapi_app, name="openapi")
app.add_typer(backup_app, name="backup")
app.add_typer(tags_app, name="tags")
app.add_typer(lists_app, name="lists")
app.add_typer(import_app, name="import")
app.add_typer(contacts_app, name="contacts")
app.add_typer(browser_app, name="browser")


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
    uvicorn.run(create_app(settings), host=bind_host, port=bind_port, log_config=None)


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


DEFAULT_CDP_PORT = 9222


@browser_app.command("launch")
def browser_launch(ctx: typer.Context) -> None:
    """Print the command that starts Chrome with a debug port. netkeeper never runs it.

    netkeeper attaches to a browser you run; it does not own one. A browser netkeeper
    started would be a second device on your LinkedIn account, and that is what gets
    accounts restricted, so this command prints a command for you to run (ADR 0002).
    """
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    cdp_url = settings.linkedin.cdp_url
    profile = data_dir() / CHROME_PROFILE_DIRNAME
    typer.echo("netkeeper attaches to a Chrome you start yourself. It never starts one.")
    typer.echo("Run this in a terminal (again whenever that Chrome is not running):")
    typer.echo()
    for line in _chrome_command(_cdp_port(cdp_url), profile):
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
    host = urlsplit(cdp_url).hostname
    if host not in (None, "localhost", "127.0.0.1", "::1"):
        typer.echo()
        typer.echo(
            f"note: linkedin.cdp_url points at {host}, not this machine. Chrome's debug\n"
            "port is only reachable on its own loopback address."
        )
    typer.echo()
    typer.echo(f"Check it with: netkeeper preflight   (attaches to {cdp_url})")


def _chrome_command(port: int, profile: Path) -> list[str]:
    """The platform's Chrome command, as lines the user can paste."""
    # Read through a plain str so mypy keeps both branches on either host, the same
    # reason paths._is_macos() does it.
    platform: str = sys.platform
    opener = 'open -na "Google Chrome" --args \\' if platform == "darwin" else "google-chrome \\"
    return [opener, f"  --remote-debugging-port={port} \\", f'  --user-data-dir="{profile}"']


def _cdp_port(cdp_url: str) -> int:
    """The debug port from ``linkedin.cdp_url``, falling back to Chrome's usual one."""
    try:
        port = urlsplit(cdp_url).port
    except ValueError:
        port = None
    return DEFAULT_CDP_PORT if port is None else port


@app.command()
def preflight(ctx: typer.Context) -> None:
    """Check the sidecar: the attach, the LinkedIn session, and the browser fingerprint.

    Everything it reports comes from your own machine. It opens one blank tab on the
    Chrome you started, reads a few navigator properties and the names of the LinkedIn
    cookies already in that profile, and closes the tab again. It visits no website,
    and it never reads or prints a cookie value. Exits non-zero when a job could not
    run right now.

    Run it when nothing else is driving the browser. The activity lock lives inside
    one process, so this command cannot see a run that `netkeeper serve` is holding,
    and attaching alongside one drops both connections.
    """
    state = ctx.ensure_object(CliState)
    settings = _load_settings_or_exit(state)
    provider = AttachBrowserProvider(settings.linkedin.cdp_url)
    report = asyncio.run(run_preflight(provider))
    for line in _preflight_lines(report):
        typer.echo(line)
    if not report.ok:
        raise typer.Exit(code=1)


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
        with open_archive(path) as archive:
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
            "row in the file at once, not one at a time. Omitted: refuse and say which rows.",
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
        committed = _commit_draft(draft, on_candidate)
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


def _commit_draft(draft: _RunSnapshot, on_candidate: OnCandidate | None) -> _RunSnapshot:
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                if on_candidate == "skip":
                    run = import_runs.commit(session, user, draft.id, skip_undecided=True)
                elif on_candidate == "new":
                    decisions = {number: CreateNew() for number in draft.candidate_rows}
                    run = import_runs.commit(session, user, draft.id, decisions=decisions)
                else:
                    run = import_runs.commit(session, user, draft.id)
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


@contextmanager
def _reporting_lock_races() -> Iterator[None]:
    """Wrap a writing import command's whole body: a lost SQLite write-lock race must
    not surface as a raw traceback. ``import csv``, ``resume``, and ``rm`` each open one
    or more writer sessions (a refused ``resume --on-candidate new`` opens a second, for
    the retry), and any of them can lose a race to a concurrent writer -- these are
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
            "undecided in the run at once, not one at a time. Omitted: refuse and say which rows.",
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
    (``import_runs.py``), so this asks ``commit()`` itself which rows are
    still undecided rather than trusting the resolution the draft recorded
    when it was read -- the same divergence between the CLI and the API this
    item exists to close, reintroduced by trusting stale state here.
    """
    with _reporting_lock_races():
        if on_candidate == "skip":
            committed = _resume_commit(run_id, skip_undecided=True)
            typer.echo(_run_report("committed", committed))
            return
        try:
            committed = _resume_commit(run_id)
        except import_runs.UndecidedCandidates as exc:
            if on_candidate is None:
                _refuse_undecided(run_id, exc.row_numbers)
            # --on-candidate new: decide exactly the rows commit() itself just said
            # are still undecided, never a set read off the draft's stored
            # resolution -- a row already resolved another way since the draft was
            # read must not be handed a decision, which identity.apply() refuses
            # for anything but a Candidate resolution and would otherwise silently
            # skip.
            decisions = {number: CreateNew() for number in exc.row_numbers}
            try:
                committed = _resume_commit(run_id, decisions=decisions)
            except import_runs.UndecidedCandidates as retry_exc:
                # The database moved again between the two commit() calls -- a
                # narrow race the decisions above cannot close (its own issue:
                # give commit() the bulk policy as a parameter so one
                # transaction does both). Refuse cleanly rather than let this
                # escape the except block that is already handling the first
                # UndecidedCandidates.
                _refuse_undecided(run_id, retry_exc.row_numbers)
        typer.echo(_run_report("committed", committed))


def _resume_commit(
    run_id: int,
    *,
    decisions: dict[int, CreateNew] | None = None,
    skip_undecided: bool = False,
) -> _RunSnapshot:
    """One ``commit()`` attempt on ``run_id``, in its own engine and transaction.

    ``import_runs.UndecidedCandidates`` is let through uncaught: the caller
    decides what a still-undecided row means for ``--on-candidate``. Every
    other service error is reported and exits, as ``_commit_draft`` does.
    """
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                run = import_runs.commit(
                    session, user, run_id, decisions=decisions, skip_undecided=skip_undecided
                )
            except import_runs.UndecidedCandidates:
                raise
            # Same defensive symmetry as _commit_draft's own except clause.
            except (import_runs.ImportRunError, import_runs.CsvImportError) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            return _snapshot_of(run)
    finally:
        engine.dispose()


@import_app.command("rollback")
def import_rollback_cmd(
    run_id: Annotated[int, typer.Argument(help="The import run to undo.")],
) -> None:
    """Undo a committed import run: delete what it created, restore what it enriched."""
    engine = make_engine(database_url())
    try:
        factory = make_session_factory(engine)
        install_scope_guard(factory)
        with session_scope(factory, write=True) as session:
            user = _local_user_or_exit(session)
            try:
                result = import_runs.rollback(session, user, run_id)
            # Same defensive symmetry as _commit_draft: rollback() does not raise
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
