"""Persisted heat state, keyed by LinkedIn account, in ``settings_kv`` (spec 9.7, 9.10).

The extractor boundary (ADR 0005, spec 9.10): nothing under ``netkeeper/linkedin/``
imports ``netkeeper.models`` or opens a session. Reading and writing a row
needs both, so that half of "heat" lives here, on the core side, while the
score's decay math -- pure, no I/O -- lives in :mod:`netkeeper.linkedin.heat`
and is only ever called, never duplicated, from here. See
``netkeeper.services.budgets`` for the matching seam for budget counters and
for why the account is a plain ``account_id: int`` rather than a
``linkedin_account`` row (that model does not exist yet; this item does not
depend on it).

Every function takes ``now`` as a parameter, never reading the clock itself,
so the decay is testable without sleeping (spec 9.7: "decay on read").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from netkeeper.config import HeatSettings
from netkeeper.db import is_writer
from netkeeper.linkedin import heat as heat_math
from netkeeper.models import User
from netkeeper.services.settings_kv import get_setting, set_setting

_KEY_PREFIX: Final = "linkedin.heat"

# "Never raised": the state a fresh account reads as before its first event.
# Its age against any real ``now`` is enormous, so it always decays to 0.0.
_EPOCH: Final = datetime.fromtimestamp(0, tz=UTC)
_COLD: Final = heat_math.HeatState(score=0.0, updated_at=_EPOCH)


def read(
    session: Session, user: User, account_id: int, *, now: datetime, settings: HeatSettings
) -> float:
    """The decayed score right now. Read-only: no writer session needed."""
    state = _load(session, user, account_id)
    return heat_math.decayed_score(state, now, half_life_hours=settings.half_life_hours)


def state(session: Session, user: User, account_id: int) -> heat_math.HeatState | None:
    """The stored state as it is, undecayed, or ``None`` when heat was never raised.

    :func:`read` answers "how warm is it now", which is what a pacing decision
    needs. This answers "when was it last raised", which is what a report needs
    (spec 9.7: the Settings page "shows the level, when it was last raised, and
    when runs resume") and which a decayed score alone cannot say -- a score of
    0.02 is the same number whether it came from one throttle six hours ago or
    five throttles two days ago. ``None`` rather than the cold placeholder
    :data:`_COLD`, so a caller can tell "never raised" from "raised at the
    epoch" instead of printing 1970 at someone. Read-only.
    """
    raw = get_setting(session, user, _key(account_id))
    return None if raw is None else _load(session, user, account_id)


def should_skip(
    session: Session, user: User, account_id: int, *, now: datetime, settings: HeatSettings
) -> bool:
    """True once the decayed score is at or above ``settings.skip_threshold``."""
    state = _load(session, user, account_id)
    return heat_math.is_skipping(
        state, now, half_life_hours=settings.half_life_hours, skip_threshold=settings.skip_threshold
    )


def cooldown_multiplier(
    session: Session, user: User, account_id: int, *, now: datetime, settings: HeatSettings
) -> float:
    """>= 1.0, for stretching pacing delays and shrinking a per-run budget while warm."""
    state = _load(session, user, account_id)
    return heat_math.cooldown_multiplier(state, now, half_life_hours=settings.half_life_hours)


def raise_heat(
    session: Session, user: User, account_id: int, *, now: datetime, settings: HeatSettings
) -> float:
    """Record one ``Throttled`` or ``Checkpoint`` outcome (spec 9.7). Returns the new score.

    Reads the current row, then writes it back, so this needs a writer
    session (``session_scope(factory, write=True)``).
    """
    _require_writer(session, "heat.raise_heat")
    state = _load(session, user, account_id)
    updated = heat_math.raise_heat(
        state, now, per_block=settings.per_block, half_life_hours=settings.half_life_hours
    )
    _store(session, user, account_id, updated)
    return updated.score


def clear(session: Session, user: User, account_id: int, *, now: datetime) -> None:
    """Manual clear: "the block was something else" (spec 9.7). Needs a writer session."""
    _require_writer(session, "heat.clear")
    _store(session, user, account_id, heat_math.clear(now))


def _key(account_id: int) -> str:
    return f"{_KEY_PREFIX}.{account_id}"


def _load(session: Session, user: User, account_id: int) -> heat_math.HeatState:
    raw = get_setting(session, user, _key(account_id))
    if raw is None:
        return _COLD
    if not isinstance(raw, dict):
        raise TypeError(f"heat state for account {account_id} is not an object: {raw!r}")
    return heat_math.HeatState(
        score=float(_field(raw, "score")),
        updated_at=datetime.fromisoformat(str(_field(raw, "updated_at"))),
    )


def _field(raw: dict[str, Any], name: str) -> Any:
    if name not in raw:
        raise TypeError(f"heat state is missing {name!r}: {raw!r}")
    return raw[name]


def _store(session: Session, user: User, account_id: int, state: heat_math.HeatState) -> None:
    set_setting(
        session,
        user,
        _key(account_id),
        {"score": state.score, "updated_at": state.updated_at.isoformat()},
    )


def _require_writer(session: Session, where: str) -> None:
    if not is_writer(session):
        raise RuntimeError(
            f"{where} needs a writer session; use session_scope(factory, write=True)"
        )
