"""The route-changed breaker: a safety gate against a wall served in place of the
connections page (spec 9.3, ADR 0006; #189 item 1).

A wall served in place at the connections url -- a 200 document that is not the
connections page, with no first screen -- stops a run as ``route_changed`` and
raises neither the session flag nor heat (ADR 0006: "it flags nothing and
raises no heat"; the wall gives netkeeper nothing that classifies as a
checkpoint or a login page, spec 9.7). Nothing else would ever stop a
*scheduled* sync from loading that same wall again at the next interval,
forever, the moment a person arms scheduled runs. This module is the guard:
:data:`THRESHOLD` consecutive connections runs -- full or incremental, by hand
or by schedule -- ending ``route_changed`` trip it, and
:mod:`netkeeper.services.scheduler` skips every further scheduled connections
fire as ``"route_changed_breaker"`` (the same way it skips one as
``"disarmed"`` or ``"heat"``: the cadence still advances, and a
``run_on_first_setup`` kind keeps its first-setup standing) until a person
clears it -- directly (:func:`reset`, ``netkeeper linkedin schedule
reset-breaker``), or by running a sync themselves that reaches a natural end
(:func:`record`, called after every connections run regardless of trigger).

**Enrichment does not share this counter, on purpose.** Spec 9.6 already caps
enrichment's own unreadable-profile streak (two in a row on different
contacts, or three in one run) with its own narrower stop, reading a
different endpoint (a profile page) through a different parser. Folding that
into this counter would let an unrelated profile-parsing hiccup arm or
disarm a gate about the connections list's own route, and the reverse. The
two stay independent.

**Manual runs are never refused for this.** The breaker only ever makes
:mod:`netkeeper.services.scheduler` skip a *scheduled* fire;
:func:`netkeeper.services.runs.create_run` never refuses a manual run for it,
the same way a manual run is allowed while scheduled runs are disarmed. That
is deliberate: once the breaker trips, a manual run is how a person checks
whether the wall is still being served, the same way the first supervised run
after arming confirms what a genuine one looks like (ADR 0006's follow-up).

**The answer-lost limit** (#199) lives here too, as a second, separate
streak. A connections run whose ``stop_reason`` is ``answer_lost`` -- the page
kept answering, but answers arrived with no body the browser could hand over,
whether the run stopped for it or read on to the end without them (#197, #200)
-- moves the route-changed streak neither way: a lost answer is not a changed
route. Nothing else would then stop LinkedIn's client superseding every
pagination fetch from costing page views on every scheduled fire, forever.
:data:`ANSWER_LOST_THRESHOLD` consecutive connections runs ending
``answer_lost`` trip it, and the scheduler (and the worker's second check)
skip scheduled connections fires as ``"answer_lost_breaker"``, exactly the
way the route-changed breaker skips them. It clears the same two ways: a
connections run that reaches a natural end with nothing lost
(:func:`record_answer_lost`), or :func:`reset`, which clears both streaks
(``netkeeper linkedin schedule reset-breaker``). Every other ending (a route
change, a budget stop, a cancel, a checkpoint) leaves its count where it was.
The two counters never feed each other: each is its own ``settings_kv`` row.

Persisted like :mod:`netkeeper.services.heat`: a ``settings_kv`` row keyed by
account id, read and written through a session and a ``User`` -- the
extractor boundary (ADR 0005, spec 9.10) keeps this off the ``linkedin/``
side of the line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import User
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

_KEY_PREFIX: Final = "linkedin.route_changed_breaker"

#: Consecutive connections runs ending ``route_changed`` that trip the breaker.
#: Pinned literally (CLAUDE.md: a safety-relevant constant gets one test
#: asserting the number, not the module's own name for it). Two is spec 9.7's
#: own bar for a run itself ("two consecutive throttled units abort the run"):
#: one route_changed run is a shape hiccup a retry might not repeat; two in a
#: row, across two whole runs on two different fires, is the wall -- or a
#: genuine shape change -- still being there.
THRESHOLD: Final = 2

_ANSWER_LOST_KEY_PREFIX: Final = "linkedin.answer_lost_breaker"

#: Consecutive connections runs ending ``answer_lost`` that trip the answer-lost
#: limit (#199). Pinned literally. One more than :data:`THRESHOLD`: a single lost
#: answer is common (#200 saw about one in 5 to 10 on supervised runs) and a run
#: already reads on past up to five, so it takes three whole runs in a row, none
#: reaching a clean end, before scheduled runs stop spending page views on it.
ANSWER_LOST_THRESHOLD: Final = 3


@dataclass(frozen=True, slots=True)
class BreakerState:
    """The stored state, as it is. ``count`` of 0 (``since`` ``None``) is never
    stored (see :func:`record`, :func:`reset`); a caller reads that as "never
    tripped, or just cleared".

    ``readable`` is false when the stored row exists but could not be parsed
    (#191 review, F7). ``count`` and ``since`` are then placeholders (0,
    ``None``), and :attr:`tripped` reads true regardless -- fail closed, the
    direction every other gate in this module points: a safety gate this
    account depends on that cannot be read is treated as firing, not as
    clear. The next :func:`record` or :func:`reset` overwrites it with a
    well-formed row either way, so corruption never persists past one call.
    """

    count: int
    since: datetime | None
    readable: bool = True
    #: The count that trips this streak: :data:`THRESHOLD` for the route-changed
    #: breaker, :data:`ANSWER_LOST_THRESHOLD` for the answer-lost limit.
    threshold: int = THRESHOLD

    @property
    def tripped(self) -> bool:
        return (not self.readable) or self.count >= self.threshold


def state(session: Session, user: User, account_id: int) -> BreakerState:
    """The current streak: how many consecutive connections runs have ended
    ``route_changed``, and when the streak started. Read-only."""
    return _load(session, user, account_id)


def tripped(session: Session, user: User, account_id: int) -> bool:
    """Whether the breaker is tripped: the streak is at or above :data:`THRESHOLD`.
    Read-only. What :mod:`netkeeper.services.scheduler` asks before a scheduled
    connections fire."""
    return _load(session, user, account_id).tripped


def record(
    session: Session,
    user: User,
    account_id: int,
    *,
    route_changed: bool,
    succeeded: bool,
    now: datetime,
) -> BreakerState:
    """Record one connections run's outcome, whatever its kind or trigger. Needs a
    writer session.

    ``route_changed`` (the run's ``stop_reason`` was ``route_changed``)
    extends the streak by one. ``succeeded`` (the run reached a natural end --
    the end of the list, or an incremental sync catching up) clears it,
    whichever trigger ran it: that is how a manual run, run by hand to check
    whether the wall is still there, has "the same effect" as :func:`reset`
    without a person having to ask for it separately. Neither true (the
    budget stopped it, a cancel, a checkpoint, an unrelated error) leaves the
    count exactly where it was: none of those says anything about whether the
    connections list's own route is readable, so none of them should move a
    counter that exists to answer just that.
    """
    _require_writer(session, "route_breaker.record")
    if route_changed and succeeded:
        raise ValueError("a run cannot both end route_changed and reach a natural end")
    current = _load(session, user, account_id)
    if succeeded:
        updated = BreakerState(count=0, since=None)
    elif route_changed:
        # A corrupt row reads as tripped (fail closed). Another route_changed run
        # must not turn that into count=1, which would read as clear: keep it
        # tripped until a success or a reset says otherwise (#191 review N2).
        count = current.count + 1 if current.readable else THRESHOLD
        updated = BreakerState(count=count, since=current.since or now)
    else:
        return current
    _store(session, user, account_id, updated)
    return updated


def answer_lost_state(session: Session, user: User, account_id: int) -> BreakerState:
    """The answer-lost streak (#199): how many consecutive connections runs have
    ended ``answer_lost``, and when the streak started. Read-only."""
    return _load(session, user, account_id, answer_lost=True)


def answer_lost_tripped(session: Session, user: User, account_id: int) -> bool:
    """Whether the answer-lost limit is tripped: the streak is at or above
    :data:`ANSWER_LOST_THRESHOLD`. Read-only. The scheduler and the worker ask it
    before a scheduled connections run, next to :func:`tripped`."""
    return _load(session, user, account_id, answer_lost=True).tripped


def record_answer_lost(
    session: Session,
    user: User,
    account_id: int,
    *,
    answer_lost: bool,
    clean_end: bool,
    now: datetime,
) -> BreakerState:
    """Record one connections run's outcome on the answer-lost streak (#199). Needs a
    writer session.

    ``answer_lost`` (the run's ``stop_reason`` was ``answer_lost``: it stopped for
    a lost answer, or read to the end without some) extends the streak by one.
    ``clean_end`` (the run reached a natural end and lost nothing) clears it,
    whatever the trigger. Neither leaves the count where it was: a route change,
    a budget stop, a cancel or a checkpoint says nothing about whether the page's
    answers can be read.
    """
    _require_writer(session, "route_breaker.record_answer_lost")
    if answer_lost and clean_end:
        raise ValueError("a run cannot both lose an answer and end cleanly")
    current = _load(session, user, account_id, answer_lost=True)
    if clean_end:
        updated = BreakerState(count=0, since=None, threshold=ANSWER_LOST_THRESHOLD)
    elif answer_lost:
        # Fail closed as record() does: a corrupt row reads as tripped, and one more
        # answer_lost run keeps it tripped rather than restarting at 1.
        count = current.count + 1 if current.readable else ANSWER_LOST_THRESHOLD
        updated = BreakerState(
            count=count, since=current.since or now, threshold=ANSWER_LOST_THRESHOLD
        )
    else:
        return current
    _store(session, user, account_id, updated, answer_lost=True)
    return updated


def reset(session: Session, user: User, account_id: int) -> BreakerState:
    """Clear the breaker directly (``netkeeper linkedin schedule reset-breaker``),
    and the answer-lost limit with it (#199): one command clears whatever skips
    scheduled connections runs. Needs a writer session. Idempotent. Returns the
    route-changed breaker's cleared state."""
    _require_writer(session, "route_breaker.reset")
    cleared = BreakerState(count=0, since=None)
    _store(session, user, account_id, cleared)
    _store(
        session,
        user,
        account_id,
        BreakerState(count=0, since=None, threshold=ANSWER_LOST_THRESHOLD),
        answer_lost=True,
    )
    return cleared


def _key(account_id: int, *, answer_lost: bool = False) -> str:
    prefix = _ANSWER_LOST_KEY_PREFIX if answer_lost else _KEY_PREFIX
    return f"{prefix}.{account_id}"


def _load(
    session: Session, user: User, account_id: int, *, answer_lost: bool = False
) -> BreakerState:
    threshold = ANSWER_LOST_THRESHOLD if answer_lost else THRESHOLD
    raw = get_setting(session, user, _key(account_id, answer_lost=answer_lost))
    if raw is None:
        return BreakerState(count=0, since=None, threshold=threshold)
    try:
        if not isinstance(raw, dict):
            raise TypeError(f"not an object: {raw!r}")
        since_raw = raw.get("since")
        return BreakerState(
            count=int(_field(raw, "count")),
            since=None if since_raw is None else datetime.fromisoformat(str(since_raw)),
            threshold=threshold,
        )
    except (TypeError, ValueError) as exc:
        # #191 review F7: a posture report, or the scheduler's gate, is the last
        # thing that should crash on a corrupt row -- fail closed instead
        # (BreakerState.tripped reads true when unreadable) and say so in the log;
        # `posture()` turns this into a warning a person actually sees.
        log.error(
            "%s state for account %d is corrupt: %s",
            "answer-lost limit" if answer_lost else "route-changed breaker",
            account_id,
            exc,
        )
        return BreakerState(count=0, since=None, readable=False, threshold=threshold)


def _field(raw: dict[str, Any], name: str) -> Any:
    if name not in raw:
        raise TypeError(f"route-changed breaker state is missing {name!r}: {raw!r}")
    return raw[name]


def _store(
    session: Session,
    user: User,
    account_id: int,
    state: BreakerState,
    *,
    answer_lost: bool = False,
) -> None:
    set_setting(
        session,
        user,
        _key(account_id, answer_lost=answer_lost),
        {
            "count": state.count,
            "since": None if state.since is None else state.since.isoformat(),
        },
    )


def _require_writer(session: Session, where: str) -> None:
    if not is_writer(session):
        raise RuntimeError(
            f"{where} needs a writer session; use session_scope(factory, write=True)"
        )
