"""Dry-run :mod:`netkeeper.services.scheduler` against a virtual clock.

The scheduler's job is entirely about *when* -- restart safety, catch-up after
downtime, reschedule-only-on-change, the interleave gap -- and none of that is
visible from a single call to any one function. Watching it emerge against a
real clock takes days per scenario and a multi-day outage takes a multi-day
test. This replays it instead: :func:`simulate` drives a `datetime` forward by
hand, in one process, with no sleeping, no browser, no network, and no real
APScheduler loop, so a fortnight of schedule -- or a synthetic three-day outage
-- costs a handful of database round trips and runs in well under a second.
Deterministic for a given ``seed``, so a change in behavior shows up as a diff
in the fires it records, not a vibe.

**Event-driven, not polled every minute.** Each iteration reads every job
kind's currently persisted due time, jumps the virtual clock straight to the
earliest one, and fires whatever has become due at that instant -- it never
steps minute by minute through however many days ``end - start`` spans. This
also means it terminates: every persisted due time, whether fresh, restored,
caught up, or just advanced by a fire, is always strictly later than the
``now`` it was computed against (see ``scheduler.compute_due`` and
``record_fired``), so the virtual clock is monotonically increasing and the
loop cannot stall.

**A restart is not a special code path.** All of the scheduler's state lives
in ``settings_kv``, keyed by ``(account_id, kind)`` -- nothing about *this*
module or ``scheduler.py`` holds state in a Python object that would need
special handling to survive one call ending and another beginning. So proving
restart safety is exactly what it sounds like: call :func:`simulate` once for
``[start, mid)``, again for ``[mid, end)`` against a *fresh* ``sessionmaker``
bound to the same on-disk database (a real "new process, same file" restart,
not a reused connection), and compare the combined fires to one continuous
call across ``[start, end)``. ``tests/test_simulate.py`` does exactly this.

**Downtime** is the ``downtime=(down_start, down_end)`` window: when a jump
would land inside it, the clock is fanned straight to ``down_end`` without
firing anything, and :func:`scheduler.sync_account_schedule` is re-run there
-- the "process restarts after an outage" moment, and the only place catch-up
detection can fire (see ``scheduler``'s module docstring, "two clocks"). A due
time that already passed by then gets exactly one catch-up fire, 5 to 20
minutes later; the interval then resumes counted from *that* fire, not from
whatever the original due time was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from random import Random

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import HeatSettings
from netkeeper.db import session_scope
from netkeeper.linkedin import pacing
from netkeeper.models import User
from netkeeper.services import scheduler as sched


@dataclass(frozen=True, slots=True)
class SimFire:
    """One fire (or skip) the simulation observed, at the virtual instant it happened."""

    at: datetime
    kind: sched.JobKind
    fired: bool
    skipped_reason: str | None
    is_catchup: bool


@dataclass
class SimResult:
    fires: list[SimFire] = field(default_factory=list)

    def count(self, kind: sched.JobKind | None = None, *, fired_only: bool = True) -> int:
        """How many fires match ``kind`` (or all kinds). ``fired_only=False`` also
        counts heat-skipped fires (the schedule slot still happened; the handler
        just didn't run)."""
        return sum(
            1
            for f in self.fires
            if (kind is None or f.kind == kind) and (fired_only is False or f.fired)
        )

    def catchups(self, kind: sched.JobKind) -> list[SimFire]:
        """The fired catch-up events for ``kind`` -- should never be more than one
        per downtime window (spec: "once")."""
        return [f for f in self.fires if f.kind == kind and f.fired and f.is_catchup]


async def simulate(
    session_factory: sessionmaker[Session],
    user: User,
    account_id: int,
    *,
    start: datetime,
    end: datetime,
    seed: int = 0,
    registry: sched.JobRegistry | None = None,
    schedules: dict[sched.JobKind, sched.JobSchedule] | None = None,
    active_start: time = pacing.DEFAULT_ACTIVE_START,
    active_end: time = pacing.DEFAULT_ACTIVE_END,
    heat_settings: HeatSettings | None = None,
    downtime: tuple[datetime, datetime] | None = None,
) -> SimResult:
    """Replay the schedule for ``(user, account_id)`` from ``start`` to ``end``.

    ``downtime`` simulates the process being down for ``[down_start, down_end)``:
    nothing is polled during that window regardless of what became due inside
    it, and the schedule is re-established (see the module docstring) the
    moment it ends -- the same "restart" moment a real process going through
    :func:`scheduler.build_scheduler` again would hit.
    """
    rng = Random(seed)  # noqa: S311 -- deterministic replay, not crypto
    registry = registry if registry is not None else sched.default_registry()
    schedules = schedules if schedules is not None else dict(sched.DEFAULT_SCHEDULES)
    result = SimResult()
    now = start
    tz = user.timezone

    def _boot(at: datetime) -> None:
        with session_scope(session_factory, write=True) as session:
            sched.sync_account_schedule(
                session,
                user,
                account_id,
                now=at,
                schedules=schedules,
                rng=rng,
                tz=tz,
                active_start=active_start,
                active_end=active_end,
            )

    _boot(now)  # "process start"

    while now < end:
        with session_scope(session_factory) as session:
            dues = {
                kind: due
                for kind in schedules
                if (due := sched.stored_due(session, user, account_id, kind)) is not None
            }
        if not dues:
            break
        next_due = min(dues.values())
        if next_due >= end:
            break
        now = max(next_due, now)

        if downtime is not None and downtime[0] <= now < downtime[1]:
            now = downtime[1]
            _boot(now)  # "process restart" after the outage
            continue

        fired = await sched.poll_once(
            session_factory,
            [(user, account_id)],
            now=now,
            registry=registry,
            schedules=schedules,
            heat_settings=heat_settings,
            active_start=active_start,
            active_end=active_end,
        )
        result.fires.extend(
            SimFire(
                at=now,
                kind=outcome.kind,
                fired=outcome.fired,
                skipped_reason=outcome.skipped_reason,
                is_catchup=outcome.is_catchup,
            )
            for outcome in fired
        )
    return result
