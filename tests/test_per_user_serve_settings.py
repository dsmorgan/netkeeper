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
        result: tuple[time, time] = real(user)
        return result

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


# --- reply poll guards that read the interval ---------------------------------------


def _mailbox_polled(
    factory: sessionmaker[Session], user_id: int, ago: timedelta, clock_now: datetime
) -> int:
    from netkeeper.models import Mailbox

    with session_scope(factory, write=True) as session:
        mailbox = Mailbox(
            user_id=user_id,
            email=f"m{user_id}@example.test",
            keychain_ref=f"gmail-{user_id}",
            daily_cap=50,
            replies_polled_at=clock_now - ago,
        )
        session.add(mailbox)
        session.flush()
        return mailbox.id


def _follow_up(user_id: int, mailbox_id: int) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        user_id=user_id, mailbox_id=mailbox_id, step_position=2, same_thread=False, thread_id=None
    )


def _clocked_sender(factory: sessionmaker[Session], per_user: Any) -> GmailSender:
    def no_gmail(user_id: int, mailbox_id: int) -> Any:
        raise AssertionError("no Gmail in this test")

    return GmailSender(factory, opener=no_gmail, replies_every_for=per_user, clock=lambda: NOW)


def _minutes_of(first: int, second: int) -> Any:
    return lambda uid: timedelta(minutes=3 if uid == first else 30)


def test_the_stale_replies_hold_uses_each_users_own_interval(
    session_factory: sessionmaker[Session],
) -> None:
    first, second = _two_users(session_factory)  # 3 and 30 minutes
    polled_ten_minutes_ago = timedelta(minutes=10)
    box_first = _mailbox_polled(session_factory, first, polled_ten_minutes_ago, NOW)
    box_second = _mailbox_polled(session_factory, second, polled_ten_minutes_ago, NOW)
    sender = _clocked_sender(session_factory, _minutes_of(first, second))
    # Held for the 3-minute user (STALE_AFTER_POLLS x 3 minutes is under 10), allowed for
    # the 30-minute one.
    assert sender._replies_stale(_follow_up(first, box_first)) is not None
    assert sender._replies_stale(_follow_up(second, box_second)) is None


def test_an_unreadable_interval_holds_the_follow_up_and_marks_the_mailbox_due(
    session_factory: sessionmaker[Session],
) -> None:
    first, _ = _two_users(session_factory)
    box = _mailbox_polled(session_factory, first, timedelta(seconds=1), NOW)  # fresh poll

    def unreadable(user_id: int) -> timedelta:
        raise RuntimeError("boom")

    sender = _clocked_sender(session_factory, unreadable)
    assert sender._replies_stale(_follow_up(first, box)) is not None
    assert sender.replies_due(first) == frozenset({box})


def test_the_failure_backoff_cap_follows_each_users_interval(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = _two_users(session_factory)  # 3 and 30 minutes
    sender = _clocked_sender(session_factory, _minutes_of(first, second))

    def failing(factory: Any, user_id: int, **kwargs: Any) -> Any:
        return campaign_replies.RepliesPolled(failed=frozenset({7}))

    monkeypatch.setattr(campaign_replies, "poll_replies", failing)
    for user_id in (first, second):
        # Each failed poll backs off 1, 2, 4, ... minutes, never past the user's interval.
        for tick in range(8):
            sender._replies_polled.pop(user_id, None)  # the interval has passed
            sender._poll_replies(session_factory, user_id, NOW + timedelta(hours=tick))
    last = NOW + timedelta(hours=7)
    assert sender._retry_at(first, 7) == last + timedelta(minutes=3)
    assert sender._retry_at(second, 7) == last + timedelta(minutes=30)


# --- a schedule that could not be established at startup ----------------------------


async def test_an_unreadable_window_at_startup_fires_nothing_until_the_schedule_is_established(
    session_factory: sessionmaker[Session],
) -> None:
    """The stored due times are from an earlier serve and have had no catch-up. Unreadable
    hours at boot must not let them fire at the first heartbeat; once the hours read, the
    schedule is established with its catch-up jitter, and still nothing fires."""
    from random import Random

    from netkeeper.services.linkedin_accounts import ensure_account

    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        account = ensure_account(session, user).id
        session.expunge(user)
    accounts = [(user, account)]
    kinds = scheduler.JobKind
    hours = [lambda u: (time(8, 30), time(21, 30))]
    clock = [NOW]
    fired: list[Any] = []

    async def handler(ctx: Any) -> None:
        fired.append(ctx.kind)

    def build() -> Any:
        return scheduler.build_scheduler(
            session_factory,
            lambda: accounts,
            registry={kind: handler for kind in kinds},
            armed=scheduler.ARMING_NOT_REQUIRED,
            active_hours=lambda u: hours[0](u),
            rng=Random(1),
            clock=lambda: clock[0],
        )

    def dues() -> dict[Any, datetime | None]:
        with session_scope(session_factory) as session:
            owner = session.get(User, user.id)
            assert owner is not None
            return {k: scheduler.stored_due(session, owner, account, k) for k in kinds}

    build()  # the earlier serve established the schedule
    before = {k: d for k, d in dues().items() if d is not None}
    assert before
    first_due = min(before.values())
    clock[0] = first_due + timedelta(minutes=1)  # just past a due time, within its interval

    def unreadable(_: User) -> tuple[time, time]:
        raise RuntimeError("database is locked")

    hours[0] = unreadable
    heartbeat = build().get_job(scheduler.HEARTBEAT_JOB_ID).func  # the restart
    await heartbeat()
    assert fired == []
    assert {k: d for k, d in dues().items() if d is not None} == before

    hours[0] = lambda u: (time(8, 30), time(21, 30))
    await heartbeat()  # the hours read now: established, with catch-up, so nothing is due
    assert fired == []
    after = {k: d for k, d in dues().items() if d is not None}
    assert after != before
    assert all(d > clock[0] for d in after.values())


class _Restart:
    """Users with a schedule an earlier serve established, then a restart just past a due
    time, with a provider the test controls."""

    def __init__(self, factory: sessionmaker[Session], count: int) -> None:
        from random import Random

        from netkeeper.services.linkedin_accounts import ensure_account

        self.factory = factory
        self.accounts: list[tuple[User, int]] = []
        for _ in range(count):
            with session_scope(factory, write=True) as session:
                user = factories.make_user(session)
                account = ensure_account(session, user).id
                session.expunge(user)
            self.accounts.append((user, account))
        self.clock = NOW
        self.fired: list[Any] = []
        self.rng = Random(1)
        self.provider: Any = lambda u: (time(8, 30), time(21, 30))
        self._build()  # the earlier serve
        self.before = self.dues()
        first_due = min(d for per in self.before.values() for d in per.values() if d is not None)
        self.clock = first_due + timedelta(minutes=1)

    async def _handler(self, ctx: Any) -> None:
        self.fired.append((ctx.user_id, ctx.kind))

    def _build(self) -> Any:
        return scheduler.build_scheduler(
            self.factory,
            lambda: self.accounts,
            registry={kind: self._handler for kind in scheduler.JobKind},
            armed=scheduler.ARMING_NOT_REQUIRED,
            active_hours=lambda u: self.provider(u),
            rng=self.rng,
            clock=lambda: self.clock,
        )

    def restart(self) -> Any:
        return self._build().get_job(scheduler.HEARTBEAT_JOB_ID).func

    def dues(self) -> dict[int, dict[Any, datetime | None]]:
        out: dict[int, dict[Any, datetime | None]] = {}
        with session_scope(self.factory) as session:
            for user, account in self.accounts:
                owner = session.get(User, user.id)
                assert owner is not None
                out[user.id] = {
                    k: scheduler.stored_due(session, owner, account, k) for k in scheduler.JobKind
                }
        return out


async def test_the_guard_holds_when_only_the_retry_read_fails(
    session_factory: sessionmaker[Session],
) -> None:
    """The heartbeat's own read of the hours succeeds, but the schedule is still
    unestablished: the retry's read failed. Without the guard, poll_once would fire the
    stale due times."""
    run = _Restart(session_factory, 1)
    fail_next = [True]
    real = run.provider

    def first_call_fails(user: User) -> tuple[time, time]:
        if fail_next[0]:
            fail_next[0] = False
            raise RuntimeError("database is locked")
        window: tuple[time, time] = real(user)
        return window

    run.provider = first_call_fails
    heartbeat = run.restart()  # the startup read consumes the first failure
    fail_next[0] = True  # the heartbeat's first read (the retry) fails; its second works
    await heartbeat()
    assert run.fired == []
    assert run.dues() == run.before


async def test_a_failing_retry_skips_only_that_user(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Restart(session_factory, 2)
    bad, good = run.accounts[0][0].id, run.accounts[1][0].id
    real_sync = scheduler.sync_account_schedule
    readable = run.provider
    run.provider = lambda u: (_ for _ in ()).throw(RuntimeError("locked"))
    heartbeat = run.restart()  # both unestablished
    run.provider = readable

    established: list[int] = []

    def sync(session: Session, user: User, *args: Any, **kwargs: Any) -> Any:
        if user.id == bad:
            raise RuntimeError("write failed")
        established.append(user.id)
        return real_sync(session, user, *args, **kwargs)

    monkeypatch.setattr(scheduler, "sync_account_schedule", sync)
    await heartbeat()  # must not raise, and must establish the other user
    after = run.dues()
    assert established == [good]
    assert after[bad] == run.before[bad]
    assert not any(fired_user == bad for fired_user, _ in run.fired)
