"""Mailboxes: connect, disconnect, the health check and its poll (#244, P3-01)."""

import asyncio
from datetime import UTC, datetime, timedelta

import factories
import pytest
from gmail_fakes import CLIENT_ID, CLIENT_SECRET, FAKE_EMAIL, FakeGoogle, MemoryKeyring
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import gmail_oauth
from netkeeper.db import session_scope
from netkeeper.models import Mailbox, MailboxArm, MailboxStatus, User
from netkeeper.scoping import unscoped
from netkeeper.services import keychain
from netkeeper.services import mailboxes as service
from netkeeper.services.events import EventBus

CLIENT = gmail_oauth.OAuthClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _user(factory: sessionmaker[Session]) -> User:
    with session_scope(factory, write=True) as session:
        return factories.make_user(session)


def _connect(
    factory: sessionmaker[Session],
    user: User,
    token: str,
    email: str = FAKE_EMAIL,
    *,
    cap: int = 80,
) -> Mailbox:
    with session_scope(factory, write=True) as session:
        return service.connect(session, user, email, token, daily_cap=cap, now=NOW)


def _row(factory: sessionmaker[Session], mailbox_id: int) -> Mailbox:
    with session_scope(factory) as session:
        row = session.scalars(unscoped(select(Mailbox).where(Mailbox.id == mailbox_id))).one()
        session.expunge(row)
        return row


@pytest.fixture
def connected(
    session_factory: sessionmaker[Session], fake_google: FakeGoogle
) -> tuple[User, Mailbox]:
    user = _user(session_factory)
    service.save_client(user, CLIENT)
    return user, _connect(session_factory, user, fake_google.issue_refresh_token())


def test_the_hard_max_is_pinned() -> None:
    """Spec 11.4: never more than 400 recipients a day from one mailbox."""
    assert service.MAILBOX_HARD_MAX_PER_DAY == 400


# --- connect and disconnect ---------------------------------------------------------


def test_connecting_stores_the_token_in_the_keychain_and_never_in_the_database(
    session_factory: sessionmaker[Session], memory_keyring: MemoryKeyring
) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt-secret-value", "Sender@Example.com")
    assert mailbox.email == "sender@example.com"
    assert mailbox.status is MailboxStatus.OK
    assert mailbox.status_reason is None
    assert mailbox.checked_at == NOW
    assert mailbox.daily_cap == 80
    assert mailbox.label_prefix == "netkeeper"
    assert mailbox.keychain_ref == f"gmail/mailbox/{mailbox.id}"
    assert memory_keyring.entries == {
        ("netkeeper", f"{user.id}/gmail/mailbox/{mailbox.id}"): "rt-secret-value"
    }
    row = _row(session_factory, mailbox.id)
    columns = [attr.key for attr in inspect(Mailbox).column_attrs]
    assert all("rt-secret-value" not in str(getattr(row, name)) for name in columns)


def test_a_cap_over_the_hard_max_is_held_to_it(session_factory: sessionmaker[Session]) -> None:
    user = _user(session_factory)
    assert _connect(session_factory, user, "rt", cap=1000).daily_cap == 400


def test_authorizing_the_same_address_again_heals_it_and_replaces_the_token(
    session_factory: sessionmaker[Session], memory_keyring: MemoryKeyring
) -> None:
    user = _user(session_factory)
    first = _connect(session_factory, user, "rt-old")
    with session_scope(session_factory, write=True) as session:
        row = service.get_mailbox(session, user, first.id)
        row.status = MailboxStatus.REAUTH_REQUIRED
        row.status_reason = "invalid_grant"
    again = _connect(session_factory, user, "rt-new", "SENDER@example.com")
    assert again.id == first.id
    assert again.status is MailboxStatus.OK
    assert again.status_reason is None
    assert memory_keyring.entries[("netkeeper", f"{user.id}/{first.keychain_ref}")] == "rt-new"


def test_a_second_address_is_refused_while_the_first_is_live(
    session_factory: sessionmaker[Session], memory_keyring: MemoryKeyring
) -> None:
    user = _user(session_factory)
    _connect(session_factory, user, "rt-1", "one@example.com")
    with pytest.raises(service.OtherMailboxConnected, match="disconnect it"):
        _connect(session_factory, user, "rt-2", "two@example.com")
    assert "rt-2" not in memory_keyring.entries.values()
    with session_scope(session_factory) as session:
        assert [row.email for row in service.list_mailboxes(session, user)] == ["one@example.com"]


def test_disconnecting_forgets_the_token_and_keeps_the_row(
    session_factory: sessionmaker[Session], memory_keyring: MemoryKeyring
) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt-1", "one@example.com")
    with session_scope(session_factory, write=True) as session:
        row = service.disconnect(session, user, service.get_mailbox(session, user, mailbox.id))
        assert row.status is MailboxStatus.DISABLED
        assert row.status_reason == "disconnected"
    assert memory_keyring.entries == {}

    second = _connect(session_factory, user, "rt-2", "two@example.com")
    assert second.id != mailbox.id
    with session_scope(session_factory, write=True) as session:
        service.disconnect(session, user, service.get_mailbox(session, user, second.id))
    back = _connect(session_factory, user, "rt-3", "one@example.com")
    assert (back.id, back.status) == (mailbox.id, MailboxStatus.OK)


def test_a_refused_keychain_write_rolls_the_row_back(
    session_factory: sessionmaker[Session], memory_keyring: MemoryKeyring
) -> None:
    user = _user(session_factory)
    memory_keyring.broken = True
    with pytest.raises(keychain.KeychainUnavailable):
        _connect(session_factory, user, "rt")
    with session_scope(session_factory) as session:
        assert service.list_mailboxes(session, user) == []


def test_another_users_mailbox_is_not_found(session_factory: sessionmaker[Session]) -> None:
    owner = _user(session_factory)
    other = _user(session_factory)
    mailbox = _connect(session_factory, owner, "rt")
    with session_scope(session_factory) as session:
        with pytest.raises(service.MailboxNotFound):
            service.get_mailbox(session, other, mailbox.id)
        assert service.mailbox_health(session, other, mailbox.id) is None


# --- health --------------------------------------------------------------------------


def test_health_reads_the_status_fresh(session_factory: sessionmaker[Session]) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    with session_scope(session_factory) as reader:
        assert service.mailbox_health(reader, user, mailbox.id) == service.MailboxHealth(
            mailbox_id=mailbox.id, healthy=True, reauth_required=False, daily_cap=80
        )
        with session_scope(session_factory, write=True) as writer:
            row = service.get_mailbox(writer, user, mailbox.id)
            row.status = MailboxStatus.REAUTH_REQUIRED
        reader.rollback()  # a new read transaction, as the next guard check would have
        health = service.mailbox_health(reader, user, mailbox.id)
        assert health is not None
        assert (health.healthy, health.reauth_required) == (False, True)


def test_a_disconnected_mailbox_is_neither_healthy_nor_waiting_for_reauth(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    with session_scope(session_factory, write=True) as session:
        service.disconnect(session, user, service.get_mailbox(session, user, mailbox.id))
        health = service.mailbox_health(session, user, mailbox.id)
    assert health is not None
    assert (health.healthy, health.reauth_required) == (False, False)


def test_a_good_token_is_checked_and_stays_ok(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox]
) -> None:
    user, mailbox = connected
    later = NOW + timedelta(hours=1)
    result = service.check_mailbox(session_factory, user.id, mailbox.id, clock=lambda: later)
    assert result == service.CheckResult(mailbox.id, user.id, MailboxStatus.OK, None, False)
    assert _row(session_factory, mailbox.id).checked_at == later


def test_a_revoked_token_needs_reauth(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox], fake_google: FakeGoogle
) -> None:
    user, mailbox = connected
    fake_google.revoke_all()
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result == service.CheckResult(
        mailbox.id, user.id, MailboxStatus.REAUTH_REQUIRED, "invalid_grant", True
    )
    row = _row(session_factory, mailbox.id)
    assert (row.status, row.status_reason, row.checked_at) == (
        MailboxStatus.REAUTH_REQUIRED,
        "invalid_grant",
        NOW,
    )


def test_a_deleted_oauth_client_needs_reauth_with_googles_code(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox], fake_google: FakeGoogle
) -> None:
    user, mailbox = connected
    fake_google.client_secret = "rotated"
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result is not None
    assert (result.status, result.reason) == (MailboxStatus.REAUTH_REQUIRED, "invalid_client")


def test_an_unauthorized_client_needs_reauth(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox], fake_google: FakeGoogle
) -> None:
    user, mailbox = connected
    fake_google.token_answer = (400, {"error": "unauthorized_client"})
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result is not None
    assert (result.status, result.reason) == (MailboxStatus.REAUTH_REQUIRED, "unauthorized_client")


@pytest.mark.parametrize(
    "answer",
    [
        (407, "<html>Proxy Authentication Required</html>"),
        (403, "<html>Forbidden</html>"),
        (404, "<html>Not Found</html>"),
        (400, {"error": "invalid_request"}),
        (200, {"token_type": "Bearer"}),
        (503, {"error": "invalid_grant"}),
    ],
)
def test_a_refusal_that_says_nothing_about_the_grant_changes_nothing(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    fake_google: FakeGoogle,
    answer: tuple[int, dict[str, object] | str],
) -> None:
    """A proxy, an HTML error page, a malformed request or a 5xx is not a dead grant (#256)."""
    user, mailbox = connected
    fake_google.token_answer = answer
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result == service.CheckResult(mailbox.id, user.id, MailboxStatus.OK, None, False)
    row = _row(session_factory, mailbox.id)
    assert (row.status, row.status_reason, row.checked_at) == (MailboxStatus.OK, None, NOW)


def test_a_token_missing_from_the_keychain_needs_reauth(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    memory_keyring: MemoryKeyring,
) -> None:
    user, mailbox = connected
    del memory_keyring.entries[("netkeeper", f"{user.id}/{mailbox.keychain_ref}")]
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result is not None
    assert (result.status, result.reason) == (MailboxStatus.REAUTH_REQUIRED, "token_missing")


def test_a_missing_client_needs_reauth(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    memory_keyring: MemoryKeyring,
) -> None:
    user, mailbox = connected
    del memory_keyring.entries[("netkeeper", f"{user.id}/gmail/oauth_client")]
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result is not None
    assert (result.status, result.reason) == (MailboxStatus.REAUTH_REQUIRED, "client_missing")


def test_google_being_down_changes_nothing(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox], fake_google: FakeGoogle
) -> None:
    user, mailbox = connected
    fake_google.token_status = 500
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result == service.CheckResult(mailbox.id, user.id, MailboxStatus.OK, None, False)
    assert _row(session_factory, mailbox.id).checked_at == NOW  # not a success either


def test_an_answer_cut_off_part_way_changes_nothing(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox], fake_google: FakeGoogle
) -> None:
    user, mailbox = connected
    fake_google.truncate = True
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result == service.CheckResult(mailbox.id, user.id, MailboxStatus.OK, None, False)


def test_a_locked_keychain_changes_nothing(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    memory_keyring: MemoryKeyring,
) -> None:
    user, mailbox = connected
    memory_keyring.broken = True
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result is not None
    assert (result.status, result.changed) == (MailboxStatus.OK, False)


def test_only_an_ok_mailbox_is_checked(
    session_factory: sessionmaker[Session], connected: tuple[User, Mailbox], fake_google: FakeGoogle
) -> None:
    user, mailbox = connected
    with session_scope(session_factory, write=True) as session:
        service.disconnect(session, user, service.get_mailbox(session, user, mailbox.id))
    calls = len(fake_google.requests)
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result is not None
    assert (result.status, result.changed) == (MailboxStatus.DISABLED, False)
    assert len(fake_google.requests) == calls


def test_a_disconnect_during_the_check_is_not_overwritten(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check writes only if the mailbox is still the one it asked Google about."""
    user, mailbox = connected
    fake_google.revoke_all()
    refresh = gmail_oauth.refresh_access_token

    def disconnect_meanwhile(*args: object, **kwargs: object) -> str:
        with session_scope(session_factory, write=True) as session:
            service.disconnect(session, user, service.get_mailbox(session, user, mailbox.id))
        return refresh(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gmail_oauth, "refresh_access_token", disconnect_meanwhile)
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result is not None
    assert (result.status, result.changed) == (MailboxStatus.DISABLED, False)
    assert _row(session_factory, mailbox.id).status_reason == "disconnected"


def test_a_reauthorization_during_the_check_is_not_overwritten(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    fake_google: FakeGoogle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll refreshes the old, dead token while a person authorizes again (#256).

    ``keychain_ref`` is the same before and after, so only ``generation`` shows
    the check's answer is about a grant that has since been replaced.
    """
    user, mailbox = connected
    fake_google.revoke_all()
    refresh = gmail_oauth.refresh_access_token

    def reauthorize_meanwhile(*args: object, **kwargs: object) -> str:
        _connect(session_factory, user, fake_google.issue_refresh_token())
        return refresh(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gmail_oauth, "refresh_access_token", reauthorize_meanwhile)
    result = service.check_mailbox(session_factory, user.id, mailbox.id)
    assert result == service.CheckResult(mailbox.id, user.id, MailboxStatus.OK, None, False)
    row = _row(session_factory, mailbox.id)
    assert (row.status, row.status_reason, row.keychain_ref) == (
        MailboxStatus.OK,
        None,
        mailbox.keychain_ref,
    )


def test_every_authorization_moves_the_generation(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    first = _connect(session_factory, user, "rt-1")
    second = _connect(session_factory, user, "rt-2")
    with session_scope(session_factory, write=True) as session:
        service.disconnect(session, user, service.get_mailbox(session, user, first.id))
    third = _connect(session_factory, user, "rt-3")
    assert first.id == second.id == third.id
    assert (first.generation, second.generation, third.generation) == (1, 2, 3)
    assert _row(session_factory, first.id).generation == 3


def test_the_check_holds_no_session_while_it_asks_google(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another writer can take the lock while the refresh is in flight."""
    user, mailbox = connected
    refresh = gmail_oauth.refresh_access_token
    wrote: list[bool] = []

    def write_meanwhile(*args: object, **kwargs: object) -> str:
        with session_scope(session_factory, write=True) as session:
            session.execute(select(1))  # BEGIN IMMEDIATE: waits, then fails, if a writer is open
        wrote.append(True)
        return refresh(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gmail_oauth, "refresh_access_token", write_meanwhile)
    service.check_mailbox(session_factory, user.id, mailbox.id)
    assert wrote == [True]


# --- the poll -----------------------------------------------------------------------


def test_the_poll_checks_every_users_ok_mailboxes_and_reports_changes(
    session_factory: sessionmaker[Session], fake_google: FakeGoogle
) -> None:
    alice = _user(session_factory)
    bob = _user(session_factory)
    carol = _user(session_factory)
    for user in (alice, bob, carol):
        service.save_client(user, CLIENT)
    kept = _connect(session_factory, alice, fake_google.issue_refresh_token("a@example.com"))
    revoked_token = fake_google.issue_refresh_token("b@example.com")
    revoked = _connect(session_factory, bob, revoked_token, "b@example.com")
    idle = _connect(session_factory, carol, fake_google.issue_refresh_token(), "c@example.com")
    with session_scope(session_factory, write=True) as session:
        service.disconnect(session, carol, service.get_mailbox(session, carol, idle.id))
    fake_google.revoked.add(revoked_token)

    results = service.poll_mailboxes(session_factory)
    assert sorted((r.mailbox_id, r.status, r.changed) for r in results) == sorted(
        [
            (kept.id, MailboxStatus.OK, False),
            (revoked.id, MailboxStatus.REAUTH_REQUIRED, True),
        ]
    )
    assert service.poll_mailboxes(session_factory) == [
        service.CheckResult(kept.id, alice.id, MailboxStatus.OK, None, False)
    ]


async def test_the_monitor_publishes_a_status_change_once(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    fake_google: FakeGoogle,
) -> None:
    user, mailbox = connected
    bus = EventBus()
    subscription = bus.subscribe()
    monitor = service.MailboxMonitor(session_factory, bus, interval_s=3600)
    await monitor.poll_once()
    fake_google.revoke_all()
    await monitor.poll_once()
    await monitor.poll_once()
    bus.unsubscribe(subscription)
    events = [event async for event in subscription]
    assert [(e.type, e.data, e.user_id) for e in events] == [
        (
            "mailbox.status",
            {"mailbox_id": mailbox.id, "status": "reauth_required", "reason": "invalid_grant"},
            user.id,
        )
    ]


async def test_the_monitor_polls_every_interval_until_stopped(
    session_factory: sessionmaker[Session],
    connected: tuple[User, Mailbox],
    fake_google: FakeGoogle,
) -> None:
    bus = EventBus()
    subscription = bus.subscribe()
    fake_google.revoke_all()
    monitor = service.MailboxMonitor(session_factory, bus, interval_s=0.01)
    monitor.start()
    event = await asyncio.wait_for(anext(subscription), timeout=5)
    await monitor.stop()
    assert event.data["status"] == "reauth_required"


async def test_the_monitor_survives_a_failing_poll(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def boom(*args: object, **kwargs: object) -> list[service.CheckResult]:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "poll_mailboxes", boom)
    monitor = service.MailboxMonitor(session_factory, EventBus(), interval_s=0.01)
    monitor.start()
    for _ in range(200):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()
    assert len(calls) >= 2


def test_the_monitor_needs_a_positive_interval(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(ValueError, match="positive"):
        service.MailboxMonitor(session_factory, EventBus(), interval_s=0)


# --- authorizations in flight -------------------------------------------------------


def _authorization(state: str) -> gmail_oauth.Authorization:
    return gmail_oauth.Authorization(
        url="u", state=state, verifier="v" * 43, redirect_uri="http://127.0.0.1/"
    )


def test_a_pending_authorization_is_handed_out_once_to_its_user() -> None:
    pending = service.PendingAuthorizations()
    pending.add(1, _authorization("s1"))
    assert pending.take("s1", 2) is None  # someone else's: refused, and now gone
    assert pending.take("s1", 1) is None
    pending.add(1, _authorization("s2"))
    assert pending.take("s2", 1) == _authorization("s2")
    assert pending.take("s2", 1) is None


def test_a_pending_authorization_expires_after_ten_minutes() -> None:
    now = [NOW]
    pending = service.PendingAuthorizations(clock=lambda: now[0])
    pending.add(1, _authorization("s1"))
    pending.add(1, _authorization("s2"))
    now[0] = NOW + timedelta(minutes=9, seconds=59)
    assert pending.take("s1", 1) is not None
    now[0] = NOW + timedelta(minutes=10)
    assert pending.take("s2", 1) is None
    assert timedelta(minutes=10) == service.PENDING_TTL


def test_starting_too_many_drops_the_oldest() -> None:
    pending = service.PendingAuthorizations()
    for index in range(service.PENDING_MAX + 1):
        pending.add(1, _authorization(f"s{index}"))
    assert pending.take("s0", 1) is None
    assert pending.take(f"s{service.PENDING_MAX}", 1) is not None


# --- arming (#277) -----------------------------------------------------------------


def _arm(
    factory: sessionmaker[Session], user: User, mailbox_id: int, mode: MailboxArm, *, by: str = "t"
) -> Mailbox:
    with session_scope(factory, write=True) as session:
        mailbox = service.get_mailbox(session, user, mailbox_id)
        service.arm(session, user, mailbox, mode, by=by, now=NOW)
        session.expunge(mailbox)
        return mailbox


def _verify(factory: sessionmaker[Session], user: User, mailbox_id: int) -> bool:
    with session_scope(factory, write=True) as session:
        return service.record_message_id_verified(session, user, mailbox_id, now=NOW)


def test_a_new_mailbox_starts_disarmed(session_factory: sessionmaker[Session]) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    assert mailbox.arm is None
    with session_scope(session_factory) as session:
        assert service.armed(session, user, mailbox.id) is None


def test_arming_for_drafts_records_who_and_when(session_factory: sessionmaker[Session]) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    row = _arm(session_factory, user, mailbox.id, MailboxArm.DRAFT, by="cli (me)")
    assert (row.arm, row.armed_at, row.armed_by, row.send_armed_at) == (
        MailboxArm.DRAFT,
        NOW,
        "cli (me)",
        None,
    )
    with session_scope(session_factory) as session:
        assert service.armed(session, user, mailbox.id) is MailboxArm.DRAFT


def test_send_is_refused_until_armed_for_drafts_and_a_draft_was_found(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    with pytest.raises(service.ArmRefused, match="not armed"):
        _arm(session_factory, user, mailbox.id, MailboxArm.SEND)
    _arm(session_factory, user, mailbox.id, MailboxArm.DRAFT)
    with pytest.raises(service.ArmRefused, match="Message-ID"):
        _arm(session_factory, user, mailbox.id, MailboxArm.SEND)
    assert _row(session_factory, mailbox.id).arm is MailboxArm.DRAFT

    assert _verify(session_factory, user, mailbox.id)
    assert not _verify(session_factory, user, mailbox.id)  # recorded once
    row = _arm(session_factory, user, mailbox.id, MailboxArm.SEND, by="web (user 1)")
    assert (row.arm, row.send_armed_at, row.armed_by) == (MailboxArm.SEND, NOW, "web (user 1)")


def test_even_verified_send_needs_the_draft_step_first(
    session_factory: sessionmaker[Session],
) -> None:
    """Arming for send is always a separate step after arming for drafts."""
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    _verify(session_factory, user, mailbox.id)
    with pytest.raises(service.ArmRefused, match="not armed"):
        _arm(session_factory, user, mailbox.id, MailboxArm.SEND)


def test_arming_for_drafts_takes_send_back_and_disarm_undoes_both(
    session_factory: sessionmaker[Session],
) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    _arm(session_factory, user, mailbox.id, MailboxArm.DRAFT)
    _verify(session_factory, user, mailbox.id)
    _arm(session_factory, user, mailbox.id, MailboxArm.SEND)
    assert _arm(session_factory, user, mailbox.id, MailboxArm.DRAFT).arm is MailboxArm.DRAFT
    with session_scope(session_factory, write=True) as session:
        service.disarm(session, user, service.get_mailbox(session, user, mailbox.id))
    row = _row(session_factory, mailbox.id)
    assert (row.arm, row.armed_at, row.send_armed_at, row.armed_by) == (None, None, None, None)
    assert row.message_id_verified_at == NOW  # the check stays passed


def test_disconnecting_disarms_and_a_disconnected_mailbox_is_never_armed(
    session_factory: sessionmaker[Session], memory_keyring: MemoryKeyring
) -> None:
    user = _user(session_factory)
    mailbox = _connect(session_factory, user, "rt")
    _arm(session_factory, user, mailbox.id, MailboxArm.DRAFT)
    with session_scope(session_factory, write=True) as session:
        service.disconnect(session, user, service.get_mailbox(session, user, mailbox.id))
    assert _row(session_factory, mailbox.id).arm is None
    with pytest.raises(service.ArmRefused, match="disconnected"):
        _arm(session_factory, user, mailbox.id, MailboxArm.DRAFT)
    assert _connect(session_factory, user, "rt-2").arm is None  # connected again: disarmed


def test_another_users_mailbox_reads_disarmed(session_factory: sessionmaker[Session]) -> None:
    owner = _user(session_factory)
    other = _user(session_factory)
    mailbox = _connect(session_factory, owner, "rt")
    _arm(session_factory, owner, mailbox.id, MailboxArm.DRAFT)
    with session_scope(session_factory, write=True) as session:
        assert service.armed(session, other, mailbox.id) is None
        assert not service.record_message_id_verified(session, other, mailbox.id, now=NOW)
