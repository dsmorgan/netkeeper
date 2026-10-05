"""When each background check last ran and runs next (#401).

The service reads what the checks already store; the endpoint never starts one. The
two-user isolation of ``GET /poll-status`` is in ``tests/isolation``.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.models import (
    Mailbox,
    MailboxStatus,
    MessageStatus,
    SettingKV,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import poll_status as service
from netkeeper.services import runs
from netkeeper.services.campaign_sender import DRAFTS_POLL_EVERY, GmailSender
from netkeeper.services.linkedin_accounts import (
    arm_scheduled_runs,
    ensure_account,
    pause_schedule,
)
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.scheduler import (
    DEFAULT_SCHEDULES,
    JobKind,
    establish_schedule,
    stored_due,
)

#: 11:00 in New York, inside the default LinkedIn active hours (08:30 to 21:30).
NOW = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
#: 02:00 in New York, outside them.
NIGHT = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)
SERVING = service.Serving(scheduler=True, campaign_engine=True)
STOPPED = service.Serving()

S = service.CheckState


def test_the_intervals_are_pinned() -> None:
    """The issue's table: 10 min for both Gmail polls, 3 h, 1 day, 7 days, 3 h for LinkedIn."""
    assert timedelta(minutes=10) == DRAFTS_POLL_EVERY
    assert Settings().campaigns.reply_poll_minutes == 10
    assert {kind: s.interval for kind, s in DEFAULT_SCHEDULES.items()} == {
        JobKind.ENRICH: timedelta(hours=3),
        JobKind.INBOX: timedelta(hours=3),
        JobKind.CONNECTIONS_INCREMENTAL: timedelta(days=1),
        JobKind.CONNECTIONS_FULL: timedelta(days=7),
    }


# --- helpers ----------------------------------------------------------------------------


@dataclass
class World:
    factory: sessionmaker[Session]
    user_id: int

    def read(
        self, *, now: datetime = NOW, serving: service.Serving = SERVING
    ) -> dict[str, service.Check]:
        status = self.status(now=now, serving=serving)
        return {check.key: check for check in status.checks}

    def status(
        self, *, now: datetime = NOW, serving: service.Serving = SERVING
    ) -> service.PollStatus:
        with session_scope(self.factory) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            return service.poll_status(session, user, now=now, settings=Settings(), serving=serving)

    def write(self) -> Iterator[tuple[Session, User]]:
        with session_scope(self.factory, write=True) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            yield session, user


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> World:
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session, timezone="America/New_York")
        user_id = user.id
    return World(session_factory, user_id)


def _mailbox(
    session: Session,
    user: User,
    *,
    armed: bool = True,
    polled: datetime | None = None,
    status: MailboxStatus = MailboxStatus.OK,
    email: str = "me@example.test",
) -> Mailbox:
    mailbox = Mailbox(
        user_id=user.id,
        email=email,
        keychain_ref=f"gmail-{email}",
        daily_cap=50,
        status=status,
        armed_at=NOW - timedelta(days=1) if armed else None,
        replies_polled_at=polled,
    )
    session.add(mailbox)
    session.flush()
    return mailbox


def _arm_linkedin(session: Session, user: User, *, kinds: tuple[JobKind, ...] = ()) -> int:
    account = ensure_account(session, user)
    arm_scheduled_runs(session, user, now=NOW - timedelta(days=1))
    for kind in kinds:
        establish_schedule(
            session,
            user,
            account.id,
            kind,
            now=NOW - timedelta(hours=1),
            schedule=DEFAULT_SCHEDULES[kind],
            rng=random.Random(0),
            tz="America/New_York",
        )
    return account.id


def _no_times(checks: dict[str, service.Check]) -> None:
    """A check that cannot run says why and gives no next time."""
    for check in checks.values():
        if check.state is not S.SCHEDULED:
            assert check.next_at is None, check


# --- Gmail ------------------------------------------------------------------------------


def test_without_serve_nothing_is_running_and_the_inbox_is_not_wired(world: World) -> None:
    for session, user in world.write():
        _mailbox(session, user, polled=NOW - timedelta(minutes=3))
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))

    status = world.status(serving=STOPPED)
    checks = {check.key: check for check in status.checks}

    assert not status.background_running
    assert [c.key for c in status.checks] == [
        "gmail_replies",
        "gmail_drafts",
        "linkedin_inbox",
        "linkedin_enrich",
        "linkedin_incremental_sync",
        "linkedin_full_sync",
    ]
    assert checks["gmail_replies"].state is S.NOT_RUNNING
    assert checks["gmail_replies"].last_at == NOW - timedelta(minutes=3)  # history still holds
    assert checks["linkedin_enrich"].state is S.NOT_RUNNING
    assert checks["linkedin_inbox"].state is S.NOT_WIRED
    assert checks["linkedin_inbox"].reason == service.INBOX_NOT_WIRED
    assert status.mailboxes[0].state is S.NOT_RUNNING
    _no_times(checks)


def test_the_inbox_is_not_wired_even_while_everything_else_runs(world: World) -> None:
    for session, user in world.write():
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))

    inbox = world.read()["linkedin_inbox"]

    assert inbox.state is S.NOT_WIRED
    assert inbox.next_at is None


def test_gmail_not_connected(world: World) -> None:
    checks = world.read()
    assert checks["gmail_replies"].state is S.OFF
    assert checks["gmail_replies"].reason == "Gmail isn't connected"
    assert checks["gmail_drafts"].state is S.OFF


def test_a_disconnected_mailbox_is_not_connected(world: World) -> None:
    for session, user in world.write():
        _mailbox(session, user, armed=False, status=MailboxStatus.DISABLED)

    status = world.status()

    assert status.checks[0].reason == "Gmail isn't connected"
    assert status.mailboxes[0].state is S.OFF
    assert status.mailboxes[0].reason == "me@example.test is disconnected"


def test_a_connected_mailbox_nobody_armed_is_off(world: World) -> None:
    for session, user in world.write():
        _mailbox(session, user, armed=False)

    status = world.status()

    assert status.checks[0].state is S.OFF
    assert status.checks[0].reason == "No mailbox is armed, so netkeeper doesn't read Gmail"
    assert status.mailboxes[0].state is S.OFF
    assert not status.mailboxes[0].armed


def test_replies_next_poll_is_the_last_plus_the_interval(world: World) -> None:
    polled = NOW - timedelta(minutes=3)
    for session, user in world.write():
        _mailbox(session, user, polled=polled)

    status = world.status()
    replies = status.checks[0]

    assert replies.state is S.SCHEDULED
    assert replies.last_at == polled
    assert replies.next_at == polled + timedelta(minutes=10)
    assert replies.reason is None
    assert status.mailboxes[0].next_at == polled + timedelta(minutes=10)


@pytest.mark.parametrize(
    "polled", [None, NOW - timedelta(minutes=10), NOW - timedelta(hours=2)], ids=str
)
def test_replies_never_polled_or_overdue_are_due(world: World, polled: datetime | None) -> None:
    """A poll that stopped part way leaves the time old, and the tick polls each minute."""
    for session, user in world.write():
        _mailbox(session, user, polled=polled)

    replies = world.read()["gmail_replies"]

    assert replies.state is S.DUE
    assert replies.next_at is None
    assert replies.last_at == polled


def test_replies_read_the_oldest_armed_mailbox(world: World) -> None:
    for session, user in world.write():
        _mailbox(session, user, polled=NOW - timedelta(minutes=2), email="a@example.test")
        _mailbox(session, user, polled=NOW - timedelta(minutes=6), email="b@example.test")
        _mailbox(session, user, armed=False, polled=NOW - timedelta(days=9), email="c@x.test")

    replies = world.read()["gmail_replies"]

    assert replies.last_at == NOW - timedelta(minutes=6)
    assert replies.next_at == NOW + timedelta(minutes=4)


def test_replies_blocked_when_every_armed_mailbox_needs_signing_in(world: World) -> None:
    for session, user in world.write():
        _mailbox(session, user, polled=NOW, status=MailboxStatus.REAUTH_REQUIRED)

    status = world.status()

    assert status.checks[0].state is S.BLOCKED
    assert status.checks[0].next_at is None
    assert status.mailboxes[0].state is S.BLOCKED


def _drafted(session: Session, user: User, mailbox: Mailbox) -> None:
    campaign = factories.make_campaign(session, user, mailbox_id=mailbox.id)
    enrollment = factories.make_enrollment(session, campaign, factories.make_contact(session, user))
    factories.make_message(
        session, enrollment, status=MessageStatus.DRAFTED, gmail_draft_id="d1", sent_at=None
    )


def test_drafts_idle_with_no_draft_waiting(world: World) -> None:
    for session, user in world.write():
        _mailbox(session, user, polled=NOW)

    drafts = world.read()["gmail_drafts"]

    assert drafts.state is S.IDLE
    assert drafts.reason == "No campaign draft is waiting to be sent"


def test_drafts_read_the_running_senders_last_poll(world: World) -> None:
    for session, user in world.write():
        _drafted(session, user, _mailbox(session, user, polled=NOW))

    never = world.read()["gmail_drafts"]
    polled = NOW - timedelta(minutes=4)
    serving = service.Serving(scheduler=True, campaign_engine=True, drafts_polled_at=polled)
    recent = world.read(serving=serving)["gmail_drafts"]

    assert never.state is S.DUE
    assert recent.state is S.SCHEDULED
    assert recent.last_at == polled
    assert recent.next_at == polled + timedelta(minutes=10)


def test_drafts_on_a_disarmed_mailbox_are_not_polled(world: World) -> None:
    for session, user in world.write():
        _drafted(session, user, _mailbox(session, user, armed=False))

    assert world.read()["gmail_drafts"].state is S.OFF


# --- LinkedIn ---------------------------------------------------------------------------


def test_linkedin_disarmed(world: World) -> None:
    checks = world.read()
    for key in ("linkedin_enrich", "linkedin_incremental_sync", "linkedin_full_sync"):
        assert checks[key].state is S.OFF
        assert checks[key].reason == "Scheduled LinkedIn runs are disarmed"
    _no_times(checks)


def test_linkedin_next_is_the_stored_due_time(world: World) -> None:
    for session, user in world.write():
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))
        account = ensure_account(session, user)
        due = stored_due(session, user, account.id, JobKind.ENRICH)

    checks = world.read()

    assert due is not None and due > NOW
    assert checks["linkedin_enrich"].state is S.SCHEDULED
    assert checks["linkedin_enrich"].next_at == due
    # Never scheduled: no due time stored, so none is made up.
    assert checks["linkedin_full_sync"].state is S.IDLE
    assert checks["linkedin_full_sync"].next_at is None


def test_linkedin_overdue_is_due(world: World) -> None:
    for session, user in world.write():
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))

    later = NOW + timedelta(days=1, hours=1)  # past the stored due, inside active hours
    assert world.read(now=later)["linkedin_enrich"].state is S.DUE


def test_linkedin_paused_says_so(world: World) -> None:
    for session, user in world.write():
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))
        pause_schedule(session, user, now=NOW)

    enrich = world.read()["linkedin_enrich"]

    assert enrich.state is S.PAUSED
    assert enrich.next_at is None
    assert enrich.reason == "The LinkedIn schedule is paused"


def test_linkedin_flagged_session_blocks(world: World) -> None:
    for session, user in world.write():
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))
        flag_session(session, user, Outcome.CHECKPOINT, url="/checkpoint/challenge")

    enrich = world.read()["linkedin_enrich"]

    assert enrich.state is S.BLOCKED
    assert enrich.next_at is None
    assert "flagged" in (enrich.reason or "")


def test_linkedin_outside_active_hours_says_so(world: World) -> None:
    for session, user in world.write():
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))

    enrich = world.read(now=NIGHT)["linkedin_enrich"]

    assert enrich.state is S.OUTSIDE_HOURS
    assert enrich.next_at is None
    assert enrich.reason


def test_linkedin_last_is_the_newest_completed_run(world: World) -> None:
    for session, user in world.write():
        _arm_linkedin(session, user, kinds=(JobKind.ENRICH,))
        done = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.SCHEDULED, now=NOW
        )
        runs.finish_run(
            session, user, done.id, status=SyncRunStatus.COMPLETED, now=NOW - timedelta(hours=2)
        )
        failed = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        runs.finish_run(session, user, failed.id, status=SyncRunStatus.FAILED, now=NOW)

    assert world.read()["linkedin_enrich"].last_at == NOW - timedelta(hours=2)


# --- the endpoint -----------------------------------------------------------------------


async def test_endpoint_answers_without_serve(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/poll-status")

    assert response.status_code == 200
    body = response.json()
    assert body["background_running"] is False
    by_key = {item["key"]: item for item in body["items"]}
    assert by_key["gmail_replies"]["state"] == "off"
    assert by_key["gmail_replies"]["interval_minutes"] == 10
    assert by_key["gmail_drafts"]["interval_minutes"] == 10
    assert by_key["linkedin_inbox"]["state"] == "not_wired"
    assert by_key["linkedin_enrich"]["state"] == "not_running"
    assert by_key["linkedin_enrich"]["interval_minutes"] == 180
    assert by_key["linkedin_incremental_sync"]["interval_minutes"] == 1440
    assert by_key["linkedin_full_sync"]["interval_minutes"] == 10080
    assert all(item["next_at"] is None for item in body["items"])
    assert body["mailboxes"] == []


class _Engine:
    """Stands in for ``serve``'s campaign engine: the endpoint only reads its sender."""

    def __init__(self, sender: GmailSender) -> None:
        self.sender = sender


def _no_gmail(user_id: int, mailbox_id: int) -> object:
    raise AssertionError("the poll status opened Gmail")


async def test_endpoint_reads_the_running_sender_and_never_runs_a_check(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    factory = running_app.state.session_factory
    polled = datetime.now(UTC) - timedelta(minutes=3)
    with session_scope(factory, write=True) as session:
        user = session.scalars(select(User)).one()
        user_id = user.id
        mailbox = _mailbox(session, user, polled=polled)
        mailbox_id = mailbox.id
        _drafted(session, user, mailbox)
        settings_rows = session.scalar(
            scoped(user, SettingKV).with_only_columns(func.count(SettingKV.id))
        )
    sender = GmailSender(factory, opener=_no_gmail)  # type: ignore[arg-type]
    drafted_at = datetime.now(UTC) - timedelta(minutes=1)
    sender._drafts_polled[user_id] = drafted_at  # what a drafts poll leaves behind
    running_app.state.campaign_engine = _Engine(sender)

    response = await client.get("/api/v1/poll-status")

    assert response.status_code == 200
    body = response.json()
    by_key = {item["key"]: item for item in body["items"]}
    assert by_key["gmail_replies"]["state"] == "scheduled"
    assert by_key["gmail_drafts"]["state"] == "scheduled"
    assert datetime.fromisoformat(by_key["gmail_drafts"]["last_at"]) == drafted_at
    assert body["mailboxes"][0]["mailbox_id"] == mailbox_id
    assert body["mailboxes"][0]["state"] == "scheduled"
    # Nothing moved: the sender's memory, the mailbox, and the settings are as they were.
    assert sender.drafts_polled_at(user_id) == drafted_at
    with session_scope(factory) as session:
        again = session.get(User, user_id)
        assert again is not None
        row = get_scoped(session, again, Mailbox, mailbox_id)
        assert row is not None and row.replies_polled_at == polled
        counted = scoped(again, SettingKV).with_only_columns(func.count(SettingKV.id))
        assert session.scalar(counted) == settings_rows
