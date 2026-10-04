"""netkeeper.services.linkedin_steps (P4-09, #379): a LinkedIn step, prefilled by a person.

The engine marks LinkedIn steps ready; only :func:`claim_prefill`, which a person
triggers, claims one, with every check an email fire runs and the LinkedIn ones
besides. These tests pin each refusal, the outcomes, stale at three days, discard,
and that the tick still never claims a LinkedIn step or touches a browser.
"""

from __future__ import annotations

import ast
import itertools
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import factories
import pytest
from campaign_fakes import NOW, SETTINGS, FakeSender, make_mailbox
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import BudgetSettings, LinkedInSettings, Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    StepMode,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped, unscoped
from netkeeper.services import budgets, heat, linkedin_steps, runs, sending_hours
from netkeeper.services import campaign_engine as engine
from netkeeper.services.campaign_engine import Skip, enroll, run_tick
from netkeeper.services.campaign_guards import Reason, check_step
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session, record_session_evidence
from netkeeper.services.linkedin_steps import (
    PrefillClaim,
    Refusal,
    claim_next,
    claim_prefill,
    discard,
    ready_to_prefill,
    record_prefill_outcome,
    waiting_for_you,
)

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
BODY_MARK = "Hi First"  # every factory template renders "Hi {{ first_name }}"
_addresses = itertools.count(1)
#: LinkedIn's active hours on the user's own zone (UTC), so they and the sending hours
#: read the same clock: 08:30 to 21:30.
LANE_SETTINGS = Settings(campaigns=SETTINGS.campaigns, linkedin=LinkedInSettings(timezone="UTC"))


@pytest.fixture(autouse=True)
def _message_send_has_a_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """``message_send`` has no runner until P4-03: these tests stand in for it, so the
    claim records its run through the real ``create_run`` (one running run per account,
    never scheduled). ``test_without_a_runner_nothing_is_claimed`` takes it away again."""
    monkeypatch.setattr(runs, "RUNNABLE_KINDS", runs.RUNNABLE_KINDS | {SyncRunKind.MESSAGE_SEND})


@dataclass
class Lane:
    factory: sessionmaker[Session]
    user_id: int
    campaign_id: int
    settings: Settings

    def write[T](self, fn: Callable[[Session, User], T]) -> T:
        with session_scope(self.factory, write=True) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            return fn(session, user)

    def read[T](self, fn: Callable[[Session, User], T]) -> T:
        with session_scope(self.factory) as session:
            user = session.get(User, self.user_id)
            assert user is not None
            return fn(session, user)

    def enroll(self, **overrides: Any) -> int:
        """A contact enrolled active, its next step due at ``NOW``."""

        def make(session: Session, user: User) -> int:
            contact_fields = overrides.pop("contact", {})
            contact = factories.make_contact(
                session, user, emails=[f"p{next(_addresses)}@example.test"], **contact_fields
            )
            campaign = get_scoped(session, user, Campaign, self.campaign_id)
            assert campaign is not None
            fields: dict[str, Any] = {"next_action_at": NOW}
            fields.update(overrides)
            return factories.make_enrollment(session, campaign, contact, **fields).id

        return self.write(make)

    def claim(self, enrollment_id: int, now: datetime = NOW, **kwargs: Any) -> PrefillClaim:
        settings = kwargs.pop("settings", self.settings)
        return self.write(
            lambda s, u: claim_prefill(s, u, enrollment_id, now=now, settings=settings, **kwargs)
        )

    def record(self, message_id: int, kind: MessageOutcomeKind, now: datetime = NOW) -> bool:
        outcome = MessageOutcome(kind, "fixed words", "urn:li:msg_conversation:TEST1", 12)
        return self.write(
            lambda s, u: record_prefill_outcome(
                s, u, message_id, outcome, settings=self.settings, now=now
            )
        )

    def enrollment(self, enrollment_id: int) -> Enrollment:
        row = self.read(lambda s, u: get_scoped(s, u, Enrollment, enrollment_id))
        assert row is not None
        return row

    def messages(self, enrollment_id: int) -> list[Message]:
        return self.read(
            lambda s, u: list(
                s.scalars(
                    scoped(u, Message)
                    .where(Message.enrollment_id == enrollment_id)
                    .order_by(Message.id)
                )
            )
        )

    def message(self, message_id: int) -> Message | None:
        return self.read(lambda s, u: get_scoped(s, u, Message, message_id))

    def runs(self) -> list[SyncRun]:
        return self.read(lambda s, u: list(s.scalars(scoped(u, SyncRun).order_by(SyncRun.id))))

    def finish_runs(self) -> None:
        """The runs end (as P4-03's runner would after recording)."""

        def finish(session: Session, user: User) -> None:
            for run in session.scalars(
                scoped(user, SyncRun).where(SyncRun.status == SyncRunStatus.RUNNING)
            ):
                runs.finish_run(session, user, run.id, status=SyncRunStatus.COMPLETED, now=NOW)

        self.write(finish)

    def prefill(self, enrollment_id: int) -> int:
        """Claim, record ``prefilled``, end the run: an open prefill."""
        claim = self.claim(enrollment_id)
        assert claim.claimed, claim.reasons
        assert claim.message_id is not None
        assert self.record(claim.message_id, MessageOutcomeKind.PREFILLED)
        self.finish_runs()
        return claim.message_id


def make_lane(
    factory: sessionmaker[Session],
    *,
    channels: tuple[TemplateChannel, ...] = (LINKEDIN, LINKEDIN),
    settings: Settings | None = None,
    evidence: bool = True,
    **campaign: Any,
) -> Lane:
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user)
        row = factories.make_campaign(
            session, user, channels=channels, mailbox_id=mailbox.id, **campaign
        )
        for step in row.steps:
            if step.channel is EMAIL:
                step.mode = StepMode.SEND
        ensure_account(session, user)
        if evidence:
            record_session_evidence(
                session, user, logged_in=True, source="preflight", now=NOW - timedelta(hours=1)
            )
        session.flush()
        lane = Lane(factory, user.id, row.id, settings or LANE_SETTINGS)
    return lane


@pytest.fixture
def lane(session_factory: sessionmaker[Session]) -> Lane:
    return make_lane(session_factory)


def _sent_step_one(lane: Lane, enrollment_id: int, *, at: datetime) -> None:
    def make(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1, sent_at=at)

    lane.write(make)


# --- constants ------------------------------------------------------------------------


def test_the_prefill_constants_are_pinned() -> None:
    """Safety constants against numbers written out here (CLAUDE.md)."""
    assert timedelta(days=3) == engine.PREFILL_STALE_AFTER
    assert timedelta(days=3) == linkedin_steps.PREFILL_STALE_AFTER
    assert linkedin_steps.NOT_TYPED_PARK_AFTER == 2
    assert budgets.HARD_MAX_PER_DAY[budgets.ActionClass.LI_PREFILLS] == 20
    assert BudgetSettings().li_prefills_per_day == 10
    assert budgets.ActionClass.LI_PREFILLS.value == "li_prefills"
    assert engine.LINKEDIN_ENRICH_PRIORITY == 1
    assert {s.value for s in linkedin_steps.OPEN_STATUSES} == {"scheduled", "prefilled"}
    assert {s.value for s in linkedin_steps.WAITING_STATUSES} == {"prefilled", "stale"}


def test_the_li_prefills_budget_is_clamped_to_its_hard_max() -> None:
    asked = BudgetSettings(li_prefills_per_day=500)
    assert budgets.configured_default(budgets.ActionClass.LI_PREFILLS, asked) == 500
    assert budgets._limits_for(budgets.ActionClass.LI_PREFILLS, asked).day == 20


# --- the claim ------------------------------------------------------------------------


def test_a_due_linkedin_step_is_claimed_with_its_run(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    claim = lane.claim(enrollment_id)

    assert claim.claimed and claim.reasons == ()
    [message] = lane.messages(enrollment_id)
    assert (message.id, message.status, message.channel) == (
        claim.message_id,
        MessageStatus.SCHEDULED,
        LINKEDIN,
    )
    assert message.direction is MessageDirection.OUT
    assert message.body_rendered is not None and message.body_rendered.startswith(BODY_MARK)
    assert message.subject is None
    assert message.scheduled_at == NOW
    assert message.sync_run_id == claim.run_id
    [run] = lane.runs()
    assert (run.id, run.kind, run.trigger, run.status) == (
        claim.run_id,
        SyncRunKind.MESSAGE_SEND,
        SyncRunTrigger.MANUAL,
        SyncRunStatus.RUNNING,
    )
    # No message body in the run row.
    assert (run.progress_json, run.counts_json, run.plan_json, run.notes, run.error) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert lane.enrollment(enrollment_id).next_action_at is None


def test_no_message_body_reaches_a_log(lane: Lane, caplog: pytest.LogCaptureFixture) -> None:
    enrollment_id = lane.enroll()
    caplog.set_level(logging.DEBUG)
    claim = lane.claim(enrollment_id)
    assert claim.message_id is not None
    lane.record(claim.message_id, MessageOutcomeKind.PREFILLED)
    assert BODY_MARK not in caplog.text
    assert "First" not in caplog.text


def test_never_twice_across_a_crash_between_the_claim_and_the_run(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    first = lane.claim(enrollment_id)
    assert first.claimed

    # The process dies before the run starts: the next start fails the run.
    def crash_and_restart(session: Session, user: User) -> None:
        restarted = NOW + timedelta(minutes=5)
        assert runs.fail_interrupted_runs(session, now=restarted, browser_held=lambda _: False) == 1
        # Something sets the due time again (a resume, a schedule change, a bad edit).
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        enrollment.next_action_at = NOW

    lane.write(crash_and_restart)
    again = lane.claim(enrollment_id, now=NOW + timedelta(minutes=5))

    assert again.reasons == (Skip.STEP_ALREADY_SENT,)
    assert [m.id for m in lane.messages(enrollment_id)] == [first.message_id]
    assert len(lane.runs()) == 1
    assert lane.enrollment(enrollment_id).next_action_at is None  # parked again
    # Nor does the tick ever fire it.
    result = _tick(lane, NOW + timedelta(minutes=6))
    assert result.fired == []
    assert [m.id for m in lane.messages(enrollment_id)] == [first.message_id]


def test_one_open_prefill_refuses_a_second(lane: Lane) -> None:
    first, second = lane.enroll(), lane.enroll()
    claim = lane.claim(first)
    assert claim.claimed and claim.message_id is not None

    # Claimed and its run not recorded: open.
    assert lane.claim(second).reasons == (Refusal.PREFILL_OPEN,)
    lane.record(claim.message_id, MessageOutcomeKind.PREFILLED)
    lane.finish_runs()
    # Prefilled and not seen sent: still open.
    assert lane.claim(second).reasons == (Refusal.PREFILL_OPEN,)
    assert lane.messages(second) == []

    # Once the person discards it, the next may go.
    lane.write(lambda s, u: discard(s, u, claim.message_id or 0, settings=lane.settings))
    assert lane.claim(second).claimed


def test_a_running_run_refuses_the_claim_and_writes_nothing(lane: Lane) -> None:
    enrollment_id = lane.enroll()

    def busy(session: Session, user: User) -> None:
        runs.create_run(session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)

    lane.write(busy)
    claim = lane.claim(enrollment_id)
    assert claim.reasons == (Refusal.RUN_IN_PROGRESS,)
    assert claim.detail is not None and "still running" in claim.detail
    assert lane.messages(enrollment_id) == []
    assert lane.enrollment(enrollment_id).next_action_at == NOW


def test_without_a_runner_nothing_is_claimed(lane: Lane, monkeypatch: pytest.MonkeyPatch) -> None:
    """Until P4-03 adds the runner, a claim is refused and leaves nothing behind."""
    monkeypatch.setattr(runs, "RUNNABLE_KINDS", runs.RUNNABLE_KINDS - {SyncRunKind.MESSAGE_SEND})
    enrollment_id = lane.enroll()
    claim = lane.claim(enrollment_id)
    assert claim.reasons == (Refusal.RUN_REFUSED,)
    assert claim.detail == "no runner exists for message_send runs yet"
    assert (lane.messages(enrollment_id), lane.runs()) == ([], [])
    assert lane.enrollment(enrollment_id).next_action_at == NOW


def test_create_run_refuses_a_scheduled_message_send(lane: Lane) -> None:
    def scheduled(session: Session, user: User) -> None:
        runs.create_run(
            session, user, SyncRunKind.MESSAGE_SEND, trigger=SyncRunTrigger.SCHEDULED, now=NOW
        )

    with pytest.raises(runs.RunError, match="never scheduled"):
        lane.write(scheduled)
    assert lane.runs() == []


# --- each refusal -----------------------------------------------------------------------


def _set(lane: Lane, model: type[Any], row_id: int, **values: Any) -> None:
    def change(session: Session, user: User) -> None:
        row = get_scoped(session, user, model, row_id)
        assert row is not None
        for name, value in values.items():
            setattr(row, name, value)

    lane.write(change)


@pytest.mark.parametrize(
    ("campaign_status", "reasons"),
    [
        (CampaignStatus.PAUSED, (Skip.GUARD_EXCLUDED, "campaign_not_active")),
        # Not yet through the review gate: no step approved, nothing claimed (#339).
        (CampaignStatus.REVIEWING, (Skip.GUARD_EXCLUDED, "campaign_not_active")),
        (CampaignStatus.DRAFT, (Skip.GUARD_EXCLUDED, "campaign_not_active")),
    ],
)
def test_a_campaign_that_is_not_active_is_refused(
    lane: Lane, campaign_status: CampaignStatus, reasons: tuple[str, ...]
) -> None:
    enrollment_id = lane.enroll()
    _set(lane, Campaign, lane.campaign_id, status=campaign_status)
    assert lane.claim(enrollment_id).reasons == reasons
    assert lane.messages(enrollment_id) == []


def test_a_paused_enrollment_is_refused(lane: Lane) -> None:
    enrollment_id = lane.enroll(status=EnrollmentStatus.PAUSED)
    assert lane.claim(enrollment_id).reasons == (Skip.GUARD_EXCLUDED, "enrollment_not_active")


@pytest.mark.parametrize("starts_at", [None, NOW + timedelta(minutes=1)])
def test_a_campaign_that_has_not_started_is_refused(lane: Lane, starts_at: datetime | None) -> None:
    """#338: nothing of a campaign goes before its scheduled start, a prefill included."""
    enrollment_id = lane.enroll()
    _set(lane, Campaign, lane.campaign_id, starts_at=starts_at)
    assert lane.claim(enrollment_id).reasons == (Skip.NOT_STARTED,)
    assert lane.read(
        lambda s, u: ready_to_prefill(s, u, now=NOW, settings=lane.settings, limit=10)
    ) == ([], 0)
    _set(lane, Campaign, lane.campaign_id, starts_at=NOW - timedelta(minutes=1))
    assert lane.claim(enrollment_id).claimed


def test_a_step_not_due_yet_is_refused(lane: Lane) -> None:
    enrollment_id = lane.enroll(next_action_at=NOW + timedelta(minutes=1))
    assert lane.claim(enrollment_id).reasons == (Skip.NOT_DUE,)
    parked = lane.enroll(next_action_at=None)
    assert lane.claim(parked).reasons == (Skip.NOT_DUE,)


def test_an_email_step_is_never_claimed_for_a_prefill(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, channels=(EMAIL, LINKEDIN))
    enrollment_id = lane.enroll()
    assert lane.claim(enrollment_id).reasons == (Refusal.NOT_A_LINKEDIN_STEP,)


def test_outside_the_sending_hours_is_refused(lane: Lane) -> None:
    """#338: a LinkedIn step keeps the sending hours, as an email step does."""
    enrollment_id = lane.enroll(current_step=None)
    _sent_hours(lane, start="09:00", end="12:00")  # NOW is 14:00 UTC
    claim = lane.claim(enrollment_id)
    assert claim.reasons == (Skip.OUTSIDE_SENDING_HOURS,)
    assert claim.detail == "it may go at 2026-09-30T09:00:00+00:00"
    [row], _ = lane.read(
        lambda s, u: ready_to_prefill(s, u, now=NOW, settings=lane.settings, limit=10)
    )
    assert row.held_until is not None and row.held_until.isoformat() == "2026-09-30T09:00:00+00:00"
    assert lane.claim(enrollment_id, now=NOW + timedelta(hours=19)).claimed


def test_the_chosen_start_day_is_exempt_from_the_sending_hours(lane: Lane) -> None:
    """#338: step 1 on the start's local day goes whatever the hour, a prefill included."""
    enrollment_id = lane.enroll()
    _set(lane, Campaign, lane.campaign_id, starts_at=NOW - timedelta(hours=1), start_chosen=True)
    _sent_hours(lane, start="09:00", end="12:00")
    assert lane.claim(enrollment_id).claimed


def test_any_time_spills_a_leftover_to_its_own_time_of_day(lane: Lane) -> None:
    """#338: with "any time", a step due on an earlier day waits for its time today."""
    enrollment_id = lane.enroll(next_action_at=NOW - timedelta(days=1) + timedelta(hours=2))
    _sent_hours(lane, any_time=True)
    assert lane.claim(enrollment_id).reasons == (Skip.SPILLED,)
    assert lane.claim(enrollment_id, now=NOW + timedelta(hours=2)).claimed


def _sent_hours(lane: Lane, **fields: Any) -> None:
    values: dict[str, Any] = {
        "enabled": not fields.pop("any_time", False),
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "start": "09:00",
        "end": "17:00",
    }
    values.update(fields)
    lane.write(lambda s, u: sending_hours.write(s, u, **values))


def test_a_reply_ends_the_enrollment(lane: Lane) -> None:
    enrollment_id = lane.enroll()

    def reply(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(
            session,
            enrollment,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            sent_at=NOW - timedelta(hours=1),
        )

    lane.write(reply)
    assert lane.claim(enrollment_id).reasons == (Skip.ENDED, Skip.REPLIED)
    enrollment = lane.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.REPLIED, None)


def test_another_waiting_message_parks_it(session_factory: sessionmaker[Session]) -> None:
    lane = make_lane(session_factory, channels=(EMAIL, LINKEDIN))
    enrollment_id = lane.enroll(current_step=1)

    def drafted(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(
            session, enrollment, position=1, status=MessageStatus.DRAFTED, sent_at=None
        )

    lane.write(drafted)
    assert lane.claim(enrollment_id).reasons == (Skip.WAITING_ON_UNSENT,)
    assert lane.enrollment(enrollment_id).next_action_at is None


def test_the_cadence_defers_a_follow_up(session_factory: sessionmaker[Session]) -> None:
    lane = make_lane(session_factory, channels=(EMAIL, LINKEDIN))
    enrollment_id = lane.enroll(current_step=1)
    _sent_step_one(lane, enrollment_id, at=NOW - timedelta(days=2))  # step 2 waits 7 days
    assert lane.claim(enrollment_id).reasons == (Skip.NOT_DUE,)
    due = lane.enrollment(enrollment_id).next_action_at
    assert due is not None and due >= NOW + timedelta(days=4)
    assert lane.claim(enrollment_id, now=due).claimed


def test_a_later_step_with_nothing_sent_before_it_is_parked(lane: Lane) -> None:
    enrollment_id = lane.enroll(current_step=1)
    assert lane.claim(enrollment_id).reasons == (Skip.WAITING_ON_UNSENT,)
    assert lane.enrollment(enrollment_id).next_action_at is None


def test_a_slug_only_contact_is_excluded_by_the_guard(lane: Lane) -> None:
    enrollment_id = lane.enroll(contact={"li_urn": None})
    claim = lane.claim(enrollment_id)
    assert claim.reasons == (Skip.GUARD_EXCLUDED, Reason.NO_LINKEDIN.value)
    assert lane.messages(enrollment_id) == []
    # Re-checked a day later, inside the sending hours.
    assert lane.enrollment(enrollment_id).next_action_at == NOW + timedelta(days=1)


def test_a_do_not_contact_contact_ends_opted_out(lane: Lane) -> None:
    enrollment_id = lane.enroll(contact={"do_not_contact": True})
    assert lane.claim(enrollment_id).reasons[0] == Skip.ENDED
    assert lane.enrollment(enrollment_id).status is EnrollmentStatus.OPTED_OUT


def test_outside_active_hours_is_refused(lane: Lane) -> None:
    """Spec 9.5: ``[linkedin] active_hours`` (08:30 to 21:30 here), whatever the sending
    hours allow."""
    late = NOW.replace(hour=22)
    enrollment_id = lane.enroll(next_action_at=late - timedelta(minutes=1))
    _sent_hours(lane, any_time=True)
    claim = lane.claim(enrollment_id, now=late)
    assert claim.reasons == (Refusal.OUTSIDE_ACTIVE_HOURS,)
    assert claim.detail is not None and "active hours" in claim.detail
    assert lane.messages(enrollment_id) == []


def test_unreadable_active_hours_refuse(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    broken = replace(
        lane.settings, linkedin=LinkedInSettings(timezone="UTC", active_hours=("late", "early"))
    )
    assert lane.claim(enrollment_id, settings=broken).reasons == (Refusal.BAD_ACTIVE_HOURS,)


def test_the_budget_at_zero_is_refused(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    zero = replace(
        lane.settings,
        linkedin=LinkedInSettings(timezone="UTC", budget=BudgetSettings(li_prefills_per_day=0)),
    )
    assert lane.claim(enrollment_id, settings=zero).reasons == ("browser_out_of_budget",)
    no_visits = replace(
        lane.settings,
        linkedin=LinkedInSettings(timezone="UTC", budget=BudgetSettings(profile_visits_per_day=0)),
    )
    assert lane.claim(enrollment_id, settings=no_visits).reasons == ("browser_out_of_budget",)


def test_a_spent_budget_is_refused(lane: Lane) -> None:
    enrollment_id = lane.enroll()

    def spend(session: Session, user: User) -> None:
        account = ensure_account(session, user).id
        for _ in range(10):
            budgets.consume(
                session,
                user,
                account,
                budgets.ActionClass.LI_PREFILLS,
                now=NOW,
                settings=lane.settings.linkedin.budget,
            )

    lane.write(spend)
    assert lane.claim(enrollment_id).reasons == ("browser_out_of_budget",)


def test_a_flagged_session_is_refused(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    lane.write(
        lambda s, u: flag_session(s, u, Outcome.CHECKPOINT, url="https://www.linkedin.com/x")
    )
    assert lane.claim(enrollment_id).reasons == ("browser_unhealthy",)


def test_heat_over_its_threshold_is_refused(lane: Lane) -> None:
    enrollment_id = lane.enroll()

    def warm(session: Session, user: User) -> None:
        account = ensure_account(session, user).id
        for _ in range(3):
            heat.raise_heat(session, user, account, now=NOW, settings=lane.settings.linkedin.heat)

    lane.write(warm)
    assert lane.claim(enrollment_id).reasons == ("browser_unhealthy",)


def test_no_evidence_of_a_session_is_refused(session_factory: sessionmaker[Session]) -> None:
    lane = make_lane(session_factory, evidence=False)
    enrollment_id = lane.enroll()
    assert lane.claim(enrollment_id).reasons == ("browser_unknown",)

    def logged_out(session: Session, user: User) -> None:
        record_session_evidence(session, user, logged_in=False, source="preflight", now=NOW)

    lane.write(logged_out)
    assert lane.claim(enrollment_id).reasons == ("browser_unhealthy",)


def test_a_template_with_lint_errors_parks_it(lane: Lane) -> None:
    enrollment_id = lane.enroll()

    def broken(session: Session, user: User) -> None:
        campaign = get_scoped(session, user, Campaign, lane.campaign_id)
        assert campaign is not None
        campaign.steps[0].template.body = "Hi {{ no_such_field }}"

    lane.write(broken)
    assert lane.claim(enrollment_id).reasons == (Skip.TEMPLATE_ERRORS,)
    assert lane.enrollment(enrollment_id).next_action_at is None


# --- prefill next -----------------------------------------------------------------------


def test_prefill_next_claims_the_oldest_ready_one(lane: Lane) -> None:
    newer = lane.enroll(next_action_at=NOW - timedelta(minutes=1))
    excluded = lane.enroll(next_action_at=NOW - timedelta(hours=3), contact={"li_urn": None})
    oldest = lane.enroll(next_action_at=NOW - timedelta(hours=2))
    claim = lane.write(lambda s, u: claim_next(s, u, now=NOW, settings=lane.settings))
    assert claim is not None and claim.enrollment_id == oldest
    assert lane.messages(newer) == [] and lane.messages(excluded) == []


def test_prefill_next_stops_at_a_refusal_about_the_user(lane: Lane) -> None:
    first, second = lane.enroll(), lane.enroll()
    lane.prefill(first)
    claim = lane.write(lambda s, u: claim_next(s, u, now=NOW, settings=lane.settings))
    assert claim is not None and claim.reasons == (Refusal.PREFILL_OPEN,)
    assert lane.messages(second) == []


def test_prefill_next_with_nothing_ready(lane: Lane) -> None:
    assert lane.write(lambda s, u: claim_next(s, u, now=NOW, settings=lane.settings)) is None


# --- recording the outcome --------------------------------------------------------------


def test_prefilled_counts_the_step_and_waits_for_the_send(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, channels=(LINKEDIN, EMAIL))
    enrollment_id = lane.enroll()
    message_id = lane.prefill(enrollment_id)

    message = lane.message(message_id)
    assert message is not None
    assert (message.status, message.prefilled_at, message.sent_at) == (
        MessageStatus.PREFILLED,
        NOW,
        None,
    )
    assert message.li_conversation_urn == "urn:li:msg_conversation:TEST1"
    enrollment = lane.enrollment(enrollment_id)
    assert (enrollment.current_step, enrollment.next_action_at) == (1, None)
    [waiting], total = lane.read(lambda s, u: waiting_for_you(s, u, limit=10))
    assert (waiting.message.id, total) == (message_id, 1)
    # Recorded once: a second outcome for it changes nothing.
    assert not lane.record(message_id, MessageOutcomeKind.UNKNOWN)


@pytest.mark.parametrize("kind", [MessageOutcomeKind.NOT_TYPED, MessageOutcomeKind.TOO_LONG])
def test_nothing_typed_gives_the_claim_back_and_two_in_a_row_park_it(
    lane: Lane, kind: MessageOutcomeKind
) -> None:
    enrollment_id = lane.enroll()
    claim = lane.claim(enrollment_id)
    assert claim.message_id is not None
    assert lane.record(claim.message_id, kind)
    lane.finish_runs()

    assert lane.messages(enrollment_id) == []  # the row goes: the step is free again
    enrollment = lane.enrollment(enrollment_id)
    assert enrollment.not_sent_count == 1
    assert enrollment.not_sent_error == f"{kind.value}: fixed words"
    assert enrollment.next_action_at == NOW + timedelta(minutes=15)

    later = NOW + timedelta(minutes=15)
    again = lane.claim(enrollment_id, now=later)
    assert again.claimed and again.message_id is not None
    lane.record(again.message_id, kind, now=later)
    enrollment = lane.enrollment(enrollment_id)
    assert (enrollment.not_sent_count, enrollment.next_action_at) == (2, None)  # parked
    assert lane.messages(enrollment_id) == []


@pytest.mark.parametrize("kind", [MessageOutcomeKind.PARTIALLY_TYPED, MessageOutcomeKind.UNKNOWN])
def test_partly_typed_fails_and_parks_and_is_never_retyped(
    lane: Lane, kind: MessageOutcomeKind
) -> None:
    enrollment_id = lane.enroll()
    claim = lane.claim(enrollment_id)
    assert claim.message_id is not None
    lane.record(claim.message_id, kind)
    lane.finish_runs()

    [message] = lane.messages(enrollment_id)
    assert (message.status, message.error) == (
        MessageStatus.FAILED,
        f"{kind.value}: fixed words",
    )
    assert lane.enrollment(enrollment_id).next_action_at is None
    _set(lane, Enrollment, enrollment_id, next_action_at=NOW)
    assert lane.claim(enrollment_id).reasons == (Skip.STEP_ALREADY_SENT,)


def test_a_success_clears_the_count_of_tries(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    claim = lane.claim(enrollment_id)
    assert claim.message_id is not None
    lane.record(claim.message_id, MessageOutcomeKind.NOT_TYPED)
    lane.finish_runs()
    later = NOW + timedelta(minutes=15)
    again = lane.claim(enrollment_id, now=later)
    assert again.message_id is not None
    lane.record(again.message_id, MessageOutcomeKind.PREFILLED, now=later)
    enrollment = lane.enrollment(enrollment_id)
    assert (enrollment.not_sent_count, enrollment.not_sent_error) == (0, None)


# --- stale ------------------------------------------------------------------------------


def _tick(lane: Lane, now: datetime) -> engine.TickResult:
    [result] = [
        r
        for r in run_tick(
            lane.factory,
            settings=lane.settings,
            sender=FakeSender(now=now),
            clock=lambda: now,
            rng=random.Random(1),
        )
        if r.user_id == lane.user_id
    ]
    return result


def test_a_prefill_goes_stale_at_exactly_three_days(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    message_id = lane.prefill(enrollment_id)

    _tick(lane, NOW + timedelta(days=3) - timedelta(seconds=1))
    message = lane.message(message_id)
    assert message is not None and message.status is MessageStatus.PREFILLED

    _tick(lane, NOW + timedelta(days=3))
    message = lane.message(message_id)
    assert message is not None and message.status is MessageStatus.STALE
    assert lane.enrollment(enrollment_id).next_action_at is None  # still parked
    [waiting], _ = lane.read(lambda s, u: waiting_for_you(s, u, limit=10))
    assert waiting.message.status is MessageStatus.STALE
    # A stale message is no longer an open prefill: the next one may go.
    other = lane.enroll(next_action_at=NOW + timedelta(days=3))
    assert lane.claim(other, now=NOW + timedelta(days=3, minutes=1)).claimed


def test_stale_marks_only_prefilled_linkedin_messages(lane: Lane) -> None:
    enrollment_id = lane.enroll()

    def others(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        for status in (MessageStatus.SCHEDULED, MessageStatus.SENT, MessageStatus.DRAFTED):
            factories.make_message(
                session, enrollment, status=status, prefilled_at=NOW - timedelta(days=9)
            )

    lane.write(others)
    assert lane.write(lambda s, u: engine.mark_stale(s, u, now=NOW)) == 0


# --- discard ----------------------------------------------------------------------------


def test_discard_counts_the_step_and_moves_the_enrollment_on(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, channels=(EMAIL, LINKEDIN, EMAIL))
    enrollment_id = lane.enroll(current_step=1)
    sent_at = NOW - timedelta(days=8)
    _sent_step_one(lane, enrollment_id, at=sent_at)
    message_id = lane.prefill(enrollment_id)

    discarded = lane.write(lambda s, u: discard(s, u, message_id, settings=lane.settings))
    assert discarded.status is MessageStatus.DISCARDED
    enrollment = lane.enrollment(enrollment_id)
    assert enrollment.current_step == 2
    assert enrollment.status is EnrollmentStatus.ACTIVE
    assert enrollment.next_action_at is not None and enrollment.next_action_at > sent_at
    # Never twice: step 2 has its message.
    assert lane.read(
        lambda s, u: engine.step_has_message(s, u, enrollment_id, discarded.step_id or 0)
    )


def test_discarding_the_last_step_completes(lane: Lane) -> None:
    lane_one = lane
    enrollment_id = lane_one.enroll(current_step=1)
    _sent_step_one(lane_one, enrollment_id, at=NOW - timedelta(days=8))
    message_id = lane_one.prefill(enrollment_id)
    lane_one.write(lambda s, u: discard(s, u, message_id, settings=lane_one.settings))
    enrollment = lane_one.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.current_step) == (EnrollmentStatus.COMPLETED, 2)


def test_discard_refuses_what_does_not_wait_for_the_person(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    claim = lane.claim(enrollment_id)
    assert claim.message_id is not None
    # Claimed, its run still running: the composer may be being typed into.
    with pytest.raises(linkedin_steps.PrefillNotWaiting):
        lane.write(lambda s, u: discard(s, u, claim.message_id or 0, settings=lane.settings))
    with pytest.raises(LookupError):
        lane.write(lambda s, u: discard(s, u, 999_999, settings=lane.settings))
    # Its run ended without recording anything (a crash): a person may let it go.
    lane.finish_runs()
    lane.write(lambda s, u: discard(s, u, claim.message_id or 0, settings=lane.settings))
    message = lane.message(claim.message_id)
    assert message is not None and message.status is MessageStatus.DISCARDED


# --- the guard and enrichment -----------------------------------------------------------


def test_the_linkedin_guard_needs_the_urn(lane: Lane) -> None:
    def verdicts(session: Session, user: User) -> list[tuple[str, ...]]:
        campaign = get_scoped(session, user, Campaign, lane.campaign_id)
        assert campaign is not None
        out = []
        missing: tuple[dict[str, Any], ...] = ({"li_urn": None}, {"li_public_id": None})
        for fields in missing:
            contact = factories.make_contact(session, user, **fields)
            enrollment = factories.make_enrollment(session, campaign, contact)
            out.append(
                tuple(
                    r.value
                    for r in check_step(
                        session, user, enrollment, campaign.steps[0], now=NOW
                    ).reasons
                )
            )
        return out

    assert lane.write(verdicts) == [("no_linkedin",), ()]


def test_enrollment_asks_enrichment_for_contacts_without_a_urn(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, channels=(EMAIL, LINKEDIN), status=CampaignStatus.DRAFT)

    def run(session: Session, user: User) -> dict[str, int]:
        slug_only = factories.make_contact(session, user, emails=["a@example.test"], li_urn=None)
        asked = factories.make_contact(
            session, user, emails=["b@example.test"], li_urn=None, enrich_priority=5
        )
        known = factories.make_contact(session, user, emails=["c@example.test"])
        no_email = factories.make_contact(session, user, li_urn=None)  # kept out for email
        enroll(
            session,
            user,
            lane.campaign_id,
            [slug_only.id, asked.id, known.id, no_email.id],
            now=NOW,
        )
        session.flush()
        return {
            name: contact.enrich_priority
            for name, contact in (
                ("slug_only", slug_only),
                ("asked", asked),
                ("known", known),
                ("no_email", no_email),
            )
        }

    assert lane.write(run) == {"slug_only": 1, "asked": 5, "known": 0, "no_email": 0}


def test_a_linkedin_first_campaign_asks_for_the_contacts_it_kept_out(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, channels=(LINKEDIN,), status=CampaignStatus.DRAFT)

    def run(session: Session, user: User) -> tuple[int, int]:
        slug_only = factories.make_contact(session, user, li_urn=None)
        result = enroll(session, user, lane.campaign_id, [slug_only.id], now=NOW)
        session.flush()
        return len(result.enrolled), slug_only.enrich_priority

    assert lane.write(run) == (0, 1)  # waits for enrichment, not enrolled


def test_an_email_only_campaign_asks_for_nothing(session_factory: sessionmaker[Session]) -> None:
    lane = make_lane(session_factory, channels=(EMAIL,), status=CampaignStatus.DRAFT)

    def run(session: Session, user: User) -> int:
        contact = factories.make_contact(session, user, emails=["d@example.test"], li_urn=None)
        enroll(session, user, lane.campaign_id, [contact.id], now=NOW)
        session.flush()
        return contact.enrich_priority

    assert lane.write(run) == 0


# --- the tick ---------------------------------------------------------------------------


def test_the_tick_lists_linkedin_steps_and_never_claims_one(lane: Lane) -> None:
    enrollment_id = lane.enroll(next_action_at=NOW - timedelta(hours=1))
    result = _tick(lane, NOW)
    assert result.fired == []
    assert result.skipped()[enrollment_id] == (Skip.READY_TO_PREFILL,)
    assert lane.messages(enrollment_id) == []
    assert lane.runs() == []  # no message_send run, ever, from the tick
    assert lane.enrollment(enrollment_id).next_action_at == NOW - timedelta(hours=1)
    [row], total = lane.read(
        lambda s, u: ready_to_prefill(s, u, now=NOW, settings=lane.settings, limit=10)
    )
    assert (row.enrollment.id, row.held_until, total) == (enrollment_id, None, 1)


def test_a_linkedin_due_time_wakes_the_tick(lane: Lane) -> None:
    due = NOW + timedelta(hours=3)
    lane.enroll(next_action_at=due)
    assert _tick(lane, NOW).next_wake == due


def test_the_engine_imports_nothing_of_the_browser_or_the_prefill() -> None:
    """The tick never imports browser code, nor the person-triggered claim."""
    tree = ast.parse(Path(engine.__file__ or "").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not [name for name in imported if name.startswith("netkeeper.linkedin")]
    assert not [name for name in imported if "linkedin_steps" in name]
    assert "netkeeper.services.runs" not in imported


def test_the_dashboard_lists_linkedin_rows_and_posture_does_not(lane: Lane) -> None:
    linkedin = lane.enroll(next_action_at=NOW + timedelta(hours=1))
    fires, total = lane.read(lambda s, u: engine.upcoming(s, u, limit=10, include_linkedin=True))
    assert [(f.enrollment.id, f.ready_to_prefill) for f in fires] == [(linkedin, True)]
    assert total == 1
    assert lane.read(lambda s, u: engine.upcoming(s, u, limit=10)) == ([], 0)


def test_waiting_lists_only_the_users_linkedin_messages(lane: Lane) -> None:
    enrollment_id = lane.enroll()
    lane.prefill(enrollment_id)
    rows, total = lane.read(lambda s, u: waiting_for_you(s, u, limit=10))
    assert total == 1 and rows[0].contact.id == lane.enrollment(enrollment_id).contact_id
    count = lane.read(lambda s, u: s.scalar(unscoped(select(func.count()).select_from(Message))))
    assert count == 1
