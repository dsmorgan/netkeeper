"""A mailbox's Gmail client: tokens from P3-01, auth failures through its pause (#262).

Google's token endpoint is the loopback ``fake_google``; the Gmail API is
:class:`RecordingHttp`. Nothing reaches Google.
"""

from datetime import UTC, datetime

import factories
import pytest
from gmail_fakes import (
    CLIENT_ID,
    CLIENT_SECRET,
    FakeGoogle,
    MemoryKeyring,
    RecordingHttp,
    gmail_error,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import gmail_oauth
from netkeeper.campaigns.gmail import GmailAuthError, GmailTransient
from netkeeper.db import session_scope
from netkeeper.models import Mailbox, MailboxStatus, User
from netkeeper.scoping import unscoped
from netkeeper.services import keychain
from netkeeper.services import mailboxes as service

CLIENT = gmail_oauth.OAuthClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
LABELS = {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]}


def _row(factory: sessionmaker[Session], mailbox_id: int) -> Mailbox:
    with session_scope(factory) as session:
        row = session.scalars(unscoped(select(Mailbox).where(Mailbox.id == mailbox_id))).one()
        session.expunge(row)
        return row


@pytest.fixture
def connected(
    session_factory: sessionmaker[Session], fake_google: FakeGoogle
) -> tuple[User, Mailbox]:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
    service.save_client(user, CLIENT)
    with session_scope(session_factory, write=True) as session:
        mailbox = service.connect(
            session,
            user,
            "sender@example.com",
            fake_google.issue_refresh_token(),
            daily_cap=80,
            now=NOW,
        )
    return user, mailbox


def test_the_client_renews_its_token_with_the_mailboxs_grant(
    session_factory: sessionmaker[Session],
    fake_google: FakeGoogle,
    connected: tuple[User, Mailbox],
) -> None:
    user, mailbox = connected
    http = RecordingHttp().answer(200, LABELS)
    client = service.open_gmail(session_factory, user.id, mailbox.id, http=http)

    client.list_labels(purpose="labels for campaign 1")

    assert fake_google.requests == [("/token", "refresh_token")]
    bearer = http.requests[0].headers["authorization"].removeprefix("Bearer ")
    assert fake_google.access_tokens[bearer] == "sender@example.com"


def test_a_revoked_grant_pauses_the_mailbox(
    session_factory: sessionmaker[Session],
    fake_google: FakeGoogle,
    connected: tuple[User, Mailbox],
) -> None:
    user, mailbox = connected
    changes: list[service.CheckResult] = []
    client = service.open_gmail(
        session_factory, user.id, mailbox.id, http=RecordingHttp(), on_status_change=changes.append
    )
    fake_google.revoke_all()

    with pytest.raises(GmailAuthError) as caught:
        client.list_labels(purpose="labels for campaign 1")

    assert caught.value.code == "invalid_grant"
    row = _row(session_factory, mailbox.id)
    assert (row.status, row.status_reason) == (MailboxStatus.REAUTH_REQUIRED, "invalid_grant")
    assert [(c.mailbox_id, c.status, c.changed) for c in changes] == [
        (mailbox.id, MailboxStatus.REAUTH_REQUIRED, True)
    ]
    health = None
    with session_scope(session_factory) as session:
        health = service.mailbox_health(session, user, mailbox.id)
    assert health is not None and health.reauth_required and not health.healthy


def test_gmail_refusing_the_token_pauses_the_mailbox(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox]
) -> None:
    user, mailbox = connected
    http = RecordingHttp().answer(403, gmail_error(403, "insufficientPermissions"))
    client = service.open_gmail(session_factory, user.id, mailbox.id, http=http)
    with pytest.raises(GmailAuthError):
        client.list_labels(purpose="labels")
    assert _row(session_factory, mailbox.id).status_reason == "insufficientPermissions"


def test_a_transient_failure_changes_nothing(
    session_factory: sessionmaker[Session],
    fake_google: FakeGoogle,
    connected: tuple[User, Mailbox],
) -> None:
    user, mailbox = connected
    client = service.open_gmail(session_factory, user.id, mailbox.id, http=RecordingHttp())
    fake_google.token_status = 503
    with pytest.raises(GmailTransient):
        client.list_labels(purpose="labels")
    assert _row(session_factory, mailbox.id).status is MailboxStatus.OK


def test_a_mailbox_re_authorized_meanwhile_is_not_paused(
    session_factory: sessionmaker[Session],
    fake_google: FakeGoogle,
    connected: tuple[User, Mailbox],
) -> None:
    user, mailbox = connected
    client = service.open_gmail(session_factory, user.id, mailbox.id, http=RecordingHttp())
    fake_google.revoke_all()
    with session_scope(session_factory, write=True) as session:
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        service.connect(
            session,
            reloaded,
            "sender@example.com",
            fake_google.issue_refresh_token(),
            daily_cap=80,
        )

    with pytest.raises(GmailAuthError):
        client.list_labels(purpose="labels")
    assert _row(session_factory, mailbox.id).status is MailboxStatus.OK


def test_a_mailbox_disconnected_meanwhile_stays_disconnected(
    session_factory: sessionmaker[Session],
    fake_google: FakeGoogle,
    connected: tuple[User, Mailbox],
) -> None:
    user, mailbox = connected
    client = service.open_gmail(session_factory, user.id, mailbox.id, http=RecordingHttp())
    fake_google.revoke_all()
    with session_scope(session_factory, write=True) as session:
        reloaded = session.get(User, user.id)
        assert reloaded is not None
        service.disconnect(session, reloaded, service.get_mailbox(session, reloaded, mailbox.id))

    with pytest.raises(GmailAuthError):
        client.list_labels(purpose="labels")
    assert _row(session_factory, mailbox.id).status is MailboxStatus.DISABLED


def test_a_paused_mailbox_does_not_open(
    session_factory: sessionmaker[Session],
    fake_google: FakeGoogle,
    connected: tuple[User, Mailbox],
) -> None:
    user, mailbox = connected
    fake_google.revoke_all()
    service.check_mailbox(session_factory, user.id, mailbox.id)
    with pytest.raises(service.MailboxNotReady) as caught:
        service.open_gmail(session_factory, user.id, mailbox.id)
    assert caught.value.code == "reauth_required"


def test_another_users_mailbox_does_not_open(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox]
) -> None:
    _, mailbox = connected
    with session_scope(session_factory, write=True) as session:
        other = factories.make_user(session)
    with pytest.raises(service.MailboxNotFound):
        service.open_gmail(session_factory, other.id, mailbox.id)


@pytest.mark.parametrize("missing", ["token", "client"])
def test_a_missing_secret_pauses_the_mailbox(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox], missing: str
) -> None:
    user, mailbox = connected
    name = mailbox.keychain_ref if missing == "token" else service.CLIENT_SECRET_NAME
    keychain.delete_secret(user.id, name)
    changes: list[service.CheckResult] = []

    with pytest.raises(service.MailboxNotReady) as caught:
        service.open_gmail(session_factory, user.id, mailbox.id, on_status_change=changes.append)

    reason = f"{missing}_missing"
    assert caught.value.code == reason
    assert _row(session_factory, mailbox.id).status_reason == reason
    assert len(changes) == 1


def test_a_locked_keychain_changes_nothing(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    memory_keyring: MemoryKeyring,
) -> None:
    user, mailbox = connected
    memory_keyring.broken = True
    with pytest.raises(service.MailboxNotReady) as caught:
        service.open_gmail(session_factory, user.id, mailbox.id)
    assert caught.value.code == "keychain_unavailable"
    assert _row(session_factory, mailbox.id).status is MailboxStatus.OK


def test_mark_reauth_required_leaves_a_re_keyed_mailbox_alone(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox]
) -> None:
    user, mailbox = connected
    assert service.mark_reauth_required(session_factory, user.id, mailbox.id, "other", "x") == (
        service.CheckResult(mailbox.id, user.id, MailboxStatus.OK, None, False)
    )
    result = service.mark_reauth_required(
        session_factory, user.id, mailbox.id, mailbox.keychain_ref, "invalid_grant"
    )
    assert result is not None and result.changed
    assert service.mark_reauth_required(session_factory, user.id, 9999, "r", "x") is None
