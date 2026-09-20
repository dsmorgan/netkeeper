"""Backups: VACUUM INTO snapshots, retention, listing, and the CLI around them."""

import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app
from netkeeper.db import DATABASE_FILENAME, database_url, make_engine, make_session_factory
from netkeeper.models import SettingKV
from netkeeper.services.backup import (
    BackupError,
    BackupInfo,
    backup_name,
    create_backup,
    list_backups,
    parse_backup_name,
    prune_backups,
)
from netkeeper.services.users import ensure_local_user

STAMP = datetime(2026, 9, 20, 14, 30, 12, tzinfo=UTC)
STAMP_NAME = "netkeeper-20260920T143012Z.sqlite3"
# Safely in the past, so seeded files never sort after one the CLI creates now.
OLD = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """A data directory holding a migrated database with the local user in it."""
    directory = tmp_path / "data"
    engine = make_engine(database_url(directory))
    try:
        migrations.upgrade(engine)
        with make_session_factory(engine)() as session:
            ensure_local_user(session)
            session.commit()
    finally:
        engine.dispose()
    return directory


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the CLI at tmp_path's data directory from an empty cwd; return the data dir."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "data"))
    return tmp_path / "data"


def _tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _seed_backups(directory: Path, stamps: list[datetime]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / backup_name(stamp) for stamp in stamps]
    for path in paths:
        path.write_bytes(b"x")
    return paths


# --- create_backup ------------------------------------------------------------


def test_backup_is_a_valid_copy_with_the_same_tables_and_revision(data: Path) -> None:
    engine = make_engine(database_url(data))
    try:
        written = create_backup(engine, data / "backups", now=STAMP)
    finally:
        engine.dispose()
    assert written == data / "backups" / STAMP_NAME
    with closing(sqlite3.connect(written)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        assert revisions == [(migrations.head_revision(),)]
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)
    assert _tables(written) == _tables(data / DATABASE_FILENAME)
    assert {"alembic_version", "users", "settings_kv"} <= _tables(written)


def test_backup_includes_rows_committed_while_the_engine_is_open(data: Path) -> None:
    """The write sits in the WAL (no connection has closed yet); the snapshot still has it."""
    engine = make_engine(database_url(data))
    try:
        with make_session_factory(engine)() as session:
            user = ensure_local_user(session)
            session.add(SettingKV(user_id=user.id, key="probe", value="live"))
            session.commit()
        written = create_backup(engine, data / "backups", now=STAMP)
    finally:
        engine.dispose()
    with closing(sqlite3.connect(written)) as connection:
        rows = connection.execute("SELECT key, value FROM settings_kv").fetchall()
    assert rows == [("probe", '"live"')]


def test_backup_accepts_a_url_and_creates_the_directory(data: Path) -> None:
    target = data / "deep" / "nested" / "backups"
    written = create_backup(database_url(data), target, now=STAMP)
    assert written == target / STAMP_NAME
    assert "users" in _tables(written)


def test_backup_leaves_the_source_untouched(data: Path) -> None:
    source = data / DATABASE_FILENAME
    before = source.read_bytes()
    create_backup(database_url(data), data / "backups", now=STAMP)
    assert source.read_bytes() == before
    engine = make_engine(database_url(data))
    try:
        assert migrations.current_revision(engine) == migrations.head_revision()
    finally:
        engine.dispose()


# --- naming -------------------------------------------------------------------


def test_name_encodes_the_timestamp_in_utc(data: Path) -> None:
    eastern = STAMP.astimezone(timezone(timedelta(hours=-4)))
    written = create_backup(database_url(data), data / "backups", now=eastern)
    assert written.name == STAMP_NAME
    assert parse_backup_name(written.name) == STAMP
    assert backup_name(eastern) == STAMP_NAME


def test_default_timestamp_is_the_current_utc_time(data: Path) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    written = create_backup(database_url(data), data / "backups")
    after = datetime.now(UTC)
    created_at = parse_backup_name(written.name)
    assert created_at is not None
    assert before <= created_at <= after
    assert re.fullmatch(r"netkeeper-\d{8}T\d{6}Z\.sqlite3", written.name)


def test_naive_timestamp_is_refused(data: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        create_backup(database_url(data), data / "backups", now=STAMP.replace(tzinfo=None))


def test_same_second_twice_is_an_error_not_an_overwrite(data: Path) -> None:
    first = create_backup(database_url(data), data / "backups", now=STAMP)
    before = first.read_bytes()
    with pytest.raises(BackupError, match="already exists"):
        create_backup(database_url(data), data / "backups", now=STAMP)
    assert first.read_bytes() == before


@pytest.mark.parametrize(
    "name",
    [
        "netkeeper.sqlite3",
        "netkeeper-latest.sqlite3",
        "netkeeper-20260920T143012Z.sqlite3.bak",
        "backup-20260920T143012Z.sqlite3",
        "netkeeper-2026-09-20T14:30:12Z.sqlite3",
        "netkeeper-20260920T143012.sqlite3",
    ],
)
def test_parse_backup_name_rejects_other_files(name: str) -> None:
    assert parse_backup_name(name) is None


# --- refusals -----------------------------------------------------------------


def test_postgresql_url_is_refused_without_leaking_the_password(tmp_path: Path) -> None:
    target = tmp_path / "backups"
    with pytest.raises(BackupError) as info:
        create_backup("postgresql+psycopg://nk:hunter2@localhost/nk", target)
    assert "PostgreSQL" in str(info.value)
    assert "hunter2" not in str(info.value)
    assert not target.exists()


def test_missing_database_file_is_refused_and_not_created(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    with pytest.raises(BackupError, match="db upgrade"):
        create_backup(database_url(empty), tmp_path / "backups")
    assert not (empty / DATABASE_FILENAME).exists()
    assert not (tmp_path / "backups").exists()


def test_in_memory_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="in-memory"):
        create_backup("sqlite://", tmp_path / "backups")


# --- prune_backups ------------------------------------------------------------


def test_prune_keeps_the_newest_by_name_and_leaves_other_files_alone(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    stamps = [STAMP + timedelta(days=i) for i in range(5)]  # oldest first
    paths = _seed_backups(directory, stamps)
    # mtimes run the other way, so a prune by mtime would pick the wrong files.
    for index, path in enumerate(paths):
        stamp = 1_700_000_000 - index * 3600
        os.utime(path, (stamp, stamp))
    foreign = [
        directory / "notes.txt",
        directory / "netkeeper-latest.sqlite3",
        directory / "netkeeper.sqlite3",
    ]
    for path in foreign:
        path.write_bytes(b"keep me")
    lookalike_dir = directory / backup_name(STAMP - timedelta(days=1))
    lookalike_dir.mkdir()

    removed = prune_backups(directory, keep=2)

    assert removed == paths[:3]
    assert not any(path.exists() for path in paths[:3])
    assert all(path.is_file() for path in paths[3:])
    assert all(path.is_file() for path in foreign)
    assert lookalike_dir.is_dir()


def test_prune_removes_nothing_at_or_under_keep(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    paths = _seed_backups(directory, [STAMP, STAMP + timedelta(hours=1)])
    assert prune_backups(directory, keep=2) == []
    assert prune_backups(directory, keep=5) == []
    assert all(path.is_file() for path in paths)


def test_prune_of_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    assert prune_backups(tmp_path / "nope", keep=1) == []
    assert not (tmp_path / "nope").exists()


@pytest.mark.parametrize("keep", [0, -1])
def test_prune_refuses_to_keep_nothing(tmp_path: Path, keep: int) -> None:
    paths = _seed_backups(tmp_path / "backups", [STAMP])
    with pytest.raises(ValueError, match="at least 1"):
        prune_backups(tmp_path / "backups", keep=keep)
    assert paths[0].is_file()


# --- list_backups -------------------------------------------------------------


def test_list_backups_is_newest_first_with_size_and_created_at(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    older, newer = STAMP, STAMP + timedelta(hours=1)
    _seed_backups(directory, [older])
    (directory / backup_name(newer)).write_bytes(b"12345")
    (directory / "notes.txt").write_text("not a backup")
    assert list_backups(directory) == [
        BackupInfo(path=directory / backup_name(newer), created_at=newer, size=5),
        BackupInfo(path=directory / backup_name(older), created_at=older, size=1),
    ]


def test_list_backups_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "nope") == []


# --- CLI ----------------------------------------------------------------------


def test_backup_create_writes_a_snapshot_and_reports_it(cli_env: Path, data: Path) -> None:
    assert cli_env == data
    result = CliRunner().invoke(app, ["backup", "create"])
    assert result.exit_code == 0, result.output
    backups = list_backups(data / "backups")
    assert len(backups) == 1
    assert f"wrote {backups[0].path}" in result.stdout
    assert "pruned 0 older backups (keeping the newest 14)" in result.stdout
    with closing(sqlite3.connect(backups[0].path)) as connection:
        revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    assert revisions == [(migrations.head_revision(),)]


def test_backup_with_no_subcommand_creates_one(cli_env: Path, data: Path) -> None:
    result = CliRunner().invoke(app, ["backup"])
    assert result.exit_code == 0, result.output
    assert len(list_backups(data / "backups")) == 1
    assert "wrote " in result.stdout


def test_backup_create_prunes_to_the_configured_keep(
    cli_env: Path, data: Path, tmp_path: Path
) -> None:
    seeded = _seed_backups(data / "backups", [OLD + timedelta(days=i) for i in range(3)])
    config = tmp_path / "c.toml"
    config.write_text("[backup]\nkeep = 2\n")
    result = CliRunner().invoke(app, ["--config", str(config), "backup"])
    assert result.exit_code == 0, result.output
    remaining = [info.path for info in list_backups(data / "backups")]
    assert len(remaining) == 2
    assert seeded[2] in remaining
    assert seeded[0] not in remaining and seeded[1] not in remaining
    assert "pruned 2 older backups (keeping the newest 2)" in result.stdout


def test_backup_create_with_keep_below_one_is_refused(
    cli_env: Path, data: Path, tmp_path: Path
) -> None:
    config = tmp_path / "c.toml"
    config.write_text("[backup]\nkeep = 0\n")
    result = CliRunner().invoke(app, ["--config", str(config), "backup", "create"])
    assert result.exit_code == 1
    assert "backup.keep" in result.stderr
    assert not (data / "backups").exists()


def test_backup_create_without_a_database_exits_nonzero(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["backup", "create"])
    assert result.exit_code == 1
    assert "db upgrade" in result.stderr
    assert "Traceback" not in result.output
    assert not (cli_env / DATABASE_FILENAME).exists()


def test_backup_list_prints_a_table_newest_first(cli_env: Path) -> None:
    directory = cli_env / "backups"
    older, newer = OLD, OLD + timedelta(days=1)
    _seed_backups(directory, [older, newer])
    (directory / "notes.txt").write_text("not a backup")
    result = CliRunner().invoke(app, ["backup", "list"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert lines[0].split() == ["NAME", "SIZE", "AGE"]
    assert lines[1].split() == [backup_name(newer), "1", "B", *lines[1].split()[3:]]
    assert lines[2].startswith(backup_name(older))
    assert lines[1].endswith(" d") and lines[2].endswith(" d")
    assert "notes.txt" not in result.stdout


def test_backup_list_with_nothing_says_so(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["backup", "list"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"no backups in {cli_env / 'backups'}"


# --- help ---------------------------------------------------------------------


def _listed_commands(help_text: str) -> set[str]:
    """Names in the Commands panel of a rich-formatted Typer help page (options start with -)."""
    return set(re.findall(r"^│ ([^-\s]\S*)\s{2,}\S", help_text, flags=re.MULTILINE))


def test_help_lists_every_command_group() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert _listed_commands(result.stdout) >= {
        "backup",
        "config",
        "db",
        "openapi",
        "serve",
        "version",
    }


def test_no_arguments_prints_the_same_help() -> None:
    bare = CliRunner().invoke(app, [])
    assert _listed_commands(bare.output) == _listed_commands(
        CliRunner().invoke(app, ["--help"]).stdout
    )


def test_backup_help_lists_create_and_list() -> None:
    result = CliRunner().invoke(app, ["backup", "--help"])
    assert result.exit_code == 0, result.output
    assert _listed_commands(result.stdout) == {"create", "list"}
    assert "backup create" in result.stdout
