"""Command-line entry point. Commands are added by later phases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy.engine import make_url

from netkeeper import __version__, migrations
from netkeeper.config import ConfigError, Settings, load_settings, render_toml
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.logging_setup import setup_logging
from netkeeper.paths import CONFIG_ENV, data_dir
from netkeeper.services.users import ensure_local_user
from netkeeper.web.app import create_app, openapi_json

APP_FACTORY = "netkeeper.web.app:dev_app"

app = typer.Typer(help="netkeeper: keep your professional network warm.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect the resolved configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Create and migrate the database.", no_args_is_help=True)
openapi_app = typer.Typer(help="Work with the API schema.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(openapi_app, name="openapi")


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
    """Print the resolved settings as TOML, followed by a [paths] block."""
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
        with session_scope(make_session_factory(engine)) as session:
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


if __name__ == "__main__":
    app()
