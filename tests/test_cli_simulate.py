"""`netkeeper simulate`: the CLI wrapper (P2-11, #156, #107).

Unlike `posture` and `rehearse`'s CLI tests (`test_cli_posture_rehearse.py`),
this one needs no `cli_db` fixture, no migrated database, and no
`NETKEEPER_DATABASE_URL` -- the whole point of this command is that it never
opens the real one. That absence is itself part of what these tests are
checking: `test_runs_with_no_database_configured_at_all` would fail loudly
(a `sqlite3.OperationalError` or similar) if the command reached for
`netkeeper.db.database_url()` the way every other data-touching command does.
"""

from __future__ import annotations

from typer.testing import CliRunner

from netkeeper.cli import app as cli


def test_runs_with_no_database_configured_at_all() -> None:
    """No `NETKEEPER_DATABASE_URL`, no migrated schema, no local user -- proof
    this command never reaches for the real database the way every other
    data-touching command in this CLI does."""
    result = CliRunner().invoke(cli, ["simulate", "--days", "3", "--throttles", "1", "--seed", "1"])

    assert result.exit_code == 0, result.output


def test_prints_the_day_table_and_the_fires_table() -> None:
    result = CliRunner().invoke(cli, ["simulate", "--days", "3", "--throttles", "1", "--seed", "1"])

    assert result.exit_code == 0, result.output
    assert "DAY" in result.output and "WARM-UP" in result.output and "HEAT SCORE" in result.output
    assert "fires by day and job kind" in result.output
    assert "enrich" in result.output and "inbox" in result.output
    assert "scratch database" in result.output
    assert "1 throttle(s) requested" in result.output


def test_is_repeatable_from_the_seed_it_was_given() -> None:
    first = CliRunner().invoke(cli, ["simulate", "--days", "5", "--throttles", "2", "--seed", "9"])
    second = CliRunner().invoke(cli, ["simulate", "--days", "5", "--throttles", "2", "--seed", "9"])

    assert first.exit_code == 0 and second.exit_code == 0
    assert first.output == second.output


def test_refuses_zero_days() -> None:
    result = CliRunner().invoke(cli, ["simulate", "--days", "0"])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert "days" in result.output


def test_refuses_negative_throttles() -> None:
    result = CliRunner().invoke(cli, ["simulate", "--throttles", "-1"])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert "throttles" in result.output


def test_matches_cp3s_demo_invocation() -> None:
    """The exact invocation #30's demo names: ``netkeeper simulate --days 14
    --throttles 2``."""
    result = CliRunner().invoke(cli, ["simulate", "--days", "14", "--throttles", "2"])

    assert result.exit_code == 0, result.output
    assert "14 simulated days" in result.output
    assert "2 throttle(s) requested" in result.output
