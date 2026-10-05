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
Each connections kind (full, incremental) keeps its own streak, because
losses may bite only the weekly full sync's long read while the daily
incremental keeps completing; a shared streak would then be cleared every day
and never trip (#199 review, M2). :data:`ANSWER_LOST_THRESHOLD` consecutive
runs of one kind ending ``answer_lost`` trip the limit, and the scheduler (and
the worker's second check) then skip scheduled fires of *both* connections
kinds as ``"answer_lost_breaker"``, exactly the way the route-changed breaker
skips them. A kind's streak clears only when a run of that same kind ends
``completed`` (a natural end with nothing lost; :func:`record_answer_lost`);
:func:`reset` clears every streak at once (``netkeeper linkedin schedule
reset-breaker``). Every other ending (a route change, a budget stop, a cancel,
a checkpoint, even with losses) leaves the count where it was. The counters
never feed each other: each is its own ``settings_kv`` row.

**The Contact info breaker** (#424) lives here too, as a third, separate
streak, for enrichment. Since #405 a Contact info overlay whose body Chrome
did not keep is a soft failure: the profile is saved without it, and only too
many of them (``MAX_CONTACT_INFO_LOST_IN_A_ROW`` in a row, or more than half
of a run's overlays after ``CONTACT_INFO_LOST_SHARE_AFTER`` clicks) stop the
run as ``answer_lost``. If the body tap breaks for good, every scheduled
enrichment run would still make five to seven clicks and stop, forever.
:data:`CONTACT_INFO_THRESHOLD` consecutive enrichment runs, by hand or by
schedule, ending ``answer_lost`` (the only way enrichment ends ``answer_lost``
is those caps) trip it, and the scheduler (and the worker's second check) then
skip every scheduled enrichment fire as ``"contact_info_breaker"``. Connections
runs are never skipped for it, and it never skips enrichment for a connections
streak: the two read different endpoints. The streak clears when an enrichment
run reaches its own end (``end_of_plan`` or ``visit_budget``) after reading at
least one Contact info answer that was not lost (:func:`record_contact_info`),
whatever its trigger, so a manual run that reads Contact info again clears it;
:func:`reset` clears it with the others. Every other ending (a budget stop, a
cancel, the window closing, a route change, a run that clicked nothing or lost
every overlay it clicked) leaves the count where it was.

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
from netkeeper.models import SyncRunKind, User
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

#: The run kinds that each keep their own answer-lost streak (#199 review, M2).
ANSWER_LOST_KINDS: Final = (SyncRunKind.CONNECTIONS_FULL, SyncRunKind.CONNECTIONS_INCREMENTAL)

_CONTACT_INFO_KEY_PREFIX: Final = "linkedin.contact_info_breaker"

#: Consecutive enrichment runs ending ``answer_lost`` (the Contact info caps, #405)
#: that trip the Contact info breaker (#424). Pinned literally. The same bar as the
#: connections answer-lost limit: one run stopping on lost overlays can be a bad
#: hour; three in a row, on three separate fires, is the body tap not working.
CONTACT_INFO_THRESHOLD: Final = 3


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
    #: breaker, :data:`ANSWER_LOST_THRESHOLD` for the answer-lost limit,
    #: :data:`CONTACT_INFO_THRESHOLD` for the Contact info breaker.
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


def answer_lost_state(
    session: Session, user: User, account_id: int, kind: SyncRunKind
) -> BreakerState:
    """One kind's answer-lost streak (#199): how many consecutive connections runs of
    ``kind`` have ended ``answer_lost``, and when the streak started. Read-only."""
    return _load(session, user, account_id, lost_kind=_lost_kind(kind))


def answer_lost_states(
    session: Session, user: User, account_id: int
) -> dict[SyncRunKind, BreakerState]:
    """Every kind's answer-lost streak, in :data:`ANSWER_LOST_KINDS` order. Read-only."""
    return {kind: answer_lost_state(session, user, account_id, kind) for kind in ANSWER_LOST_KINDS}


def answer_lost_tripped(session: Session, user: User, account_id: int) -> bool:
    """Whether the answer-lost limit is tripped: either kind's streak is at or above
    :data:`ANSWER_LOST_THRESHOLD`, or unreadable (fail closed). Read-only. The
    scheduler and the worker ask it before a scheduled connections run of either
    kind, next to :func:`tripped`."""
    return any(state.tripped for state in answer_lost_states(session, user, account_id).values())


def record_answer_lost(
    session: Session,
    user: User,
    account_id: int,
    *,
    kind: SyncRunKind,
    answer_lost: bool,
    clean_end: bool,
    now: datetime,
) -> BreakerState:
    """Record one connections run's outcome on its own kind's answer-lost streak
    (#199). Needs a writer session.

    ``answer_lost`` (the run's ``stop_reason`` was ``answer_lost``: it stopped for
    a lost answer, or read to the end without some) extends ``kind``'s streak by
    one. ``clean_end`` (the run ended ``completed``: a natural end, nothing lost)
    clears it, whatever the trigger. Only ``kind``'s streak moves: a clean daily
    incremental says nothing about whether the weekly full sync's long read can
    finish, so it must not clear that streak (#199 review, M2). Neither flag
    leaves the count where it was: a route change, a budget stop, a cancel or a
    checkpoint says nothing about whether the page's answers can be read, and a
    corrupt row stays as it is (still read as tripped).
    """
    _require_writer(session, "route_breaker.record_answer_lost")
    if answer_lost and clean_end:
        raise ValueError("a run cannot both lose an answer and end cleanly")
    lost_kind = _lost_kind(kind)
    current = _load(session, user, account_id, lost_kind=lost_kind)
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
    _store(session, user, account_id, updated, lost_kind=lost_kind)
    return updated


def contact_info_state(session: Session, user: User, account_id: int) -> BreakerState:
    """The Contact info breaker's streak (#424): how many consecutive enrichment runs
    have ended ``answer_lost``, and when the streak started. Read-only."""
    return _load_streak(session, user, account_id, _CONTACT_INFO)


def contact_info_tripped(session: Session, user: User, account_id: int) -> bool:
    """Whether the Contact info breaker is tripped: the streak is at or above
    :data:`CONTACT_INFO_THRESHOLD`, or unreadable (fail closed). Read-only. The
    scheduler and the worker ask it before a scheduled enrichment run."""
    return contact_info_state(session, user, account_id).tripped


def record_contact_info(
    session: Session,
    user: User,
    account_id: int,
    *,
    answer_lost: bool,
    clean_end: bool,
    now: datetime,
) -> BreakerState:
    """Record one enrichment run's outcome on the Contact info breaker (#424). Needs a
    writer session.

    ``answer_lost`` (the run's ``stop_reason`` was ``answer_lost``: the Contact info
    caps stopped it) extends the streak by one. ``clean_end`` (the run reached its own
    end, ``end_of_plan`` or ``visit_budget``, and read at least one Contact info
    answer that was not lost) clears it, whatever the trigger: that is how a manual
    run shows the overlay can be read again. Neither leaves the count where it was,
    and a corrupt row stays as it is (still read as tripped).
    """
    _require_writer(session, "route_breaker.record_contact_info")
    if answer_lost and clean_end:
        raise ValueError("a run cannot both lose too many answers and end cleanly")
    current = _load_streak(session, user, account_id, _CONTACT_INFO)
    if clean_end:
        updated = BreakerState(count=0, since=None, threshold=CONTACT_INFO_THRESHOLD)
    elif answer_lost:
        # Fail closed as record() does: a corrupt row reads as tripped, and one more
        # answer_lost run keeps it tripped rather than restarting at 1.
        count = current.count + 1 if current.readable else CONTACT_INFO_THRESHOLD
        updated = BreakerState(
            count=count, since=current.since or now, threshold=CONTACT_INFO_THRESHOLD
        )
    else:
        return current
    _store_streak(session, user, account_id, _CONTACT_INFO, updated)
    return updated


def reset(session: Session, user: User, account_id: int) -> BreakerState:
    """Clear the breaker directly (``netkeeper linkedin schedule reset-breaker``),
    and every answer-lost streak with it (#199), and the Contact info breaker (#424):
    one command clears whatever skips scheduled LinkedIn runs after failed runs. Needs
    a writer session. Idempotent. Returns the route-changed breaker's cleared state."""
    _require_writer(session, "route_breaker.reset")
    cleared = BreakerState(count=0, since=None)
    _store(session, user, account_id, cleared)
    for kind in ANSWER_LOST_KINDS:
        _store(
            session,
            user,
            account_id,
            BreakerState(count=0, since=None, threshold=ANSWER_LOST_THRESHOLD),
            lost_kind=kind,
        )
    _store_streak(
        session,
        user,
        account_id,
        _CONTACT_INFO,
        BreakerState(count=0, since=None, threshold=CONTACT_INFO_THRESHOLD),
    )
    return cleared


def _lost_kind(kind: SyncRunKind) -> SyncRunKind:
    if kind not in ANSWER_LOST_KINDS:
        raise ValueError(f"the answer-lost limit counts connections runs only, not {kind}")
    return kind


@dataclass(frozen=True, slots=True)
class _Streak:
    """Where one streak is stored, what trips it, and its name in the log."""

    prefix: str
    threshold: int
    label: str


_CONTACT_INFO: Final = _Streak(
    _CONTACT_INFO_KEY_PREFIX, CONTACT_INFO_THRESHOLD, "Contact info breaker"
)


def _streak(lost_kind: SyncRunKind | None) -> _Streak:
    if lost_kind is None:
        return _Streak(_KEY_PREFIX, THRESHOLD, "route-changed breaker")
    return _Streak(
        f"{_ANSWER_LOST_KEY_PREFIX}.{lost_kind.value}",
        ANSWER_LOST_THRESHOLD,
        f"answer-lost limit ({lost_kind.value})",
    )


def _load(
    session: Session, user: User, account_id: int, *, lost_kind: SyncRunKind | None = None
) -> BreakerState:
    return _load_streak(session, user, account_id, _streak(lost_kind))


def _load_streak(session: Session, user: User, account_id: int, streak: _Streak) -> BreakerState:
    threshold = streak.threshold
    raw = get_setting(session, user, f"{streak.prefix}.{account_id}")
    if raw is None:
        return BreakerState(count=0, since=None, threshold=threshold)
    try:
        if not isinstance(raw, dict):
            raise TypeError(f"not an object: {raw!r}")
        since_raw = raw.get("since")
        count = int(_field(raw, "count"))
        if count < 0:
            # A negative count would read as clear however far below zero; nothing
            # this module writes is ever negative, so it is corrupt (fail closed).
            raise ValueError(f"a negative count: {count}")
        return BreakerState(
            count=count,
            since=None if since_raw is None else datetime.fromisoformat(str(since_raw)),
            threshold=threshold,
        )
    except (TypeError, ValueError) as exc:
        # #191 review F7: a posture report, or the scheduler's gate, is the last
        # thing that should crash on a corrupt row -- fail closed instead
        # (BreakerState.tripped reads true when unreadable) and say so in the log;
        # `posture()` turns this into a warning a person actually sees.
        log.error("%s state for account %d is corrupt: %s", streak.label, account_id, exc)
        return BreakerState(count=0, since=None, readable=False, threshold=threshold)


def _field(raw: dict[str, Any], name: str) -> Any:
    if name not in raw:
        raise TypeError(f"breaker state is missing {name!r}: {raw!r}")
    return raw[name]


def _store(
    session: Session,
    user: User,
    account_id: int,
    state: BreakerState,
    *,
    lost_kind: SyncRunKind | None = None,
) -> None:
    _store_streak(session, user, account_id, _streak(lost_kind), state)


def _store_streak(
    session: Session, user: User, account_id: int, streak: _Streak, state: BreakerState
) -> None:
    set_setting(
        session,
        user,
        f"{streak.prefix}.{account_id}",
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
