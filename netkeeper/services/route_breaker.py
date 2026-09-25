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

    @property
    def tripped(self) -> bool:
        return (not self.readable) or self.count >= THRESHOLD


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


def reset(session: Session, user: User, account_id: int) -> BreakerState:
    """Clear the breaker directly (``netkeeper linkedin schedule reset-breaker``).
    Needs a writer session. Idempotent."""
    _require_writer(session, "route_breaker.reset")
    cleared = BreakerState(count=0, since=None)
    _store(session, user, account_id, cleared)
    return cleared


def _key(account_id: int) -> str:
    return f"{_KEY_PREFIX}.{account_id}"


def _load(session: Session, user: User, account_id: int) -> BreakerState:
    raw = get_setting(session, user, _key(account_id))
    if raw is None:
        return BreakerState(count=0, since=None)
    try:
        if not isinstance(raw, dict):
            raise TypeError(f"not an object: {raw!r}")
        since_raw = raw.get("since")
        return BreakerState(
            count=int(_field(raw, "count")),
            since=None if since_raw is None else datetime.fromisoformat(str(since_raw)),
        )
    except (TypeError, ValueError) as exc:
        # #191 review F7: a posture report, or the scheduler's gate, is the last
        # thing that should crash on a corrupt row -- fail closed instead
        # (BreakerState.tripped reads true when unreadable) and say so in the log;
        # `posture()` turns this into a warning a person actually sees.
        log.error("route-changed breaker state for account %d is corrupt: %s", account_id, exc)
        return BreakerState(count=0, since=None, readable=False)


def _field(raw: dict[str, Any], name: str) -> Any:
    if name not in raw:
        raise TypeError(f"route-changed breaker state is missing {name!r}: {raw!r}")
    return raw[name]


def _store(session: Session, user: User, account_id: int, state: BreakerState) -> None:
    set_setting(
        session,
        user,
        _key(account_id),
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
