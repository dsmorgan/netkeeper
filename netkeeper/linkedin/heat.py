"""Heat: an escalating back-off score for the LinkedIn extractor (spec 9.7).

Pure and behind the extractor boundary (spec 9.10, ADR 0005): no import of
``netkeeper.models``, no session, no clock read of its own. Every function
takes ``now`` as a parameter rather than calling ``datetime.now()`` internally,
which is what makes the decay testable without sleeping.

A raised score decays exponentially with a configured half-life, computed
fresh on every read from the stored score and the elapsed time
(:func:`decayed_score`). Nothing here ticks the number down in the
background -- reading it twice with no new event between the reads returns a
smaller number the second time, as long as real time passed, and no scheduler
job exists whose only purpose is to keep the score honest.

Persisting a :class:`HeatState` -- reading and writing the row that survives
between calls -- needs a session and a ``User`` (for the account it belongs
to), so it lives on the core side, in ``netkeeper.services.heat``, which
calls the pure functions here to do the math. See that module's docstring for
why the seam falls here rather than inside ``linkedin/``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Final

# The multiplier never contracts a delay or a budget below its configured
# value: cold (score 0) is exactly 1.0, and it only grows from there.
COOLDOWN_FLOOR: Final = 1.0

# Below this, a decayed score reads as exactly 0.0 (cold). Exponential decay
# never reaches zero on its own, and ``shrink`` floors, so without a cutoff the
# residue of one throttle -- a multiplier of ``1.000...1`` -- took one unit off
# every budget for about 318 hours, until float64 rounded ``1.0 + score`` to
# 1.0, while the score displayed as 0.00 (#160). Appendix C says "a clean day
# resets".
#
# Why 0.01: it is the smallest score with an effect worth keeping. At 0.01 the
# multiplier is 1.01 -- a 25 s delay median stretches by a quarter of a
# second, inside the lognormal jitter (sigma 0.6) it rides on, and a
# profile-visit budget (at most 100 by spec 9.6's hard max) loses at most the
# one unit flooring always takes. Every score that should still stretch
# delays is far above it: one ``per_block`` (1.0) is 100 times larger, and
# the skip threshold (2.5) 250 times. With the Appendix C defaults (6-hour
# half-life) one throttle goes cold after about 40 hours
# (``6 * log2(1 / 0.01)``), and a score at the skip threshold after about 48
# -- the throttle's own day, then a clean one.
COLD_EPSILON: Final = 0.01


@dataclass(frozen=True, slots=True)
class HeatState:
    """A score of ``score`` as of ``updated_at``. ``score=0`` reads as cold at any time."""

    score: float
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("HeatState.updated_at must be timezone-aware")
        if self.score < 0:
            raise ValueError("HeatState.score must not be negative")


def decayed_score(state: HeatState, now: datetime, *, half_life_hours: float) -> float:
    """``state.score`` decayed exponentially from ``updated_at`` to ``now``.

    ``now`` before ``updated_at`` (clock skew, or a stale read) returns the
    stored score unchanged rather than projecting it backward into a larger
    number. Elapsed time is real hours, not calendar days, so this needs no
    notion of a local day or timezone at all -- that belongs to the budget
    counters (spec 9.6), not to heat.

    A result below :data:`COLD_EPSILON` is returned as exactly 0.0, so every
    reader -- the skip gate, the multiplier, the budget, the display -- agrees
    that the account is cold rather than carrying an invisible residue.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if half_life_hours <= 0:
        raise ValueError("half_life_hours must be positive")
    elapsed_hours = (now - state.updated_at).total_seconds() / 3600
    if elapsed_hours <= 0:
        score = state.score
    else:
        score = state.score * math.pow(0.5, elapsed_hours / half_life_hours)
    return 0.0 if score < COLD_EPSILON else score


def raise_heat(
    state: HeatState, now: datetime, *, per_block: float, half_life_hours: float
) -> HeatState:
    """Add ``per_block`` to the score, decayed forward to ``now`` first.

    Each ``Throttled`` or ``Checkpoint`` outcome (spec 9.7) calls this once.
    """
    if per_block < 0:
        raise ValueError("per_block must not be negative")
    current = decayed_score(state, now, half_life_hours=half_life_hours)
    return HeatState(score=current + per_block, updated_at=now)


def clear(now: datetime) -> HeatState:
    """A fresh, cold state as of ``now``: the manual clear for "the block was something else"."""
    return HeatState(score=0.0, updated_at=now)


def is_skipping(
    state: HeatState, now: datetime, *, half_life_hours: float, skip_threshold: float
) -> bool:
    """True once the score, decayed to ``now``, is at or above ``skip_threshold``.

    The scheduler's cue to skip browser jobs entirely (spec 9.7) rather than
    run them slower.
    """
    return decayed_score(state, now, half_life_hours=half_life_hours) >= skip_threshold


def cooldown_multiplier(state: HeatState, now: datetime, *, half_life_hours: float) -> float:
    """>= 1.0: how much warmer than baseline things are right now.

    The caller multiplies ``human_delay`` medians by this and divides a
    per-run budget by it (through :func:`shrink`) -- the same number drives
    both, because both are "how cautious to be while warm" (spec 9.7).

    ``1.0 + score`` is this module's own choice, not spec 9.7's: the spec says
    only that delays stretch and the budget shrinks while warm, not by how
    much. The score itself is not capped here, but in practice the caller
    stops browser work once :func:`is_skipping` trips well before the
    multiplier grows large -- with the defaults in Appendix C
    (``skip_threshold=2.5``), the worst multiplier a run ever operates under
    is 3.5.
    """
    score = decayed_score(state, now, half_life_hours=half_life_hours)
    return COOLDOWN_FLOOR + score


def shrink(base_limit: int, multiplier: float) -> int:
    """``base_limit`` divided by ``multiplier``, floored at 1 unit.

    Spec 9.7 requires only the property this has: "the per-run budget
    shrinks, never to zero." Division-with-a-floor is this module's own way
    of getting there, not a curve the spec specifies. A multiplier below 1
    would grow the budget instead of shrinking it, so it is refused.
    """
    if multiplier < COOLDOWN_FLOOR:
        raise ValueError("multiplier must be at least 1.0")
    if base_limit < 1:
        raise ValueError("base_limit must be at least 1")
    return max(1, math.floor(base_limit / multiplier))
