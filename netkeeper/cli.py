"""Command-line entry point. Commands are added by later phases."""

from __future__ import annotations

import typer

from netkeeper import __version__

app = typer.Typer(help="netkeeper: keep your professional network warm.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """netkeeper command group."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)
