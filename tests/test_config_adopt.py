"""``netkeeper config adopt`` (#343): move config.toml's values onto the Settings page.

Every file here is a scratch file under ``tmp_path``.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings, load_settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import User, UserKind
from netkeeper.scoping import install_scope_guard
from netkeeper.services import config_adopt, ui_settings
from netkeeper.services.users import ensure_local_user

CONFIG = """\
# my netkeeper config
[web]
port = 8001  # a port of my own

[linkedin]
active_hours = ["07:00", "20:00"]
weekend_multiplier = 0.25

[linkedin.budget]
# keep prefills modest
li_prefills_per_day = 80
profile_visits_per_day = 40

[campaigns]
linkedin_auto_send = false
send_spacing_floor_s = 60
holidays = [
  "2026-12-25",  # Christmas
  "2026-01-01",
]
"""


def _write(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_plan_moves_what_the_page_holds_and_keeps_the_rest(tmp_path: Path) -> None:
    path = _write(tmp_path)
    adopt = config_adopt.plan(CONFIG, load_settings(path))
    assert adopt.moved == {
        "linkedin.budget.li_prefills_per_day": 50,
        "linkedin.budget.profile_visits_per_day": 40,
        "linkedin.active_hours": ["07:00", "20:00"],
        "linkedin.weekend_multiplier": 0.25,
        "campaigns.holidays": ["2026-01-01", "2026-12-25"],
    }
    assert adopt.clamped == {"linkedin.budget.li_prefills_per_day": (80, 50)}
    assert set(adopt.kept) == {"campaigns.send_spacing_floor_s"}  # below the page's 90
    assert tomllib.loads(adopt.new_text) == {
        "web": {"port": 8001},
        "linkedin": {"budget": {}},
        "campaigns": {"linkedin_auto_send": False, "send_spacing_floor_s": 60},
    }
    assert "# my netkeeper config" in adopt.new_text
    assert "# keep prefills modest" in adopt.new_text
    assert "port = 8001  # a port of my own" in adopt.new_text
    assert "Christmas" not in adopt.new_text


def test_a_key_written_another_way_stops_the_plan(tmp_path: Path) -> None:
    text = "[linkedin]\nbudget.li_prefills_per_day = 30\n"
    with pytest.raises(config_adopt.AdoptError, match="li_prefills_per_day"):
        config_adopt.plan(text, load_settings(_write(tmp_path, text)))


@pytest.fixture
def local_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path / "data")
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


def _stored(factory: sessionmaker[Session]) -> dict[str, object]:
    with session_scope(factory) as session:
        user = session.query(User).filter(User.kind == UserKind.LOCAL).one()
        return dict(ui_settings.stored(session, user))


def test_adopt_stores_backs_up_and_removes(
    tmp_path: Path, local_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = CliRunner().invoke(cli, ["--config", str(path), "config", "adopt"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "li_prefills_per_day = 50 (the file has 80, above the hard maximum)" in result.output
    assert "stays in" in result.output and "send_spacing_floor_s" in result.output
    assert _stored(local_db)["linkedin.budget.li_prefills_per_day"] == 50
    [backup] = list(tmp_path.glob("config.toml.*.bak"))
    assert backup.read_text(encoding="utf-8") == CONFIG
    left = tomllib.loads(path.read_text(encoding="utf-8"))
    assert left["campaigns"] == {"linkedin_auto_send": False, "send_spacing_floor_s": 60}
    # What is in force is unchanged, but for the clamp the hard max already applied.
    resolved = cli_module._load_settings_or_exit(cli_module.CliState(config=path))
    assert resolved.linkedin.active_hours == ("07:00", "20:00")
    assert resolved.linkedin.budget.li_prefills_per_day == 50


def test_adopt_changes_nothing_when_you_say_no(
    tmp_path: Path, local_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = CliRunner().invoke(cli, ["--config", str(path), "config", "adopt"], input="n\n")
    assert result.exit_code == 0 and "nothing changed" in result.output
    assert path.read_text(encoding="utf-8") == CONFIG
    assert _stored(local_db) == {}
    assert list(tmp_path.glob("*.bak")) == []


def test_adopt_refuses_without_a_terminal(
    tmp_path: Path, local_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: False)
    result = CliRunner().invoke(cli, ["--config", str(path), "config", "adopt"], input="y\n")
    assert result.exit_code == 1 and "run it in a terminal" in result.output
    assert path.read_text(encoding="utf-8") == CONFIG
    assert _stored(local_db) == {}
