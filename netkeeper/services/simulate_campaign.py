"""Replay the campaign engine's minute tick against a virtual clock (P3-06's "done when").

:func:`simulate_campaign` drives :func:`netkeeper.services.campaign_engine.run_tick`
through weeks of campaign time in one process, with no sleeping and no network:
a :class:`SimulatedSender` stands in for Gmail and says each message went out a
random few minutes after it was handed over, so the next step's timing has to
come from that ``sent_at`` and not from the fire time.

**Minute ticks, without every minute.** The real loop ticks every minute. Here
a tick that fired is followed by one a minute later, as in ``serve``; a tick
that did not fire jumps to its ``next_wake`` (the earliest due time, the end of
the spacing, or the next day's window when a cap is reached), rounded up to a
whole minute, which is when the real loop would next have something to do.
Every other minute in between is a tick that would have done nothing.

Deterministic for a ``seed``. ``max_ticks`` bounds the loop, so a schedule that
stops moving fails fast instead of hanging.

The command-line ``netkeeper simulate`` extension for campaign schedules is
P3-13; this is the engine it will drive.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import Settings
from netkeeper.services.campaign_engine import (
    Firing,
    SendOutcome,
    SendResult,
    run_tick,
)

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
) -> CampaignSimulation:
    """Tick every local user's campaigns from ``start`` until ``end`` or nothing is left to do."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("the simulation's times must be timezone-aware")
    sender = SimulatedSender(random.Random(seed))  # noqa: S311 -- deterministic replay
    spacing = random.Random(seed + 1)  # noqa: S311 -- deterministic replay
    now = _next_minute(start)
    ticks = 0
    while now < end:
        if ticks >= max_ticks:
            raise RuntimeError(f"the campaign schedule stopped moving after {ticks} ticks")
        ticks += 1
        sender.now = now
        results = run_tick(
            factory, settings=settings, sender=sender, clock=_frozen(now), rng=spacing
        )
        if any(result.fired for result in results):
            now += timedelta(minutes=1)
            continue
        wakes = [r.next_wake for r in results if r.next_wake is not None]
        if not wakes:
            break
        now = _next_minute(max(min(wakes), now + timedelta(minutes=1)))
    return CampaignSimulation(tuple(sender.firings), ticks, min(now, end))
