"""Command-line entry point: serve, db, config, backup, openapi, tags, version."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from netkeeper import __version__, migrations
from netkeeper.config import ConfigError, Settings, load_settings, render_toml
from netkeeper.crm.tags import ensure_default_rules, list_tags, run_rules
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.logging_setup import setup_logging
from netkeeper.models import User, UserKind
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
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(openapi_app, name="openapi")
app.add_typer(backup_app, name="backup")
app.add_typer(tags_app, name="tags")


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
