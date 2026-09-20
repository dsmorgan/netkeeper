"""Command-line entry point. Commands are added by later phases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from netkeeper import __version__
from netkeeper.config import ConfigError, load_settings, render_toml
from netkeeper.logging_setup import setup_logging
from netkeeper.paths import data_dir

app = typer.Typer(help="netkeeper: keep your professional network warm.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect the resolved configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


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


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the resolved settings as TOML, followed by a [paths] block."""
    state = ctx.ensure_object(CliState)
    try:
        settings = load_settings(state.config)
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    source = "defaults" if settings.source_path is None else str(settings.source_path)
    paths = _PathsBlock(data_dir=str(data_dir()), config=source)
    typer.echo(render_toml(settings), nl=False)
    typer.echo()
    typer.echo(render_toml(paths, table="paths"), nl=False)


if __name__ == "__main__":
    app()
