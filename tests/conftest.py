import shutil
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import factories
import httpx
import keyring
import pytest
from fastapi import FastAPI
from gmail_fakes import FakeGoogle, MemoryKeyring
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netkeeper import migrations
from netkeeper.campaigns import gmail_oauth
from netkeeper.config import Settings
from netkeeper.db import DATABASE_FILENAME, database_url, make_engine, make_session_factory
from netkeeper.models import Base
from netkeeper.scoping import install_scope_guard
from netkeeper.web.app import create_app

# The per-test time limit (#210), and pytester for the test that shows it fails a test.
pytest_plugins = ["time_limit", "pytester"]


@pytest.fixture(autouse=True)
def _reset_factory_counters() -> None:
    """Every test's first factory rows are ``User 1`` and ``First1 Last1``."""
    factories.reset_counters()


@pytest.fixture(autouse=True)
def _clean_netkeeper_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's shell environment, and their data directory, out of every test.

    ``NETKEEPER_DATA`` points at a fresh directory rather than being unset: unset, the
    data directory on macOS is the real one under Application Support, and the
    activity lock writes its lock files there the moment a test opens a browser run.
    A test about the default location deletes the variable itself.
    """
    for name in (
        "NETKEEPER_CONFIG",
        "NETKEEPER_LOG_LEVEL",
        "NETKEEPER_DATABASE_URL",
        "NETKEEPER_FRONTEND_DIST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path / "netkeeper-data"))


#: What ``gmail_oauth.GOOGLE`` is during a test: a scheme urllib cannot open, so a
#: call that forgot its fake fails at once with ``OAuthUnavailable``, offline.
NO_GOOGLE = gmail_oauth.GoogleEndpoints(
    auth_uri="blocked-in-tests://google/auth",
    token_uri="blocked-in-tests://google/token",
    profile_uri="blocked-in-tests://google/profile",
    use_system_proxy=False,
)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryKeyring]:
    """No test touches a real Keychain, and no test can reach Google (#244)."""
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(gmail_oauth, "GOOGLE", NO_GOOGLE)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def fake_google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    """Google's token endpoint and Gmail's profile call, faked on loopback, as ``GOOGLE``."""
    fake = FakeGoogle()
    fake.start()
    monkeypatch.setattr(gmail_oauth, "GOOGLE", fake.endpoints)
    yield fake
    fake.stop()


def _template(directory: Path, build: Callable[[Engine], None]) -> Path:
    """A SQLite file under ``directory`` that ``build`` filled, closed and checkpointed.

    Built straight from a path, never through ``database_url``: a session fixture runs
    before ``_clean_netkeeper_env`` could clear a ``NETKEEPER_DATABASE_URL``.
    """
    file = directory / DATABASE_FILENAME
    engine = make_engine(f"sqlite:///{file}")
    try:
        build(engine)
    finally:
        engine.dispose()  # the last close checkpoints WAL into the file and removes it
    assert not file.with_name(f"{file.name}-wal").exists(), "the template is not one file"
    return file


def _copy_template(template: Path, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, data_dir / DATABASE_FILENAME)


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The models' schema, built once per run (once per xdist worker) (#210).

    ``create_all`` costs about 40 ms and a thousand tests ask for it; a copy of the
    file costs well under one.
    """
    return _template(tmp_path_factory.mktemp("schema-template"), Base.metadata.create_all)


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty database the migrations have run on, built once per run (#210).

    The app's startup migrates its database; from empty that costs about 130 ms, and
    on this copy it finds nothing to do. Migrating from empty is tests/test_migrations.py's
    subject, and tests that need it take ``bare_engine``.
    """
    return _template(tmp_path_factory.mktemp("migrated-template"), migrations.upgrade)


@pytest.fixture
def engine(tmp_path: Path, _schema_template: Path) -> Iterator[Engine]:
    """A fresh SQLite database under tmp_path with the schema built from the models.

    Tests that need the schema as the migrations create it use ``migration_engine``
    in tests/test_migrations.py instead.
    """
    _copy_template(_schema_template, tmp_path)
    engine = make_engine(database_url(tmp_path))
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
def app(
    bare_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _migrated_template: Path,
) -> FastAPI:
    """The application on ``bare_engine`` with no frontend build, not yet started.

    ``bare_engine``'s file starts as a copy of the migrated template, so the startup
    migration finds nothing to do (#210); everything else startup does, it still does.
    """
    _copy_template(_migrated_template, tmp_path / "bare")
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
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield client


@pytest.fixture
def inside_active_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lets a run started by hand start whatever the wall clock says (#213).

    The API and the CLI refuse a manual run outside ``[linkedin] active_hours``,
    checked against the real clock. A test that starts runs by hand to exercise
    something else takes this, so it passes at 3 a.m. too; the refusal itself is
    tested in tests/test_active_hours_stop.py with its own clock.
    """
    from netkeeper.services import runs

    monkeypatch.setattr(runs, "refuse_if_outside_active_hours", lambda *_, **__: None)
