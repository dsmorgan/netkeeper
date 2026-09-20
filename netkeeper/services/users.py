"""User rows. v1 has exactly one, of kind ``local`` (spec section 5, ADR 0005)."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from netkeeper.config import Settings
from netkeeper.models import User, UserKind

log = logging.getLogger(__name__)


def ensure_local_user(session: Session, *, settings: Settings | None = None) -> User:
    """Return the local user, creating it on first start.

    Idempotent: a second call returns the existing row untouched. The timezone of a
    new user comes from ``settings.linkedin.timezone`` when settings are given,
    else ``UTC``.
    """
    existing = session.scalars(
        select(User).where(User.kind == UserKind.LOCAL).order_by(User.id)
    ).first()
    if existing is not None:
        return existing
    timezone = settings.linkedin.timezone if settings is not None else "UTC"
    user = User(kind=UserKind.LOCAL, timezone=timezone)
    session.add(user)
    session.flush()
    log.info("created local user %d with timezone %s", user.id, timezone)
    return user
