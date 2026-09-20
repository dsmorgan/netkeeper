from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from netkeeper.db import database_url, make_engine, make_session_factory
from netkeeper.models import Base


@pytest.fixture(autouse=True)
def _clean_netkeeper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell environment out of every test."""
    for name in (
        "NETKEEPER_DATA",
        "NETKEEPER_CONFIG",
        "NETKEEPER_LOG_LEVEL",
        "NETKEEPER_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """A fresh SQLite database under tmp_path with the schema built from the models.

    Tests that need the schema as the migrations create it use ``migration_engine``
    in tests/test_migrations.py instead.
    """
    engine = make_engine(database_url(tmp_path))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session on ``engine``. Work left uncommitted is discarded at teardown."""
    with make_session_factory(engine)() as session:
        yield session
