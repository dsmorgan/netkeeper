"""Replay the campaign engine's minute tick against a virtual clock (P3-06's "done when").

:func:`simulate_campaign` drives :func:`netkeeper.services.campaign_engine.run_tick`
through weeks of campaign time in one process, with no sleeping and no network:
a :class:`SimulatedSender` stands in for Gmail and says each message went out a
random few minutes after it was handed over, so the next step's timing has to
come from that ``sent_at`` and not from the fire time.

**Minute ticks, without every minute.** The real loop ticks every minute. Here
a tick that fired is followed by one a minute later, as in ``serve``; a tick
that did not fire jumps to its ``next_wake`` (the earliest due time, the end of
the spacing, or the next local day when a cap is reached), rounded up to a
whole minute, which is when the real loop would next have something to do.
Every other minute in between is a tick that would have done nothing.

Deterministic for a ``seed``. ``max_ticks`` bounds the loop, so a schedule that
stops moving fails fast instead of hanging.

``netkeeper simulate --campaign ID`` (P3-13) drives it through
:func:`campaign_shape` and :func:`simulate_schedule`: the campaign's shape (its
steps, delays, times of day, modes, caps and audience size) is read from the real
database, and the replay runs in a throwaway one built for the purpose, with
synthetic contacts, as if activated to start at the replay's start (#338). The
real database is only read; nothing is sent.
"""

from __future__ import annotations

import itertools
import logging
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import schedule
from netkeeper.config import Settings
from netkeeper.crm.inbox_apply import apply_delta
from netkeeper.db import session_scope
from netkeeper.linkedin.messaging import MessageOutcome, MessageOutcomeKind
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    ContactEmail,
    Enrollment,
    EnrollmentStatus,
    Mailbox,
    MailboxStatus,
    Message,
    MessageDirection,
    StepCondition,
    StepMode,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    Template,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import runs
from netkeeper.services.campaign_engine import (
    Firing,
    Sender,
    SendOutcome,
    SendResult,
    run_tick,
)
from netkeeper.services.campaign_review import source_contact_ids
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import record_session_evidence
from netkeeper.services.linkedin_steps import claim_next, record_prefill_outcome
from netkeeper.services.simulate_inbox import (
    SimulatedInboxMessage,
    simulated_conversation_urn,
    simulated_delta,
)
from netkeeper.services.simulate_run import scratch_database

MAX_TICKS: Final = 20_000
"""The stall guard: three weeks of a 100-contact campaign takes well under a thousand."""

LATENCY_S: Final = (30.0, 120.0)
"""How long after the hand-over the simulated send goes out, in seconds."""


@dataclass
class SimulatedSender:
    """Sends everything, a random ``latency_s`` after the virtual ``now``."""

    rng: random.Random
    latency_s: tuple[float, float] = LATENCY_S
    now: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    firings: list[Firing] = field(default_factory=list)

    def send(self, firing: Firing) -> SendResult:
        self.firings.append(firing)
        at = self.now + timedelta(seconds=self.rng.uniform(*self.latency_s))
        return SendResult(
            SendOutcome.SENT,
            at=at,
            gmail_message_id=f"sim-{firing.message_id}",
            gmail_thread_id=f"sim-thread-{firing.enrollment_id}",
        )


@dataclass(frozen=True, slots=True)
class CampaignSimulation:
    firings: tuple[Firing, ...]
    ticks: int
    ended_at: datetime


# --- LinkedIn steps and replies, simulated (P4-02, #381) ------------------------------------

SEND_AFTER_PREFILL_S: Final = (60.0, 600.0)
"""How long after a prefill the simulated person sends it, in seconds."""

PREFILL_RETRY: Final = timedelta(minutes=15)
"""How soon a replay asks again for a LinkedIn step it could not prefill yet (LinkedIn's
active hours, the sending hours)."""


@dataclass
class SimulatedLinkedIn:
    """The person and the LinkedIn inbox, for a replay. Nothing touches a browser.

    After each tick, a LinkedIn step that is ready is claimed through the real claim
    (:func:`~netkeeper.services.linkedin_steps.claim_next`: one open prefill at a time,
    LinkedIn's active hours, the sending hours) and recorded ``prefilled``. The replay
    spends no ``li_prefills`` budget (the prefill run spends it, P4-03), so it does not
    show the day's cap. The
    simulated person sends it a random ``send_after_s`` later, and the next inbox read
    shows it: the real apply (:func:`~netkeeper.crm.inbox_apply.apply_delta`) and its
    reply hook confirm it ``sent``, and the next step counts from then.

    ``replies`` maps a contact's LinkedIn URN to how long after the first message sent
    to them, on any channel, they answer on LinkedIn. The same reply hook records it, so
    a pending step on any channel never fires.
    """

    rng: random.Random
    replies: dict[str, timedelta] = field(default_factory=dict)
    send_after_s: tuple[float, float] = SEND_AFTER_PREFILL_S
    prefilled: list[int] = field(default_factory=list)
    """The message ids prefilled, in order."""
    _inbox: list[tuple[int, SimulatedInboxMessage]] = field(default_factory=list)
    """What the inbox will show, with the user it belongs to."""
    _queued_replies: set[str] = field(default_factory=set)
    _numbers: Iterator[int] = field(default_factory=lambda: itertools.count(1))
    _retry: bool = False

    def before_tick(
        self, factory: sessionmaker[Session], *, settings: Settings, now: datetime
    ) -> None:
        """Queue the replies whose first send happened, then read what the inbox shows."""
        self._queue_replies(factory)
        due = [(owner, item) for owner, item in self._inbox if item.at <= now]
        if not due:
            return
        self._inbox = [(owner, item) for owner, item in self._inbox if item.at > now]
        for user_id in sorted({user_id for user_id, _ in due}):
            delta = simulated_delta(
                (item for owner, item in due if owner == user_id), numbers=self._numbers
            )
            with session_scope(factory, write=True) as session:
                user = session.get_one(User, user_id)
                apply_delta(session, user, delta, polled_at=now, settings=settings)

    def after_tick(
        self, factory: sessionmaker[Session], *, settings: Settings, now: datetime
    ) -> None:
        """Prefill what is ready, as a person asking for "prefill next" would, and queue
        the replies whose first send has happened."""
        self._queue_replies(factory)
        self._retry = False
        with session_scope(factory) as session:
            users = [user.id for user in _local_users(session)]
        for user_id in users:
            with session_scope(factory, write=True) as session:
                user = session.get_one(User, user_id)
                claim = claim_next(session, user, now=now, settings=settings, start_run=_start_run)
                if claim is None:
                    continue
                if not claim.claimed or claim.message_id is None:
                    self._retry = True
                    continue
                message = get_scoped(session, user, Message, claim.message_id)
                assert message is not None
                outcome = MessageOutcome(
                    MessageOutcomeKind.PREFILLED,
                    "simulated",
                    simulated_conversation_urn(message.contact_id),
                    len(message.body_rendered or ""),
                )
                record_prefill_outcome(
                    session, user, claim.message_id, outcome, settings=settings, now=now
                )
                if claim.run_id is not None:
                    runs.finish_run(
                        session, user, claim.run_id, status=SyncRunStatus.COMPLETED, now=now
                    )
                contact = get_scoped(session, user, Contact, message.contact_id)
                assert contact is not None
                self.prefilled.append(claim.message_id)
                sent = SimulatedInboxMessage(
                    at=now + timedelta(seconds=self.rng.uniform(*self.send_after_s)),
                    contact_id=contact.id,
                    counterpart_urn=contact.li_urn or "",
                    outbound=True,
                )
                self._inbox.append((user_id, sent))

    def next_wake(self, now: datetime) -> datetime | None:
        """The next inbox read with something to show, or a retry of a refused prefill."""
        found = [item.at for _, item in self._inbox if item.at > now]
        if self._retry:
            found.append(now + PREFILL_RETRY)
        return min(found) if found else None

    def _queue_replies(self, factory: sessionmaker[Session]) -> None:
        todo = {urn: wait for urn, wait in self.replies.items() if urn not in self._queued_replies}
        if not todo:
            return
        found: list[tuple[int, int, str, datetime]] = []
        with session_scope(factory) as session:
            for user in _local_users(session):
                rows = session.execute(
                    scoped(user, Message)
                    .join(Contact, Contact.id == Message.contact_id)
                    .with_only_columns(Contact.id, Contact.li_urn, func.min(Message.sent_at))
                    .where(
                        Contact.user_id == user.id,
                        Contact.li_urn.in_(todo),
                        Message.direction == MessageDirection.OUT,
                        Message.sent_at.is_not(None),
                    )
                    .group_by(Contact.id, Contact.li_urn)
                ).all()
                found += [(user.id, c, u, at) for c, u, at in rows if u is not None and at]
        for user_id, contact_id, urn, first in found:
            if urn in self._queued_replies:
                continue
            self._queued_replies.add(urn)
            reply = SimulatedInboxMessage(
                at=first + todo[urn], contact_id=contact_id, counterpart_urn=urn, outbound=False
            )
            self._inbox.append((user_id, reply))


def _local_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)))


def _start_run(session: Session, user: User, now: datetime) -> SyncRun:
    """The claim's ``message_send`` run, recorded straight in: a replay has no runner."""
    account = ensure_account(session, user)
    run = SyncRun(
        user_id=user.id,
        linkedin_account_id=account.id,
        kind=SyncRunKind.MESSAGE_SEND,
        status=SyncRunStatus.RUNNING,
        trigger=SyncRunTrigger.MANUAL,
        started_at=now,
    )
    session.add(run)
    session.flush()
    return run


def _next_minute(at: datetime) -> datetime:
    whole = at.replace(second=0, microsecond=0)
    return whole if whole == at else whole + timedelta(minutes=1)


def _frozen(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def simulate_campaign(
    factory: sessionmaker[Session],
    *,
    settings: Settings,
    start: datetime,
    end: datetime,
    seed: int = 0,
    max_ticks: int = MAX_TICKS,
    sender: Sender | None = None,
    linkedin: SimulatedLinkedIn | None = None,
) -> CampaignSimulation:
    """Tick every local user's campaigns from ``start`` until ``end`` or nothing is left to do.

    ``sender`` replaces the :class:`SimulatedSender`: a test passes a Gmail sender over
    :class:`~netkeeper.campaigns.gmail_fake.FakeGmail` to replay replies too (P3-08). It
    reads the virtual time from its ``reconcile``'s ``now``; ``firings`` lists only what
    the default sender was handed.

    ``linkedin`` prefills LinkedIn steps and replays the LinkedIn inbox
    (:class:`SimulatedLinkedIn`, P4-02): its inbox is read before each tick, and what is
    ready is prefilled after it. Without it, a LinkedIn step never fires.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("the simulation's times must be timezone-aware")
    simulated = SimulatedSender(random.Random(seed))  # noqa: S311 -- deterministic replay
    spacing = random.Random(seed + 1)  # noqa: S311 -- deterministic replay
    now = _next_minute(start)
    ticks = 0
    while now < end:
        if ticks >= max_ticks:
            raise RuntimeError(f"the campaign schedule stopped moving after {ticks} ticks")
        ticks += 1
        simulated.now = now
        if linkedin is not None:
            linkedin.before_tick(factory, settings=settings, now=now)
        results = run_tick(
            factory,
            settings=settings,
            sender=sender or simulated,
            clock=_frozen(now),
            rng=spacing,
        )
        prefilled = 0
        if linkedin is not None:
            before = len(linkedin.prefilled)
            linkedin.after_tick(factory, settings=settings, now=now)
            prefilled = len(linkedin.prefilled) - before
        if prefilled or any(result.fired for result in results):
            now += timedelta(minutes=1)
            continue
        wakes = [r.next_wake for r in results if r.next_wake is not None]
        if linkedin is not None and (wake := linkedin.next_wake(now)) is not None:
            wakes.append(wake)
        if not wakes:
            break
        now = _next_minute(max(min(wakes), now + timedelta(minutes=1)))
    return CampaignSimulation(tuple(simulated.firings), ticks, min(now, end))


# --- a real campaign's schedule, replayed in a scratch database (P3-13) -------------

DEFAULT_SCHEDULE_DAYS: Final = 21
"""Three weeks: long enough for spec 11.2's three steps a week apart."""

LIVE_STATUSES: Final = frozenset(
    {EnrollmentStatus.PENDING, EnrollmentStatus.ACTIVE, EnrollmentStatus.PAUSED}
)


@dataclass(frozen=True, slots=True)
class StepShape:
    position: int
    channel: TemplateChannel
    mode: StepMode
    delay_days: int
    condition: StepCondition
    same_thread: bool
    send_time: str | None = None


@dataclass(frozen=True, slots=True)
class CampaignShape:
    """What decides a campaign's schedule, and nothing about its contacts or content."""

    campaign_id: int
    name: str
    status: CampaignStatus
    steps: tuple[StepShape, ...]
    audience: int
    audience_from: str
    """``enrollments`` (its live ones) or ``source`` (its list or filter, nobody enrolled)."""
    daily_cap: int | None
    mailbox_daily_cap: int
    timezone: str
    already_fired: int = 0
    """Outbound messages the real campaign has already fired."""


@contextmanager
def _quiet_engine() -> Iterator[None]:
    """The engine narrates each claim and send at INFO. In a replay the report is the
    output, so its logger is raised to WARNING for the replay and restored after."""
    logger = logging.getLogger(run_tick.__module__)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous)


class InvalidSchedule(ValueError):
    """A campaign or a span that no schedule can be replayed for."""


def campaign_shape(
    session: Session, user: User, campaign_id: int, *, settings: Settings
) -> CampaignShape:
    """Read the shape of ``user``'s campaign. Reads only."""
    campaign = get_scoped(session, user, Campaign, campaign_id)
    if campaign is None:
        raise InvalidSchedule(f"no campaign {campaign_id}")
    steps = tuple(
        StepShape(
            s.position, s.channel, s.mode, s.delay_days, s.condition, s.same_thread, s.send_time
        )
        for s in session.scalars(
            scoped(user, CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position)
        )
    )
    if not steps:
        raise InvalidSchedule(f"campaign {campaign_id} has no steps")
    live = session.scalar(
        scoped(user, Enrollment)
        .with_only_columns(func.count(Enrollment.id))
        .where(Enrollment.campaign_id == campaign_id, Enrollment.status.in_(LIVE_STATUSES))
    )
    audience, audience_from = live or 0, "enrollments"
    if not audience:
        audience, audience_from = len(source_contact_ids(session, user, campaign)), "source"
    mailbox = (
        None
        if campaign.mailbox_id is None
        else get_scoped(session, user, Mailbox, campaign.mailbox_id)
    )
    fired = session.scalar(
        scoped(user, Message)
        .with_only_columns(func.count(Message.id))
        .join(Enrollment, Enrollment.id == Message.enrollment_id)
        .where(
            Enrollment.user_id == user.id,
            Enrollment.campaign_id == campaign_id,
            Message.direction == MessageDirection.OUT,
        )
    )
    return CampaignShape(
        campaign_id=campaign.id,
        name=campaign.name,
        status=campaign.status,
        steps=steps,
        audience=audience,
        audience_from=audience_from,
        daily_cap=campaign.daily_cap,
        mailbox_daily_cap=(
            settings.campaigns.mailbox_daily_cap if mailbox is None else mailbox.daily_cap
        ),
        timezone=user.timezone,
        already_fired=fired or 0,
    )


@dataclass(frozen=True, slots=True)
class ScheduleDay:
    day: date
    sends: tuple[int, ...]
    """How many fired that local day, per step position (index 0 is step 1)."""


@dataclass(frozen=True, slots=True)
class ScheduleReport:
    shape: CampaignShape
    seed: int
    start: datetime
    days_requested: int
    ended_at: datetime
    ticks: int
    days: tuple[ScheduleDay, ...]
    per_step: tuple[int, ...]
    finished: int
    """Enrollments that fired every step."""


def _seed_scratch(
    session: Session, shape: CampaignShape, *, start: datetime, settings: Settings
) -> int:
    """The shape as an active campaign in the scratch database, with synthetic contacts.

    Written straight in as ``active``: this database is thrown away after the replay,
    so there is no review to pass, and the real campaign's state is never touched.
    """
    user = User(kind=UserKind.LOCAL, display_name="simulated account", timezone=shape.timezone)
    session.add(user)
    session.flush()
    mailbox = Mailbox(
        user_id=user.id,
        email="simulated-mailbox@example.com",
        keychain_ref="simulated",
        daily_cap=shape.mailbox_daily_cap,
        status=MailboxStatus.OK,
    )
    session.add(mailbox)
    session.flush()
    campaign = Campaign(
        user_id=user.id,
        name=f"Simulated {shape.campaign_id}",
        status=CampaignStatus.ACTIVE,
        mailbox_id=mailbox.id,
        daily_cap=shape.daily_cap,
        contacted_within_days_guard=0,
        approved_at=start,
        starts_at=start,
        start_chosen=True,
    )
    for step in shape.steps:
        email = step.channel is TemplateChannel.EMAIL
        template = Template(
            user_id=user.id,
            name=f"Simulated step {step.position}",
            channel=step.channel,
            subject=f"Simulated step {step.position}" if email else None,
            body="Hello {{ first_name }}",
            lint_json=[],
        )
        campaign.steps.append(
            CampaignStep(
                user_id=user.id,
                position=step.position,
                channel=step.channel,
                template=template,
                delay_days=step.delay_days,
                mode=step.mode,
                condition=step.condition,
                same_thread=step.same_thread,
                send_time=step.send_time,
            )
        )
    session.add(campaign)
    session.flush()
    first = shape.steps[0]
    try:
        first_due = schedule.step_due(
            start,
            delay_days=first.delay_days,
            send_time=first.send_time,
            slots=schedule.suggested(settings.campaigns, shape.timezone),
            first=True,
        )
    except schedule.ScheduleError as exc:
        raise InvalidSchedule(str(exc)) from exc
    if any(step.channel is TemplateChannel.LINKEDIN for step in shape.steps):
        # The replay's prefills need an account whose session is known to be signed in.
        ensure_account(session, user)
        record_session_evidence(session, user, logged_in=True, source="simulation", now=start)
    for n in range(1, shape.audience + 1):
        contact = Contact(
            user_id=user.id,
            first_name=f"Sim{n}",
            last_name="Contact",
            li_urn=f"urn:li:fsd_profile:SIMULATED{n}",
        )
        contact.emails.append(
            ContactEmail(user_id=user.id, email=f"sim{n}@example.com", is_primary=True)
        )
        session.add(contact)
        session.flush()
        session.add(
            Enrollment(
                user_id=user.id,
                campaign_id=campaign.id,
                contact_id=contact.id,
                status=EnrollmentStatus.ACTIVE,
                next_action_at=first_due,
            )
        )
    session.flush()
    return user.id


def simulate_schedule(
    shape: CampaignShape,
    *,
    settings: Settings,
    start: datetime,
    days: int = DEFAULT_SCHEDULE_DAYS,
    seed: int = 0,
) -> ScheduleReport:
    """Replay ``shape`` from its first step, for ``days`` from ``start``, in a scratch
    database deleted again before this returns. Nothing is sent: the sender is
    :class:`SimulatedSender`, which says every step went out, drafts included, and
    LinkedIn steps are prefilled and sent by :class:`SimulatedLinkedIn`."""
    if days < 1:
        raise InvalidSchedule(f"days must be at least 1, got {days}")
    if start.tzinfo is None:
        raise InvalidSchedule("the start must be timezone-aware")
    if shape.audience < 1:
        raise InvalidSchedule(
            f"campaign {shape.campaign_id} has nobody to simulate: enroll an audience first"
        )
    zone = ZoneInfo(shape.timezone)
    with scratch_database() as factory, _quiet_engine():
        with session_scope(factory, write=True) as session:
            user_id = _seed_scratch(session, shape, start=start, settings=settings)
        run = simulate_campaign(
            factory,
            settings=settings,
            start=start,
            end=start + timedelta(days=days),
            seed=seed,
            linkedin=SimulatedLinkedIn(random.Random(seed + 2)),  # noqa: S311 -- replay
        )
        with session_scope(factory) as session:
            user = session.get_one(User, user_id)
            fired = session.execute(
                scoped(user, Message)
                .with_only_columns(
                    Message.scheduled_at, CampaignStep.position, Message.enrollment_id
                )
                .join(CampaignStep, CampaignStep.id == Message.step_id)
                .where(
                    CampaignStep.user_id == user.id,
                    Message.direction == MessageDirection.OUT,
                )
                .order_by(Message.scheduled_at)
            ).all()
    width = len(shape.steps)
    by_day: dict[date, list[int]] = {}
    per_step = [0] * width
    steps_of: dict[int, set[int]] = {}
    for at, position, enrollment_id in fired:
        if at is None:
            continue
        counts = by_day.setdefault(at.astimezone(zone).date(), [0] * width)
        counts[position - 1] += 1
        per_step[position - 1] += 1
        steps_of.setdefault(enrollment_id, set()).add(position)
    return ScheduleReport(
        shape=shape,
        seed=seed,
        start=start,
        days_requested=days,
        ended_at=run.ended_at,
        ticks=run.ticks,
        days=tuple(ScheduleDay(d, tuple(c)) for d, c in sorted(by_day.items())),
        per_step=tuple(per_step),
        finished=sum(1 for s in steps_of.values() if len(s) == width),
    )


def render_schedule(report: ScheduleReport) -> str:
    """The CLI's output for ``netkeeper simulate --campaign``."""
    shape = report.shape
    zone = ZoneInfo(shape.timezone)
    lines = [
        f"campaign {shape.campaign_id} {shape.name!r} ({shape.status.value}):"
        f" {shape.audience} contacts from its {shape.audience_from},"
        f" {len(shape.steps)} steps, replayed from step 1",
        f"{report.days_requested} simulated days from"
        f" {report.start.astimezone(zone):%Y-%m-%d %H:%M} {shape.timezone}, seed {report.seed}",
        "steps: "
        + "; ".join(
            f"{s.position} {s.channel.value} {s.mode.value} +{s.delay_days}d"
            f"{'' if s.send_time is None else ' at ' + s.send_time} {s.condition.value}"
            for s in shape.steps
        ),
        f"caps: campaign {'config' if shape.daily_cap is None else shape.daily_cap},"
        f" mailbox {shape.mailbox_daily_cap} per day",
        "",
    ]
    if shape.status in (CampaignStatus.ACTIVE, CampaignStatus.PAUSED) and shape.already_fired:
        lines[-1:-1] = [
            f"warning: this campaign is {shape.status.value} and partway through"
            f" ({shape.already_fired} messages fired so far); the replay starts every contact"
            " at step 1, so it shows the whole schedule again, not what is left of it",
        ]
    if report.days:
        headers = ("DATE", "DAY", *(f"STEP {s.position}" for s in shape.steps), "TOTAL")
        rows = [
            (f"{d.day:%Y-%m-%d}", f"{d.day:%a}", *(str(n) for n in d.sends), str(sum(d.sends)))
            for d in report.days
        ]
        widths = [max(len(c) for c in col) for col in zip(headers, *rows, strict=True)]
        for row in (headers, *rows):
            lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
        lines.append("days not listed send nothing")
    else:
        lines.append("nothing fires in the simulated days")
    lines += [
        "",
        "per step: " + ", ".join(f"step {i} {n}" for i, n in enumerate(report.per_step, 1)),
        f"{report.finished} of {shape.audience} contacts got every step",
    ]
    if any(s.channel is TemplateChannel.LINKEDIN for s in shape.steps):
        lines.append(
            "LinkedIn steps are prefilled one at a time, within LinkedIn's active hours, and the"
            " next step counts from when the replay sends each, minutes later; in use, each"
            " waits for you to send it, and the day's prefill budget also limits them"
        )
    if any(s.mode is StepMode.DRAFT for s in shape.steps):
        lines.append(
            "draft steps count as sent the moment they are drafted; in use, each waits for you"
            " to send it, and the next step counts from then"
        )
    lines.append(
        "no replies, bounces or guard exclusions are simulated; a scratch database was used"
        " and deleted, nothing was sent, and your campaign is unchanged"
    )
    return "\n".join(lines) + "\n"
