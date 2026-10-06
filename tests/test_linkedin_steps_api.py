"""``/campaigns/linkedin`` (P4-09, #379): the person's side of a LinkedIn step.

The two lists' two-user isolation is in ``tests/isolation``. The claim's own checks
are in ``tests/test_linkedin_steps.py``; these tests pin what the API answers, that
a prefill is only ever submitted, never awaited, and that a refusal keeps what it
changed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import NOW
from fastapi import FastAPI
from inbox_fakes import FIXTURE_NOTE, FakeInboxSource, record_poll
from run_fakes import fake_provider
from sqlalchemy import select
from sqlalchemy.orm import Session

from netkeeper.db import session_scope
from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind
from netkeeper.models import (
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import budgets, runs
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import record_session_evidence
from netkeeper.services.linkedin_steps import record_prefill_outcome
from netkeeper.web.api import linkedin_steps as api
from netkeeper.web.security import CLIENT_HEADER, CLIENT_HEADER_VALUE
from netkeeper.worker import BrowserWorker

HEADERS = {CLIENT_HEADER: CLIENT_HEADER_VALUE}
BASE = "/api/v1/campaigns/linkedin"


class FakeExecutor:
    """Records what was submitted; never touches a browser."""

    def __init__(self) -> None:
        self.executed: list[tuple[int, int]] = []

    async def execute(self, run_id: int, user_id: int) -> runs.RunOutcome:
        self.executed.append((run_id, user_id))
        return runs.RunOutcome.DONE


@pytest.fixture
def executor(running_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> FakeExecutor:
    fake = FakeExecutor()
    running_app.state.executor = fake
    monkeypatch.setattr(api, "utcnow", lambda: NOW)
    return fake


@pytest.fixture
def with_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """``message_send`` has no runner until P4-03; these tests stand in for it."""
    monkeypatch.setattr(runs, "RUNNABLE_KINDS", runs.RUNNABLE_KINDS | {SyncRunKind.MESSAGE_SEND})


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _seed(app: FastAPI, *, people: int = 1, **contact: Any) -> list[int]:
    """Due enrollments in an active LinkedIn campaign of the local user (on UTC)."""
    with session_scope(app.state.session_factory, write=True) as session:
        user = _local(session)
        user.timezone = "UTC"
        ensure_account(session, user)
        record_poll(session, user, NOW - timedelta(hours=1))  # the hold on a stale inbox (#417)
        record_session_evidence(
            session, user, logged_in=True, source="preflight", now=NOW - timedelta(hours=1)
        )
        campaign = factories.make_campaign(session, user, channels=(TemplateChannel.LINKEDIN,))
        return [
            factories.make_enrollment(
                session,
                campaign,
                factories.make_contact(session, user, **contact),
                next_action_at=NOW - timedelta(minutes=n + 1),
            ).id
            for n in range(people)
        ]


def _messages(app: FastAPI) -> list[Message]:
    with session_scope(app.state.session_factory) as session:
        return list(session.scalars(scoped(_local(session), Message).order_by(Message.id)))


def _runs(app: FastAPI) -> list[SyncRun]:
    with session_scope(app.state.session_factory) as session:
        return list(
            session.scalars(
                scoped(_local(session), SyncRun)
                .where(
                    # Not the fixture's poll, which keeps claims from being held (#417).
                    SyncRun.notes.is_distinct_from(FIXTURE_NOTE)
                )
                .order_by(SyncRun.id)
            )
        )


async def test_ready_lists_due_linkedin_steps_with_no_message_text(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor
) -> None:
    first, second = _seed(running_app, people=2)
    response = await client.get(f"{BASE}/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["enrollment_id"] for item in body["items"]] == [second, first]
    assert set(body["items"][0]) == {
        "enrollment_id",
        "campaign_id",
        "campaign_name",
        "step_position",
        "contact_id",
        "contact_name",
        "due",
        "held_until",
    }
    assert "Hi " not in response.text


async def test_a_prefill_is_claimed_and_submitted_never_awaited(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    [enrollment_id] = _seed(running_app)
    response = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    body = response.json()
    [message] = _messages(running_app)
    [run] = _runs(running_app)
    assert body["message_id"] == message.id and body["run_id"] == run.id
    assert (message.status, message.sync_run_id) == (MessageStatus.SCHEDULED, run.id)
    assert run.kind is SyncRunKind.MESSAGE_SEND
    assert "Hi " not in response.text  # no message text in the answer


async def test_prefill_next_takes_the_oldest(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    _, older = _seed(running_app, people=2)
    response = await client.post(f"{BASE}/prefill", json={"next": True}, headers=HEADERS)
    assert response.status_code == 202, response.text
    assert response.json()["enrollment_id"] == older


async def test_a_refusal_answers_409_and_keeps_what_it_changed(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    [enrollment_id] = _seed(running_app, do_not_contact=True)
    response = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["enrollment_id"] == enrollment_id and detail["reasons"][0] == "ended"
    with session_scope(running_app.state.session_factory) as session:
        enrollment = get_scoped(session, _local(session), Enrollment, enrollment_id)
        assert enrollment is not None and enrollment.status is EnrollmentStatus.OPTED_OUT
    assert (_messages(running_app), _runs(running_app), executor.executed) == ([], [], [])


async def test_message_send_has_a_runner_so_a_prefill_is_submitted(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor
) -> None:
    """P4-03 (#382): ``message_send`` is a runnable kind, so a claim records its run and
    submits it, with no stand-in for the runner."""
    assert SyncRunKind.MESSAGE_SEND in runs.RUNNABLE_KINDS
    [enrollment_id] = _seed(running_app)
    response = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    [run] = _runs(running_app)
    assert run.kind is SyncRunKind.MESSAGE_SEND


async def test_prefill_answers_404_503_and_422(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    nobody = await client.post(f"{BASE}/prefill", json={"enrollment_id": 999}, headers=HEADERS)
    nothing = await client.post(f"{BASE}/prefill", json={"next": True}, headers=HEADERS)
    both = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": 1, "next": True}, headers=HEADERS
    )
    neither = await client.post(f"{BASE}/prefill", json={}, headers=HEADERS)
    assert (nobody.status_code, nothing.status_code) == (404, 404)
    assert (both.status_code, neither.status_code) == (422, 422)
    running_app.state.executor = None
    [enrollment_id] = _seed(running_app)
    no_worker = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    assert no_worker.status_code == 503
    assert _messages(running_app) == []


async def _prefilled(client: httpx.AsyncClient, app: FastAPI) -> int:
    [enrollment_id] = _seed(app)
    response = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    message_id: int = response.json()["message_id"]
    with session_scope(app.state.session_factory, write=True) as session:
        user = _local(session)
        outcome = MessageOutcome(MessageOutcomeKind.PREFILLED, "typed", None, 10)
        assert record_prefill_outcome(
            session, user, message_id, outcome, settings=app.state.settings, now=NOW
        )
        for run in session.scalars(scoped(user, SyncRun)):
            runs.finish_run(session, user, run.id, status=SyncRunStatus.COMPLETED, now=NOW)
    return message_id


async def test_waiting_and_discard(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    message_id = await _prefilled(client, running_app)
    waiting = (await client.get(f"{BASE}/waiting")).json()
    assert waiting["total"] == 1
    [item] = waiting["items"]
    assert (item["message_id"], item["status"]) == (message_id, "prefilled")
    assert item["prefilled_at"] is not None

    discarded = await client.post(f"{BASE}/messages/{message_id}/discard", headers=HEADERS)
    assert discarded.status_code == 200
    assert discarded.json()["status"] == "discarded"
    assert discarded.json()["enrollment_status"] == "completed"  # its only step
    again = await client.post(f"{BASE}/messages/{message_id}/discard", headers=HEADERS)
    missing = await client.post(f"{BASE}/messages/999/discard", headers=HEADERS)
    assert (again.status_code, missing.status_code) == (409, 404)
    assert (await client.get(f"{BASE}/waiting")).json()["total"] == 0


async def test_check_asks_for_an_inbox_poll(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    executor: FakeExecutor,
    with_runner: None,
    inside_active_hours: None,
) -> None:
    message_id = await _prefilled(client, running_app)
    # The inbox poll has a runner since P4-08 (#378): a manual poll is started.
    accepted = await client.post(f"{BASE}/messages/{message_id}/check", headers=HEADERS)
    assert accepted.status_code == 202
    assert [run.kind for run in _runs(running_app)] == [
        SyncRunKind.MESSAGE_SEND,
        SyncRunKind.INBOX,
    ]
    # The real worker, on a fake Chrome, with the inbox source a test hands it: the poll
    # attaches once, reads, and spends one inbox_polls unit.
    provider, connector = fake_provider()
    worker = BrowserWorker(
        provider,
        running_app.state.session_factory,
        running_app.state.settings.linkedin,
        clock=lambda: NOW,
        inbox_sources=lambda run, *, sleep: FakeInboxSource(),
    )
    run_id = accepted.json()["run_id"]
    with session_scope(running_app.state.session_factory) as session:
        user_id = _local(session).id
    await worker.execute(run_id, user_id)
    inbox = next(run for run in _runs(running_app) if run.id == run_id)
    assert inbox.status is SyncRunStatus.COMPLETED
    assert connector.attaches == 1
    with session_scope(running_app.state.session_factory) as session:
        user = _local(session)
        spent = budgets.status(
            session,
            user,
            ensure_account(session, user).id,
            budgets.ActionClass.INBOX_POLLS,
            now=NOW,
            settings=running_app.state.settings.linkedin.budget,
        ).day.count
    assert spent == 1
    missing = await client.post(f"{BASE}/messages/999/check", headers=HEADERS)
    assert missing.status_code == 404


async def test_check_refuses_a_message_that_waits_for_nobody(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor
) -> None:
    [enrollment_id] = _seed(running_app)
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        sent = factories.make_message(session, enrollment, direction=MessageDirection.OUT)
        message_id = sent.id
    response = await client.post(f"{BASE}/messages/{message_id}/check", headers=HEADERS)
    assert response.status_code == 409


async def test_the_dashboard_marks_linkedin_rows_ready_to_prefill(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor
) -> None:
    [enrollment_id] = _seed(running_app)
    body = (await client.get("/api/v1/dashboard/next-fires")).json()
    assert [(i["enrollment_id"], i["ready_to_prefill"]) for i in body["items"]] == [
        (enrollment_id, True)
    ]


async def test_a_message_send_run_cannot_be_started_through_the_runs_api(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    executor: FakeExecutor,
    with_runner: None,
    inside_active_hours: None,
) -> None:
    """S5: even with a runner, only a prefill claim records a message_send run."""
    _seed(running_app)
    response = await client.post(
        "/api/v1/linkedin/runs", json={"kind": "message_send"}, headers=HEADERS
    )
    assert response.status_code == 422
    assert "prefill claim" in response.json()["detail"]
    assert (_runs(running_app), executor.executed) == ([], [])


async def test_waiting_marks_an_interrupted_claim(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    [enrollment_id] = _seed(running_app)
    claimed = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    message_id = claimed.json()["message_id"]
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        for run in session.scalars(scoped(user, SyncRun)):
            runs.finish_run(session, user, run.id, status=SyncRunStatus.FAILED, now=NOW)
    [item] = (await client.get(f"{BASE}/waiting")).json()["items"]
    assert (item["message_id"], item["status"], item["interrupted"]) == (
        message_id,
        "scheduled",
        True,
    )
    discarded = await client.post(f"{BASE}/messages/{message_id}/discard", headers=HEADERS)
    assert discarded.status_code == 200 and discarded.json()["status"] == "discarded"
