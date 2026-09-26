"""``netkeeper gmail``: client, login, status, check, disconnect (#244, P3-01).

``login`` prints Google's URL and waits for the redirect on a loopback port. The
tests play the browser: ``_present_authorization_url`` is replaced by one that
consents on the fake and follows the redirect from a thread.
"""

import json
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from gmail_fakes import CLIENT_ID, CLIENT_SECRET, FAKE_EMAIL, FakeGoogle, MemoryKeyring
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.campaigns import gmail_oauth
from netkeeper.cli import app as cli
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import MailboxStatus
from netkeeper.scoping import install_scope_guard
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.users import ensure_local_user

runner = CliRunner()


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, pointed to by ``NETKEEPER_DATABASE_URL``."""
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    yield factory
    engine.dispose()


@pytest.fixture
def client_file(tmp_path: Path) -> Path:
    path = tmp_path / "client_secret.json"
    path.write_text(
        json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}),
        encoding="utf-8",
    )
    return path


def _browser(fake: FakeGoogle, monkeypatch: pytest.MonkeyPatch, **consent: str) -> list[str]:
    """Replace the URL's presentation with a person who allows access. Returns the URLs shown."""
    shown: list[str] = []

    def present(url: str) -> None:
        shown.append(url)
        answer = fake.deny(url) if consent.get("deny") else fake.consent(url, **consent)
        threading.Thread(target=_follow, args=(answer,), daemon=True).start()

    monkeypatch.setattr(cli_module, "_present_authorization_url", present)
    return shown


def _follow(redirect: str) -> None:
    """Be the browser Google sends back to the loopback receiver."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(redirect, timeout=5) as response:
        response.read()


def test_client_stores_the_downloaded_file(
    cli_db: sessionmaker[Session], client_file: Path, memory_keyring: MemoryKeyring
) -> None:
    result = runner.invoke(cli, ["gmail", "client", str(client_file)])
    assert result.exit_code == 0, result.output
    assert CLIENT_ID in result.output
    assert CLIENT_SECRET not in result.output
    assert any(key[1].endswith("/gmail/oauth_client") for key in memory_keyring.entries)


def test_client_refuses_a_web_client(cli_db: sessionmaker[Session], tmp_path: Path) -> None:
    path = tmp_path / "web.json"
    path.write_text(json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": "s"}}))
    result = runner.invoke(cli, ["gmail", "client", str(path)])
    assert result.exit_code == 1
    assert "Desktop app" in result.output


def test_login_without_a_client_says_how_to_add_one(cli_db: sessionmaker[Session]) -> None:
    result = runner.invoke(cli, ["gmail", "login"])
    assert result.exit_code == 1
    assert "--client-file" in result.output


def test_login_connects_the_mailbox(
    cli_db: sessionmaker[Session],
    client_file: Path,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = _browser(fake_google, monkeypatch)
    result = runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    assert result.exit_code == 0, result.output
    assert f"connected {FAKE_EMAIL}" in result.output
    [url] = shown
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A" in url
    with session_scope(cli_db) as session:
        user = cli_module._local_user_or_exit(session)
        [mailbox] = mailbox_service.list_mailboxes(session, user)
    assert (mailbox.email, mailbox.status) == (FAKE_EMAIL, MailboxStatus.OK)

    status = runner.invoke(cli, ["gmail", "status"])
    assert status.exit_code == 0
    assert f"client: {CLIENT_ID}" in status.output
    assert FAKE_EMAIL in status.output
    assert "ok" in status.output


def test_login_reports_a_denial_and_stores_nothing(
    cli_db: sessionmaker[Session],
    client_file: Path,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _browser(fake_google, monkeypatch, deny="yes")
    result = runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    assert result.exit_code == 1
    assert "access_denied" in result.output
    assert "no mailboxes" in runner.invoke(cli, ["gmail", "status"]).output


def test_login_times_out(
    cli_db: sessionmaker[Session], client_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "_present_authorization_url", lambda url: None)
    monkeypatch.setattr(gmail_oauth.LoopbackReceiver, "wait", _raise_timeout)
    result = runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    assert result.exit_code == 1
    assert "no answer from Google" in result.output


def _raise_timeout(self: object, timeout_s: float) -> dict[str, str]:
    raise TimeoutError


def test_check_reports_a_revoked_token_and_fails(
    cli_db: sessionmaker[Session],
    client_file: Path,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _browser(fake_google, monkeypatch)
    runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    ok = runner.invoke(cli, ["gmail", "check"])
    assert ok.exit_code == 0
    assert ": ok" in ok.output

    fake_google.revoke_all()
    revoked = runner.invoke(cli, ["gmail", "check"])
    assert revoked.exit_code == 1
    assert "reauth_required (invalid_grant)" in revoked.output
    assert "reauth_required" in runner.invoke(cli, ["gmail", "status"]).output


def test_disconnect_disables_the_mailbox(
    cli_db: sessionmaker[Session],
    client_file: Path,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _browser(fake_google, monkeypatch)
    runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    result = runner.invoke(cli, ["gmail", "disconnect", FAKE_EMAIL.upper()])
    assert result.exit_code == 0, result.output
    assert "disabled" in runner.invoke(cli, ["gmail", "status"]).output
    assert (
        runner.invoke(cli, ["gmail", "check"]).output.strip() == "no connected mailboxes to check"
    )
    missing = runner.invoke(cli, ["gmail", "disconnect", "nobody@example.com"])
    assert missing.exit_code == 1


def test_a_second_account_is_refused(
    cli_db: sessionmaker[Session],
    client_file: Path,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _browser(fake_google, monkeypatch)
    runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    _browser(fake_google, monkeypatch, email="other@example.com")
    result = runner.invoke(cli, ["gmail", "login"])
    assert result.exit_code == 1
    assert f"gmail disconnect {FAKE_EMAIL}" in result.output


def test_login_refuses_an_answer_for_another_login(
    cli_db: sessionmaker[Session],
    client_file: Path,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect whose ``state`` is not this login's is never exchanged (#256)."""

    def forged(url: str) -> None:
        answer = fake_google.consent(url)
        state = parse_qs(urlsplit(answer).query)["state"][0]
        threading.Thread(
            target=_follow, args=(answer.replace(state, "someone-elses-state"),), daemon=True
        ).start()

    monkeypatch.setattr(cli_module, "_present_authorization_url", forged)
    result = runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    assert result.exit_code == 1
    assert "(state)" in result.output
    assert ("/token", "authorization_code") not in fake_google.requests
    assert "no mailboxes" in runner.invoke(cli, ["gmail", "status"]).output


def test_login_never_prints_the_code_or_a_token(
    cli_db: sessionmaker[Session],
    client_file: Path,
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []

    def present(url: str) -> None:
        answers.append(fake_google.consent(url))
        threading.Thread(target=_follow, args=(answers[-1],), daemon=True).start()

    monkeypatch.setattr(cli_module, "_present_authorization_url", present)
    result = runner.invoke(cli, ["gmail", "login", "--client-file", str(client_file)])
    assert result.exit_code == 0, result.output
    [answer] = answers
    [refresh_token] = fake_google.refresh_tokens
    shown = (
        parse_qs(urlsplit(answer).query)["code"][0],
        refresh_token,
        *fake_google.access_tokens,
        CLIENT_SECRET,
    )
    for secret in shown:
        assert secret not in result.output
