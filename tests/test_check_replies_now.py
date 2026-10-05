"""Gmail "Check now" for replies (#409).

The request only sets the running sender's "poll on the next tick" flag: the poll
still runs in the campaign tick, gated as usual. The request handler never opens
Gmail, and every sender here fails the test if anything does.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from isolation.harness import acting_as
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.models import Mailbox, MailboxStatus, User
from netkeeper.scoping import scoped
from netkeeper.services import campaign_replies
from netkeeper.services import poll_status as service
from netkeeper.services.campaign_sender import GmailSender

URL = "/api/v1/poll-status/gmail-replies/check-now"
CSRF = {"X-Netkeeper-Client": "1"}
EVERY = timedelta(minutes=10)


def _no_gmail(user_id: int, mailbox_id: int) -> Any:
    raise AssertionError("Check now opened Gmail")


class _Engine:
    """Stands in for ``serve``'s campaign engine: the endpoint only reads its sender."""

    def __init__(self, sender: object) -> None:
        self.sender = sender


def _mailbox(
    session: Session,
    user: User,
    *,
    armed: bool = True,
    status: MailboxStatus = MailboxStatus.OK,
    email: str = "me@example.test",
) -> Mailbox:
    mailbox = Mailbox(
        user_id=user.id,
        email=email,
        keychain_ref=f"gmail-{email}",
        daily_cap=50,
        status=status,
        armed_at=datetime.now(UTC) - timedelta(days=1) if armed else None,
    )
    session.add(mailbox)
    session.flush()
    return mailbox


def _local_user(app: FastAPI, **mailbox: Any) -> int:
    with session_scope(app.state.session_factory, write=True) as session:
        user = session.scalars(select(User)).one()
        if mailbox.pop("none", False) is False:
            _mailbox(session, user, **mailbox)
        return user.id


def _serve(app: FastAPI) -> GmailSender:
    sender = GmailSender(app.state.session_factory, opener=_no_gmail, replies_every=EVERY)
    app.state.campaign_engine = _Engine(sender)
    return sender


def _replies(body: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in body["items"] if item["key"] == "gmail_replies")


# --- the endpoint -----------------------------------------------------------------------


async def test_check_now_sets_the_flag_and_never_opens_gmail(
    running_app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_poll(*_: Any, **__: Any) -> bool:
        raise AssertionError("Check now ran the reply poll")

    monkeypatch.setattr(campaign_replies, "poll_replies", no_poll)
    user_id = _local_user(running_app)
    sender = _serve(running_app)
    polled = datetime.now(UTC) - timedelta(minutes=2)
    sender._replies_polled[user_id] = polled  # what the last poll left behind

    before = _replies((await client.get("/api/v1/poll-status")).json())
    assert before["state"] == "scheduled" and before["requested"] is False

    first = await client.post(URL, headers=CSRF)
    second = await client.post(URL, headers=CSRF)

    assert first.status_code == 202, first.text
    assert first.json() == {"already_requested": False}
    assert second.json() == {"already_requested": True}  # one poll, not two
    assert sender.replies_poll_requested(user_id)
    assert sender.replies_polled_at(user_id) == polled  # the last check is still shown
    status = (await client.get("/api/v1/poll-status")).json()
    after = _replies(status)
    assert after["state"] == "due" and after["next_at"] is None
    assert after["requested"] is True
    asked = sender.replies_poll_requested_at(user_id)
    assert asked is not None  # the first press's time: the second did not move it
    assert datetime.fromisoformat(after["requested_at"]) == asked
    assert asked <= datetime.now(UTC)
    assert datetime.fromisoformat(after["last_at"]) == polled
    assert status["mailboxes"][0]["state"] == "due"
    assert all(item["requested"] is False for item in status["items"] if item is not after)


async def test_check_now_needs_the_csrf_header(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    user_id = _local_user(running_app)
    sender = _serve(running_app)

    response = await client.post(URL)

    assert response.status_code == 403
    assert not sender.replies_poll_requested(user_id)


async def test_check_now_refuses_without_serve(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    _local_user(running_app)

    response = await client.post(URL, headers=CSRF)

    assert response.status_code == 409
    assert response.json()["detail"] == service.NOT_SERVING


async def test_check_now_refuses_a_campaign_engine_without_gmail(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    _local_user(running_app)
    running_app.state.campaign_engine = _Engine(object())

    response = await client.post(URL, headers=CSRF)

    assert response.status_code == 409
    assert "doesn't send through Gmail" in response.json()["detail"]


@pytest.mark.parametrize(
    ("mailbox", "reason"),
    [
        ({"none": True}, "Gmail isn't connected"),
        ({"status": MailboxStatus.DISABLED, "armed": False}, "Gmail isn't connected"),
        ({"armed": False}, "No mailbox is armed, so netkeeper doesn't read Gmail"),
        (
            {"status": MailboxStatus.REAUTH_REQUIRED},
            "me@example.test needs you to sign in to Gmail again (Settings, Gmail)",
        ),
    ],
    ids=["not-connected", "disconnected", "disarmed", "needs-sign-in"],
)
async def test_check_now_refuses_with_the_reason(
    running_app: FastAPI, client: httpx.AsyncClient, mailbox: dict[str, Any], reason: str
) -> None:
    user_id = _local_user(running_app, **mailbox)
    sender = _serve(running_app)

    response = await client.post(URL, headers=CSRF)

    assert response.status_code == 409
    assert response.json()["detail"] == reason
    assert not sender.replies_poll_requested(user_id)


async def test_one_mailbox_needing_sign_in_does_not_refuse(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    user_id = _local_user(running_app)
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        _mailbox(session, user, status=MailboxStatus.REAUTH_REQUIRED, email="old@example.test")
    sender = _serve(running_app)

    response = await client.post(URL, headers=CSRF)

    assert response.status_code == 202
    assert sender.replies_poll_requested(user_id)


async def test_check_now_is_the_callers_own(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    """User B's press reads B's mailboxes and flags B alone; A's armed mailbox is A's."""
    a = _local_user(running_app)
    with session_scope(running_app.state.session_factory, write=True) as session:
        b = factories.make_user(session).id
    sender = _serve(running_app)

    with acting_as(running_app, b):
        refused = await client.post(URL, headers=CSRF)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Gmail isn't connected"

    with session_scope(running_app.state.session_factory, write=True) as session:
        user_b = session.get(User, b)
        assert user_b is not None
        _mailbox(session, user_b, email="b@example.test")
    with acting_as(running_app, b):
        assert (await client.post(URL, headers=CSRF)).status_code == 202
        b_status = _replies((await client.get("/api/v1/poll-status")).json())
    a_status = _replies((await client.get("/api/v1/poll-status")).json())

    assert sender.replies_poll_requested(b)
    assert not sender.replies_poll_requested(a)
    assert b_status["requested"] is True
    assert a_status["requested"] is False


# --- the tick runs the poll -------------------------------------------------------------


class _Polls:
    """Records the reply polls the sender starts, in place of the real one: when, and
    which mailboxes (``None`` for every armed one). ``result`` is what each answers;
    ``during`` runs inside the poll, for a press that lands while it reads."""

    def __init__(self) -> None:
        self.calls: list[tuple[datetime, frozenset[int] | None]] = []
        self.skipped: list[frozenset[int]] = []
        self.result = campaign_replies.RepliesPolled()
        self.during: Callable[[int], object] | None = None
        self.answer: Callable[[frozenset[int]], campaign_replies.RepliesPolled] | None = None
        """With ``armed`` set, what each poll answers for the mailboxes it read."""
        self.armed: frozenset[int] = frozenset()

    def __call__(
        self,
        factory: Any,
        user_id: int,
        *,
        now: datetime,
        only: Collection[int] | None = None,
        skip: Collection[int] = (),
        **_: Any,
    ) -> campaign_replies.RepliesPolled:
        self.calls.append((now, None if only is None else frozenset(only)))
        self.skipped.append(frozenset(skip))
        if self.during is not None:
            self.during(user_id)
        if self.answer is not None:
            read = (self.armed if only is None else frozenset(only)) - frozenset(skip)
            return self.answer(read)
        return self.result

    def last(self) -> tuple[datetime, frozenset[int] | None]:
        """The latest poll; a call, so mypy does not narrow it between asserts."""
        return self.calls[-1]

    @property
    def times(self) -> list[datetime]:
        return [at for at, _ in self.calls]


@pytest.fixture
def polls(monkeypatch: pytest.MonkeyPatch) -> _Polls:
    counter = _Polls()
    monkeypatch.setattr(campaign_replies, "poll_replies", counter)
    return counter


T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
M = timedelta(minutes=1)


def _reconcile(
    factory: sessionmaker[Session], user_id: int
) -> tuple[GmailSender, Callable[[datetime], None]]:
    sender = GmailSender(factory, opener=_no_gmail, replies_every=EVERY)

    def tick(now: datetime) -> None:
        sender.reconcile(factory, user_id, settings=Settings(), now=now)

    return sender, tick


def _user(factory: sessionmaker[Session]) -> int:
    with session_scope(factory, write=True) as session:
        return factories.make_user(session).id


def test_the_next_tick_polls_once_however_many_presses(
    session_factory: sessionmaker[Session], polls: _Polls
) -> None:
    user_id = _user(session_factory)
    sender, tick = _reconcile(session_factory, user_id)
    tick(T0)
    assert polls.times == [T0]
    tick(T0 + M)
    assert polls.times == [T0]  # inside the interval: no poll

    assert sender.request_replies_poll(user_id) is True
    asked = sender.replies_poll_requested_at(user_id)
    assert sender.request_replies_poll(user_id) is False
    assert sender.replies_poll_requested_at(user_id) == asked  # the first press's time
    assert sender.request_replies_poll(user_id) is False
    assert polls.times == [T0]  # the press itself polls nothing

    tick(T0 + 2 * M)
    assert polls.last() == (T0 + 2 * M, None)  # a full poll: every armed mailbox
    assert not sender.replies_poll_requested(user_id)
    assert sender.replies_poll_requested_at(user_id) is None
    assert sender.replies_polled_at(user_id) == T0 + 2 * M

    tick(T0 + 3 * M)
    tick(T0 + 11 * M)
    # Back on the interval, measured from the requested poll.
    assert polls.times == [T0, T0 + 2 * M]
    tick(T0 + 12 * M)
    assert polls.times[-1] == T0 + 12 * M


def test_check_now_in_a_due_only_interval_polls_every_mailbox_and_clears_the_due_set(
    session_factory: sessionmaker[Session], polls: _Polls
) -> None:
    """Between full polls only the due mailboxes are read (#413). A press makes the next
    tick a full poll instead, and the due set starts over from what that poll found."""
    user_id = _user(session_factory)
    sender, tick = _reconcile(session_factory, user_id)
    polls.result = campaign_replies.RepliesPolled(caught_up=frozenset({1}), behind=frozenset({2}))
    tick(T0)
    polls.result = campaign_replies.RepliesPolled(behind=frozenset({2}))
    tick(T0 + M)
    assert polls.last() == (T0 + M, frozenset({2}))  # due alone
    assert sender.replies_due(user_id) == {2}

    sender.request_replies_poll(user_id)
    polls.result = campaign_replies.RepliesPolled(caught_up=frozenset({1, 2}))
    tick(T0 + 2 * M)

    assert polls.last() == (T0 + 2 * M, None)
    assert sender.replies_due(user_id) == frozenset()
    assert sender.replies_polled_at(user_id) == T0 + 2 * M
    tick(T0 + 3 * M)
    assert len(polls.calls) == 3  # nothing due, inside the interval


def _healthy_and_failing(read: frozenset[int]) -> campaign_replies.RepliesPolled:
    """Mailbox 1 always reads; mailbox 2's poll always fails."""
    return campaign_replies.RepliesPolled(caught_up=read & {1}, failed=read & {2})


def test_check_now_follows_a_failing_mailboxs_backoff(
    session_factory: sessionmaker[Session], polls: _Polls
) -> None:
    """A press every minute reads the healthy mailbox every time, and the failing one
    only at its retry times: 1, 2, then 4 minutes apart (doubling, never past the
    interval)."""
    user_id = _user(session_factory)
    sender, tick = _reconcile(session_factory, user_id)
    polls.armed = frozenset({1, 2})
    polls.answer = _healthy_and_failing
    reads: dict[int, list[int]] = {1: [], 2: []}
    for minute in range(10):
        if minute:
            assert sender.request_replies_poll(user_id)
        tick(T0 + minute * M)
        at, only = polls.last()
        assert at == T0 + minute * M and only is None  # every press is a full poll...
        for mailbox_id in (1, 2):
            if mailbox_id not in polls.skipped[-1]:  # ...less what is backing off
                reads[mailbox_id].append(minute)

    assert reads[1] == list(range(10))
    assert reads[2] == [0, 1, 3, 7]
    assert sender.replies_due(user_id) == {2}  # waiting for its retry time, still due
    assert sender.replies_retry_at(user_id) == {2: T0 + 15 * M}  # 7 + 8


def test_the_scheduled_full_poll_still_ignores_a_backoff(
    session_factory: sessionmaker[Session], polls: _Polls
) -> None:
    """#420's full poll each interval reads every armed mailbox, backoff or not."""
    user_id = _user(session_factory)
    sender, tick = _reconcile(session_factory, user_id)
    polls.armed = frozenset({1, 2})
    polls.answer = _healthy_and_failing
    for minute in (0, 1, 3, 7):  # its retries; the last backs off 8 min, to T0 + 15
        tick(T0 + minute * M)
    assert sender.replies_retry_at(user_id) == {2: T0 + 15 * M}

    tick(T0 + 10 * M)  # the interval from the last full poll, at T0

    assert polls.last() == (T0 + 10 * M, None)
    assert polls.skipped[-1] == frozenset()


def _backing_off_status(
    factory: sessionmaker[Session], *, now: datetime, full_at: datetime, retry_at: datetime
) -> dict[int, service.MailboxPoll]:
    """Two ready mailboxes, one due again after a failed poll and backing off until
    ``retry_at``; the last full poll at ``full_at``; a "Check now" waiting."""
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        ok = _mailbox(session, user, email="ok@example.test").id
        failing = _mailbox(session, user, email="failing@example.test").id
        serving = service.Serving(
            campaign_engine=True,
            replies_polled_at=full_at,
            replies_every=EVERY,
            replies_due=frozenset({failing}),
            replies_retry_at={failing: retry_at},
            replies_requested_at=now,
        )
        status = service.poll_status(session, user, now=now, settings=Settings(), serving=serving)
    polls = {poll.mailbox_id: poll for poll in status.mailboxes}
    return {0: polls[ok], 1: polls[failing]}


def test_poll_status_shows_a_backing_off_mailbox_waiting_for_its_retry(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    polls = _backing_off_status(
        session_factory,
        now=now,
        full_at=now - timedelta(minutes=2),
        retry_at=now + timedelta(minutes=3),
    )
    assert polls[0].state is service.CheckState.DUE  # the press reads it at once
    assert polls[1].state is service.CheckState.SCHEDULED
    assert polls[1].next_at == now + timedelta(minutes=3)


def test_a_backing_off_mailbox_is_read_at_the_full_poll_if_that_comes_first(
    session_factory: sessionmaker[Session],
) -> None:
    """It failed at T0 + 7 and backs off to T0 + 15, but the full poll at T0 + 10 (the
    interval from T0) reads it anyway: at T0 + 8 its next read is T0 + 10."""
    polls = _backing_off_status(session_factory, now=T0 + 8 * M, full_at=T0, retry_at=T0 + 15 * M)
    assert polls[1].state is service.CheckState.SCHEDULED
    assert polls[1].next_at == T0 + 10 * M


def test_a_backing_off_mailbox_past_its_retry_is_due(
    session_factory: sessionmaker[Session],
) -> None:
    polls = _backing_off_status(session_factory, now=T0 + 4 * M, full_at=T0, retry_at=T0 + 3 * M)
    assert polls[1].state is service.CheckState.DUE
    assert polls[1].next_at is None


async def test_the_endpoint_shows_a_backing_off_mailbox_at_its_retry(
    running_app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /poll-status`` reads the running sender's retry times (``_serving``)."""
    user_id = _local_user(running_app)
    sender = _serve(running_app)
    with session_scope(running_app.state.session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        mailbox_id = session.scalars(scoped(user, Mailbox)).one().id
    now = datetime.now(UTC)
    retry_at = now + timedelta(minutes=3)
    sender._replies_polled[user_id] = now - timedelta(minutes=1)
    sender._replies_due[user_id] = {mailbox_id}
    sender._replies_backoff[(user_id, mailbox_id)] = (2, retry_at)

    body = (await client.get("/api/v1/poll-status")).json()

    [poll] = body["mailboxes"]
    assert poll["state"] == "scheduled"
    assert datetime.fromisoformat(poll["next_at"]) == retry_at


def test_a_not_ready_mailbox_does_not_keep_the_request_alive(
    session_factory: sessionmaker[Session], polls: _Polls
) -> None:
    """The request ends with the poll it asked for. A mailbox that could not be opened
    goes back in the due set and is tried alone at the next tick, as #413 does it."""
    user_id = _user(session_factory)
    sender, tick = _reconcile(session_factory, user_id)
    tick(T0)
    sender.request_replies_poll(user_id)
    polls.result = campaign_replies.RepliesPolled(
        caught_up=frozenset({1}), not_ready={2: "keychain_unavailable"}
    )

    tick(T0 + M)

    assert not sender.replies_poll_requested(user_id)
    assert sender.replies_due(user_id) == {2}
    assert sender.replies_not_ready(user_id) == {2: "keychain_unavailable"}
    tick(T0 + 2 * M)
    assert polls.last() == (T0 + 2 * M, frozenset({2}))  # alone, not a full poll


@pytest.mark.parametrize(
    "short",
    [
        campaign_replies.RepliesPolled(caught_up=frozenset({1}), behind=frozenset({2})),
        campaign_replies.RepliesPolled(caught_up=frozenset({1}), failed=frozenset({2})),
    ],
    ids=["behind", "failed"],
)
def test_a_request_leaves_the_send_hold_alone(
    session_factory: sessionmaker[Session], polls: _Polls, short: campaign_replies.RepliesPolled
) -> None:
    """A mailbox seen not ready holds its new conversations until a poll of it catches
    up. A requested poll that stops short of that keeps the hold; one that catches up
    releases it, as any poll does."""
    user_id = _user(session_factory)
    sender, tick = _reconcile(session_factory, user_id)
    polls.result = campaign_replies.RepliesPolled(
        caught_up=frozenset({1}), not_ready={2: "reauth_required"}
    )
    tick(T0)
    assert sender._replies_catch_up[user_id] == {2}

    sender.request_replies_poll(user_id)
    assert sender._replies_catch_up[user_id] == {2}  # the press moves nothing
    polls.result = short
    tick(T0 + M)
    assert sender._replies_catch_up[user_id] == {2}  # not caught up: still held

    sender.request_replies_poll(user_id)
    polls.result = campaign_replies.RepliesPolled(caught_up=frozenset({1, 2}))
    tick(T0 + 5 * M)  # past the failed one's backoff too
    assert sender._replies_catch_up[user_id] == set()


def test_concurrent_presses_ask_once(
    session_factory: sessionmaker[Session],
) -> None:
    """Presses arrive on worker threads: exactly one of many at once asks."""
    from concurrent.futures import ThreadPoolExecutor

    def slow_clock() -> datetime:  # widens the gap between the check and the set
        time.sleep(0.005)
        return datetime.now(UTC)

    sender = GmailSender(session_factory, opener=_no_gmail, replies_every=EVERY, clock=slow_clock)
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: sender.request_replies_poll(7), range(64)))
    assert answers.count(True) == 1


def test_a_request_flags_only_its_own_user(
    session_factory: sessionmaker[Session], polls: _Polls
) -> None:
    a = _user(session_factory)
    b = _user(session_factory)
    sender = GmailSender(session_factory, opener=_no_gmail, replies_every=EVERY)
    for user_id in (a, b):
        sender.reconcile(session_factory, user_id, settings=Settings(), now=T0)

    sender.request_replies_poll(a)
    for user_id in (a, b):
        sender.reconcile(session_factory, user_id, settings=Settings(), now=T0 + M)

    assert sender.replies_polled_at(a) == T0 + M
    assert sender.replies_polled_at(b) == T0


def test_a_press_during_the_poll_asks_for_another(
    session_factory: sessionmaker[Session], polls: _Polls
) -> None:
    """The flag is cleared before any mailbox is polled: a press while the poll reads may
    have come after it read that mailbox, so the next tick polls again."""
    user_id = _user(session_factory)
    sender, tick = _reconcile(session_factory, user_id)
    tick(T0)
    sender.request_replies_poll(user_id)
    polls.during = sender.request_replies_poll  # pressed while the requested poll runs

    tick(T0 + M)

    assert sender.replies_poll_requested(user_id)
    polls.during = None
    tick(T0 + 2 * M)
    assert polls.calls == [(T0, None), (T0 + M, None), (T0 + 2 * M, None)]
    assert not sender.replies_poll_requested(user_id)


# --- what the poll status shows while a request waits ------------------------------------


def test_a_waiting_request_is_due_but_a_not_ready_mailbox_stays_blocked(
    session_factory: sessionmaker[Session],
) -> None:
    """The order in ``_mailbox_poll``: sign-in, then not ready, then due, then the
    request. A press never hides a mailbox netkeeper can't open."""
    now = datetime.now(UTC)
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        ready = _mailbox(session, user, email="ready@example.test").id
        locked = _mailbox(session, user, email="locked@example.test").id
        serving = service.Serving(
            campaign_engine=True,
            replies_polled_at=now - timedelta(minutes=2),
            replies_every=EVERY,
            replies_not_ready={locked: "keychain_unavailable"},
            replies_requested_at=now,
        )
        status = service.poll_status(session, user, now=now, settings=Settings(), serving=serving)

    states = {poll.mailbox_id: poll.state for poll in status.mailboxes}
    assert states == {ready: service.CheckState.DUE, locked: service.CheckState.BLOCKED}
    check = next(c for c in status.checks if c.key == service.GMAIL_REPLIES)
    assert check.state is service.CheckState.DUE
    assert check.requested_at == now
    assert check.reason is not None and "Keychain is locked" in check.reason
