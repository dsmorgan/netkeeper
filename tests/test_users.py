from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app
from netkeeper.config import LinkedInSettings, Settings
from netkeeper.db import database_url, make_engine, make_session_factory
from netkeeper.models import User, UserKind
from netkeeper.services.users import ensure_local_user


def _users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.id)))


def test_ensure_local_user_creates_one_row_and_is_idempotent(session: Session) -> None:
    first = ensure_local_user(session)
    session.commit()
    second = ensure_local_user(session)
    assert second is first
    assert first.kind is UserKind.LOCAL
    assert first.timezone == "UTC"
    assert first.created_at.tzinfo is not None
    assert session.scalar(select(func.count()).select_from(User)) == 1


def test_ensure_local_user_survives_a_new_session(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with factory() as one:
        created = ensure_local_user(one)
        one.commit()
        created_id = created.id
    with factory() as two:
        assert ensure_local_user(two).id == created_id
        assert len(_users(two)) == 1


def test_ensure_local_user_takes_timezone_from_settings(session: Session) -> None:
    settings = replace(Settings(), linkedin=LinkedInSettings(timezone="Europe/Berlin"))
    user = ensure_local_user(session, settings=settings)
    assert user.timezone == "Europe/Berlin"


def test_ensure_local_user_resyncs_the_timezone_from_settings(session: Session) -> None:
    """``linkedin.timezone`` is the single source of truth, after the first start too.

    This used to seed a new row only, which left ``User.timezone`` and
    ``linkedin.timezone`` permanently disagreeing the moment anyone edited the
    config after `netkeeper db upgrade`. They are read by different things --
    budget counters key off the user row, the active window and the scheduler's
    deferral read the config -- so disagreeing means a day's budget resets at
    an hour the window is open. See ``ensure_local_user``'s docstring.
    """
    berlin = replace(Settings(), linkedin=LinkedInSettings(timezone="Europe/Berlin"))
    user = ensure_local_user(session, settings=berlin)
    tokyo = replace(Settings(), linkedin=LinkedInSettings(timezone="Asia/Tokyo"))

    resynced = ensure_local_user(session, settings=tokyo)

    assert resynced.id == user.id, "the row was replaced rather than updated"
    assert resynced.timezone == "Asia/Tokyo"
    assert session.get(User, user.id) is not None


def test_ensure_local_user_without_settings_leaves_the_timezone_alone(session: Session) -> None:
    """No config in hand is not a reason to overwrite the zone with a default."""
    berlin = replace(Settings(), linkedin=LinkedInSettings(timezone="Europe/Berlin"))
    user = ensure_local_user(session, settings=berlin)

    assert ensure_local_user(session).timezone == "Europe/Berlin"
    assert user.timezone == "Europe/Berlin"


# --- netkeeper db -----------------------------------------------------------


@pytest.fixture
def cli_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run the CLI from an unrelated cwd with its data dir under tmp_path."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    data = tmp_path / "data"
    monkeypatch.setenv("NETKEEPER_DATA", str(data))
    return data


def test_db_upgrade_migrates_and_creates_the_local_user(cli_data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert f"at revision {migrations.head_revision()}" in result.stdout
    assert "local user id 1" in result.stdout

    engine = make_engine(database_url(cli_data_dir))
    try:
        assert migrations.current_revision(engine) == migrations.head_revision()
        with make_session_factory(engine)() as session:
            users = _users(session)
            assert [u.kind for u in users] == [UserKind.LOCAL]
            assert users[0].timezone == LinkedInSettings().timezone  # defaults: no config file
    finally:
        engine.dispose()

    # Second run: nothing to migrate, still one user.
    again = runner.invoke(app, ["db", "upgrade"])
    assert again.exit_code == 0, again.output
    assert "local user id 1" in again.stdout


def test_db_upgrade_uses_the_config_timezone(cli_data_dir: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ntimezone = "Europe/Lisbon"\n')
    result = CliRunner().invoke(app, ["--config", str(config), "db", "upgrade"])
    assert result.exit_code == 0, result.output
    engine = make_engine(database_url(cli_data_dir))
    try:
        with make_session_factory(engine)() as session:
            assert _users(session)[0].timezone == "Europe/Lisbon"
    finally:
        engine.dispose()


def test_db_upgrade_refuses_a_broken_config(cli_data_dir: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[linkedin]\ntimezone = 5\n")
    result = CliRunner().invoke(app, ["--config", str(config), "db", "upgrade"])
    assert result.exit_code == 1
    assert "error:" in result.output
    assert not (cli_data_dir / "netkeeper.sqlite3").exists()


def test_db_current_before_and_after_upgrade(cli_data_dir: Path) -> None:
    runner = CliRunner()
    before = runner.invoke(app, ["db", "current"])
    assert before.exit_code == 0, before.output
    assert "current: (none)" in before.stdout
    assert f"head:    {migrations.head_revision()}" in before.stdout

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    after = runner.invoke(app, ["db", "current"])
    assert f"current: {migrations.head_revision()}" in after.stdout
