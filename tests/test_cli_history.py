"""``netkeeper history import`` and ``history scan-gmail`` (#65), against a migrated scratch
database under ``tmp_path`` and the Gmail fake. Every person is invented."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import factories
import pytest
from campaign_fakes import make_mailbox
from history_fixtures import Tab, workbook_bytes
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.campaigns.gmail import GmailRateLimited
from netkeeper.campaigns.gmail_fake import FakeGmail
from netkeeper.cli import app as cli
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Contact,
    DoNotSendAddress,
    HistoryCampaign,
    HistoryRecipient,
    Interaction,
    UserOwned,
)
from netkeeper.scoping import install_scope_guard, scoped_count
from netkeeper.services import history_scan
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.users import ensure_local_user

runner = CliRunner()
ME = "me@example.test"
COUNTED: tuple[type[UserOwned], ...] = (
    HistoryCampaign,
    HistoryRecipient,
    Interaction,
    Contact,
    DoNotSendAddress,
)


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, emails=["ada@example.test"])
        make_mailbox(session, user, email=ME)
    yield factory
    engine.dispose()


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    path = tmp_path / "history.xlsx"
    path.write_bytes(workbook_bytes(Tab()))
    return path


def _counts(factory: sessionmaker[Session]) -> dict[str, int]:
    with session_scope(factory) as session:
        user = ensure_local_user(session)
        return {m.__name__: session.scalar(scoped_count(user, m)) or 0 for m in COUNTED}


def test_import_is_a_dry_run_unless_asked_to_apply(
    cli_db: sessionmaker[Session], workbook: Path
) -> None:
    before = _counts(cli_db)

    result = runner.invoke(cli, ["history", "import", str(workbook), "--create-missing"])

    assert result.exit_code == 0, result.output
    assert "dry run: nothing was written" in result.output
    assert "Spring check-in" in result.output
    assert _counts(cli_db) == before


def test_import_apply_writes_and_a_second_run_adds_nothing(
    cli_db: sessionmaker[Session], workbook: Path
) -> None:
    first = runner.invoke(cli, ["history", "import", str(workbook), "--apply"])
    assert first.exit_code == 0, first.output
    assert "applied" in first.output
    assert "1 new campaign(s), 4 new recipient row(s), 1 new timeline entry" in first.output
    assert "unmatched addresses (3)" in first.output
    assert "  cy@example.test" in first.output
    assert "1 recipient(s) across these campaigns are not named in it" in first.output
    after_first = _counts(cli_db)
    assert after_first["HistoryCampaign"] == 1 and after_first["HistoryRecipient"] == 4

    second = runner.invoke(cli, ["history", "import", str(workbook), "--apply"])

    assert second.exit_code == 0, second.output
    assert "0 new campaign(s), 0 new recipient row(s), 0 new timeline entries" in second.output
    assert _counts(cli_db) == after_first


def test_import_refuses_a_file_that_is_not_a_workbook(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    path = tmp_path / "contacts.csv"
    path.write_text("First Name,Last Name,Email\n")
    result = runner.invoke(cli, ["history", "import", str(path)])
    assert result.exit_code == 1
    assert "not an xlsx workbook" in result.output


def _fake(**kwargs: Any) -> FakeGmail:
    fake = FakeGmail(ME, mailbox_id=1, clock=lambda: datetime(2026, 10, 3, tzinfo=UTC))
    reply = EmailMessage()
    reply["From"] = "ada@example.test"
    reply["To"] = ME
    reply["Subject"] = "Re: Catching up"
    reply.set_content("Good to hear from you.")
    fake.deliver(reply, at=datetime(2026, 3, 4, 15, 0, tzinfo=UTC))
    return fake


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[FakeGmail]:
    """Every Gmail the command opens is a fake; never a real client."""
    made: list[FakeGmail] = []

    def open_gmail(factory: object, user_id: int, mailbox_id: int) -> FakeGmail:
        fake = _fake()
        made.append(fake)
        return fake

    monkeypatch.setattr(mailbox_service, "open_gmail", open_gmail)
    return made


def test_scan_is_a_dry_run_unless_asked_to_apply(
    cli_db: sessionmaker[Session], workbook: Path, opened: list[FakeGmail]
) -> None:
    runner.invoke(cli, ["history", "import", str(workbook), "--apply"])
    before = _counts(cli_db)

    result = runner.invoke(cli, ["history", "scan-gmail"])

    assert result.exit_code == 0, result.output
    assert "dry run: nothing was written" in result.output
    assert "replies (first 1):\n  ada@example.test" in result.output
    assert "1 contact(s) flagged for review" in result.output
    assert _counts(cli_db) == before
    (fake,) = opened
    assert {method for method, _ in fake.calls} <= history_scan.READ_ONLY_METHODS


def test_scan_apply_writes_and_skips_what_it_scanned(
    cli_db: sessionmaker[Session], workbook: Path, opened: list[FakeGmail]
) -> None:
    runner.invoke(cli, ["history", "import", str(workbook), "--apply"])

    result = runner.invoke(cli, ["history", "scan-gmail", "--mailbox", ME, "--apply"])

    assert result.exit_code == 0, result.output
    assert _counts(cli_db)["Interaction"] == 2  # the import's email_out, the reply's email_in
    again = runner.invoke(cli, ["history", "scan-gmail", "--apply"])
    assert again.exit_code == 0, again.output
    assert "nothing to scan" in again.output
    assert len(opened) == 1  # the second run had nothing to ask Gmail


def test_scan_stopped_by_a_rate_limit_exits_1_and_says_how_to_resume(
    cli_db: sessionmaker[Session], workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(cli, ["history", "import", str(workbook), "--apply"])
    fake = _fake()
    fake.fail_next("messages.list", GmailRateLimited("429", code="rateLimitExceeded"))
    monkeypatch.setattr(mailbox_service, "open_gmail", lambda *args: fake)

    result = runner.invoke(cli, ["history", "scan-gmail", "--apply"])

    assert result.exit_code == 1
    assert "Gmail stopped the scan (rateLimitExceeded); 4 recipient(s)" in result.output


def test_scan_without_a_mailbox_says_so(cli_db: sessionmaker[Session], workbook: Path) -> None:
    result = runner.invoke(cli, ["history", "scan-gmail", "--mailbox", "other@example.test"])
    assert result.exit_code == 1
    assert "no mailbox other@example.test" in result.output
