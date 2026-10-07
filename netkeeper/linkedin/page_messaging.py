"""The prefill's page work: open the profile, click Message once, type, stop (P4-03, ADR 0007).

:class:`PagePrefill` is the :class:`~netkeeper.linkedin.messaging.PrefillSource` a
``message_send`` run reads and types through. It drives the page only through
:class:`~netkeeper.linkedin.browser.BrowserRun`'s narrow methods; nothing here clicks,
types, locates, or runs script itself (``tests/test_browser_safety.py`` holds it to
that, as it does the other observing modules). In order:

1. Bring the run's tab to the front, once, before anything else (ADR 0007, decision 1).
2. Navigate to the contact's profile, ``/in/<slug>/``, and classify where it landed.
   A checkpoint or a login wall stops here, with no key sent; the runner sets the
   session flag (spec 9.7).
3. Rest the pointer and scroll briefly, like a person reading the top of the profile.
4. Read the profile's one ``h1``, if there's exactly one, for the chip check, and open
   the two observations the click is read through: the compose option, and the
   page's own thread request (for the conversation's URN). After this, nothing
   observes, scrolls, or navigates again.
5. Click **Message** once (:meth:`~netkeeper.linkedin.browser.BrowserRun.click_message`).
6. Read the compose option the click caused, and check it names the contact
   (:func:`~netkeeper.linkedin.messaging.read_compose_option`). None, or more than one,
   is ``not_typed``.
7. Type the plan into the verified composer
   (:meth:`~netkeeper.linkedin.browser.BrowserRun.type_into_composer`).
8. Auto-send only (ADR 0008): with an ``auto_send`` spec, a
   :class:`~netkeeper.linkedin.messaging.SendPermit`, and the whole body typed, dwell
   (median :data:`SEND_DWELL_MEDIAN_S`) and click **Send** once
   (:meth:`~netkeeper.linkedin.browser.BrowserRun.click_send`), which checks every
   gate and the composer again first. A refusal there leaves the typed text in place:
   ``prefilled`` when a gate or the Send control refused, ``partially_typed`` when the
   composer itself changed. Without both the mode and the permit, nothing clicks Send.
   After a landed click, close the sent bubble by its own close control
   (:meth:`~netkeeper.linkedin.browser.BrowserRun.close_sent_bubble`, decision D1), and
   once it closed, close the run's own tab
   (:meth:`~netkeeper.linkedin.browser.BrowserRun.close_sent_tab`, decision D3).
9. Otherwise dwell briefly. Then hand the tab over
   (:meth:`~netkeeper.linkedin.browser.BrowserRun.hand_over`): it stays open, and is no
   longer netkeeper's. Every outcome after the first key hands it over.

No body, name, slug, or url reaches a log line or a reason from here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote, unquote, urlsplit

from netkeeper.linkedin.browser import (
    BrowserRun,
    BubbleLayout,
    BubbleRecipient,
    SendClick,
    TypingEnd,
    TypingResult,
)
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.enrich import LINKEDIN_ORIGIN
from netkeeper.linkedin.messaging import (
    COMPOSE_OPTIONS_PATH,
    MESSAGES_CREATE_PATH,
    MESSAGING_GRAPHQL_PATH,
    Cancelled,
    ComposeKind,
    ComposeOption,
    ComposeRefusal,
    MessageJobSpec,
    MessageOutcome,
    MessageOutcomeKind,
    PrefillResult,
    SendPermit,
    conversation_from_thread_request,
    is_create_message,
    read_compose_option,
    send_answer_refusal,
    typed_text,
)
from netkeeper.linkedin.observe import (
    Observation,
    ObservationFailed,
    ObservationLimits,
    ResponseMatch,
    ResponseRule,
)
from netkeeper.linkedin.pacing import TypingPlan, scroll_like_a_person
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin

log = logging.getLogger(__name__)

#: How long the compose option may take to arrive after the click, in seconds.
COMPOSE_WAIT_S: Final = 10.0
#: How long the run waits after the first compose option for a second one, in seconds.
#: A second one means it can't tell which bubble the composer belongs to.
COMPOSE_SETTLE_S: Final = 1.5
#: How long the thread request may take to be read after typing, in seconds.
THREAD_WAIT_S: Final = 0.5
#: The pause before the Message click, uniform in this range, in seconds.
CLICK_PAUSE_RANGE_S: Final = (0.6, 1.8)
#: ADR 0008: how long a landed Send waits for the page's createMessage answer, in seconds.
SEND_CONFIRM_WAIT_S: Final = 10.0
#: The dwell after typing, before the hand-over, uniform in this range, in seconds.
DWELL_RANGE_S: Final = (0.8, 2.0)
#: ADR 0008: the dwell between the last key and the Send click, lognormal around this
#: median with this sigma, held inside :data:`SEND_DWELL_RANGE_S`, in seconds.
SEND_DWELL_MEDIAN_S: Final = 4.0
SEND_DWELL_SIGMA: Final = 0.35
SEND_DWELL_RANGE_S: Final = (2.0, 9.0)
#: The brief scroll before the click: a step or two, small, and a short read.
BRIEF_SCROLL_STEPS: Final = (1, 2)
BRIEF_SCROLL_DELTA_PX: Final = (120, 360)
BRIEF_SCROLL_DWELL_S: Final = 1.2
#: The profile path prefix the prefill opens.
PROFILE_PREFIX: Final = "/in/"

_WALLS: Final = frozenset({Outcome.CHECKPOINT, Outcome.LOGGED_OUT, Outcome.THROTTLED})
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _require_origin(origin: str) -> str:
    """LinkedIn's origin, or this machine's loopback for the smoke replica. Nothing else."""
    try:
        parsed = parse_strict_origin(origin)
    except NotAStrictOrigin as exc:
        raise ValueError(f"{origin!r} is not an origin the prefill may open") from exc
    if str(parsed) == LINKEDIN_ORIGIN:
        return LINKEDIN_ORIGIN
    if parsed.scheme in ("http", "https") and parsed.host in _LOOPBACK_HOSTS:
        return str(parsed)
    raise ValueError(f"the prefill opens {LINKEDIN_ORIGIN!r} or loopback, never {origin!r}")


def _on_path(url: str, path: str) -> bool:
    try:
        seen = unquote(urlsplit(url).path).rstrip("/").casefold()
    except ValueError:
        return False
    return seen == unquote(path).rstrip("/").casefold()


def _not_typed(reason: str) -> PrefillResult:
    return PrefillResult(MessageOutcome(MessageOutcomeKind.NOT_TYPED, reason, None, 0))


_KINDS: Final[dict[TypingEnd, MessageOutcomeKind]] = {
    TypingEnd.TYPED: MessageOutcomeKind.PREFILLED,
    TypingEnd.NOT_TYPED: MessageOutcomeKind.NOT_TYPED,
    TypingEnd.PARTIALLY_TYPED: MessageOutcomeKind.PARTIALLY_TYPED,
    TypingEnd.UNKNOWN: MessageOutcomeKind.UNKNOWN,
}


class PagePrefill:
    """One prefill on the run's tab. One instance per run; :meth:`prefill` runs once.

    ``origin`` is LinkedIn's, or loopback for the smoke replica. ``sleep`` waits out
    every pause and the typing delays; ``clock`` stamps the typing's start; ``rng``
    shapes the pauses and the scroll.
    """

    def __init__(
        self,
        run: BrowserRun,
        *,
        origin: str = LINKEDIN_ORIGIN,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = _utcnow,
        rng: random.Random | None = None,
        compose_wait_s: float = COMPOSE_WAIT_S,
        compose_settle_s: float = COMPOSE_SETTLE_S,
        thread_wait_s: float = THREAD_WAIT_S,
        send_confirm_wait_s: float = SEND_CONFIRM_WAIT_S,
    ) -> None:
        self._run = run
        self._origin = _require_origin(origin)
        self._sleep = sleep
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing
        self._compose_wait_s = compose_wait_s
        self._compose_settle_s = compose_settle_s
        self._thread_wait_s = thread_wait_s
        self._send_confirm_wait_s = send_confirm_wait_s
        self._used = False

    @property
    def keys_sent(self) -> int:
        return self._run.keys_sent

    @property
    def message_click_attempted(self) -> bool:
        return self._run.message_click_attempted

    @property
    def message_clicked(self) -> bool:
        return self._run.message_clicked

    @property
    def message_click_diagnostics(self) -> dict[str, str | None]:
        return self._run.message_click_diagnostics

    @property
    def send_attempted(self) -> bool:
        return self._run.send_attempted

    @property
    def tab_closed(self) -> bool:
        """Whether the run closed its own tab after a sent message (ADR 0008, D3)."""
        return self._run.tab_closed

    async def prefill(
        self,
        spec: MessageJobSpec,
        plan: TypingPlan,
        *,
        cancelled: Cancelled,
        permit: SendPermit | None = None,
    ) -> PrefillResult:
        """Run the steps in the module docstring. See there for each refusal."""
        if self._used:
            raise RuntimeError("a PagePrefill prefills once")
        self._used = True
        if spec.recipient_public_id is None:
            return _not_typed("the contact has no public profile id to open")
        path = f"{PROFILE_PREFIX}{quote(spec.recipient_public_id, safe='')}/"
        await self._run.bring_tab_forward()
        page = await self._run.goto(f"{self._origin}{path}")
        landed = classify(200, page.url, "")
        if landed in _WALLS:
            log.warning("prefill: the profile landed on a %s page", landed.value)
            return PrefillResult(
                MessageOutcome(
                    MessageOutcomeKind.NOT_TYPED, f"the page answered {landed.value}", None, 0
                ),
                wall=landed,
                wall_url=page.url,
            )
        if not _on_path(page.url, path):
            return _not_typed("the profile opened somewhere else")
        if await cancelled():
            return _not_typed("cancelled")
        scroll = scroll_like_a_person(
            self._rng,
            steps_range=BRIEF_SCROLL_STEPS,
            delta_range_px=BRIEF_SCROLL_DELTA_PX,
            dwell_median_s=BRIEF_SCROLL_DWELL_S,
        )
        await self._run.scroll(scroll, sleep=self._sleep, rng=self._rng)
        if await cancelled():
            return _not_typed("cancelled")
        profile_name = await self._run.read_profile_heading()
        compose = await self._run.observe(
            ResponseMatch(self._origin, (ResponseRule("GET", COMPOSE_OPTIONS_PATH, prefix=True),))
        )
        threads = await self._run.observe(
            ResponseMatch(self._origin, (ResponseRule("GET", MESSAGING_GRAPHQL_PATH),)),
            limits=ObservationLimits(max_pending=64),
        )
        # ADR 0008: an auto-send's proof that the message went is the page's own
        # createMessage answer, so it is observed from before the click too.
        sends = (
            await self._run.observe(
                ResponseMatch(self._origin, (ResponseRule("POST", MESSAGES_CREATE_PATH),))
            )
            if spec.mode == "auto_send" and permit is not None
            else None
        )
        try:
            click = await self._run.click_message(
                path,
                spec.profile_id,
                pause_s=self._rng.uniform(*CLICK_PAUSE_RANGE_S),
                sleep=self._sleep,
            )
            if not click.clicked:
                return _not_typed(click.refusal or "the Message control was not clicked")
            option = await self._compose_option(compose, spec.profile_id)
            if isinstance(option, ComposeRefusal):
                return _not_typed(option.reason)
            layout = (
                BubbleLayout.EXISTING
                if option.kind is ComposeKind.REPLY
                else BubbleLayout.NEVER_MESSAGED
            )
            # Read before typing: the thread request follows the click, not the keys.
            conversation = await self._conversation(threads, option)
            name_checked: bool | None = None
            if layout is BubbleLayout.NEVER_MESSAGED:
                name_checked = profile_name is not None
                if not name_checked:
                    log.info("prefill: the chip's name check is skipped (no single readable h1)")
            recipient = BubbleRecipient(
                layout, spec.profile_id, spec.recipient_public_id, profile_name
            )
            typing = await self._run.type_into_composer(
                plan,
                recipient,
                clock=self._clock,
                sleep=self._sleep,
                cancelled=cancelled,
                # Exactly one compose option: a later one refuses the authorizing pass.
                another_compose=lambda: compose.kept > 1 or compose.overflowed,
            )
            if typing.end is TypingEnd.TYPED and spec.mode == "auto_send" and permit is not None:
                # ADR 0008: the one Send click, behind every gate, checked again there.
                send = await self._run.click_send(
                    # What typing put in and verified, newlines as typed (\r\n is one).
                    typed_text(plan),
                    recipient,
                    permit=permit,
                    dwell_s=self._send_dwell(),
                    clock=self._clock,
                    sleep=self._sleep,
                    another_compose=lambda: compose.kept > 1 or compose.overflowed,
                )
                result = self._sent(typing, send, conversation, name_checked)
                if send.clicked:
                    unconfirmed = await self._send_proof(sends, typed_text(plan), conversation)
                    if unconfirmed is None:
                        # D1: only a send the page proved is closed after.
                        closing = await self._run.close_sent_bubble(
                            recipient, confirmed=True, sleep=self._sleep
                        )
                        result = replace(
                            result, bubble_closed=closing.closed, close_refusal=closing.refusal
                        )
                    else:
                        log.warning("auto-send: the send was not confirmed (%s)", unconfirmed)
                        result = replace(result, bubble_closed=False, send_unconfirmed=unconfirmed)
                return result
            if typing.end is TypingEnd.TYPED:
                await self._sleep(self._rng.uniform(*DWELL_RANGE_S))
            return self._result(typing, conversation, name_checked)
        finally:
            # An attempted click counts as a click, even one that raised or was
            # cancelled: it may have opened a bubble (ADR 0007, "Handing the tab over").
            if self._run.message_click_attempted:
                await self._end_after_click()

    async def _end_after_click(self) -> None:
        """After any ending that followed an attempted click: close the tab when its sent
        message's bubble closed (ADR 0008, D3), and otherwise hand it over."""
        if self._run.bubble_closed:
            await self._run.close_sent_tab()
        else:
            await self._run.hand_over()

    async def _compose_option(
        self, observation: Observation, profile_id: str
    ) -> ComposeOption | ComposeRefusal:
        """The one compose option the click caused, checked against the contact."""
        try:
            first = await observation.next(self._compose_wait_s)
            if first is None:
                return ComposeRefusal("no compose option was loaded after the click")
            second = await observation.next(self._compose_settle_s)
        except ObservationFailed:
            return ComposeRefusal("the compose option could not be read")
        if second is not None:
            return ComposeRefusal("more than one compose option was loaded")
        if first.status != 200:
            return ComposeRefusal("the compose option was not answered")
        return read_compose_option(first.url, first.text(), profile_id)

    async def _conversation(self, observation: Observation, option: ComposeOption) -> str | None:
        """The existing conversation's full URN, from the page's own thread request, or
        ``None`` (never the compose option's other form, which no poll would match)."""
        thread = option.thread_id
        if thread is None:
            return None
        try:
            while (seen := await observation.next(self._thread_wait_s)) is not None:
                urn = conversation_from_thread_request(seen.url, thread)
                if urn is not None:
                    return urn
        except Exception as exc:
            # Only the report's URN is at stake, never what was typed.
            log.info("prefill: the thread request could not be read (%s)", type(exc).__name__)
            return None
        return None

    async def _send_proof(
        self, observation: Observation | None, typed: str, conversation: str | None
    ) -> str | None:
        """``None`` when the page's own ``createMessage`` answer proves the message went
        (:func:`~netkeeper.linkedin.messaging.send_answer_refusal`); otherwise why not. The
        first such answer within :data:`SEND_CONFIRM_WAIT_S` decides."""
        if observation is None:
            return "the send answer was not observed"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._send_confirm_wait_s
        try:
            while (left := deadline - loop.time()) > 0:
                seen = await observation.next(left)
                if seen is None:
                    break
                if is_create_message(seen.url):
                    return send_answer_refusal(seen.status, seen.text(), typed, conversation)
        except ObservationFailed:
            return "the send answer could not be read"
        return "no send answer was seen"

    def _send_dwell(self) -> float:
        """The pause before Send: lognormal around :data:`SEND_DWELL_MEDIAN_S`, clamped."""
        low, high = SEND_DWELL_RANGE_S
        drawn = self._rng.lognormvariate(math.log(SEND_DWELL_MEDIAN_S), SEND_DWELL_SIGMA)
        return min(max(drawn, low), high)

    def _sent(
        self,
        typing: TypingResult,
        send: SendClick,
        conversation: str | None,
        name_checked: bool | None,
    ) -> PrefillResult:
        """The outcome of an auto-send whose whole body was typed (ADR 0008)."""
        chars = typing.typed_chars
        if send.clicked:
            outcome = MessageOutcome(
                MessageOutcomeKind.SEND_CLICKED,
                "Send was clicked",
                conversation,
                chars,
                name_checked,
            )
        elif send.attempted:
            outcome = MessageOutcome(
                MessageOutcomeKind.UNKNOWN,
                "the Send control could not be clicked",
                conversation,
                chars,
                name_checked,
            )
        elif send.composer_changed:
            outcome = MessageOutcome(
                MessageOutcomeKind.PARTIALLY_TYPED,
                f"before Send: {send.refusal}",
                conversation,
                chars,
                name_checked,
            )
        else:
            # A gate or the Send control refused: the whole body waits, typed, for the
            # person, as any prefill's does. Why it wasn't sent goes on the run's notes.
            outcome = MessageOutcome(
                MessageOutcomeKind.PREFILLED, "typed", conversation, chars, name_checked
            )
        refusal = None if send.clicked else send.refusal
        if refusal is not None:
            log.info("auto-send: Send was not clicked (%s)", refusal)
        return PrefillResult(
            outcome,
            typing_started_at=typing.started_at,
            send_clicked_at=send.clicked_at,
            send_refusal=refusal,
        )

    def _result(
        self, typing: TypingResult, conversation: str | None, name_checked: bool | None
    ) -> PrefillResult:
        kind = _KINDS[typing.end]
        return PrefillResult(
            MessageOutcome(kind, typing.reason, conversation, typing.typed_chars, name_checked),
            typing_started_at=typing.started_at,
        )
