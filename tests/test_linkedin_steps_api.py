"""``/campaigns/linkedin`` (P4-09, #379): the person's side of a LinkedIn step.

The two lists' two-user isolation is in ``tests/isolation``. The claim's own checks
are in ``tests/test_linkedin_steps.py``; these tests pin what the API answers, that
a prefill is only ever submitted, never awaited, and that a refusal keeps what it
changed.
"""

from __future__ import annotations

from dataclasses import replace
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
        "auto_send",
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


async def test_a_partly_typed_prefill_stays_listed_blocks_a_claim_and_discard_clears_it(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    """B1: the list comes from the API, so a reload still shows it."""
    enrollment_id, other = _seed(running_app, people=2)
    claimed = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    message_id = claimed.json()["message_id"]
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        outcome = MessageOutcome(MessageOutcomeKind.PARTIALLY_TYPED, "fixed words", None, 5)
        assert record_prefill_outcome(
            session, user, message_id, outcome, settings=running_app.state.settings, now=NOW
        )
        for run in session.scalars(scoped(user, SyncRun)):
            runs.finish_run(session, user, run.id, status=SyncRunStatus.FAILED, now=NOW)

    for _ in range(2):  # a reload asks again and gets the same answer
        [item] = (await client.get(f"{BASE}/waiting")).json()["items"]
        assert (item["message_id"], item["status"], item["partly_typed"]) == (
            message_id,
            "failed",
            True,
        )
        assert item["interrupted"] is False
    blocked = await client.post(f"{BASE}/prefill", json={"enrollment_id": other}, headers=HEADERS)
    assert blocked.status_code == 409
    assert "prefill_open" in blocked.json()["detail"]["reasons"]
    checked = await client.post(f"{BASE}/messages/{message_id}/check", headers=HEADERS)
    assert checked.status_code == 409  # nothing was sent; only Discard applies

    discarded = await client.post(f"{BASE}/messages/{message_id}/discard", headers=HEADERS)
    assert discarded.status_code == 200 and discarded.json()["status"] == "discarded"
    assert (await client.get(f"{BASE}/waiting")).json()["items"] == []


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


async def test_ready_keeps_one_campaigns_and_counts_its_steps(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor
) -> None:
    """#383: the campaign page's queue and its per-step ready count."""
    first, second = _seed(running_app, people=2)
    [other] = _seed(running_app)
    with session_scope(running_app.state.session_factory) as session:
        user = _local(session)
        mine = get_scoped(session, user, Enrollment, first)
        theirs = get_scoped(session, user, Enrollment, other)
        assert mine is not None and theirs is not None
        campaign_id, other_campaign = mine.campaign_id, theirs.campaign_id
    assert campaign_id != other_campaign

    unfiltered = (await client.get(f"{BASE}/ready")).json()
    assert (unfiltered["total"], unfiltered["by_step"]) == (3, {})
    one = (await client.get(f"{BASE}/ready", params={"campaign_id": campaign_id})).json()
    assert one["total"] == 2
    assert {item["enrollment_id"] for item in one["items"]} == {first, second}
    assert one["by_step"] == {"1": 2}
    paged = (
        await client.get(f"{BASE}/ready", params={"campaign_id": campaign_id, "limit": 1})
    ).json()
    assert (len(paged["items"]), paged["by_step"]) == (1, {"1": 2})  # every page's worth
    nobody = (await client.get(f"{BASE}/ready", params={"campaign_id": 999})).json()
    assert nobody == {
        "items": [],
        "total": 0,
        "by_step": {},
        "try_again": [],
        "prefills_left_today": 15,
    }


async def test_options_say_whether_auto_send_may_be_chosen(
    client: httpx.AsyncClient, running_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get(f"{BASE}/options")).json() == {
        "auto_send": False,
        "auto_send_warning": None,
        "auto_send_hold": None,
    }
    settings = running_app.state.settings
    on = replace(settings, campaigns=replace(settings.campaigns, linkedin_auto_send=True))
    monkeypatch.setattr(running_app.state, "settings", on)
    assert (await client.get(f"{BASE}/options")).json() == {
        "auto_send": True,
        "auto_send_warning": None,
        "auto_send_hold": None,
    }


async def test_a_linkedin_steps_counts_by_status_are_on_the_campaign(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    """#383: the campaign page shows a LinkedIn step's prefilled, sent and stale counts."""
    message_id = await _prefilled(client, running_app)
    with session_scope(running_app.state.session_factory) as session:
        message = get_scoped(session, _local(session), Message, message_id)
        assert message is not None
        enrollment = get_scoped(session, _local(session), Enrollment, message.enrollment_id)
        assert enrollment is not None
        campaign_id = enrollment.campaign_id
    campaign = (await client.get(f"/api/v1/campaigns/{campaign_id}")).json()
    [step] = campaign["steps"]
    assert step["outbound"] == {"prefilled": 1}
    assert (step["fired"], step["sent"]) == (1, 0)


async def test_waiting_keeps_one_campaigns(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    """#383: the campaign page's waiting list, filtered on the server."""
    message_id = await _prefilled(client, running_app)
    with session_scope(running_app.state.session_factory) as session:
        message = get_scoped(session, _local(session), Message, message_id)
        assert message is not None
        enrollment = get_scoped(session, _local(session), Enrollment, message.enrollment_id)
        assert enrollment is not None
        campaign_id = enrollment.campaign_id
    mine = (await client.get(f"{BASE}/waiting", params={"campaign_id": campaign_id})).json()
    assert [item["message_id"] for item in mine["items"]] == [message_id]
    other = (await client.get(f"{BASE}/waiting", params={"campaign_id": campaign_id + 1})).json()
    assert other == {"items": [], "total": 0}


# --- try again (#445) -----------------------------------------------------------------------


async def _typed_nothing(
    client: httpx.AsyncClient, app: FastAPI, counts: dict[str, Any]
) -> tuple[int, int]:
    """A prefill whose run ended ``not_typed`` with ``counts``: the enrollment and run."""
    [enrollment_id] = _seed(app)
    claimed = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    assert claimed.status_code == 202, claimed.text
    run_id: int = claimed.json()["run_id"]
    with session_scope(app.state.session_factory, write=True) as session:
        user = _local(session)
        outcome = MessageOutcome(
            MessageOutcomeKind.NOT_TYPED, "the Message control could not be clicked", None, 0
        )
        assert record_prefill_outcome(
            session,
            user,
            claimed.json()["message_id"],
            outcome,
            settings=app.state.settings,
            now=NOW,
        )
        runs.finish_run(
            session,
            user,
            run_id,
            status=SyncRunStatus.ABORTED,
            now=NOW,
            stop_reason="not_typed",
            counts=counts,
        )
    return enrollment_id, run_id


async def test_a_step_that_typed_nothing_is_listed_for_try_again_and_retried(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    clicked = {"message_click_attempted": True, "message_clicked": False, "li_prefills_spent": True}
    enrollment_id, run_id = await _typed_nothing(client, running_app, clicked)

    page = (await client.get(f"{BASE}/ready")).json()
    assert (page["items"], page["total"]) == ([], 0)  # not ready: only Try again claims it
    [row] = page["try_again"]
    assert row["enrollment_id"] == enrollment_id and row["held_until"] is None
    assert row["last_try"] == {
        "reason": "the Message control could not be clicked",
        "tries": 1,
        "run_id": run_id,
        "at": row["last_try"]["at"],
        "click_attempted": True,
        "budget_spent": True,
        "counted_today": True,
        "needs_confirmation": True,
    }
    assert isinstance(page["prefills_left_today"], int)
    assert "Hi " not in str(page)

    plain = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id}, headers=HEADERS
    )
    assert plain.status_code == 409
    assert plain.json()["detail"]["reasons"] == ["try_again_needed"]
    unconfirmed = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id, "retry": True}, headers=HEADERS
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["detail"]["reasons"] == ["confirm_no_bubble"]
    assert len(_messages(running_app)) == 0 and len(executor.executed) == 1

    confirmed = await client.post(
        f"{BASE}/prefill",
        json={"enrollment_id": enrollment_id, "retry": True, "no_bubble_open": True},
        headers=HEADERS,
    )
    assert confirmed.status_code == 202, confirmed.text
    assert confirmed.json()["enrollment_id"] == enrollment_id
    assert len(executor.executed) == 2
    # Claimed: it no longer waits for Try again, and holds the one open slot.
    assert (await client.get(f"{BASE}/ready")).json()["try_again"] == []


async def test_a_retry_that_never_clicked_needs_no_confirmation(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    enrollment_id, _ = await _typed_nothing(client, running_app, {"li_prefills_spent": False})
    [row] = (await client.get(f"{BASE}/ready")).json()["try_again"]
    assert row["last_try"]["needs_confirmation"] is False
    assert row["last_try"]["counted_today"] is False
    retried = await client.post(
        f"{BASE}/prefill", json={"enrollment_id": enrollment_id, "retry": True}, headers=HEADERS
    )
    assert retried.status_code == 202, retried.text


async def test_a_retry_is_one_enrollment_and_the_confirmation_goes_with_it(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    for body in (
        {"next": True, "retry": True},
        {"enrollment_id": 1, "no_bubble_open": True},
        {"next": True, "no_bubble_open": True},
    ):
        response = await client.post(f"{BASE}/prefill", json=body, headers=HEADERS)
        assert response.status_code == 422, body
    assert _messages(running_app) == []


async def test_the_enrollment_row_says_try_again_with_no_next_action(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor, with_runner: None
) -> None:
    enrollment_id, _ = await _typed_nothing(client, running_app, {})
    with session_scope(running_app.state.session_factory) as session:
        enrollment = get_scoped(session, _local(session), Enrollment, enrollment_id)
        assert enrollment is not None
        campaign_id = enrollment.campaign_id
    page = (await client.get(f"/api/v1/campaigns/{campaign_id}/enrollments")).json()
    [row] = page["items"]
    assert (row["try_again"], row["next_action_at"]) == (True, None)
    assert row["not_sent_error"] == "not_typed: the Message control could not be clicked"


async def test_the_enrollment_row_says_try_again_only_for_a_linkedin_step(
    client: httpx.AsyncClient, running_app: FastAPI, executor: FakeExecutor
) -> None:
    """A "not_typed" note on an enrollment whose next step is an email never says Try again."""
    with session_scope(running_app.state.session_factory, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user, channels=(TemplateChannel.EMAIL,))
        enrollment = factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            next_action_at=NOW,
            not_sent_count=1,
            not_sent_error="not_typed: the browser was busy",
        )
        campaign_id = campaign.id
        enrollment_id = enrollment.id
    [row] = (await client.get(f"/api/v1/campaigns/{campaign_id}/enrollments")).json()["items"]
    assert (row["id"], row["try_again"]) == (enrollment_id, False)


# --- the auto-send hold (ADR 0008, #458 review) -------------------------------------------


def _hold(app: FastAPI) -> tuple[int, int]:
    """Hold the local user's auto-send, and another user's too; their account ids."""
    from netkeeper.services.linkedin_steps import AUTO_SEND_HOLD_BUBBLE, hold_auto_send

    with session_scope(app.state.session_factory, write=True) as session:
        user = _local(session)
        mine = ensure_account(session, user).id
        hold_auto_send(session, user, mine, reason=AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=1)
        other = factories.make_user(session, kind=UserKind.HOSTED)
        theirs = ensure_account(session, other).id
        hold_auto_send(session, other, theirs, reason=AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=2)
        return mine, theirs


def _held(app: FastAPI) -> tuple[bool, bool]:
    from netkeeper.services.linkedin_steps import auto_send_hold

    with session_scope(app.state.session_factory) as session:
        user = _local(session)
        other = session.scalars(select(User).where(User.kind == UserKind.HOSTED)).one()
        return (
            auto_send_hold(session, user, ensure_account(session, user).id) is not None,
            auto_send_hold(session, other, ensure_account(session, other).id) is not None,
        )


async def test_options_carry_the_hold_and_how_to_clear_it(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _hold(running_app)
    hold = (await client.get(f"{BASE}/options")).json()["auto_send_hold"]
    assert hold["reason"] == "a message bubble is open in Chrome"
    assert "netkeeper linkedin auto-send-resume" in hold["how_to_clear"]


async def test_resume_needs_the_csrf_header_and_confirm_and_is_idempotent_and_scoped(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    _hold(running_app)
    url = f"{BASE}/auto-send/resume"
    assert (await client.post(url, json={"confirm": True})).status_code == 403
    assert (await client.post(url, json={}, headers=HEADERS)).status_code == 422
    assert (await client.post(url, json={"confirm": False}, headers=HEADERS)).status_code == 422
    assert _held(running_app) == (True, True)
    first = await client.post(url, json={"confirm": True}, headers=HEADERS)
    assert first.status_code == 200 and first.json() == {"resumed": True}
    again = await client.post(url, json={"confirm": True}, headers=HEADERS)
    assert again.json() == {"resumed": False}
    assert _held(running_app) == (False, True)  # another user's hold is untouched
