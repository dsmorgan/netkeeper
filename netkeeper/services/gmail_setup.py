"""The guided Gmail setup's progress, per user (#302).

Settings → Gmail walks a first-time user through their own Google Cloud project
one step at a time (docs/gmail-setup.md). Most steps happen in Google's console,
where netkeeper can't see whether they're done, so the wizard asks the person to
mark them done, and keeps what they said here: the project ID the deep links
name, the address they send from (copied into the consent screen and the test
users), and which of the console steps they've marked done.

The steps netkeeper can check live aren't stored at all. Whether the OAuth client
is saved and whether a mailbox is connected come from ``/mailboxes/status``, and a
connected mailbox also proves the Gmail API is on, because the callback asks Gmail
for the address before it stores anything (``gmail_api_refused`` otherwise).

One ``settings_kv`` row per user under :data:`SETTING_KEY`. Nothing secret lives
here: a project ID and an address aren't credentials.

netkeeper never runs ``gcloud`` itself. Starting a process from ``netkeeper/`` is
refused by ``tests/test_browser_safety.py`` (``netkeeper browser launch`` prints a
command and never runs one), so the wizard shows the ``gcloud`` commands to copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from netkeeper.models import User
from netkeeper.services.settings_kv import get_setting, set_setting

SETTING_KEY = "gmail.setup"

#: The console steps a person marks done. The order is the wizard's.
MANUAL_STEPS: tuple[str, ...] = (
    "project",
    "gmail_api",
    "branding",
    "test_user",
    "client_created",
    "published",
)

#: Google's rule for a project ID: 6 to 30 characters, lowercase letters, digits
#: and hyphens, starting with a letter and not ending with a hyphen.
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")

#: Enough to catch a slip, not an RFC 5322 parser: Google checks the real thing.
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

MAX_EMAIL = 254


class InvalidSetup(ValueError):
    """A value the wizard can't use; the message says which and why."""


@dataclass(frozen=True)
class GmailSetup:
    project_id: str | None = None
    sender_email: str | None = None
    done: tuple[str, ...] = field(default_factory=tuple)


def validate(project_id: str | None, sender_email: str | None, done: list[str]) -> GmailSetup:
    """Normalize and check what the wizard sent. Raises :class:`InvalidSetup`."""
    project = (project_id or "").strip().lower() or None
    if project is not None and PROJECT_ID.fullmatch(project) is None:
        raise InvalidSetup(
            "a project ID is 6 to 30 lowercase letters, digits or hyphens,"
            " starting with a letter and not ending with a hyphen"
        )
    email = (sender_email or "").strip() or None
    if email is not None and (len(email) > MAX_EMAIL or EMAIL.fullmatch(email) is None):
        raise InvalidSetup("the sending address doesn't look like an email address")
    unknown = sorted(set(done) - set(MANUAL_STEPS))
    if unknown:
        raise InvalidSetup(f"unknown setup steps: {', '.join(unknown)}")
    ordered = tuple(step for step in MANUAL_STEPS if step in set(done))
    return GmailSetup(project_id=project, sender_email=email, done=ordered)


def load(session: Session, user: User) -> GmailSetup:
    """The user's progress, or an empty one. A stored value that no longer validates
    (say, a step renamed since) is kept as far as it still makes sense."""
    raw = get_setting(session, user, SETTING_KEY)
    if not isinstance(raw, dict):
        return GmailSetup()
    project = _str(raw.get("project_id"))
    email = _str(raw.get("sender_email"))
    done_raw = raw.get("done")
    done = [step for step in done_raw if step in MANUAL_STEPS] if isinstance(done_raw, list) else []
    try:
        return validate(project, email, done)
    except InvalidSetup:
        return GmailSetup(done=tuple(step for step in MANUAL_STEPS if step in done))


def save(session: Session, user: User, setup: GmailSetup) -> GmailSetup:
    """Store ``setup`` for ``user``, replacing what was there."""
    set_setting(
        session,
        user,
        SETTING_KEY,
        {
            "project_id": setup.project_id,
            "sender_email": setup.sender_email,
            "done": list(setup.done),
        },
    )
    return setup


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
