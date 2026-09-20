from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory
from netkeeper.models import Base
from netkeeper.scoping import install_scope_guard
from netkeeper.web.app import create_app


@pytest.fixture(autouse=True)
def _reset_factory_counters() -> None:
    """Every test's first factory rows are ``User 1`` and ``First1 Last1``."""
    factories.reset_counters()


@pytest.fixture(autouse=True)
def _clean_netkeeper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell environment out of every test."""
    for name in (
        "NETKEEPER_DATA",
        "NETKEEPER_CONFIG",
        "NETKEEPER_LOG_LEVEL",
        "NETKEEPER_DATABASE_URL",
        "NETKEEPER_FRONTEND_DIST",
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
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """A factory on ``engine`` with the scope guard installed, as the app has it."""
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    return factory


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A guarded session on ``engine``. Work left uncommitted is discarded at teardown."""
    with session_factory() as session:
        yield session


@pytest.fixture
def bare_engine(tmp_path: Path) -> Iterator[Engine]:
    """A tmp_path SQLite database with no schema, for code that runs the migrations itself."""
    engine = make_engine(database_url(tmp_path / "bare"))
    yield engine
    engine.dispose()


@pytest.fixture
def app(bare_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    """The application on ``bare_engine`` with no frontend build, not yet started."""
    monkeypatch.setenv("NETKEEPER_FRONTEND_DIST", str(tmp_path / "no-dist"))
    return create_app(Settings(), engine=bare_engine)


@pytest.fixture
async def running_app(app: FastAPI) -> AsyncIterator[FastAPI]:
    """``app`` inside its lifespan: migrated, local user present, bus and runner live."""
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(running_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process HTTP client. State-changing requests need the CSRF header."""
    transport = httpx.ASGITransport(app=running_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
