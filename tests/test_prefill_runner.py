"""The prefill's runner and the worker's ``message_send`` route (P4-03, #382, ADR 0007).

A real claim (P4-09's ``claim_prefill``) records the message and its run; the real
worker runs it against :mod:`messaging_dom`'s fake profile. Each refusal before the
first key is checked to spend no budget and open nothing, and each outcome to land on
the message the way P4-09 records it.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

import pytest
from campaign_fakes import NOW
from inbox_fakes import FIXTURE_NOTE
from messaging_dom import Bubble, MessagingSite, MessagingTab
from messaging_pages import ZEPHYRINE, conversation_urn
from run_fakes import fake_provider
from sqlalchemy.orm import Session, sessionmaker
from test_linkedin_steps import LINKEDIN, Lane, make_lane

from netkeeper.config import BudgetSettings, Settings
from netkeeper.db import session_scope
from netkeeper.linkedin import messaging
from netkeeper.linkedin.activity_lock import account_key
from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserRun
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.messaging import (
    PLAN_INVARIANT_MESSAGE,
    MessageJobSpec,
    MessageOutcome,
    MessageOutcomeKind,
    PlanInvariantBroken,
    PrefillResult,
    plan_typing,
)
from netkeeper.linkedin.pacing import TypingPlan, TypingTooLong
from netkeeper.linkedin.page_messaging import PagePrefill
from netkeeper.models import (
    Enrollment,
    Message,
    MessageStatus,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import budgets, message_send, runs, scheduler
from netkeeper.services.budgets import ActionClass
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session, session_flag
from netkeeper.services.linkedin_steps import record_prefill_outcome
from netkeeper.worker import BrowserWorker

CONTACT = {"li_urn": ZEPHYRINE.urn, "li_public_id": ZEPHYRINE.slug, "first_name": "Zephyrine"}


class Clock:
    """The worker's clock: ``NOW`` plus a second a call, from ``start``."""

    def __init__(self, start: datetime = NOW) -> None:
        self.at = start

    def __call__(self) -> datetime:
        self.at += timedelta(milliseconds=10)
        return self.at


async def no_sleep(seconds: float) -> None:
    return None


def fast_source(run: BrowserRun, *, sleep: Any, clock: Any) -> PagePrefill:
    return PagePrefill(
        run,
        sleep=no_sleep,
        clock=clock,
        rng=random.Random(2),
        compose_wait_s=0.05,
        compose_settle_s=0.01,
        thread_wait_s=0.01,
    )


class Fixture:
    def __init__(self, lane: Lane, site: MessagingSite, *, clock: Clock | None = None) -> None:
        self.lane = lane
        self.site = site
        self.provider, self.connector = fake_provider(site)
        self.clock = clock or Clock()
        self.worker = BrowserWorker(
            self.provider,
            lane.factory,
            lane.settings.linkedin,
            clock=self.clock,
            sleep=no_sleep,
            prefill_sources=fast_source,
            campaign_settings=lane.settings,
        )
        self.enrollment_id = lane.enroll(contact=dict(CONTACT))
        claim = lane.claim(self.enrollment_id)
        assert claim.claimed, claim.reasons
        assert claim.message_id is not None and claim.run_id is not None
        self.message_id = claim.message_id
        self.run_id = claim.run_id

    async def execute(self) -> runs.RunOutcome:
        return await self.worker.execute(self.run_id, self.lane.user_id)

    def message(self) -> Message | None:
        return self.lane.read(lambda s, u: get_scoped(s, u, Message, self.message_id))

    def run(self) -> SyncRun:
        return self.lane.read(lambda s, u: runs.get_run(s, u, self.run_id))

    def enrollment(self) -> Enrollment:
        return self.lane.enrollment(self.enrollment_id)

    def spent(self, action: ActionClass) -> int:
        def read(session: Session, user: User) -> int:
            account = ensure_account(session, user)
            return budgets.status(
                session, user, account.id, action, now=NOW, settings=BudgetSettings()
            ).day.count

        return self.lane.write(read)


@pytest.fixture
def lane(session_factory: sessionmaker[Session]) -> Lane:
    return make_lane(session_factory, channels=(LINKEDIN, LINKEDIN))


def _nothing_opened(f: Fixture) -> None:
    assert f.site.navigations == [] and f.site.pages == []
    assert f.spent(ActionClass.LI_PREFILLS) == 0
    assert f.spent(ActionClass.PROFILE_VISITS) == 0


def _given_back(f: Fixture, reason: str) -> None:
    assert f.message() is None  # P4-09 gives the claim back: the row goes
    enrollment = f.enrollment()
    assert enrollment.not_sent_error is not None and reason in enrollment.not_sent_error
    assert f.run().status is not SyncRunStatus.RUNNING


# --- the whole path ----------------------------------------------------------------------


async def test_a_prefill_types_the_message_and_records_it_from_the_typing_start(
    lane: Lane,
) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    outcome = await f.execute()
    assert outcome is runs.RunOutcome.DONE
    message = f.message()
    assert message is not None and message.status is MessageStatus.PREFILLED
    tab = f.site.tab
    assert tab.typed == message.body_rendered
    run = f.run()
    assert (run.status, run.stop_reason) == (SyncRunStatus.COMPLETED, "prefilled")
    assert run.completed_at is not None and message.prefilled_at is not None
    assert message.prefilled_at < run.completed_at  # the typing start, not the recording
    assert message.li_conversation_urn == conversation_urn(7)
    assert f.spent(ActionClass.LI_PREFILLS) == 1 and f.spent(ActionClass.PROFILE_VISITS) == 1
    assert len(tab.clicks) == 1 and tab.fronted == 1 and not tab.is_closed()
    assert message.body_rendered not in (run.error or "") + str(run.counts_json)
    assert run.counts_json == {
        "typed_chars": len(message.body_rendered or ""),
        "recipient_name_checked": None,  # an existing conversation has no chip
        "message_click_attempted": True,
        "message_clicked": True,
    }


async def test_a_lapsed_claim_opens_nothing_and_spends_nothing(lane: Lane) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE), clock=Clock(NOW + timedelta(seconds=61)))
    await f.execute()
    _nothing_opened(f)
    assert f.connector.attaches == 0
    _given_back(f, "the claim lapsed")


def test_the_claim_lapse_is_pinned() -> None:
    assert timedelta(seconds=60) == message_send.CLAIM_LAPSE


async def test_a_busy_browser_lock_records_not_typed(lane: Lane) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    account = f.run().linkedin_account_id
    async with f.provider.locks.hold(account_key(account)):
        outcome = await f.execute()
    assert outcome is runs.RunOutcome.RETRY_LATER
    _nothing_opened(f)
    _given_back(f, "the browser was busy")


async def test_a_flagged_session_records_not_typed_before_the_browser(lane: Lane) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    lane.write(lambda s, u: flag_session(s, u, Outcome.CHECKPOINT, url="https://x.test/"))
    await f.execute()
    _nothing_opened(f)
    assert f.connector.attaches == 0
    assert f.message() is None


@pytest.mark.parametrize(
    ("error", "kind"),
    [(TypingTooLong(400.0, 300.0), "too_long"), (PlanInvariantBroken(), "not_typed")],
)
async def test_a_refused_plan_opens_nothing_and_spends_nothing(
    lane: Lane, monkeypatch: pytest.MonkeyPatch, error: Exception, kind: str
) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))

    def refuse(body: str, seed: int) -> TypingPlan:
        raise error

    monkeypatch.setattr(message_send, "plan_typing", refuse)
    await f.execute()
    _nothing_opened(f)
    assert f.connector.attaches == 0
    _given_back(f, kind)
    if kind == "too_long":
        assert f.enrollment().next_action_at is None  # parked at once


async def test_an_unexpected_plan_failure_is_wrapped_and_typed_nothing(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))

    def broken(text: str, rng: random.Random) -> TypingPlan:
        raise KeyError(text)  # would quote the body if it leaked

    monkeypatch.setattr(messaging, "typing_plan", broken)
    await f.execute()
    _nothing_opened(f)
    _given_back(f, "PlanInvariantBroken")
    error = f.enrollment().not_sent_error or ""
    assert "Zephyrine" not in error


def test_plan_typing_wraps_only_exceptions_and_chains_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(text: str, rng: random.Random) -> TypingPlan:
        raise ValueError(f"secret {text}")

    monkeypatch.setattr(messaging, "typing_plan", broken)
    with pytest.raises(PlanInvariantBroken) as raised:
        plan_typing("secret body", 1)
    assert str(raised.value) == PLAN_INVARIANT_MESSAGE
    assert "secret" not in str(raised.value)
    assert isinstance(raised.value.__cause__, ValueError)

    def interrupted(text: str, rng: random.Random) -> TypingPlan:
        raise KeyboardInterrupt

    monkeypatch.setattr(messaging, "typing_plan", interrupted)
    with pytest.raises(KeyboardInterrupt):
        plan_typing("x", 1)


def test_plan_typing_never_passes_allow_newlines() -> None:
    plan = plan_typing("a\nb", 1)  # the flag's default decides, and it allows them
    assert [step.newline for step in plan] == [False, True, False]


async def test_a_spent_budget_records_not_typed_before_the_navigation(lane: Lane) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))

    def spend_all(session: Session, user: User) -> None:
        account = ensure_account(session, user)
        for _ in range(25):
            try:
                budgets.consume(
                    session,
                    user,
                    account.id,
                    ActionClass.LI_PREFILLS,
                    now=NOW,
                    settings=lane.settings.linkedin.budget,
                )
            except budgets.BudgetExceeded:
                return

    lane.write(spend_all)
    await f.execute()
    assert f.site.navigations == []
    _given_back(f, "budget")
    assert _click_counts(f) == NO_CLICK


async def test_a_wall_flags_the_session_and_types_nothing(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE, land_on="https://www.linkedin.com/checkpoint/challenge/x")
    f = Fixture(lane, site)
    await f.execute()
    assert site.tab.attempts == [] and site.tab.clicks == []
    assert lane.read(lambda s, u: session_flag(s, u)) is not None
    _given_back(f, "checkpoint")


async def test_a_stop_after_the_first_key_fails_the_message_and_is_never_retried(
    lane: Lane,
) -> None:
    site = MessagingSite(ZEPHYRINE)

    def move(tab: MessagingTab) -> None:
        tab.focused = None

    site.after_key[2] = move
    f = Fixture(lane, site)
    await f.execute()
    message = f.message()
    assert message is not None and message.status is MessageStatus.FAILED
    assert (message.error or "").startswith("partially_typed")
    assert f.enrollment().next_action_at is None
    assert f.run().status is SyncRunStatus.FAILED
    assert len(site.tab.attempts) == 2 and not site.tab.is_closed()


async def test_a_refusal_after_the_click_gives_the_claim_back_and_hands_the_tab_over(
    lane: Lane,
) -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_composer=False))
    site.focus_ignored = True
    f = Fixture(lane, site)
    await f.execute()
    _given_back(f, "focus")
    assert site.tab.attempts == [] and not site.tab.is_closed()


class RaisingSource:
    """A source that raises after ``keys`` keys, having clicked as told."""

    def __init__(
        self,
        keys: int,
        *,
        attempted: bool | None = None,
        clicked: bool | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._keys = keys
        self._attempted = keys > 0 if attempted is None else attempted
        self._clicked = keys > 0 if clicked is None else clicked
        self._error = error

    @property
    def keys_sent(self) -> int:
        return self._keys

    @property
    def message_click_attempted(self) -> bool:
        return self._attempted

    @property
    def message_clicked(self) -> bool:
        return self._clicked

    async def prefill(
        self, spec: MessageJobSpec, plan: TypingPlan, *, cancelled: Any
    ) -> PrefillResult:
        if self._error is not None:
            raise self._error
        raise RuntimeError(f"failed with {spec.body}")


@pytest.mark.parametrize(
    ("keys", "status"), [(0, None), (1, MessageStatus.FAILED), (3, MessageStatus.FAILED)]
)
async def test_a_source_that_raises_is_not_typed_before_a_key_and_unknown_after(
    lane: Lane, keys: int, status: MessageStatus | None
) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    f.worker._prefill_sources = lambda run, *, sleep, clock: RaisingSource(keys)
    await f.execute()
    message = f.message()
    if status is None:
        assert message is None
    else:
        assert message is not None and message.status is status
        assert (message.error or "").startswith("unknown")
        assert "Zephyrine" not in (message.error or "")


# --- the click is recorded on every path (S3) ---------------------------------------------

NO_CLICK = {"message_click_attempted": False, "message_clicked": False}


def _click_counts(f: Fixture) -> dict[str, Any]:
    counts = f.run().counts_json or {}
    return {key: counts.get(key) for key in NO_CLICK}


async def test_a_run_refused_before_the_runner_says_nothing_about_a_click(lane: Lane) -> None:
    """The claim lapsed at prepare(): no source ran, so the keys are left out and the UI
    falls back to the reason's words instead of reading "no bubble"."""
    f = Fixture(lane, MessagingSite(ZEPHYRINE), clock=Clock(NOW + timedelta(seconds=61)))
    await f.execute()
    assert _click_counts(f) == {"message_click_attempted": None, "message_clicked": None}
    assert "message_click_attempted" not in (f.run().counts_json or {})


async def test_a_claim_that_lapsed_in_the_runner_records_no_click(lane: Lane) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    prepared = message_send.prepare(
        lane.factory, lane.user_id, f.run_id, settings=lane.settings, clock=lambda: NOW
    )
    assert isinstance(prepared, message_send.PreparedPrefill)
    late = Clock(NOW + timedelta(seconds=61))
    await message_send.run_prefill(
        lane.factory,
        lane.user_id,
        prepared,
        RecordingSource(),
        settings=lane.settings,
        clock=late,
    )
    assert _click_counts(f) == NO_CLICK


async def test_a_refusal_before_the_click_records_no_click(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE, profile_html="<html><body><h1>Nobody</h1></body></html>")
    f = Fixture(lane, site)
    await f.execute()
    run = f.run()
    assert (run.stop_reason, _click_counts(f)) == ("not_typed", NO_CLICK)


async def test_a_refusal_after_a_click_that_landed_records_both(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, focus_composer=False))
    site.focus_ignored = True
    f = Fixture(lane, site)
    await f.execute()
    run = f.run()
    assert (run.stop_reason, _click_counts(f)) == (
        "not_typed",
        {"message_click_attempted": True, "message_clicked": True},
    )


async def test_a_click_that_raised_records_attempted_but_not_clicked(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE)
    site.click_error = RuntimeError("the page refused the click")
    f = Fixture(lane, site)
    await f.execute()
    assert _click_counts(f) == {"message_click_attempted": True, "message_clicked": False}


@pytest.mark.parametrize(("attempted", "clicked"), [(False, False), (True, False), (True, True)])
async def test_a_source_that_raises_records_what_it_clicked(
    lane: Lane, attempted: bool, clicked: bool
) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    f.worker._prefill_sources = lambda run, *, sleep, clock: RaisingSource(
        0, attempted=attempted, clicked=clicked
    )
    await f.execute()
    assert _click_counts(f) == {"message_click_attempted": attempted, "message_clicked": clicked}


async def test_a_cancelled_run_records_what_it_clicked(lane: Lane) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    f.worker._prefill_sources = lambda run, *, sleep, clock: RaisingSource(
        0, attempted=True, clicked=True, error=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await f.execute()
    assert _click_counts(f) == {"message_click_attempted": True, "message_clicked": True}


# --- recording ---------------------------------------------------------------------------


def test_prefilled_at_is_recorded_as_given_and_may_not_be_later_than_now(lane: Lane) -> None:
    enrollment_id = lane.enroll(contact=dict(CONTACT))
    claim = lane.claim(enrollment_id)
    assert claim.message_id is not None
    outcome = MessageOutcome(MessageOutcomeKind.PREFILLED, "typed", None, 5)
    started = NOW - timedelta(seconds=40)

    def later(session: Session, user: User) -> None:
        with pytest.raises(ValueError, match="no later than now"):
            record_prefill_outcome(
                session,
                user,
                claim.message_id or 0,
                outcome,
                settings=lane.settings,
                now=NOW,
                prefilled_at=NOW + timedelta(seconds=1),
            )

    lane.write(later)
    lane.write(
        lambda s, u: record_prefill_outcome(
            s,
            u,
            claim.message_id or 0,
            outcome,
            settings=lane.settings,
            now=NOW,
            prefilled_at=started,
        )
    )
    message_id = claim.message_id
    message = lane.read(lambda s, u: get_scoped(s, u, Message, message_id))
    assert message is not None and message.prefilled_at == started


def test_a_message_send_run_is_never_scheduled(lane: Lane) -> None:
    def scheduled(session: Session, user: User) -> None:
        with pytest.raises(runs.RunError, match="never scheduled"):
            runs.create_run(
                session,
                user,
                SyncRunKind.MESSAGE_SEND,
                trigger=SyncRunTrigger.SCHEDULED,
                now=NOW,
                gate=runs.MESSAGE_SEND_GATE,
            )

    lane.write(scheduled)


def test_the_interleave_gap_follows_a_prefill_run(lane: Lane) -> None:
    def ended(at: datetime) -> None:
        def make(session: Session, user: User) -> None:
            run = runs.create_run(
                session,
                user,
                SyncRunKind.MESSAGE_SEND,
                trigger=SyncRunTrigger.MANUAL,
                now=at,
                gate=runs.MESSAGE_SEND_GATE,
            )
            runs.finish_run(session, user, run.id, status=SyncRunStatus.COMPLETED, now=at)

        lane.write(make)

    user = lane.read(lambda s, u: u)
    assert scheduler._last_message_send(lane.factory, user, NOW) is None
    ended(NOW - timedelta(minutes=3))
    assert scheduler._last_message_send(lane.factory, user, NOW) is None
    ended(NOW - timedelta(seconds=30))
    assert scheduler._last_message_send(lane.factory, user, NOW) == NOW - timedelta(seconds=30)
    with session_scope(lane.factory) as session:
        made = [r for r in session.scalars(scoped(user, SyncRun)) if r.notes != FIXTURE_NOTE]
        assert made and all(r.kind is SyncRunKind.MESSAGE_SEND for r in made)


def test_settings_carry_the_workers_linkedin_section(lane: Lane) -> None:
    provider: AttachBrowserProvider = fake_provider(MessagingSite(ZEPHYRINE))[0]
    worker = BrowserWorker(provider, lane.factory, lane.settings.linkedin)
    assert worker._prefill_settings().linkedin is lane.settings.linkedin
    assert isinstance(worker._prefill_settings(), Settings)


class RecordingSource:
    """A source that must never be reached."""

    keys_sent = 0
    message_click_attempted = False
    message_clicked = False

    def __init__(self) -> None:
        self.calls = 0

    async def prefill(
        self, spec: MessageJobSpec, plan: TypingPlan, *, cancelled: Any
    ) -> PrefillResult:
        self.calls += 1
        raise AssertionError("the prefill ran")


async def test_the_runner_rechecks_the_session_flag_under_the_lock(lane: Lane) -> None:
    """The worker checks the flag before the lock; the runner checks it again after, before
    any budget or navigation, for a flag set in between."""
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    prepared = message_send.prepare(
        lane.factory, lane.user_id, f.run_id, settings=lane.settings, clock=Clock()
    )
    assert isinstance(prepared, message_send.PreparedPrefill)
    lane.write(lambda s, u: flag_session(s, u, Outcome.LOGGED_OUT, url="https://x.test/"))
    source = RecordingSource()
    report = await message_send.run_prefill(
        lane.factory, lane.user_id, prepared, source, settings=lane.settings, clock=Clock()
    )
    assert source.calls == 0
    assert report.outcome is not None and "flagged" in report.outcome.reason
    _nothing_opened(f)
    _given_back(f, "flagged")


def _set_scheduled_at(f: Fixture, at: datetime | None) -> None:
    def change(session: Session, user: User) -> None:
        message = get_scoped(session, user, Message, f.message_id)
        assert message is not None
        message.scheduled_at = at

    f.lane.write(change)


@pytest.mark.parametrize("at", [NOW + timedelta(seconds=30), None], ids=["future", "missing"])
async def test_a_claim_from_the_future_or_with_no_time_has_lapsed(
    lane: Lane, at: datetime | None
) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    _set_scheduled_at(f, at)
    await f.execute()
    _nothing_opened(f)
    assert f.connector.attaches == 0
    _given_back(f, "the claim lapsed")


async def test_the_lapse_is_checked_again_after_the_attach(lane: Lane) -> None:
    f = Fixture(lane, MessagingSite(ZEPHYRINE))
    prepared = message_send.prepare(
        lane.factory, lane.user_id, f.run_id, settings=lane.settings, clock=Clock()
    )
    assert isinstance(prepared, message_send.PreparedPrefill)
    source = RecordingSource()
    report = await message_send.run_prefill(
        lane.factory,
        lane.user_id,
        prepared,
        source,
        settings=lane.settings,
        clock=Clock(NOW + timedelta(seconds=61)),
    )
    assert source.calls == 0
    assert report.outcome is not None and report.outcome.reason == "the claim lapsed"
    _nothing_opened(f)
    _given_back(f, "the claim lapsed")
