"""The invented LinkedIn inbox a campaign replay reads (P4-02, #381).

:class:`~netkeeper.services.simulate_campaign.SimulatedLinkedIn` decides what the
inbox shows and when; this module only builds the
:class:`~netkeeper.linkedin.inbox.InboxDelta` a poll of it would return, which
:func:`netkeeper.crm.inbox_apply.apply_delta` then maps onto rows like any other
(spec 9.10: no module but the apply maps an extractor result onto a table, so
nothing here names one). Every URN and snippet is invented.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from netkeeper.linkedin.inbox import InboxConversation, InboxDelta, InboxMessage

#: The account owner's URN in a replay's invented inbox.
SIMULATED_OWNER_URN: Final = "urn:li:fsd_profile:SIMULATEDOWNER"

#: What every simulated reply says. Invented, and never matches an unsubscribe phrase.
SIMULATED_REPLY: Final = "Thanks, happy to talk."


def simulated_conversation_urn(contact_id: int) -> str:
    """The invented one-to-one conversation the replay uses for a contact."""
    return f"urn:li:msg_conversation:SIMULATED{contact_id}"


@dataclass(frozen=True, slots=True)
class SimulatedInboxMessage:
    """One message the replay's inbox shows: one you sent, or the contact's reply."""

    at: datetime
    contact_id: int
    counterpart_urn: str
    outbound: bool


def simulated_delta(
    messages: Iterable[SimulatedInboxMessage], *, numbers: Iterator[int]
) -> InboxDelta:
    """A complete poll's delta: one conversation per contact, its messages oldest first.
    ``numbers`` numbers the invented message URNs, so a replay never repeats one."""
    by_contact: dict[int, list[SimulatedInboxMessage]] = {}
    for message in sorted(messages, key=lambda m: (m.contact_id, m.at)):
        by_contact.setdefault(message.contact_id, []).append(message)
    conversations = []
    for contact_id, items in sorted(by_contact.items()):
        counterpart = items[0].counterpart_urn
        conversations.append(
            InboxConversation(
                conversation_urn=simulated_conversation_urn(contact_id),
                counterpart_urn=counterpart,
                last_activity_at=items[-1].at,
                messages=tuple(
                    InboxMessage(
                        message_urn=f"urn:li:msg_message:SIMULATED{next(numbers)}",
                        sender_urn=SIMULATED_OWNER_URN if item.outbound else counterpart,
                        outbound=item.outbound,
                        at=item.at,
                        text_snippet="" if item.outbound else SIMULATED_REPLY,
                    )
                    for item in items
                ),
            )
        )
    return InboxDelta(tuple(conversations), skipped_group=0, skipped_other=0, complete=True)
