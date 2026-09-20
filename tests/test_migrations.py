"""Migrations against a fresh database: SQLite always, PostgreSQL when
NETKEEPER_TEST_DATABASE_URL points at one (P1-17 wires that up in CI)."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, MetaData, inspect, text
from sqlalchemy.exc import IntegrityError

from netkeeper import migrations
from netkeeper.db import database_url, make_engine
from netkeeper.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_ENV = "NETKEEPER_TEST_DATABASE_URL"

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


def test_alembic_root_is_the_repository_root() -> None:
    assert migrations.alembic_root() == REPO_ROOT
    assert (REPO_ROOT / "alembic.ini").is_file()
    assert (REPO_ROOT / "alembic" / "env.py").is_file()


def test_revision_ids_are_sequential() -> None:
    head = migrations.head_revision()
    assert head is not None and head.isdigit() and len(head) == 4
    assert migrations.next_revision_id() == f"{int(head) + 1:04d}"


def test_migration_files_do_not_import_netkeeper() -> None:
    """Migrations are frozen history; they must not track the models module."""
    for path in (REPO_ROOT / "alembic" / "versions").glob("*.py"):
        assert "netkeeper" not in path.read_text(), path
