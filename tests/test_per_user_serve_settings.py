"""``serve``'s scheduler, mailbox monitor and reply poll use each user's own Settings-page
values (#464), read as they run, and fail closed when a user's values cannot be read."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import User
from netkeeper.services import campaign_replies, mailboxes, scheduler
from netkeeper.services import ui_settings as ui
from netkeeper.services.campaign_sender import GmailSender
from netkeeper.services.events import EventBus
from netkeeper.services.scheduled_runs import user_active_hours
from netkeeper.services.settings_kv import set_setting

NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
HOURS = "config.linkedin.active_hours"
POLL = "config.campaigns.reply_poll_minutes"


def _two_users(factory: sessionmaker[Session]) -> tuple[int, int]:
    with session_scope(factory, write=True) as session:
        first = factories.make_user(session)
        second = factories.make_user(session)
        set_setting(session, first, HOURS, ["07:15", "19:45"])
        set_setting(session, first, POLL, 3)
        set_setting(session, second, HOURS, ["10:00", "16:00"])
        set_setting(session, second, POLL, 30)
        return first.id, second.id


def _user(factory: sessionmaker[Session], user_id: int) -> User:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        session.expunge(user)
        return user


# --- active hours -----------------------------------------------------------------


def test_each_user_gets_their_own_active_hours(session_factory: sessionmaker[Session]) -> None:
    first, second = _two_users(session_factory)
    hours = user_active_hours(session_factory, Settings())
    assert hours(_user(session_factory, first)) == (time(7, 15), time(19, 45))
    assert hours(_user(session_factory, second)) == (time(10, 0), time(16, 0))


def test_a_file_pinned_window_wins_for_every_user(session_factory: sessionmaker[Session]) -> None:
    first, second = _two_users(session_factory)
    base = Settings()
    base = _pinned_hours(base, ("09:00", "17:00"))
    hours = user_active_hours(session_factory, base)
    assert hours(_user(session_factory, first)) == (time(9, 0), time(17, 0))
    assert hours(_user(session_factory, second)) == (time(9, 0), time(17, 0))


def _pinned_hours(base: Settings, window: tuple[str, str]) -> Settings:
    from dataclasses import replace

    return replace(
        base,
        linkedin=replace(base.linkedin, active_hours=window),
        file_keys=frozenset({"linkedin.active_hours"}),
    )


def test_a_value_that_does_not_parse_raises_not_defaults(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        user_id = user.id
    hours = user_active_hours(session_factory, _pinned_hours(Settings(), ("later", "never")))
    with pytest.raises(ValueError):
        hours(_user(session_factory, user_id))


async def test_the_heartbeat_gives_each_user_their_own_window_and_skips_an_unreadable_one(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first, second = _two_users(session_factory)
    with session_scope(session_factory, write=True) as session:
        third = factories.make_user(session).id
    seen: dict[int, tuple[time, time]] = {}

    async def fake_poll_and_fire(
        factory: Any, user: User, account_id: int, kind: Any, **kwargs: Any
    ) -> None:
        seen[user.id] = (kwargs["active_start"], kwargs["active_end"])
        return None

    async def due(*args: Any, **kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(scheduler, "poll_and_fire", fake_poll_and_fire)
    monkeypatch.setattr(scheduler, "_due_now", lambda *a: {scheduler.JobKind.INBOX: NOW})
    monkeypatch.setattr(scheduler, "_last_message_send", lambda *a: None)
    real = user_active_hours(session_factory, Settings())

    def flaky(user: User) -> tuple[time, time]:
        if user.id == third:
            raise RuntimeError("database is locked")
        return real(user)

    users = [(_user(session_factory, uid), 1) for uid in (first, second, third)]
    with caplog.at_level(logging.ERROR):
        await scheduler.poll_once(
            session_factory,
            users,
            now=NOW,
            registry={},
            schedules={
                scheduler.JobKind.INBOX: scheduler.DEFAULT_SCHEDULES[scheduler.JobKind.INBOX]
            },
            active_hours=flaky,
        )
    assert seen == {first: (time(7, 15), time(19, 45)), second: (time(10, 0), time(16, 0))}
    assert f"active hours of user {third}" in caplog.text


def test_an_unreadable_window_leaves_the_schedule_unestablished(
    session_factory: sessionmaker[Session],
) -> None:
    first, second = _two_users(session_factory)
    real = user_active_hours(session_factory, Settings())

    def flaky(user: User) -> tuple[time, time]:
        if user.id == second:
            raise RuntimeError("boom")
        return real(user)

    from netkeeper.services.linkedin_accounts import ensure_account

    accounts: list[tuple[User, int]] = []
    for uid in (first, second):
        with session_scope(session_factory, write=True) as session:
            user = session.get(User, uid)
            assert user is not None
            accounts.append((user, ensure_account(session, user).id))
        # expunged copies are what the scheduler sees
    scheduler.build_scheduler(
        session_factory, lambda: accounts, active_hours=flaky, clock=lambda: NOW
    )
    with session_scope(session_factory) as session:
        got = {
            uid: scheduler.stored_due(session, owner, acct, scheduler.JobKind.INBOX)
            for (owner, acct), uid in zip(accounts, (first, second), strict=True)
        }
    assert got[first] is not None
    assert got[second] is None


# --- mailbox monitor --------------------------------------------------------------


async def test_the_monitor_polls_each_user_on_their_own_interval(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _two_users(session_factory)  # 3 and 30 minutes
    polled: list[tuple[float, list[int]]] = []
    clock = [1000.0]

    def fake_poll(factory: Any, **kwargs: Any) -> list[Any]:
        polled.append((clock[0] - 1000.0, sorted(kwargs["user_ids"])))
        return []

    monkeypatch.setattr(mailboxes, "poll_mailboxes", fake_poll)
    file = Settings()

    def interval(user_id: int) -> float:
        with session_scope(session_factory) as session:
            user = session.get(User, user_id)
            assert user is not None
            return ui.resolve(session, user, file).campaigns.reply_poll_minutes * 60.0

    monitor = mailboxes.MailboxMonitor(
        session_factory,
        EventBus(),
        interval_s=60,
        interval_for=interval,
        monotonic=lambda: clock[0],
    )
    for minute in range(0, 61):
        clock[0] = 1000.0 + minute * 60
        await monitor.poll_due()
    by_user = {first: [], second: []}  # type: dict[int, list[float]]
    for at, users in polled:
        for uid in users:
            by_user.setdefault(uid, []).append(at / 60)
    assert by_user[first][:3] == [3, 6, 9]
    assert by_user[second] == [30, 60]


async def test_the_monitor_skips_a_user_whose_interval_cannot_be_read(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first, second = _two_users(session_factory)
    polled: list[list[int]] = []

    def fake_poll(factory: Any, **kw: Any) -> list[Any]:
        polled.append(sorted(kw["user_ids"]))
        return []

    monkeypatch.setattr(mailboxes, "poll_mailboxes", fake_poll)
    clock = [0.0]

    def interval(user_id: int) -> float:
        if user_id == second:
            raise RuntimeError("boom")
        return 60.0

    monitor = mailboxes.MailboxMonitor(
        session_factory,
        EventBus(),
        interval_s=60,
        interval_for=interval,
        monotonic=lambda: clock[0],
    )
    clock[0] = 120.0
    with caplog.at_level(logging.ERROR):
        await monitor.poll_due()
    assert polled == [[first]]
    assert f"interval of user {second}" in caplog.text


async def test_a_single_user_still_polls_every_interval(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_scope(session_factory, write=True) as session:
        user_id = factories.make_user(session).id
    polled: list[Any] = []

    def fake_poll(factory: Any, **kw: Any) -> list[Any]:
        polled.append(kw["user_ids"])
        return []

    monkeypatch.setattr(mailboxes, "poll_mailboxes", fake_poll)
    monitor = mailboxes.MailboxMonitor(session_factory, EventBus(), interval_s=0.01)
    monitor.start()
    for _ in range(200):
        if polled:
            break
        await asyncio.sleep(0.01)
    await monitor.stop()
    assert polled and polled[0] == [user_id]


# --- reply poll -------------------------------------------------------------------


def _sender(factory: sessionmaker[Session], per_user: Any) -> GmailSender:
    def no_gmail(user_id: int, mailbox_id: int) -> Any:
        raise AssertionError("no Gmail in this test")

    return GmailSender(factory, opener=no_gmail, replies_every_for=per_user)


def test_each_user_gets_their_own_reply_interval(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _two_users(session_factory)  # 3 and 30 minutes
    polled: list[int] = []

    def fake(factory: Any, user_id: int, **kwargs: Any) -> Any:
        polled.append(user_id)
        return campaign_replies.RepliesPolled()

    monkeypatch.setattr(campaign_replies, "poll_replies", fake)
    file = Settings()

    def minutes(user_id: int) -> timedelta:
        with session_scope(session_factory) as session:
            user = session.get(User, user_id)
            assert user is not None
            return timedelta(minutes=ui.resolve(session, user, file).campaigns.reply_poll_minutes)

    sender = _sender(session_factory, minutes)
    assert sender.replies_every_of(first) == timedelta(minutes=3)
    assert sender.replies_every_of(second) == timedelta(minutes=30)
    for user_id in (first, second):
        sender._poll_replies(session_factory, user_id, NOW)  # the first tick polls
    assert polled == [first, second]
    polled.clear()
    for user_id in (first, second):
        sender._poll_replies(session_factory, user_id, NOW + timedelta(minutes=4))
    assert polled == [first]  # only the 3-minute user's interval has passed


def test_an_unreadable_reply_interval_polls_nothing_for_that_user(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first, second = _two_users(session_factory)
    polled: list[int] = []

    def fake_poll(factory: Any, user_id: int, **kw: Any) -> Any:
        polled.append(user_id)
        return campaign_replies.RepliesPolled()

    monkeypatch.setattr(campaign_replies, "poll_replies", fake_poll)

    def minutes(user_id: int) -> timedelta:
        if user_id == second:
            raise RuntimeError("boom")
        return timedelta(minutes=5)

    sender = _sender(session_factory, minutes)
    with caplog.at_level(logging.ERROR):
        for user_id in (first, second):
            sender._poll_replies(session_factory, user_id, NOW)
    assert polled == [first]
    assert f"interval of user {second}" in caplog.text
