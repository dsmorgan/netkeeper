"""The LinkedIn inbox poll, the extractor half: the contract (P4-08, #378).

Behind the extractor boundary (spec 9.10, ADR 0005): an :class:`InboxJobSpec`
comes in, an :class:`InboxDelta` goes out. Nothing here imports the models or
opens a session. This module holds types only. The page source that reads the
messaging page (:class:`InboxSource`) is P4-01's (#380); the core maps a delta
onto rows in :mod:`netkeeper.crm.inbox_apply`, and
:mod:`netkeeper.services.inbox_poll` is the runner that wires the two together
with the budget, heat, and the session flag.

**What a source does.** It reads the conversations active after
``spec.since``, at most ``spec.max_conversations`` of them, and hands back the
one-to-one conversations with their loaded messages. It counts group threads
(``skipped_group``) and InMail, sponsored, and system threads
(``skipped_other``) and never returns them. It opens a thread only for the
conversation URNs in ``spec.open_threads_for``, by navigation, and never more
than :data:`MAX_THREADS_OPENED`. ``complete`` says the page proved it read back
to ``since``; a source that stopped at ``max_conversations`` first says false.

**When the page is not the inbox.** A source that lands on a wall (a
checkpoint, a login page, a throttle, a page it cannot read) raises
:class:`InboxReadStopped` with the classified
:class:`~netkeeper.linkedin.classify.Outcome` and nothing else. The runner
raises heat or sets the session flag from it. Nothing retries.

**Message text.** ``text_snippet`` is at most :data:`SNIPPET_MAX` characters.
It is left out of every ``repr`` here, so a delta that reaches a log line
carries no message text. Never log it, and never put it in an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from netkeeper.linkedin.classify import Outcome

#: The longest message snippet a source hands over, and the core stores.
SNIPPET_MAX: Final = 200

#: The most threads one poll may open by navigation (P4-06's decision, #374).
MAX_THREADS_OPENED: Final = 5


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class InboxJobSpec:
    """What one poll may read.

    ``since`` is the start of the last complete poll, or ``None`` for the first
    poll. ``watched_urns`` are the ``urn:li:fsd_profile:<id>`` URNs of the
    contacts the core cares about. ``open_threads_for`` are the conversation
    URNs whose thread the poll may open, at most :data:`MAX_THREADS_OPENED`.
    """

    since: datetime | None
    watched_urns: frozenset[str]
    max_conversations: int
    open_threads_for: frozenset[str]

    def __post_init__(self) -> None:
        if self.since is not None:
            _require_aware(self.since, "since")
        if self.max_conversations < 1:
            raise ValueError("a poll reads at least one conversation")
        if len(self.open_threads_for) > MAX_THREADS_OPENED:
            raise ValueError(f"a poll opens at most {MAX_THREADS_OPENED} threads")


@dataclass(frozen=True, slots=True)
class InboxMessage:
    """One message in a one-to-one conversation. ``outbound``: the account owner sent it.

    ``text_snippet`` is at most :data:`SNIPPET_MAX` characters and is left out of
    the ``repr``. A snippet that is too long is refused with a message that does
    not quote it.
    """

    message_urn: str
    sender_urn: str
    outbound: bool
    at: datetime
    text_snippet: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_aware(self.at, "a message's time")
        if len(self.text_snippet) > SNIPPET_MAX:
            raise ValueError(f"a message snippet is at most {SNIPPET_MAX} characters")


@dataclass(frozen=True, slots=True)
class InboxConversation:
    """One one-to-one conversation: the other participant, and the messages the page loaded.

    ``messages`` are oldest first.
    """

    conversation_urn: str
    counterpart_urn: str
    last_activity_at: datetime
    messages: tuple[InboxMessage, ...]

    def __post_init__(self) -> None:
        _require_aware(self.last_activity_at, "a conversation's last activity")


@dataclass(frozen=True, slots=True)
class InboxDelta:
    """What one poll read.

    ``skipped_group`` counts group threads, ``skipped_other`` InMail, sponsored,
    and system threads; neither is in ``conversations``. ``complete`` is true
    only when the page proved it read back to the spec's ``since``.
    """

    conversations: tuple[InboxConversation, ...]
    skipped_group: int
    skipped_other: int
    complete: bool


class InboxReadStopped(Exception):
    """The page was not the inbox: a wall, a checkpoint, a throttle, or a changed page.

    ``outcome`` is the classified response (spec 9.7), never ``Ok``. ``final_url``
    is where the tab landed, for the session flag. The message names the outcome
    only.
    """

    def __init__(self, outcome: Outcome, *, final_url: str = "") -> None:
        if outcome is Outcome.OK:
            raise ValueError("an Ok page is not a stop")
        super().__init__(f"the inbox page answered {outcome.value}")
        self.outcome = outcome
        self.final_url = final_url


class InboxSource(Protocol):
    """Reads the inbox through the run's browser tab. P4-01 (#380) implements it."""

    async def read(self, spec: InboxJobSpec) -> InboxDelta: ...
