"""The LinkedIn prefill's contract with the core, and its pure checks (spec 11.6; P4-03, P4-09).

What a ``message_send`` run reports back about one message, for
:func:`netkeeper.services.linkedin_steps.record_prefill_outcome`. P4-09 (#379)
defined the outcome types as P4-03's (#382) issue states them; P4-03 adds the job
spec, the typing plan's wrapper, and the checks [ADR 0007](../../docs/adr/0007-prefill-inputs.md)
makes on what the page loaded, here, beside them. The page work is
:mod:`netkeeper.linkedin.page_messaging` and the two inputs are
:class:`netkeeper.linkedin.browser.BrowserRun`'s ``click_message`` and
``type_into_composer``.

Nothing here touches a page. Like the rest of ``netkeeper/linkedin/``, nothing here
imports ``netkeeper.models`` or opens a session (ADR 0005). No message body, name, or
page text reaches a log line, an exception, or a ``reason``: every reason is fixed
words.
"""

from __future__ import annotations

import enum
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.pacing import (
    TypingPlan,
    TypingPlanError,
    TypingTooLong,
    typing_plan,
)

#: The prefix of every profile URN a prefill names (``urn:li:fsd_profile:<id>``).
PROFILE_URN_PREFIX: Final = "urn:li:fsd_profile:"
#: The path the compose option is loaded from, then its percent-encoded URN.
COMPOSE_OPTIONS_PATH: Final = "/voyager/api/voyagerMessagingDashComposeOptions/"
#: The compose option URN's prefix: ``(<bare profile id>,<screen context>,<token>)`` follows.
COMPOSE_OPTION_URN_PREFIX: Final = "urn:li:fsd_composeOption:("
#: An existing conversation, as the compose option names it: ``<prefix><thread id>``.
FSD_CONVERSATION_PREFIX: Final = "urn:li:fsd_conversation:"
#: The messaging GraphQL path the page's thread requests use (``messengerMessages``).
MESSAGING_GRAPHQL_PATH: Final = "/voyager/api/voyagerMessagingGraphQL/graphql"
#: The query name a thread request's ``queryId`` starts with.
MESSAGES_QUERY: Final = "messengerMessages"


class MessageOutcomeKind(enum.StrEnum):
    """How a prefill ended.

    - ``prefilled``: the whole body is in the composer, for the person to send.
    - ``not_typed``: refused before any key (the control missing, a composer that
      was not empty, a recipient that was not the contact, a wall).
    - ``too_long``: refused before any key: the body is over the composer's ceiling.
    - ``partially_typed``: some of the body went in, then the run stopped.
    - ``unknown``: the run cannot say what the composer holds.
    - ``send_clicked``: auto-send only (ADR 0008): the whole body was typed, every gate
      held, and the one click on **Send** landed. The next inbox poll confirms the
      send, as it confirms a send the person made.
    """

    PREFILLED = "prefilled"
    NOT_TYPED = "not_typed"
    PARTIALLY_TYPED = "partially_typed"
    TOO_LONG = "too_long"
    UNKNOWN = "unknown"
    SEND_CLICKED = "send_clicked"


@dataclass(frozen=True, slots=True)
class MessageOutcome:
    """One prefill's outcome. ``reason`` is fixed words, never page text or the body.
    ``conversation_urn`` is the conversation, when the page loaded it."""

    kind: MessageOutcomeKind
    reason: str
    conversation_urn: str | None
    typed_chars: int
    #: Whether the never-messaged chip was compared with the profile's ``h1``: ``True``
    #: when it was, ``False`` when that check was skipped (zero or two ``h1`` elements,
    #: or an ``h1`` name the matcher didn't confirm), ``None`` when it doesn't apply.
    recipient_name_checked: bool | None = None


@dataclass(frozen=True, slots=True)
class MessageJobSpec:
    """One prefill's input (P4-03's contract). ``body`` is the rendered message: it is left
    out of ``repr`` and never logged.

    ``recipient_urn`` is the contact's ``urn:li:fsd_profile:<id>``;
    ``recipient_public_id`` the vanity slug the profile is opened by. ``mode`` is
    ``prefill``, or ``auto_send`` for a step the scheduler sends (ADR 0008). The mode
    alone never sends: the page clicks Send only with a :class:`SendPermit` as well.
    """

    recipient_urn: str
    recipient_public_id: str | None
    body: str = field(repr=False)
    mode: Literal["prefill", "auto_send"]
    typing_seed: int

    def __post_init__(self) -> None:
        if not self.recipient_urn.startswith(PROFILE_URN_PREFIX) or not self.profile_id:
            raise ValueError("a prefill's recipient is an urn:li:fsd_profile URN")
        if any(c in self.profile_id for c in "/?#&=,()% "):
            raise ValueError("a prefill's recipient URN holds an unexpected character")
        if self.mode not in ("prefill", "auto_send"):
            raise ValueError("a LinkedIn step's mode is prefill or auto_send")

    @property
    def profile_id(self) -> str:
        """The bare profile id: the URN without ``urn:li:fsd_profile:``."""
        return self.recipient_urn.removeprefix(PROFILE_URN_PREFIX)


# --- the typing plan (ADR 0007, "The typing plan comes first") ------------------------


#: The one message :class:`PlanInvariantBroken` carries, whatever went wrong.
PLAN_INVARIANT_MESSAGE: Final = "the typing plan failed unexpectedly; nothing was typed"


class PlanInvariantBroken(TypingPlanError):
    """``typing_plan`` raised something other than a :class:`TypingPlanError` (#426's review).

    Its message is :data:`PLAN_INVARIANT_MESSAGE`, never the body, and its cause is
    chained. No lint rule matches it, so the lint and plan agreement fuzz can assert
    that it never fires. The prefill handles it as ``not_typed`` and never retries."""

    def __init__(self) -> None:
        super().__init__(PLAN_INVARIANT_MESSAGE)


def plan_typing(body: str, seed: int) -> TypingPlan:
    """The whole typing plan for ``body``, built before any budget or navigation.

    Raises :class:`~netkeeper.linkedin.pacing.TypingPlanError` for a body the plan
    refuses, and wraps any other exception (``Exception`` only) in
    :class:`PlanInvariantBroken`, chained with ``from``."""
    rng = random.Random(seed)  # noqa: S311 -- pacing, not crypto
    try:
        return typing_plan(body, rng)
    except TypingPlanError:
        raise
    except Exception as exc:
        raise PlanInvariantBroken() from exc


def plan_refusal(exc: TypingPlanError) -> MessageOutcome:
    """The outcome a refused plan records: ``too_long`` for :class:`TypingTooLong`,
    ``not_typed`` for every other :class:`TypingPlanError`. Zero keys either way. The
    reason names the exception's class, never its message."""
    if isinstance(exc, TypingTooLong):
        return MessageOutcome(
            MessageOutcomeKind.TOO_LONG, "the body is over the typing ceiling", None, 0
        )
    return MessageOutcome(
        MessageOutcomeKind.NOT_TYPED,
        f"the typing plan refused the body ({type(exc).__name__})",
        None,
        0,
    )


def plan_control_characters(plan: TypingPlan) -> bool:
    """Whether any chunk of ``plan`` holds ``\\n``, ``\\r``, or any other C0 or C1 control
    character (ADR 0007). ``keyboard.type`` would press Enter for a newline, so a
    newline is only ever a ``newline=True`` step. Checked again here, whatever
    :class:`~netkeeper.linkedin.pacing.TypeStep`'s own constructor refused."""
    for step in plan:
        for char in step.chunk:
            code = ord(char)
            if code < 0x20 or 0x7F <= code <= 0x9F:
                return True
        if step.newline and step.chunk:
            return True
    return False


def typed_text(plan: TypingPlan, steps: int | None = None) -> str:
    """What the first ``steps`` steps of ``plan`` type, a newline step as ``\\n``."""
    chosen = plan if steps is None else plan[:steps]
    return "".join("\n" if step.newline else step.chunk for step in chosen)


def composer_text(paragraphs: list[str]) -> str:
    """The composer's text from its paragraphs' ``inner_text`` (ADR 0007, "Reading the
    composer's text").

    A paragraph boundary is one newline, and a ``<br>`` inside a paragraph is the one
    newline ``inner_text`` reads for it. A paragraph's trailing ``<br>`` adds nothing,
    so one trailing newline is dropped from each paragraph, and the empty composer's
    ``<p><br></p>`` reads as the empty string. A no-break space reads as a space."""
    lines = []
    for paragraph in paragraphs:
        text = paragraph.replace("\r\n", "\n").replace("\r", "\n")
        lines.append(text.removesuffix("\n").replace("\u00a0", " "))
    return "\n".join(lines)


def composer_text_matches(observed: str | None, expected: str) -> bool:
    """Whether the composer's text (:func:`composer_text`) is ``expected``, exactly: the
    typed prefix with each newline step as ``\\n``, and a no-break space read as a space on
    both sides, as the read maps it. ``None`` is text the rule couldn't
    read, which never matches."""
    return observed is not None and observed == expected.replace("\u00a0", " ")


# --- what the page loaded: the compose option (ADR 0007, "The recipient") -------------


class ComposeKind(enum.StrEnum):
    """The two bubble layouts the compose option names."""

    REPLY = "REPLY"
    """An existing conversation: the bubble is a ``Messaging`` dialog."""

    CONNECTION_MESSAGE = "CONNECTION_MESSAGE"
    """Never messaged: a ``New message`` bubble with one recipient chip."""


@dataclass(frozen=True, slots=True)
class ComposeOption:
    """A compose option that names the contact. ``conversation_urn`` is the existing
    conversation's ``urn:li:fsd_conversation:<thread id>`` for ``REPLY``, else ``None``."""

    kind: ComposeKind
    conversation_urn: str | None

    @property
    def thread_id(self) -> str | None:
        if self.conversation_urn is None:
            return None
        return self.conversation_urn.removeprefix(FSD_CONVERSATION_PREFIX)


@dataclass(frozen=True, slots=True)
class ComposeRefusal:
    """Why the compose option does not authorize typing: fixed words."""

    reason: str


def read_compose_option(
    url: str, body: str | None, profile_id: str
) -> ComposeOption | ComposeRefusal:
    """Check one compose option answer against the contact (ADR 0007, "The recipient", 2).

    - the first part of the ``fsd_composeOption`` URN in the request's path is the
      contact's bare profile id;
    - ``data.composeNavigationContext.recipientUrns`` is exactly one URN, the
      contact's ``urn:li:fsd_profile:<id>``;
    - ``composeOptionType`` is ``REPLY`` with an ``existingConversationUrn``, or
      ``CONNECTION_MESSAGE`` without one. Anything else is refused.
    """
    path = urlsplit(url).path
    if not path.startswith(COMPOSE_OPTIONS_PATH):
        return ComposeRefusal("the compose option came from another path")
    urn = unquote(path[len(COMPOSE_OPTIONS_PATH) :])
    if not urn.startswith(COMPOSE_OPTION_URN_PREFIX) or not urn.endswith(")"):
        return ComposeRefusal("the compose option's URN has an unexpected shape")
    parts = urn[len(COMPOSE_OPTION_URN_PREFIX) : -1].split(",")
    if not parts or parts[0] != profile_id:
        return ComposeRefusal("the compose option names another profile")
    if body is None:
        return ComposeRefusal("the compose option's answer could not be read")
    try:
        answer = json.loads(body)
    except ValueError:
        return ComposeRefusal("the compose option's answer is not JSON")
    data = _mapping(answer, "data")
    context = _mapping(data, "composeNavigationContext")
    if data is None or context is None:
        return ComposeRefusal("the compose option's answer has an unexpected shape")
    recipients = context.get("recipientUrns")
    if not isinstance(recipients, list) or recipients != [f"{PROFILE_URN_PREFIX}{profile_id}"]:
        return ComposeRefusal("the compose option names another recipient, or more than one")
    kind = data.get("composeOptionType")
    existing = context.get("existingConversationUrn")
    if kind == ComposeKind.REPLY:
        if (
            not isinstance(existing, str)
            or not existing.startswith(FSD_CONVERSATION_PREFIX)
            or len(existing) == len(FSD_CONVERSATION_PREFIX)
        ):
            return ComposeRefusal("a reply compose option names no conversation")
        return ComposeOption(ComposeKind.REPLY, existing)
    if kind == ComposeKind.CONNECTION_MESSAGE:
        if existing is not None:
            return ComposeRefusal("a new-message compose option names a conversation")
        return ComposeOption(ComposeKind.CONNECTION_MESSAGE, None)
    return ComposeRefusal("the compose option's type is not one the prefill knows")


def _mapping(value: object, key: str) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    inner = value.get(key)
    return inner if isinstance(inner, Mapping) else None


def conversation_from_thread_request(url: str, thread_id: str) -> str | None:
    """The full ``urn:li:msg_conversation:(<owner>,<thread id>)`` a ``messengerMessages``
    request names, when its thread id is ``thread_id``; ``None`` otherwise.

    The inbox poll (P4-01) and send confirmation (P4-02) match a conversation by this
    URN, and the compose option names only ``urn:li:fsd_conversation:<thread id>``, so the
    run reads the page's own thread request that follows the click (ADR 0007, "The
    recipient"). Never the ``fsd_conversation`` form: a URN in another form would stop
    the send from ever being confirmed."""
    split = urlsplit(url)
    if split.path.rstrip("/") != MESSAGING_GRAPHQL_PATH:
        return None
    query = parse_qs(split.query)
    query_ids = query.get("queryId", [])
    if not query_ids or query_ids[0].split(".", 1)[0] != MESSAGES_QUERY:
        return None
    variables = unquote("".join(query.get("variables", [])))
    marker = "conversationUrn:"
    at = variables.find(marker)
    if at < 0:
        return None
    start = at + len(marker)
    prefix = "urn:li:msg_conversation:("
    if not variables.startswith(prefix, start):
        return None
    end = variables.find(")", start)
    if end < 0:
        return None
    urn = variables[start : end + 1]
    inner = urn[len(prefix) : -1].split(",")
    if len(inner) != 2 or not inner[0].startswith(PROFILE_URN_PREFIX) or inner[1] != thread_id:
        return None
    return urn


# --- auto-send's proof that the message went (ADR 0008, #458 review) -------------------

#: The page's own send: ``POST <this path>?action=createMessage``, answered with the message
#: it created (``docs/linkedin-messaging-shapes.md``, "Sending"). netkeeper only reads it.
MESSAGES_CREATE_PATH: Final = "/voyager/api/voyagerMessagingDashMessengerMessages"
CREATE_MESSAGE_ACTION: Final = "createMessage"


def _send_text(text: str) -> str:
    """Text as the send proof compares it: CR and CRLF as one newline, a no-break space as
    a space, and no trailing newline (a composer's last ``<br>``)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").rstrip("\n")


def is_create_message(url: str) -> bool:
    """Whether ``url`` is the page's own send (``?action=createMessage``)."""
    split = urlsplit(url)
    if split.path.rstrip("/") != MESSAGES_CREATE_PATH:
        return False
    return parse_qs(split.query).get("action") == [CREATE_MESSAGE_ACTION]


def send_answer_refusal(
    status: int, body: str | None, expected: str, conversation_urn: str | None
) -> str | None:
    """``None`` when one ``createMessage`` answer proves the message went: status 200, its
    ``value.body.text`` the typed text, and its ``value.conversationUrn`` this
    conversation's when that is known. Otherwise why not, in fixed words."""
    if status != 200:
        return "the send answer was an error"
    if body is None:
        return "the send answer could not be read"
    try:
        answer = json.loads(body)
    except ValueError:
        return "the send answer could not be read"
    value = _mapping(answer, "value")
    sent = _mapping(value, "body")
    text = None if sent is None else sent.get("text")
    if value is None or not isinstance(text, str):
        return "the send answer has an unexpected shape"
    if _send_text(text) != _send_text(expected):
        return "the send answer holds other text"
    if conversation_urn is not None and value.get("conversationUrn") != conversation_urn:
        return "the send answer is for another conversation"
    return None


# --- the source seam ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrefillResult:
    """What one prefill did. ``typing_started_at`` is taken before the first key, and is
    ``None`` when no key was sent. ``wall`` is set when the profile landed on a
    checkpoint, a login wall, or a throttle, with ``wall_url`` (masked by the caller
    before it is logged)."""

    outcome: MessageOutcome
    typing_started_at: datetime | None = None
    wall: Outcome | None = None
    wall_url: str | None = field(default=None, repr=False)
    #: Auto-send only (ADR 0008): when the Send click was sent, taken just before it.
    send_clicked_at: datetime | None = None
    #: Auto-send only: why Send was not clicked after the whole body was typed, in
    #: fixed words. The typed text stays in the composer for the person.
    send_refusal: str | None = None
    #: Auto-send only (ADR 0008, D1): after a landed Send, whether the run closed the
    #: sent bubble, and why not, in fixed words. ``None`` when nothing was sent.
    bubble_closed: bool | None = None
    close_refusal: str | None = None
    #: Auto-send only: why a landed Send click is not proven to have sent the message
    #: (no ``createMessage`` answer, an error, other text). Then nothing is closed.
    send_unconfirmed: str | None = None
    #: A refusal before the Message click that holds auto-send (#444's checks, ADR 0008).
    pre_click_hold: PreClickHold | None = None


Cancelled = Callable[[], Awaitable[bool]]


class PreClickHold(enum.StrEnum):
    """A refusal before the Message click that a person must clear (#444's pre-click
    checks), so an auto-send holds on it (ADR 0008): the next try would find the same."""

    BUBBLE = "bubble"
    """A message bubble or composer is already on the page, or that couldn't be read."""

    COVERED = "covered"
    """No Message control is on screen with nothing over it."""


#: A send gate checked again just before the click: ``None`` when it still holds, or
#: why not, in fixed words.
SendRecheck = Callable[[], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class SendPermit:
    """ADR 0008: the page may click **Send** once, for this run, only while holding one.

    :func:`netkeeper.services.message_send.run_prefill` builds it, and nothing else in
    the package does (``tests/test_browser_safety.py``), only after the run's gates
    held: ``[campaigns] linkedin_auto_send`` on, the step's mode ``auto_send``, a
    scheduled run, inside active hours, heat under its skip threshold, today's
    ``li_messages_auto`` budget not spent, and ``budgets.consume`` for it
    done. ``recheck`` asks the time-bound gates again (the flag, active hours, the
    session flag, heat, a cancel) just before the click, after the dwell.
    """

    recheck: SendRecheck = field(repr=False)


class PrefillSource(Protocol):
    """The page side of one prefill: :class:`netkeeper.linkedin.page_messaging.PagePrefill`.

    ``keys_sent`` is how many keys reached the page so far, so a runner that sees an
    exception can tell ``not_typed`` (none) from ``unknown``. ``message_click_attempted``
    is whether the Message click was sent, and ``message_clicked`` whether it landed: the
    run records both, so the UI knows whether a message bubble may be open (ADR 0007)."""

    @property
    def keys_sent(self) -> int: ...

    @property
    def message_click_attempted(self) -> bool: ...

    @property
    def message_clicked(self) -> bool: ...

    @property
    def message_click_diagnostics(self) -> Mapping[str, str | None]:
        """Which Message control was chosen and why a click that raised failed, as fixed
        categories (#444), for the run's counts; empty when no control was chosen."""
        ...

    async def prefill(
        self,
        spec: MessageJobSpec,
        plan: TypingPlan,
        *,
        cancelled: Cancelled,
        permit: SendPermit | None = None,
    ) -> PrefillResult: ...
