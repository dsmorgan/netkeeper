"""Migrations against a fresh database: SQLite always, PostgreSQL when
NETKEEPER_TEST_DATABASE_URL points at one.

CI (.github/workflows/ci.yml) sets that variable against a postgres:16 service, so
a construct that only SQLite accepts fails there, not on a hosted install. To run
the PostgreSQL params locally, start a throwaway server and point the variable at it:

    docker run --rm -e POSTGRES_PASSWORD=netkeeper -e POSTGRES_USER=netkeeper \
        -e POSTGRES_DB=netkeeper_test -p 5432:5432 postgres:16
    NETKEEPER_TEST_DATABASE_URL=postgresql+psycopg://netkeeper:netkeeper@localhost/netkeeper_test \
        make test
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, MetaData, inspect, text
from sqlalchemy.exc import IntegrityError

from netkeeper import migrations
from netkeeper.db import database_url, make_engine
from netkeeper.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = migrations.alembic_root() / "alembic" / "versions"
PG_ENV = "NETKEEPER_TEST_DATABASE_URL"

# Text only SQLite understands. Alembic's batch mode (render_as_batch,
# batch_alter_table) is deliberately absent: it is how Alembic edits tables on
# SQLite and renders as a plain ALTER TABLE everywhere else.
SQLITE_ONLY_MARKERS: dict[str, re.Pattern[str]] = {
    "sqlite_autoincrement": re.compile(r"sqlite_autoincrement"),
    "PRAGMA": re.compile(r"\bPRAGMA\b", re.IGNORECASE),
    "strftime(": re.compile(r"\bstrftime\s*\(", re.IGNORECASE),
    # The SQL keyword, not SQLAlchemy's portable ``autoincrement=`` column argument.
    "AUTOINCREMENT": re.compile(r"\bAUTOINCREMENT\b(?!\s*=)", re.IGNORECASE),
    "WITHOUT ROWID": re.compile(r"\bWITHOUT\s+ROWID\b", re.IGNORECASE),
}

BACKENDS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "postgresql",
        id="postgresql",
        marks=pytest.mark.skipif(not os.environ.get(PG_ENV), reason=f"{PG_ENV} not set"),
    ),
]


def _drop_everything(engine: Engine) -> None:
    reflected = MetaData()
    reflected.reflect(engine)
    reflected.drop_all(engine)


@pytest.fixture(params=BACKENDS)
def migration_engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """An empty database on the requested backend."""
    backend: str = request.param
    if backend == "sqlite":
        engine = make_engine(database_url(tmp_path))
    else:
        engine = make_engine(os.environ[PG_ENV])
        _drop_everything(engine)
    try:
        yield engine
    finally:
        if backend == "postgresql":
            _drop_everything(engine)
        engine.dispose()


def _revision_chain() -> list[str]:
    """Revision ids from the first migration to head, in upgrade order."""
    script = ScriptDirectory.from_config(migrations.alembic_config())
    return [rev.revision for rev in reversed(list(script.walk_revisions()))]


def _sqlite_only_markers(source: str) -> list[str]:
    return [name for name, pattern in SQLITE_ONLY_MARKERS.items() if pattern.search(source)]


def _diff_against_models(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff: list[Any] = compare_metadata(context, Base.metadata)
        return diff


def test_fresh_database_migrates_to_head_and_matches_the_models(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    assert migrations.current_revision(migration_engine) == migrations.head_revision()
    # A model change without a migration (or the reverse) shows up here.
    assert _diff_against_models(migration_engine) == []


def test_upgrade_downgrade_upgrade(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    migrations.downgrade(migration_engine)
    assert migrations.current_revision(migration_engine) is None
    assert set(inspect(migration_engine).get_table_names()) <= {"alembic_version"}
    migrations.upgrade(migration_engine)
    assert migrations.current_revision(migration_engine) == migrations.head_revision()
    assert _diff_against_models(migration_engine) == []


def test_every_revision_upgrades_and_downgrades_one_step_at_a_time(
    migration_engine: Engine,
) -> None:
    """Each migration's downgrade has to work on its own, not only inside a full run."""
    chain = _revision_chain()
    assert chain[0] == "0001"
    for revision in chain:
        migrations.upgrade(migration_engine, "+1")
        assert migrations.current_revision(migration_engine) == revision
    assert _diff_against_models(migration_engine) == []
    expected: str | None
    for expected in [*reversed(chain[:-1]), None]:
        migrations.downgrade(migration_engine, "-1")
        assert migrations.current_revision(migration_engine) == expected
    assert set(inspect(migration_engine).get_table_names()) <= {"alembic_version"}


def test_user_kind_is_checked_by_the_database(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    insert = text(
        "INSERT INTO users (kind, timezone, created_at) VALUES (:kind, 'UTC', :created_at)"
    )
    with migration_engine.begin() as connection:
        connection.execute(insert, {"kind": "local", "created_at": "2026-09-20 12:00:00"})
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(insert, {"kind": "other", "created_at": "2026-09-20 12:00:00"})


def test_settings_key_is_unique_per_user(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    stamp = "2026-09-20 12:00:00"
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, kind, timezone, created_at) VALUES (1, 'local', 'UTC', :t)"
            ),
            {"t": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, kind, timezone, created_at) VALUES (2, 'local', 'UTC', :t)"
            ),
            {"t": stamp},
        )
    insert = text(
        "INSERT INTO settings_kv (user_id, key, value, created_at, updated_at)"
        " VALUES (:user_id, 'k', '1', :t, :t)"
    )
    with migration_engine.begin() as connection:
        connection.execute(insert, {"user_id": 1, "t": stamp})
        connection.execute(insert, {"user_id": 2, "t": stamp})  # same key, other user: fine
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(insert, {"user_id": 1, "t": stamp})


def test_deleting_a_user_cascades_to_its_settings(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    stamp = "2026-09-20 12:00:00"
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, kind, timezone, created_at) VALUES (1, 'local', 'UTC', :t)"
            ),
            {"t": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO settings_kv (user_id, key, value, created_at, updated_at)"
                " VALUES (1, 'k', '1', :t, :t)"
            ),
            {"t": stamp},
        )
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        remaining = connection.execute(text("SELECT count(*) FROM settings_kv")).scalar()
    assert remaining == 0


# --- script directory -------------------------------------------------------


def test_alembic_root_is_the_package_directory() -> None:
    root = migrations.alembic_root()
    assert root == REPO_ROOT / "netkeeper"
    assert (root / "alembic.ini").is_file()
    assert (root / "alembic" / "env.py").is_file()
    assert (root / "alembic" / "script.py.mako").is_file()


def test_revision_ids_are_sequential() -> None:
    head = migrations.head_revision()
    assert head is not None and head.isdigit() and len(head) == 4
    assert migrations.next_revision_id() == f"{int(head) + 1:04d}"


def test_migration_files_do_not_import_netkeeper() -> None:
    """Migrations are frozen history; they must not track the models module."""
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        assert "netkeeper" not in path.read_text(), path


# --- portability ------------------------------------------------------------


def test_migration_files_use_no_sqlite_only_constructs() -> None:
    """The PostgreSQL params catch these at run time; this names them at a glance."""
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        assert _sqlite_only_markers(path.read_text()) == [], path


@pytest.mark.parametrize(
    ("source", "markers"),
    [
        (
            'op.create_table("t", sa.Column("id", sa.Integer()), sqlite_autoincrement=True)',
            ["sqlite_autoincrement"],
        ),
        ('op.execute("PRAGMA foreign_keys=OFF")', ["PRAGMA"]),
        ('op.execute("pragma journal_mode=WAL")', ["PRAGMA"]),
        ("""server_default=sa.text("(strftime('%s','now'))")""", ["strftime("]),
        ('op.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")', ["AUTOINCREMENT"]),
        ('op.execute("CREATE TABLE t (id INTEGER PRIMARY KEY) WITHOUT ROWID")', ["WITHOUT ROWID"]),
        ("context.configure(render_as_batch=True)", []),
        ('with op.batch_alter_table("users") as batch_op:', []),
        ('sa.Column("id", sa.Integer(), autoincrement=True)', []),
    ],
)
def test_sqlite_only_marker_scan(source: str, markers: list[str]) -> None:
    assert _sqlite_only_markers(source) == markers


def test_ci_runs_the_postgresql_params() -> None:
    """Without the URL the PostgreSQL params skip silently, so CI must always set it."""
    if not os.environ.get("CI"):
        pytest.skip("only meaningful on CI")
    assert os.environ.get(PG_ENV), f"CI must set {PG_ENV}; see .github/workflows/ci.yml"
