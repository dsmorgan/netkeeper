"""``netkeeper config adopt`` (#343): move config.toml's values onto the Settings page.

Every file here is a scratch file under ``tmp_path``.
"""

from __future__ import annotations

import stat
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
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


#: Every key whose file value above the hard maximum is clamped, with a value above it
#: and the maximum it is stored at (written out, not read from the module).
CLAMPED = [
    ("linkedin.budget", "connection_pages_per_day", 500, 400),
    ("linkedin.budget", "profile_visits_per_day", 300, 250),
    ("linkedin.budget", "profile_visits_per_week", 2000, 1250),
    ("linkedin.budget", "inbox_polls_per_day", 30, 24),
    ("linkedin.budget", "li_prefills_per_day", 80, 50),
    ("linkedin.budget", "li_messages_auto_per_day", 80, 50),
    ("campaigns", "mailbox_daily_cap", 500, 400),
    ("linkedin", "weekend_multiplier", 1.5, 1.0),
]

#: Keys whose value above the page's maximum stays in the file: a clamp would loosen
#: something (shorter spacing, a shorter guard) or change what it does.
KEPT = [
    ("campaigns", "send_spacing_floor_s", 5000),
    ("campaigns", "send_spacing_median_s", 5000),
    ("campaigns", "contacted_within_days_guard", 5000),
    ("campaigns", "reply_poll_minutes", 2000),
    ("backup", "keep", 500),
]


def test_the_clamped_keys_are_exactly_these() -> None:
    assert {f"{table}.{name}" for table, name, _, _ in CLAMPED} == config_adopt.CLAMP_SAFELY


@pytest.mark.parametrize(("table", "name", "above", "stored"), CLAMPED)
def test_a_key_where_lowering_is_safer_is_clamped(
    tmp_path: Path, table: str, name: str, above: float, stored: float
) -> None:
    text = f"[{table}]\n{name} = {above}\n"
    adopt = config_adopt.plan(text, load_settings(_write(tmp_path, text)))
    key = f"{table}.{name}"
    assert adopt.moved == {key: stored}
    assert adopt.clamped == {key: (above, stored)}
    assert name not in adopt.new_text


@pytest.mark.parametrize(("table", "name", "above"), KEPT)
def test_a_key_a_clamp_would_loosen_stays_in_the_file(
    tmp_path: Path, table: str, name: str, above: int
) -> None:
    text = f"[{table}]\n{name} = {above}\n"
    adopt = config_adopt.plan(text, load_settings(_write(tmp_path, text)))
    key = f"{table}.{name}"
    assert adopt.moved == {} and adopt.clamped == {}
    assert key in adopt.kept and "hard maximum" in adopt.kept[key]


def test_whitespace_in_a_table_header_is_understood(tmp_path: Path) -> None:
    text = "[ linkedin . budget ]  # budgets\nli_prefills_per_day = 10\n"
    adopt = config_adopt.plan(text, load_settings(_write(tmp_path, text)))
    assert adopt.moved == {"linkedin.budget.li_prefills_per_day": 10}
    assert adopt.new_text == "[ linkedin . budget ]  # budgets\n"


def test_the_backup_name_is_never_reused(tmp_path: Path) -> None:
    path = _write(tmp_path)
    now = datetime(2026, 10, 7, 12, 0, 0, 123456, tzinfo=UTC)
    taken = config_adopt.backup_path(path, now)
    assert taken.name == "config.toml.20261007-120000-123456.bak"
    taken.write_text("x", encoding="utf-8")
    with pytest.raises(config_adopt.AdoptError, match="already exists"):
        config_adopt.backup_path(path, now)


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_rewrite_is_atomic_keeps_the_mode_and_refuses_a_changed_file(
    tmp_path: Path, mode: int
) -> None:
    path = _write(tmp_path)
    path.chmod(mode)
    with pytest.raises(config_adopt.FileChanged):
        config_adopt.rewrite(path, "[web]\n", expected="something else")
    assert path.read_text(encoding="utf-8") == CONFIG
    config_adopt.rewrite(path, "[web]\n", expected=CONFIG)
    assert path.read_text(encoding="utf-8") == "[web]\n"
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_failed_rename_leaves_the_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)

    def refuse(self: Path, target: Path) -> Path:
        raise OSError("rename refused")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(OSError, match="rename refused"):
        config_adopt.rewrite(path, "[web]\n", expected=CONFIG)
    assert path.read_text(encoding="utf-8") == CONFIG
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_the_backup_is_created_exclusively(tmp_path: Path) -> None:
    path = _write(tmp_path)
    path.chmod(0o640)
    now = datetime(2026, 10, 7, 12, 0, 0, 1, tzinfo=UTC)
    backup = config_adopt.write_backup(path, now)
    assert backup.read_text(encoding="utf-8") == CONFIG
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640
    with pytest.raises(config_adopt.AdoptError, match="already exists"):
        config_adopt.write_backup(path, now)


def test_adopt_rewrites_a_symlinked_config_where_it_lives(
    tmp_path: Path, local_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _write(tmp_path)
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    result = CliRunner().invoke(cli, ["--config", str(link), "config", "adopt"], input="y\n")
    assert result.exit_code == 0, result.output
    assert link.is_symlink()
    assert "li_prefills_per_day" not in real.read_text(encoding="utf-8")


def test_adopt_stops_when_the_file_changes_after_the_prompt(
    tmp_path: Path, local_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)

    def confirm_while_someone_edits(*args: object, **kwargs: object) -> bool:
        path.write_text(CONFIG + "\n# edited\n", encoding="utf-8")
        return True

    monkeypatch.setattr("netkeeper.cli.typer.confirm", confirm_while_someone_edits)
    result = CliRunner().invoke(cli, ["--config", str(path), "config", "adopt"])
    assert result.exit_code == 1
    assert "config.toml was not changed" in result.output
    assert path.read_text(encoding="utf-8").endswith("# edited\n")
    assert _stored(local_db) == {}
    assert list(tmp_path.glob("*.bak")) == []


def test_a_failed_rewrite_says_the_file_was_not_changed(
    tmp_path: Path, local_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_adopt, "rewrite", fail)
    result = CliRunner().invoke(cli, ["--config", str(path), "config", "adopt"], input="y\n")
    assert result.exit_code == 1
    assert "disk full. config.toml was not changed" in result.output
    assert "backed up" in result.output
    assert path.read_text(encoding="utf-8") == CONFIG
