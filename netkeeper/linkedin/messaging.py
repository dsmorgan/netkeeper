"""The LinkedIn prefill's contract with the core (spec 11.6; P4-03, P4-09).

What a ``message_send`` run reports back about one message, for
:func:`netkeeper.services.linkedin_steps.record_prefill_outcome`. P4-09 (#379)
defines these types as P4-03's (#382) issue states them, so the engine side can be
built and tested first; P4-03 adds the job that produces them, here, beside them.

Like the rest of ``netkeeper/linkedin/``, nothing here imports ``netkeeper.models``
or opens a session (ADR 0005).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class MessageOutcomeKind(enum.StrEnum):
    """How a prefill ended.

    - ``prefilled``: the whole body is in the composer, for the person to send.
    - ``not_typed``: refused before any key (the control missing, a composer that
      was not empty, a recipient that was not the contact, a wall).
    - ``too_long``: refused before any key: the body is over the composer's ceiling.
    - ``partially_typed``: some of the body went in, then the run stopped.
    - ``unknown``: the run cannot say what the composer holds.
    """

    PREFILLED = "prefilled"
    NOT_TYPED = "not_typed"
    PARTIALLY_TYPED = "partially_typed"
    TOO_LONG = "too_long"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MessageOutcome:
    """One prefill's outcome. ``reason`` is fixed words, never page text or the body.
    ``conversation_urn`` is the conversation, when the page loaded it."""

    kind: MessageOutcomeKind
    reason: str
    conversation_urn: str | None
    typed_chars: int
