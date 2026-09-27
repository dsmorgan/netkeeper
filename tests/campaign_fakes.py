"""Fakes shared by the campaign engine tests (P3-06): a sender, a mailbox, a clock."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from netkeeper.config import CampaignSettings, Settings
from netkeeper.models import Mailbox, MailboxStatus, User
from netkeeper.services.campaign_engine import Firing, SendOutcome, SendResult

NOW = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)  # a Tuesday
ALWAYS_OPEN = CampaignSettings(
    send_window_days=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    send_window_hours=("00:00", "23:59"),
)
SETTINGS = Settings(campaigns=ALWAYS_OPEN)
LATENCY = timedelta(minutes=7)


@dataclass
class FakeSender:
    """Sends everything, ``LATENCY`` after it was asked, unless told otherwise."""

    outcome: SendOutcome = SendOutcome.SENT
    latency: timedelta = LATENCY
    raises: BaseException | None = None
    firings: list[Firing] = field(default_factory=list)
    now: datetime = NOW

    def send(self, firing: Firing) -> SendResult:
        self.firings.append(firing)
        if self.raises is not None:
            raise self.raises
        return SendResult(
            self.outcome,
            at=self.now + self.latency if self.outcome is SendOutcome.SENT else None,
            gmail_thread_id=f"thread-{firing.enrollment_id}",
            gmail_message_id=f"gm-{firing.message_id}",
            gmail_draft_id=f"draft-{firing.message_id}"
            if self.outcome is SendOutcome.DRAFTED
            else None,
        )


def make_mailbox(session: Session, user: User, **overrides: Any) -> Mailbox:
    fields: dict[str, Any] = {
        "email": f"me{user.id}@example.test",
        "keychain_ref": "gmail/mailbox/1",
        "daily_cap": 80,
        "status": MailboxStatus.OK,
    }
    fields.update(overrides)
    mailbox = Mailbox(user_id=user.id, **fields)
    session.add(mailbox)
    session.flush()
    return mailbox
