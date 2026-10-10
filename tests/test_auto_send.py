"""Auto-send (P4-04, #384, ADR 0008): one click on Send, behind every gate.

Two levels, both against :mod:`messaging_dom`'s fake profile, which records every Send
click and what the composer held when it came:

- **The page**: :class:`~netkeeper.linkedin.page_messaging.PagePrefill` and
  :meth:`~netkeeper.linkedin.browser.BrowserRun.click_send`, with a hand-made permit.
  Every check ``click_send`` makes before the click refuses with no click.
- **The run**: the scheduler's claim (``claim_auto_send``), the real worker and
  ``message_send``. The flag, the mode, the budget, heat at its skip threshold, active hours and the
  recheck before the click each stop it with no click.

No test here touches a browser, LinkedIn, or the network.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from campaign_fakes import NOW
from inbox_fakes import record_poll
from messaging_dom import Bubble, MessagingSite, MessagingTab
from messaging_pages import THADDEUS, ZEPHYRINE, existing_bubble_html, never_messaged_bubble_html
from run_fakes import fake_provider
from sqlalchemy.orm import Session, sessionmaker
from test_linkedin_steps import LINKEDIN, Lane, make_lane

from netkeeper.config import BudgetSettings, LinkedInSettings, Settings
from netkeeper.linkedin.browser import (
    BrowserRun,
    BrowserUnavailable,
    BubbleLayout,
    BubbleRecipient,
    SendClick,
)
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.messaging import (
    MessageJobSpec,
    MessageOutcomeKind,
    PrefillResult,
    SendPermit,
    plan_typing,
)
from netkeeper.linkedin.page_messaging import (
    SEND_DWELL_MEDIAN_S,
    SEND_DWELL_RANGE_S,
    SEND_DWELL_SIGMA,
    PagePrefill,
)
from netkeeper.models import (
    CampaignStep,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageStatus,
    StepMode,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import (
    budgets,
    linkedin_steps,
    message_send,
    runs,
    scheduled_runs,
    scheduler,
)
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass
from netkeeper.services.linkedin_accounts import (
    arm_scheduled_runs,
    disarm_scheduled_runs,
    ensure_account,
    pause_schedule,
)
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.linkedin_steps import (
    Refusal,
    claim_auto_send,
    claim_prefill,
    record_prefill_outcome,
    waiting_for_you,
)
from netkeeper.services.settings_kv import set_setting
from netkeeper.worker import BrowserWorker

BODY = "Hi Zephyrine, good to see you!"
T0 = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)


def auto_spec(body: str = BODY, mode: str = "auto_send") -> MessageJobSpec:
    return MessageJobSpec(
        recipient_urn=ZEPHYRINE.urn,
        recipient_public_id=ZEPHYRINE.slug,
        body=body,
        mode=mode,  # type: ignore[arg-type]
        typing_seed=3,
    )


def permit(recheck: Callable[[], Awaitable[str | None]] | None = None) -> SendPermit:
    async def holds() -> str | None:
        return None

    return SendPermit(recheck=recheck or holds)


class Steps:
    """The page run's clock and sleep, recording every pause."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.ticks = 0

    def clock(self) -> datetime:
        self.ticks += 1
        return T0 + timedelta(seconds=self.ticks)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


async def page_run(
    site: MessagingSite,
    *,
    spec: MessageJobSpec | None = None,
    send: SendPermit | None = None,
) -> tuple[PrefillResult, BrowserRun, Steps]:
    provider, _ = fake_provider(site)
    steps = Steps()
    the_spec = spec or auto_spec()

    async def never() -> bool:
        return False

    async with provider.run() as run:
        source = PagePrefill(
            run,
            sleep=steps.sleep,
            clock=steps.clock,
            rng=random.Random(5),
            compose_wait_s=0.05,
            compose_settle_s=0.01,
            thread_wait_s=0.01,
            send_confirm_wait_s=0.05,
        )
        result = await source.prefill(
            the_spec,
            plan_typing(the_spec.body, the_spec.typing_seed),
            cancelled=never,
            permit=send,
        )
    return result, run, steps


def sends(site: MessagingSite) -> list[Any]:
    """Every click on a submit button the page took."""
    return [c for c in site.tab.clicks if c.tag == "button" and c.attrs.get("type") == "submit"]


def closes(site: MessagingSite) -> list[Any]:
    """Every click on a bubble's close control the page took."""
    return [c for c in site.tab.clicks if c.tag == "button" and c.attrs.get("type") != "submit"]


# --- the page ---------------------------------------------------------------------------


async def test_an_auto_send_types_the_body_then_clicks_send_once() -> None:
    site = MessagingSite(ZEPHYRINE)
    result, run, steps = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED, result
    assert site.sent == [BODY]  # the page sent exactly the rendered body
    assert len(sends(site)) == 1 and run.send_attempted
    assert result.send_clicked_at is not None and result.typing_started_at is not None
    assert result.typing_started_at < result.send_clicked_at
    assert result.send_refusal is None
    # The dwell before the click: one pause inside the range, after the last key.
    low, high = SEND_DWELL_RANGE_S
    assert any(low <= s <= high for s in steps.sleeps)
    # D1 and D3: the composer emptied, the run closed the sent bubble by its own close
    # control, once, and then its own tab.
    assert result.bubble_closed is True and result.close_refusal is None
    assert len(closes(site)) == 1 and site.closed_bubbles == 1
    assert run.bubble_closed and run.tab_closed and site.tab.is_closed()
    assert not run.handed_over


@pytest.mark.parametrize(
    ("mode", "with_permit"), [("prefill", True), ("auto_send", False), ("prefill", False)]
)
async def test_without_both_the_mode_and_a_permit_nothing_clicks_send(
    mode: str, with_permit: bool
) -> None:
    site = MessagingSite(ZEPHYRINE)
    result, run, _ = await page_run(
        site, spec=auto_spec(mode=mode), send=permit() if with_permit else None
    )
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert sends(site) == [] and site.sent == [] and not run.send_attempted


async def test_a_gate_that_fails_at_the_recheck_leaves_the_text_and_clicks_nothing() -> None:
    site = MessagingSite(ZEPHYRINE)

    async def outside() -> str | None:
        return "outside LinkedIn's active hours"

    result, run, _ = await page_run(site, send=permit(outside))
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "outside LinkedIn's active hours"
    assert sends(site) == [] and not run.send_attempted
    assert site.tab.draft + site.tab.typed == BODY  # the text stays for the person


async def test_a_recheck_that_raises_clicks_nothing() -> None:
    site = MessagingSite(ZEPHYRINE)

    async def broken() -> str | None:
        raise RuntimeError("db gone")

    result, _, _ = await page_run(site, send=permit(broken))
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "the send gates could not be checked"
    assert sends(site) == []


async def _with_change(
    change: Callable[[MessagingTab], None],
) -> tuple[PrefillResult, MessagingSite]:
    site = MessagingSite(ZEPHYRINE)

    async def recheck() -> str | None:
        change(site.tab)
        return None

    result, _, _ = await page_run(site, send=permit(recheck))
    return result, site


async def test_a_composer_whose_text_differs_from_the_body_is_refused() -> None:
    def edit(tab: MessagingTab) -> None:
        tab.typed += "!"
        tab._render_composer()

    result, site = await _with_change(edit)
    assert result.outcome.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert result.outcome.reason == "before Send: the composer's text changed"
    assert sends(site) == [] and site.sent == []


async def test_a_second_composer_is_refused() -> None:
    def another(tab: MessagingTab) -> None:
        composer = tab.composer
        tab.add_html(existing_bubble_html(ZEPHYRINE))
        tab.composer = composer  # the verified one stays the one typed into

    result, site = await _with_change(another)
    assert result.outcome.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert sends(site) == []


async def test_a_composer_that_lost_focus_is_refused() -> None:
    def blur(tab: MessagingTab) -> None:
        tab.focused = None

    result, site = await _with_change(blur)
    assert result.outcome.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert "focus" in result.outcome.reason
    assert sends(site) == []


async def test_a_bubble_for_someone_else_is_refused() -> None:
    def other(tab: MessagingTab) -> None:
        for element in tab.document.elements():
            if element.tag == "a" and element.attrs.get("href", "").startswith("/in/"):
                element.attrs["href"] = "/in/ACoAAAnotherPerson/"

    result, site = await _with_change(other)
    assert result.outcome.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert sends(site) == []


async def test_a_disabled_send_is_not_clicked() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, existing_conversation=None))
    site.send_stays_disabled = True
    result, _, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "the Send control is disabled"
    assert sends(site) == []


async def test_a_never_messaged_bubble_is_sent_once_send_enables() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, existing_conversation=None))
    result, _, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    assert site.sent == [BODY]


def _two_link_card() -> str:
    """#481: the never-messaged card linking the contact twice, by member id and slug."""
    cards = f'<a href="/in/{ZEPHYRINE.slug}/">{ZEPHYRINE.name}</a>'
    return never_messaged_bubble_html([ZEPHYRINE]).replace(
        cards,
        f'<a href="/in/{ZEPHYRINE.profile_id}/"><img alt=""></a>{cards}',
    )


async def test_a_never_messaged_card_linking_the_contact_twice_is_sent_once() -> None:
    bubble = Bubble(ZEPHYRINE, existing_conversation=None, html=_two_link_card())
    site = MessagingSite(ZEPHYRINE, bubble=bubble)
    result, _, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED, result
    assert site.sent == [BODY] and len(sends(site)) == 1


async def test_a_never_messaged_card_that_links_someone_else_before_send_is_not_sent() -> None:
    bubble = Bubble(ZEPHYRINE, existing_conversation=None, html=_two_link_card())
    site = MessagingSite(ZEPHYRINE, bubble=bubble)

    async def recheck() -> str | None:
        photo = next(
            e
            for e in site.tab.document.elements()
            if e.tag == "a" and e.attrs.get("href") == f"/in/{ZEPHYRINE.profile_id}/"
        )
        photo.attrs["href"] = f"/in/{THADDEUS.profile_id}/"
        return None

    result, _, _ = await page_run(site, send=permit(recheck))
    assert result.outcome.kind is MessageOutcomeKind.PARTIALLY_TYPED, result
    assert result.outcome.reason == (
        "before Send: the new-message bubble links to more than one person"
    )
    assert sends(site) == [] and site.sent == []


async def test_a_second_send_button_on_the_page_is_refused() -> None:
    def extra(tab: MessagingTab) -> None:
        tab.add_html('<form><button type="submit">Send</button></form>')

    result, site = await _with_change(extra)
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "the page does not show exactly one Send control"
    assert sends(site) == []


async def test_a_send_that_is_not_the_submit_button_is_refused() -> None:
    def retype(tab: MessagingTab) -> None:
        for element in tab.document.elements():
            if element.tag == "button" and element.attrs.get("type") == "submit":
                element.attrs["type"] = "button"

    result, site = await _with_change(retype)
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "the Send control is not the form's submit button"
    assert sends(site) == []


async def test_a_send_click_that_raises_is_unknown_and_never_tried_again() -> None:
    site = MessagingSite(ZEPHYRINE)
    site.send_error = RuntimeError("detached")
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.UNKNOWN
    assert result.outcome.reason == "the Send control could not be clicked"
    assert len(sends(site)) == 1 and run.send_attempted


async def test_click_send_refuses_without_a_permit_before_typing_and_a_second_time() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    steps = Steps()
    recipient = BubbleRecipient(BubbleLayout.EXISTING, ZEPHYRINE.profile_id, ZEPHYRINE.slug)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        refused = await run.click_send(
            BODY,
            recipient,
            permit=cast(Any, object()),
            dwell_s=0,
            clock=steps.clock,
            sleep=steps.sleep,
        )
        assert refused == SendClick(False, False, "no send permit")
        early = await run.click_send(
            BODY, recipient, permit=permit(), dwell_s=0, clock=steps.clock, sleep=steps.sleep
        )
        assert not early.attempted and early.composer_changed  # nothing typed yet
    assert sends(site) == []
    # And after a landed click, a second call refuses at once.
    site = MessagingSite(ZEPHYRINE)
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    again = await run.click_send(
        BODY, recipient, permit=permit(), dwell_s=0, clock=steps.clock, sleep=steps.sleep
    )
    assert again == SendClick(False, False, "Send was already clicked")
    assert len(sends(site)) == 1


def test_the_send_constants_are_pinned() -> None:
    from netkeeper.linkedin import browser

    assert browser.SEND_CONTROL_ROLE == "button"
    assert browser.SEND_CONTROL_NAME == "Send"
    assert browser.SEND_CONTROL_TYPE == "submit"
    assert browser.COMPOSER_FORM == "xpath=ancestor::form[1]"
    assert SEND_DWELL_MEDIAN_S == 4.0
    assert SEND_DWELL_SIGMA == 0.35
    assert SEND_DWELL_RANGE_S == (2.0, 9.0)
    assert timedelta(minutes=10) == scheduler.AUTO_SEND_INTERVAL
    assert (scheduler.AUTO_SEND_SPACING_MIN, scheduler.AUTO_SEND_SPACING_MAX) == (20.0, 45.0)


def test_the_dwell_has_a_four_second_median() -> None:
    source = PagePrefill.__new__(PagePrefill)
    source._rng = random.Random(1)
    draws = sorted(source._send_dwell() for _ in range(2001))
    assert 3.6 < draws[1000] < 4.4
    assert draws[0] >= 2.0 and draws[-1] <= 9.0


# --- the run ----------------------------------------------------------------------------

AUTO = Settings(
    campaigns=replace(Settings().campaigns, linkedin_auto_send=True),
    linkedin=LinkedInSettings(timezone="UTC"),
)


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.at = start

    def __call__(self) -> datetime:
        self.at += timedelta(milliseconds=10)
        return self.at


async def no_sleep(seconds: float) -> None:
    return None


def fast_source(run: BrowserRun, *, sleep: Any, clock: Any) -> PagePrefill:
    return PagePrefill(
        run,
        sleep=no_sleep,
        clock=clock,
        rng=random.Random(2),
        compose_wait_s=0.05,
        compose_settle_s=0.01,
        thread_wait_s=0.01,
        send_confirm_wait_s=0.05,
    )


def _auto_steps(session: Session, user: User) -> None:
    for step in session.scalars(scoped(user, CampaignStep)):
        step.mode = StepMode.AUTO_SEND


@pytest.fixture
def lane(session_factory: sessionmaker[Session]) -> Lane:
    made = make_lane(session_factory, channels=(LINKEDIN, LINKEDIN), settings=AUTO)
    made.write(_auto_steps)
    made.write(lambda s, u: arm_scheduled_runs(s, u, now=NOW - timedelta(days=1)))
    return made


CONTACT = {"li_urn": ZEPHYRINE.urn, "li_public_id": ZEPHYRINE.slug, "first_name": "Zephyrine"}


class Auto:
    """One auto-send claimed by the scheduler's claim and run by the real worker."""

    def __init__(
        self,
        lane: Lane,
        site: MessagingSite | None = None,
        *,
        worker_settings: Settings = AUTO,
        claim_settings: Settings = AUTO,
        clock: Clock | None = None,
    ) -> None:
        self.lane = lane
        self.site = site or MessagingSite(ZEPHYRINE)
        self.provider, self.connector = fake_provider(self.site)
        self.clock = clock or Clock()
        self.worker = BrowserWorker(
            self.provider,
            lane.factory,
            worker_settings.linkedin,
            clock=self.clock,
            sleep=no_sleep,
            prefill_sources=fast_source,
            campaign_settings=worker_settings,
        )
        self.enrollment_id = lane.enroll(contact=dict(CONTACT))
        lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
        claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=claim_settings))
        assert claim is not None and claim.claimed, claim
        assert claim.run_id is not None and claim.message_id is not None
        self.run_id = claim.run_id
        self.message_id = claim.message_id

    async def execute(self) -> runs.RunOutcome:
        return await self.worker.execute(self.run_id, self.lane.user_id)

    def message(self) -> Message | None:
        return self.lane.read(lambda s, u: get_scoped(s, u, Message, self.message_id))

    def run(self) -> SyncRun:
        return self.lane.read(lambda s, u: runs.get_run(s, u, self.run_id))

    def spent(self, action: ActionClass) -> int:
        def read(session: Session, user: User) -> int:
            account = ensure_account(session, user)
            return budgets.status(
                session, user, account.id, action, now=NOW, settings=BudgetSettings()
            ).day.count

        return self.lane.read(read)


def _given_back_unopened(a: Auto, why: str) -> None:
    """Stopped before opening anything: the step is due again at its claim, never a Try
    again, and the run says why (#458 final review)."""
    assert a.message() is None
    enrollment = a.lane.enrollment(a.enrollment_id)
    assert enrollment.next_action_at == NOW
    assert enrollment.not_sent_count == 0 and enrollment.not_sent_error is None
    run = a.run()
    assert run.notes == f"not started: {why}" and (run.counts_json or {})["opened"] is False
    assert a.enrollment_id not in [
        row.enrollment.id
        for row in a.lane.read(lambda s, u: linkedin_steps.try_again(s, u, now=NOW, settings=AUTO))
    ]


def _no_send(a: Auto) -> None:
    assert a.site.sent == []
    assert not [c for p in a.site.pages for c in p.clicks if c.tag == "button"]  # type: ignore[attr-defined]


async def test_an_auto_send_run_clicks_send_and_waits_for_the_inbox_poll(lane: Lane) -> None:
    a = Auto(lane)
    assert a.run().trigger is SyncRunTrigger.SCHEDULED
    await a.execute()
    message = a.message()
    assert message is not None and message.status is MessageStatus.PREFILLED
    assert message.send_clicked_at is not None and message.prefilled_at is not None
    assert message.prefilled_at < message.send_clicked_at
    assert a.site.sent == [message.body_rendered]
    run = a.run()
    assert (run.status, run.stop_reason) == (SyncRunStatus.COMPLETED, "send_clicked")
    assert (run.counts_json or {})["send_clicked"] is True
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 1
    assert a.spent(ActionClass.PROFILE_VISITS) == 1
    assert a.spent(ActionClass.LI_PREFILLS) == 0  # never charged to prefills
    # It waits on the inbox poll, not on the person: not listed, holds no slot.
    rows, _ = lane.read(lambda s, u: waiting_for_you(s, u, limit=10))
    assert rows == []
    other = lane.enroll(contact={"li_urn": "urn:li:fsd_profile:ACoAAOther1", "li_public_id": "o"})
    lane.write(lambda s, u: record_poll(s, u, NOW))
    claim = lane.write(lambda s, u: claim_prefill(s, u, other, now=NOW, settings=AUTO))
    assert claim.claimed, claim.reasons


async def test_with_the_flag_off_at_the_worker_nothing_opens_and_nothing_is_sent(
    lane: Lane,
) -> None:
    off = replace(AUTO, campaigns=replace(AUTO.campaigns, linkedin_auto_send=False))
    a = Auto(lane, worker_settings=off)
    await a.execute()
    assert a.site.navigations == [] and a.connector.attaches == 0
    _no_send(a)
    _given_back_unopened(a, "auto-send is off")
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 0


async def test_a_step_no_longer_auto_send_is_not_typed(lane: Lane) -> None:
    a = Auto(lane)

    def back_to_prefill(session: Session, user: User) -> None:
        for step in session.scalars(scoped(user, CampaignStep)):
            step.mode = StepMode.PREFILL

    lane.write(back_to_prefill)
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    _given_back_unopened(a, "the step is not an auto-send step")


async def test_a_spent_auto_send_budget_stops_it_before_the_navigation(lane: Lane) -> None:
    def spend(session: Session, user: User) -> None:
        account = ensure_account(session, user)
        key = budgets._day_key(account.id, ActionClass.LI_MESSAGES_AUTO, NOW.date())
        from netkeeper.services.settings_kv import set_setting

        limit = budgets._limits_for(ActionClass.LI_MESSAGES_AUTO, AUTO.linkedin.budget).day
        set_setting(session, user, key, limit)  # at the limit: consume alone would allow one

    a = Auto(lane)
    lane.write(spend)
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    _given_back_unopened(a, "today's auto-send budget is spent")


async def test_heat_pauses_auto_send_and_never_shrinks_its_budget(lane: Lane) -> None:
    def warm(session: Session, user: User) -> int:
        account = ensure_account(session, user)
        heat_service.raise_heat(session, user, account.id, now=NOW, settings=AUTO.linkedin.heat)
        return account.id

    account_id = lane.write(warm)
    day = budgets._limits_for(ActionClass.LI_MESSAGES_AUTO, AUTO.linkedin.budget).day
    count, limit = lane.read(
        lambda s, u: message_send.auto_send_allowance(s, u, account_id, now=NOW, settings=AUTO)
    )
    assert (count, limit) == (0, day)  # warm, under the skip threshold: no shrink

    def hot(session: Session, user: User) -> None:
        for _ in range(10):
            heat_service.raise_heat(session, user, account_id, now=NOW, settings=AUTO.linkedin.heat)

    a = Auto(lane)
    lane.write(hot)
    assert lane.read(
        lambda s, u: heat_service.should_skip(
            s, u, account_id, now=NOW, settings=AUTO.linkedin.heat
        )
    )
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    assert a.message() is None
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 0


async def test_outside_active_hours_nothing_is_sent(lane: Lane) -> None:
    closed = replace(
        AUTO, linkedin=LinkedInSettings(timezone="UTC", active_hours=("01:00", "02:00"))
    )
    a = Auto(lane, worker_settings=closed)
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    _given_back_unopened(a, "outside LinkedIn's active hours")


async def test_a_flag_raised_while_typing_stops_the_click_and_leaves_it_prefilled(
    lane: Lane,
) -> None:
    site = MessagingSite(ZEPHYRINE)
    a = Auto(lane, site)
    body = (a.message() or Message()).body_rendered or ""
    last = len(plan_typing(body, 0))  # every plan for this body has this many steps

    def flag(tab: MessagingTab) -> None:
        lane.write(lambda s, u: flag_session(s, u, Outcome.CHECKPOINT, url="https://x.test/"))

    site.after_key[last] = flag
    await a.execute()
    _no_send(a)
    message = a.message()
    assert message is not None and message.status is MessageStatus.PREFILLED
    assert message.send_clicked_at is None  # it waits for the person
    assert a.run().notes == "not sent: the LinkedIn session is flagged"
    rows, _ = lane.read(lambda s, u: waiting_for_you(s, u, limit=10))
    assert [r.message.id for r in rows] == [message.id]


async def test_a_person_prefilling_an_auto_send_step_never_clicks_send(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    worker = BrowserWorker(
        provider,
        lane.factory,
        AUTO.linkedin,
        clock=Clock(),
        sleep=no_sleep,
        prefill_sources=fast_source,
        campaign_settings=AUTO,
    )
    enrollment = lane.enroll(contact=dict(CONTACT))
    claim = lane.claim(enrollment)
    assert claim.claimed and claim.run_id is not None
    await worker.execute(claim.run_id, lane.user_id)
    assert site.sent == [] and not [c for c in site.tab.clicks if c.tag == "button"]
    message = lane.message(claim.message_id or 0)
    assert message is not None and message.status is MessageStatus.PREFILLED
    assert message.send_clicked_at is None


# --- the claim and the run row -----------------------------------------------------------


def test_claim_auto_send_is_none_with_the_flag_off(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    off = replace(AUTO, campaigns=replace(AUTO.campaigns, linkedin_auto_send=False))
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=off)) is None
    assert lane.runs() == []


def test_claim_auto_send_skips_prefill_steps(session_factory: sessionmaker[Session]) -> None:
    plain = make_lane(session_factory, settings=AUTO)
    plain.write(lambda s, u: arm_scheduled_runs(s, u, now=NOW - timedelta(days=1)))
    plain.enroll(contact=dict(CONTACT))
    assert plain.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO)) is None
    assert plain.runs() == []


def test_claim_auto_send_on_a_disarmed_account_records_no_run(
    session_factory: sessionmaker[Session],
) -> None:
    disarmed = make_lane(session_factory, settings=AUTO)
    disarmed.write(_auto_steps)
    disarmed.enroll(contact=dict(CONTACT))
    claim = disarmed.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and Refusal.RUN_REFUSED in claim.reasons
    assert disarmed.runs() == []


def test_an_auto_send_claim_refuses_a_prefill_step_and_the_flag_off(lane: Lane) -> None:
    from netkeeper.services.linkedin_steps import _Claimer, start_auto_send_run

    enrollment = lane.enroll(contact=dict(CONTACT))
    off = replace(AUTO, campaigns=replace(AUTO.campaigns, linkedin_auto_send=False))
    refused = lane.write(
        lambda s, u: _Claimer(s, u, NOW, off, start_auto_send_run, auto=True).claim(enrollment)
    )
    assert refused.reasons == (Refusal.AUTO_SEND_OFF,)

    def to_prefill(session: Session, user: User) -> None:
        for step in session.scalars(scoped(user, CampaignStep)):
            step.mode = StepMode.PREFILL

    lane.write(to_prefill)
    refused = lane.write(
        lambda s, u: _Claimer(s, u, NOW, AUTO, start_auto_send_run, auto=True).claim(enrollment)
    )
    assert refused.reasons == (Refusal.NOT_AUTO_SEND,)


def test_create_run_takes_a_scheduled_message_send_only_with_the_auto_send_gate(
    lane: Lane,
) -> None:
    def make(trigger: SyncRunTrigger, gate: object) -> Any:
        return lane.write(
            lambda s, u: runs.create_run(
                s, u, SyncRunKind.MESSAGE_SEND, trigger=trigger, now=NOW, gate=gate
            )
        )

    for trigger, gate in (
        (SyncRunTrigger.SCHEDULED, None),
        (SyncRunTrigger.SCHEDULED, runs.MESSAGE_SEND_GATE),
        (SyncRunTrigger.MANUAL, runs.AUTO_SEND_GATE),
        (SyncRunTrigger.MANUAL, None),
    ):
        with pytest.raises(runs.RunError):
            make(trigger, gate)
    assert make(SyncRunTrigger.SCHEDULED, runs.AUTO_SEND_GATE).trigger is SyncRunTrigger.SCHEDULED


def test_an_auto_sent_message_is_listed_only_once_stale(lane: Lane) -> None:
    from netkeeper.linkedin.messaging import MessageOutcome
    from netkeeper.services import campaign_engine as engine

    enrollment = lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and claim.message_id is not None and claim.enrollment_id == enrollment
    outcome = MessageOutcome(MessageOutcomeKind.SEND_CLICKED, "Send was clicked", None, 12)
    lane.write(
        lambda s, u: record_prefill_outcome(
            s, u, claim.message_id or 0, outcome, settings=AUTO, now=NOW, prefilled_at=NOW
        )
    )
    assert lane.read(lambda s, u: waiting_for_you(s, u, limit=10))[0] == []
    later = NOW + engine.PREFILL_STALE_AFTER + timedelta(minutes=1)
    lane.write(lambda s, u: engine.mark_stale(s, u, now=later))
    rows, _ = lane.read(lambda s, u: waiting_for_you(s, u, limit=10))
    assert [r.auto_sent for r in rows] == [True]


# --- the scheduler -------------------------------------------------------------------------


def test_auto_send_is_scheduled_only_with_the_flag_on() -> None:
    assert scheduler.JobKind.AUTO_SEND not in scheduler.served_schedules(False)
    assert scheduler.JobKind.AUTO_SEND not in scheduler.SERVED_SCHEDULES
    on = scheduler.served_schedules(True)
    assert on[scheduler.JobKind.AUTO_SEND] == scheduler.AUTO_SEND_SCHEDULE
    assert on[scheduler.JobKind.AUTO_SEND].respect_active_hours


class _NoTasks:
    def submit(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("nothing may be submitted")


def _no_database() -> Any:
    raise AssertionError("the handler opened a session with auto-send off")


async def test_the_handler_claims_nothing_with_the_flag_off(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    off = replace(AUTO, campaigns=replace(AUTO.campaigns, linkedin_auto_send=False))
    handler = scheduled_runs._auto_send_handler(
        cast(Any, _no_database),  # the flag is checked before any session
        cast(Any, None),
        cast(Any, _NoTasks()),
        off,
        clock=lambda: NOW,
    )
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    ctx = scheduler.JobContext(lane.user_id, account, scheduler.JobKind.AUTO_SEND, NOW, False)
    assert await handler(ctx) is scheduler.JobOutcome.NOTHING_TO_SEND
    assert lane.runs() == []


async def test_the_handler_waits_out_the_spacing(lane: Lane) -> None:
    a = Auto(lane)
    await a.execute()
    lane.enroll(contact={"li_urn": "urn:li:fsd_profile:ACoAAOther2", "li_public_id": "p"})
    lane.write(lambda s, u: record_poll(s, u, NOW))
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    ctx = scheduler.JobContext(lane.user_id, account, scheduler.JobKind.AUTO_SEND, NOW, False)
    soon = NOW + timedelta(minutes=19)
    handler = scheduled_runs._auto_send_handler(
        lane.factory,
        cast(Any, None),
        cast(Any, _NoTasks()),
        AUTO,
        clock=lambda: soon,
    )
    assert await handler(ctx) is scheduler.JobOutcome.NOTHING_TO_SEND
    left = lane.read(lambda s, u: scheduled_runs.auto_send_spacing_left(s, u, soon))
    assert timedelta(minutes=1) <= left <= timedelta(minutes=26)
    later = NOW + timedelta(minutes=46)
    assert lane.read(lambda s, u: scheduled_runs.auto_send_spacing_left(s, u, later)) == timedelta(
        0
    )


def test_the_budget_warning_shows_only_with_auto_send_on_above_twenty() -> None:
    from netkeeper.services.posture import _auto_send

    high = replace(
        AUTO, linkedin=replace(AUTO.linkedin, budget=BudgetSettings(li_messages_auto_per_day=25))
    )
    warning = linkedin_steps.auto_send_budget_warning(high)
    assert warning is not None and "25 a day, above 20 a day" in warning
    assert linkedin_steps.auto_send_budget_warning(AUTO) is None  # 15 a day
    off = replace(high, campaigns=replace(high.campaigns, linkedin_auto_send=False))
    assert linkedin_steps.auto_send_budget_warning(off) is None
    assert budgets.LI_MESSAGE_WARN_ABOVE == 20
    assert warning in _auto_send(high).warnings
    assert len(_auto_send(AUTO).warnings) == 1  # the auto-send warning alone


async def test_click_send_refuses_after_a_partly_typed_body() -> None:
    site = MessagingSite(ZEPHYRINE)

    def blur(tab: MessagingTab) -> None:
        tab.focused = None

    site.after_key[3] = blur
    provider, _ = fake_provider(site)
    steps = Steps()
    recipient = BubbleRecipient(BubbleLayout.EXISTING, ZEPHYRINE.profile_id, ZEPHYRINE.slug)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        click = await run.click_message(
            f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0, sleep=steps.sleep
        )
        assert click.clicked
        typing = await run.type_into_composer(
            plan_typing(BODY, 3), recipient, clock=steps.clock, sleep=steps.sleep
        )
        assert typing.end.value == "partially_typed"
        refused = await run.click_send(
            BODY, recipient, permit=permit(), dwell_s=0, clock=steps.clock, sleep=steps.sleep
        )
        assert refused == SendClick(False, False, "the whole body was not typed", True)
        await run.hand_over()
    assert sends(site) == []


# --- #458 review: the bubble and tab closes (D1, D3), and the Send control's edges -------


async def test_a_sent_bubble_that_does_not_empty_is_left_open_with_the_tab() -> None:
    site = MessagingSite(ZEPHYRINE)
    site.send_clears = False
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    assert result.bubble_closed is False
    assert result.close_refusal == "the composer did not empty after Send"
    assert closes(site) == [] and not site.tab.is_closed() and run.handed_over


async def test_a_close_click_that_raises_or_is_ignored_leaves_the_tab() -> None:
    for error, ignored, refusal in (
        (RuntimeError("detached"), False, "the bubble's close control could not be clicked"),
        (None, True, "the bubble did not close"),
    ):
        site = MessagingSite(ZEPHYRINE)
        site.close_error = error
        site.close_ignored = ignored
        result, run, _ = await page_run(site, send=permit())
        assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
        assert (result.bubble_closed, result.close_refusal) == (False, refusal)
        assert site.closed_bubbles == 1  # one close click, never a second
        assert not site.tab.is_closed() and run.handed_over and not run.tab_closed


async def test_a_never_messaged_bubble_is_not_closed_after_send() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, existing_conversation=None))
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    assert result.close_refusal == "the sent message is not in one conversation bubble"
    assert closes(site) == [] and not site.tab.is_closed() and run.handed_over


async def test_an_uncertain_send_never_closes_anything() -> None:
    site = MessagingSite(ZEPHYRINE)
    site.send_error = RuntimeError("detached")
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.UNKNOWN
    assert result.bubble_closed is None and closes(site) == []
    assert not site.tab.is_closed() and run.handed_over


async def test_close_sent_bubble_and_close_sent_tab_refuse_without_a_landed_send() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    recipient = BubbleRecipient(BubbleLayout.EXISTING, ZEPHYRINE.profile_id, ZEPHYRINE.slug)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        closing = await run.close_sent_bubble(recipient, confirmed=True, sleep=Steps().sleep)
        assert closing.refusal == "Send did not land" and not closing.attempted
        with pytest.raises(RuntimeError):
            await run.close_sent_tab()
    assert closes(site) == []


async def _send_with_change(
    change: Callable[[MessagingTab], None],
) -> tuple[PrefillResult, MessagingSite]:
    return await _with_change(change)


def _submit(tab: MessagingTab) -> Any:
    [button] = [
        e for e in tab.document.elements() if e.tag == "button" and e.attrs.get("type") == "submit"
    ]
    return button


async def test_send_only_in_another_form_is_refused() -> None:
    def move(tab: MessagingTab) -> None:
        button = _submit(tab)
        assert button.parent is not None
        button.parent.children.remove(button)
        tab.add_html('<form><button type="submit">Send</button></form>')

    result, site = await _send_with_change(move)
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "the Send control is not in the composer's form"
    assert sends(site) == []


async def test_a_composer_outside_any_form_is_refused() -> None:
    def unwrap(tab: MessagingTab) -> None:
        for element in list(tab.document.elements()):
            if element.tag == "form":
                element.tag = "div"

    result, site = await _send_with_change(unwrap)
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "the composer is not in one message form"
    assert sends(site) == []


async def test_a_hidden_send_is_refused() -> None:
    def hide(tab: MessagingTab) -> None:
        _submit(tab).attrs["style"] = "display: none"

    result, site = await _send_with_change(hide)
    assert result.send_refusal == "the Send control is not visible"
    assert sends(site) == []


async def test_a_second_hidden_send_on_the_page_is_refused() -> None:
    def extra(tab: MessagingTab) -> None:
        tab.add_html('<form hidden><button type="submit">Send</button></form>')

    result, site = await _send_with_change(extra)
    assert result.send_refusal == "the page does not show exactly one Send control"
    assert sends(site) == []


async def test_a_compose_option_that_arrives_during_the_dwell_is_refused() -> None:
    from messaging_pages import compose_option_answer, compose_option_url

    def late(tab: MessagingTab) -> None:
        tab.emit(
            compose_option_url(ZEPHYRINE),
            compose_option_answer(ZEPHYRINE, existing_conversation=7),
        )

    result, site = await _send_with_change(late)
    assert result.outcome.kind is MessageOutcomeKind.PARTIALLY_TYPED
    assert result.outcome.reason == "before Send: another_compose"
    assert sends(site) == []


async def test_a_send_control_read_that_raises_refuses_with_no_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(send: Any) -> str | None:
        raise RuntimeError("read failed")

    monkeypatch.setattr(BrowserRun, "_send_refusal", staticmethod(broken))
    site = MessagingSite(ZEPHYRINE)
    result, _, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.PREFILLED
    assert result.send_refusal == "the Send control could not be read"
    assert sends(site) == []


async def test_click_send_refuses_when_the_message_click_did_not_land() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    steps = Steps()
    recipient = BubbleRecipient(BubbleLayout.EXISTING, ZEPHYRINE.profile_id, ZEPHYRINE.slug)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        click = await run.click_message(
            f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0, sleep=steps.sleep
        )
        assert click.clicked
        typing = await run.type_into_composer(
            plan_typing(BODY, 3), recipient, clock=steps.clock, sleep=steps.sleep
        )
        assert typing.end.value == "typed"
        run._message_click_landed = False  # as if the click had raised
        refused = await run.click_send(
            BODY, recipient, permit=permit(), dwell_s=0, clock=steps.clock, sleep=steps.sleep
        )
        assert refused == SendClick(False, False, "no tab with a clicked Message control", True)
        await run.hand_over()
    assert sends(site) == []


@pytest.mark.parametrize("body", ["Hi\r\nthere", "Hi\rthere"])
async def test_a_body_with_carriage_returns_is_sent_as_typed(body: str) -> None:
    site = MessagingSite(ZEPHYRINE)
    result, _, _ = await page_run(site, spec=auto_spec(body), send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED, result
    assert site.sent == ["Hi\nthere"]


# --- #458 review: the run's live gates, the hold, the refund ------------------------------


def _last_key_does(a: Auto, act: Callable[[Session, User], Any]) -> None:
    body = (a.message() or Message()).body_rendered or ""
    last = len(plan_typing(body, 0))
    a.site.after_key[last] = lambda tab: a.lane.write(act)


@pytest.mark.parametrize(
    ("act", "why"),
    [
        (lambda s, u: disarm_scheduled_runs(s, u), "scheduled runs are disarmed"),
        (lambda s, u: pause_schedule(s, u, now=NOW), "the schedule is paused"),
    ],
)
async def test_disarming_or_pausing_after_the_last_key_stops_the_click(
    lane: Lane, act: Callable[[Session, User], Any], why: str
) -> None:
    a = Auto(lane)
    _last_key_does(a, act)
    await a.execute()
    _no_send(a)
    message = a.message()
    assert message is not None and message.status is MessageStatus.PREFILLED
    assert a.run().notes == f"not sent: {why}"
    rows, _ = lane.read(lambda s, u: waiting_for_you(s, u, limit=10))
    from netkeeper.web.api.linkedin_steps import _auto_send_notes, _not_sent_reason

    run_id = rows[0].message.sync_run_id
    notes = lane.read(lambda s, u: _auto_send_notes(s, u, [run_id]))
    assert _not_sent_reason(notes.get(run_id)) == why


async def test_a_landed_send_with_its_bubble_closed_holds_nothing(lane: Lane) -> None:
    a = Auto(lane)
    await a.execute()
    assert (a.run().counts_json or {})["tab_closed"] is True
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    assert lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account)) is None


async def test_an_auto_send_that_leaves_a_bubble_holds_auto_send_until_resumed(
    lane: Lane,
) -> None:
    from netkeeper.services.posture import _auto_send

    a = Auto(lane, MessagingSite(ZEPHYRINE, before=existing_bubble_html(ZEPHYRINE)))
    await a.execute()
    _no_send(a)
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    hold = lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account))
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_BUBBLE
    assert hold.run_id == a.run_id
    # Nothing else is claimed or sent while it holds.
    lane.enroll(contact={"li_urn": "urn:li:fsd_profile:ACoAAOther3", "li_public_id": "q"})
    lane.write(lambda s, u: record_poll(s, u, NOW))
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO)) is None
    ctx = scheduler.JobContext(lane.user_id, account, scheduler.JobKind.AUTO_SEND, NOW, False)
    later = NOW + timedelta(hours=2)
    handler = scheduled_runs._auto_send_handler(
        lane.factory, cast(Any, None), cast(Any, _NoTasks()), AUTO, clock=lambda: later
    )
    assert await handler(ctx) is scheduler.JobOutcome.NOTHING_TO_SEND
    # Posture says why and how to clear it.
    [_, held] = _auto_send(AUTO, hold).warnings
    assert "auto-send is held" in held and "resume auto-send" in held
    # The person resumes; then it may claim again.
    assert lane.write(lambda s, u: linkedin_steps.resume_auto_send(s, u, account))
    assert not lane.write(lambda s, u: linkedin_steps.resume_auto_send(s, u, account))
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO)) is not None


async def test_a_refusal_before_any_key_refunds_li_messages_auto_not_the_visit(
    lane: Lane,
) -> None:
    a = Auto(lane, MessagingSite(ZEPHYRINE, profile_html="<main><h1>No controls</h1></main>"))
    await a.execute()
    assert a.message() is None  # not_typed: the claim is given back
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 0
    assert a.spent(ActionClass.PROFILE_VISITS) == 1


async def test_a_failure_after_the_send_click_is_never_not_typed(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempted Send counts as a key: whatever fails after it is ``unknown``."""
    a = Auto(lane)

    async def boom(self: BrowserRun, recipient: Any, **kwargs: Any) -> Any:
        raise RuntimeError("after send")

    monkeypatch.setattr(BrowserRun, "close_sent_bubble", boom)
    await a.execute()
    message = a.message()
    assert message is not None and message.status is MessageStatus.FAILED
    assert (message.error or "").startswith("unknown:")
    assert message_send._after_failure(0, "x", send_attempted=True).kind is (
        MessageOutcomeKind.UNKNOWN
    )


def test_spacing_counts_the_newest_message_send_run_hand_prefills_included(
    lane: Lane,
) -> None:
    def add(session: Session, user: User) -> None:
        account = ensure_account(session, user)
        for minutes, trigger in ((90, SyncRunTrigger.SCHEDULED), (1, SyncRunTrigger.MANUAL)):
            session.add(
                SyncRun(
                    user_id=user.id,
                    linkedin_account_id=account.id,
                    kind=SyncRunKind.MESSAGE_SEND,
                    status=SyncRunStatus.COMPLETED,
                    trigger=trigger,
                    started_at=NOW - timedelta(minutes=minutes),
                    completed_at=NOW - timedelta(minutes=minutes),
                )
            )

    lane.write(add)
    left = lane.read(lambda s, u: scheduled_runs.auto_send_spacing_left(s, u, NOW))
    assert timedelta(minutes=18) <= left <= timedelta(minutes=44)


def test_the_claim_refuses_when_li_messages_auto_is_spent(lane: Lane) -> None:
    from netkeeper.services.settings_kv import set_setting

    def spend(session: Session, user: User) -> None:
        account = ensure_account(session, user)
        key = budgets._day_key(account.id, ActionClass.LI_MESSAGES_AUTO, NOW.date())
        limit = budgets._limits_for(ActionClass.LI_MESSAGES_AUTO, AUTO.linkedin.budget).day
        set_setting(session, user, key, limit)

    lane.enroll(contact=dict(CONTACT))
    lane.write(spend)
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    # A spent day claims nothing at all: no aborted run fills the runs list.
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO)) is None
    assert lane.runs() == []
    # And the claim's own check refuses it too, as browser_out_of_budget.
    from netkeeper.services.linkedin_steps import _Claimer, start_auto_send_run

    [enrollment] = [e.id for e in lane.read(lambda s, u: list(s.scalars(scoped(u, Enrollment))))]
    refused = lane.write(
        lambda s, u: _Claimer(s, u, NOW, AUTO, start_auto_send_run, auto=True).claim(enrollment)
    )
    assert "browser_out_of_budget" in refused.reasons
    assert lane.runs() == []


def test_a_hot_account_claims_nothing(lane: Lane) -> None:
    lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))

    def hot(session: Session, user: User) -> None:
        account = ensure_account(session, user).id
        for _ in range(10):
            heat_service.raise_heat(session, user, account, now=NOW, settings=AUTO.linkedin.heat)

    lane.write(hot)
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO)) is None
    assert lane.runs() == []


@pytest.mark.parametrize("state", ["disarmed", "paused"])
async def test_the_handler_checks_arming_and_the_pause_itself(lane: Lane, state: str) -> None:
    lane.enroll(contact=dict(CONTACT))
    if state == "disarmed":
        lane.write(lambda s, u: disarm_scheduled_runs(s, u))
    else:
        lane.write(lambda s, u: pause_schedule(s, u, now=NOW))
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    ctx = scheduler.JobContext(lane.user_id, account, scheduler.JobKind.AUTO_SEND, NOW, False)
    handler = scheduled_runs._auto_send_handler(
        lane.factory, cast(Any, None), cast(Any, _NoTasks()), AUTO, clock=lambda: NOW
    )
    expected = (
        scheduler.JobOutcome.DISARMED_AFTER_GATE
        if state == "disarmed"
        else scheduler.JobOutcome.PAUSED_AFTER_GATE
    )
    assert await handler(ctx) is expected
    assert lane.runs() == []


def test_the_claim_reaches_an_auto_send_step_behind_a_long_prefill_backlog(lane: Lane) -> None:
    import factories
    from campaign_fakes import make_mailbox

    from netkeeper.models import Campaign

    def backlog(session: Session, user: User) -> None:
        mailbox = make_mailbox(session, user, email="backlog@example.test")
        other = factories.make_campaign(
            session, user, channels=(LINKEDIN,), mailbox_id=mailbox.id, name="Backlog"
        )
        for step in other.steps:
            step.mode = StepMode.PREFILL
        for n in range(linkedin_steps.READY_PAGE_MAX + 5):
            contact = factories.make_contact(
                session,
                user,
                emails=[f"b{n}@example.test"],
                li_urn=f"urn:li:fsd_profile:ACoAABacklog{n}",
                li_public_id=f"backlog-{n}",
            )
            factories.make_enrollment(
                session, other, contact, next_action_at=NOW - timedelta(days=1, minutes=n)
            )
        assert isinstance(other, Campaign)

    lane.write(backlog)
    enrollment = lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and claim.claimed and claim.enrollment_id == enrollment


@pytest.mark.parametrize(("per_day", "shown"), [(20, None), (21, 21), (500, None)])
def test_the_budget_warning_starts_above_twenty_and_reads_the_clamped_limit(
    per_day: int, shown: int | None
) -> None:
    settings = replace(
        AUTO,
        linkedin=replace(AUTO.linkedin, budget=BudgetSettings(li_messages_auto_per_day=per_day)),
    )
    warning = linkedin_steps.auto_send_budget_warning(settings)
    hard = budgets.HARD_MAX_PER_DAY[ActionClass.LI_MESSAGES_AUTO]
    expected = shown if per_day <= hard else hard
    if per_day == 20:
        assert warning is None
    else:
        assert warning is not None and f"set to {expected} a day" in warning


def test_posture_lists_the_auto_send_job_only_with_the_flag_on(lane: Lane) -> None:
    from netkeeper.services.posture import _scheduler_posture

    account = lane.read(lambda s, u: ensure_account(s, u).id)
    on = lane.read(
        lambda s, u: _scheduler_posture(s, u, account, gate=AUTO.linkedin.heat, auto_send=True)
    )
    off = lane.read(lambda s, u: _scheduler_posture(s, u, account, gate=AUTO.linkedin.heat))
    assert "auto_send" in [kind for kind, _, _ in on.jobs]
    assert "auto_send" not in [kind for kind, _, _ in off.jobs]


def test_the_serve_registry_has_no_auto_send_handler_with_the_flag_off(lane: Lane) -> None:
    off = replace(AUTO, campaigns=replace(AUTO.campaigns, linkedin_auto_send=False))
    none = scheduled_runs.serve_registry(
        lane.factory, cast(Any, None), cast(Any, _NoTasks()), clock=lambda: NOW, settings=off
    )
    assert scheduler.JobKind.AUTO_SEND not in none
    on = scheduled_runs.serve_registry(
        lane.factory, cast(Any, None), cast(Any, _NoTasks()), clock=lambda: NOW, settings=AUTO
    )
    assert scheduler.JobKind.AUTO_SEND in on


# --- #458 review, second pass: the close checks, the hold's own gates, the refund guard ----


async def _close_after(
    change: Callable[[MessagingTab], None],
) -> tuple[PrefillResult, MessagingSite]:
    site = MessagingSite(ZEPHYRINE)
    site.after_send = change
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    assert result.bubble_closed is False and closes(site) == []
    assert not site.tab.is_closed() and run.handed_over
    return result, site


def _dialog(tab: MessagingTab) -> Any:
    [dialog] = [e for e in tab.document.elements() if e.attrs.get("role") == "dialog"]
    return dialog


async def test_a_second_messaging_dialog_keeps_the_sent_bubble_open() -> None:
    def another(tab: MessagingTab) -> None:
        tab.add_html('<div role="dialog" aria-label="Messaging" hidden><p>other</p></div>')

    result, _ = await _close_after(another)
    assert result.close_refusal == "the sent message is not in one conversation bubble"


async def test_a_sent_bubble_for_someone_else_is_not_closed() -> None:
    def other(tab: MessagingTab) -> None:
        for element in _dialog(tab).elements():
            if element.tag == "a":
                element.attrs["href"] = "/in/ACoAAAnotherPerson/"

    result, _ = await _close_after(other)
    assert result.close_refusal == "the sent bubble is for someone else"


async def test_two_close_controls_are_not_clicked() -> None:
    def twice(tab: MessagingTab) -> None:
        [header] = [e for e in _dialog(tab).elements() if e.tag == "header"]
        name = ZEPHYRINE.name
        from messaging_dom import parse_into

        parse_into(header, f"<button><span>Close your conversation with {name}</span></button>")

    result, _ = await _close_after(twice)
    assert result.close_refusal == (
        "the sent bubble does not have one close control for this person (2 close button(s)"
        " by prefix; 2 visible; 2 by the exact name; button 1: visible, suffix vs header"
        " name: same length, exact, its name from its text, no hidden text in its name;"
        " button 2: visible, suffix vs header name: same length, exact, its name from its"
        " text, no hidden text in its name; header name from its text, no aria-label,"
        " 0 element(s) under the link, 0 aria-hidden, its text is its name)"
    )


async def test_a_hidden_close_control_is_not_clicked() -> None:
    def hide(tab: MessagingTab) -> None:
        for element in _dialog(tab).elements():
            if element.tag == "button" and "Close your conversation" in element.text():
                element.attrs["style"] = "display: none"

    result, _ = await _close_after(hide)
    assert result.close_refusal == "the sent bubble's close control is not visible"


async def test_a_hold_set_after_the_claim_stops_the_run_before_it_opens_anything(
    lane: Lane,
) -> None:
    a = Auto(lane)
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    lane.write(
        lambda s, u: linkedin_steps.hold_auto_send(
            s, u, account, reason=linkedin_steps.AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=a.run_id
        )
    )
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    _given_back_unopened(a, "auto-send is held until the open message bubbles are closed")


async def test_the_handler_checks_the_hold_before_it_claims(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("claimed while held")

    monkeypatch.setattr(scheduled_runs, "claim_auto_send", never)
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    lane.write(
        lambda s, u: linkedin_steps.hold_auto_send(
            s, u, account, reason=linkedin_steps.AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=1
        )
    )
    ctx = scheduler.JobContext(lane.user_id, account, scheduler.JobKind.AUTO_SEND, NOW, False)
    handler = scheduled_runs._auto_send_handler(
        lane.factory, cast(Any, None), cast(Any, _NoTasks()), AUTO, clock=lambda: NOW
    )
    assert await handler(ctx) is scheduler.JobOutcome.NOTHING_TO_SEND


class _SentButNothingTyped:
    """A source that attempted Send yet reports no key and ``not_typed``: never refunded."""

    keys_sent = 0
    message_click_attempted = True
    message_clicked = True
    send_attempted = True
    tab_closed = False
    message_click_diagnostics: dict[str, str | None] = {}

    async def prefill(
        self, spec: MessageJobSpec, plan: Any, *, cancelled: Any, permit: Any = None
    ) -> PrefillResult:
        from netkeeper.linkedin.messaging import MessageOutcome

        return PrefillResult(MessageOutcome(MessageOutcomeKind.NOT_TYPED, "x", None, 0))


async def test_no_refund_once_send_was_attempted(lane: Lane) -> None:
    a = Auto(lane)
    prepared = message_send.prepare(
        lane.factory, lane.user_id, a.run_id, settings=AUTO, clock=lambda: NOW
    )
    assert isinstance(prepared, message_send.PreparedPrefill)
    await message_send.run_prefill(
        lane.factory,
        lane.user_id,
        prepared,
        cast(Any, _SentButNothingTyped()),
        settings=AUTO,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 1


# --- #458 re-review: proof the message went, before anything is closed --------------------


def _answer(status: int, text: str | None = None, *, other: bool = False) -> Any:
    from messaging_dom import send_answer_ok

    def answer(sent: str, conversation: str | None) -> tuple[int, str]:
        _, body = send_answer_ok(
            sent if text is None else text,
            "urn:li:msg_conversation:(other,thread)" if other else conversation,
        )
        return status, body

    return answer


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (None, "no send answer was seen"),
        (_answer(500), "the send answer was an error"),
        (_answer(200, "Something else"), "the send answer holds other text"),
        (_answer(200, other=True), "the send answer is for another conversation"),
        (lambda sent, conversation: (200, "not json"), "the send answer could not be read"),
    ],
)
async def test_an_unproven_send_closes_nothing(answer: Any, why: str) -> None:
    site = MessagingSite(ZEPHYRINE)
    site.send_answer = answer
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    assert result.send_unconfirmed == why
    assert result.bubble_closed is False and closes(site) == []
    assert not site.tab.is_closed() and run.handed_over


async def test_close_sent_bubble_refuses_without_proof() -> None:
    site = MessagingSite(ZEPHYRINE)
    provider, _ = fake_provider(site)
    recipient = BubbleRecipient(BubbleLayout.EXISTING, ZEPHYRINE.profile_id, ZEPHYRINE.slug)
    async with provider.run() as run:
        run._send_landed = True  # as if Send had landed
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        closing = await run.close_sent_bubble(recipient, confirmed=False, sleep=Steps().sleep)
    assert closing.refusal == "the send was not confirmed" and closes(site) == []


async def test_an_error_answer_after_send_holds_auto_send(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE)
    site.send_answer = _answer(500)
    a = Auto(lane, site)
    await a.execute()
    assert site.closed_bubbles == 0 and not site.tab.is_closed()
    assert "send not confirmed: the send answer was an error" in (a.run().notes or "")
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    assert lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account)) is not None


async def test_text_back_in_the_composer_after_a_brief_empty_moment_closes_nothing() -> None:
    site = MessagingSite(ZEPHYRINE)

    def restore(tab: MessagingTab) -> None:
        def back(page: MessagingTab) -> None:
            # Once the run has found the close control, text comes back.
            if back in page._read_hooks and any(
                "Close your conversation" in lookup for lookup in page.lookups
            ):
                page._read_hooks.remove(back)
                page.typed = BODY
                page._render_composer()

        tab.on_read(back)

    site.after_send = restore
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    assert result.close_refusal == "the composer did not empty after Send"
    assert closes(site) == [] and run.handed_over


async def test_a_composer_that_empties_a_moment_after_send_is_still_closed() -> None:
    site = MessagingSite(ZEPHYRINE)
    site.send_clears = False

    def later(tab: MessagingTab) -> None:
        start = tab.reads
        seen = len(tab.lookups)

        def empty(page: MessagingTab) -> None:
            if empty not in page._read_hooks:
                return
            if any("dialog" in lookup for lookup in page.lookups[seen:]):
                # Looking for the bubble before the composer emptied: it never empties.
                page._read_hooks.remove(empty)
            elif page.reads >= start + 4:
                page._read_hooks.remove(empty)
                page.draft = ""
                page.typed = ""
                page._render_composer()

        tab.on_read(empty)

    site.after_send = later
    result, run, _ = await page_run(site, send=permit())
    assert result.bubble_closed is True, result
    assert run.tab_closed


async def test_a_never_messaged_bubble_that_became_a_conversation_is_closed() -> None:
    site = MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, existing_conversation=None))

    def become(tab: MessagingTab) -> None:
        old = tab.composer
        assert old is not None
        root = next(a for a in old.ancestors() if a.tag == "form").parent
        while root is not None and root.parent is not tab.document and root.parent is not None:
            if any(e.tag == "h2" for e in root.elements()):
                break
            root = root.parent
        assert root is not None and root.parent is not None
        root.parent.children.remove(root)
        tab.add_html(existing_bubble_html(ZEPHYRINE))

    site.after_send = become
    result, run, _ = await page_run(site, send=permit())
    assert result.outcome.kind is MessageOutcomeKind.SEND_CLICKED
    assert result.bubble_closed is True, result
    assert run.tab_closed and site.tab.is_closed()


# --- #458 re-review: every close check, killed one by one ---------------------------------


async def test_a_close_control_with_more_to_its_name_is_not_clicked() -> None:
    def rename(tab: MessagingTab) -> None:
        for element in _dialog(tab).elements():
            if element.tag == "span" and element.text().startswith("Close your conversation"):
                element.children = [element.text() + " and others"]

    result, _ = await _close_after(rename)
    assert result.close_refusal == (
        "the sent bubble does not have one close control for this person (1 close button(s)"
        " by prefix; 1 visible; 0 by the exact name; button 1: visible, suffix vs header"
        " name: suffix longer by 11, suffix starts with header name, its name from its"
        " text, no hidden text in its name; header name from its text, no aria-label,"
        " 0 element(s) under the link, 0 aria-hidden, its text is its name)"
    )


async def test_a_close_control_outside_the_dialog_is_not_clicked() -> None:
    def move(tab: MessagingTab) -> None:
        [button] = [
            e
            for e in _dialog(tab).elements()
            if e.tag == "button" and "Close your conversation" in e.text()
        ]
        assert button.parent is not None
        button.parent.children.remove(button)
        tab.add_html(f"<button><span>Close your conversation with {ZEPHYRINE.name}</span></button>")

    result, _ = await _close_after(move)
    assert result.close_refusal == (
        "the sent bubble does not have one close control for this person (0 close button(s)"
        " by prefix; 0 visible; 0 by the exact name; 1 by prefix on the page; header name"
        " from its text, no aria-label, 0 element(s) under the link, 0 aria-hidden, its"
        " text is its name)"
    )


async def test_a_dialog_that_does_not_hold_the_composer_is_not_closed() -> None:
    def split(tab: MessagingTab) -> None:
        dialog = _dialog(tab)
        dialog.attrs["role"] = "region"
        tab.add_html(
            '<div role="dialog" aria-label="Messaging"><header><h2>'
            f'<a href="/in/{ZEPHYRINE.profile_id}/">{ZEPHYRINE.name}</a></h2>'
            f"<button><span>Close your conversation with {ZEPHYRINE.name}</span></button>"
            "</header></div>"
        )

    result, _ = await _close_after(split)
    assert result.close_refusal == "the sent message is not in one conversation bubble"


async def test_a_second_empty_composer_is_not_an_emptied_bubble() -> None:
    def another(tab: MessagingTab) -> None:
        composer = tab.composer
        tab.add_html(
            '<div hidden><div contenteditable="true" role="textbox" '
            'aria-label="Write a message…"><p><br></p></div></div>'
        )
        tab.composer = composer

    result, _ = await _close_after(another)
    assert result.close_refusal == "the composer did not empty after Send"


async def test_a_header_with_two_links_is_not_closed() -> None:
    def two(tab: MessagingTab) -> None:
        from messaging_dom import parse_into

        [h2] = [e for e in _dialog(tab).elements() if e.tag == "h2"]
        parse_into(h2, f'<a href="/in/{ZEPHYRINE.profile_id}/">again</a>')

    result, _ = await _close_after(two)
    assert result.close_refusal == "the sent bubble's header does not name one person"


async def test_the_close_control_is_clicked_at_most_once() -> None:
    site = MessagingSite(ZEPHYRINE)
    site.close_ignored = True
    provider, _ = fake_provider(site)
    steps = Steps()
    recipient = BubbleRecipient(BubbleLayout.EXISTING, ZEPHYRINE.profile_id, ZEPHYRINE.slug)
    async with provider.run() as run:
        await run.goto(f"https://www.linkedin.com/in/{ZEPHYRINE.slug}/")
        assert (
            await run.click_message(
                f"/in/{ZEPHYRINE.slug}/", ZEPHYRINE.profile_id, pause_s=0, sleep=steps.sleep
            )
        ).clicked
        await run.type_into_composer(
            plan_typing(BODY, 3), recipient, clock=steps.clock, sleep=steps.sleep
        )
        assert (
            await run.click_send(
                BODY, recipient, permit=permit(), dwell_s=0, clock=steps.clock, sleep=steps.sleep
            )
        ).clicked
        first = await run.close_sent_bubble(recipient, confirmed=True, sleep=steps.sleep)
        second = await run.close_sent_bubble(recipient, confirmed=True, sleep=steps.sleep)
        await run.hand_over()
    assert first.attempted and not first.closed
    assert second.refusal == "the bubble's close control was already clicked"
    assert site.closed_bubbles == 1


async def test_a_tab_that_will_not_close_is_not_counted_closed_and_holds(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stuck(self: MessagingTab) -> None:
        raise RuntimeError("target closed")

    monkeypatch.setattr(MessagingTab, "close", stuck)
    a = Auto(lane)
    await a.execute()
    counts = a.run().counts_json or {}
    assert counts["bubble_closed"] is True and counts["tab_closed"] is False
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    hold = lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account))
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_TAB


def test_the_close_constants_are_pinned() -> None:
    from netkeeper.linkedin import browser, page_messaging

    assert browser.CLOSE_CONTROL_ROLE == "button"
    assert browser.CLOSE_CONTROL_PREFIX == "Close your conversation with "
    assert (browser.CLOSE_WAIT_S, browser.CLOSE_WAIT_POLLS) == (5.0, 25)
    assert browser.CLOSE_CLICK_TIMEOUT_MS == 1_000.0
    assert browser.SEND_CLICK_TIMEOUT_MS == 1_000.0
    assert page_messaging.SEND_CONFIRM_WAIT_S == 10.0


# --- #458 re-review: the hold is written whatever happens to the record --------------------


def test_the_hold_is_written_for_a_run_that_already_ended(lane: Lane) -> None:
    from netkeeper.linkedin.messaging import MessageOutcome

    enrollment = lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and claim.run_id is not None and claim.enrollment_id == enrollment
    run_id = claim.run_id
    lane.write(
        lambda s, u: runs.finish_run(
            s, u, run_id, status=SyncRunStatus.FAILED, now=NOW, stop_reason="interrupted"
        )
    )
    message_send.record(
        lane.factory,
        lane.user_id,
        run_id,
        MessageOutcome(MessageOutcomeKind.UNKNOWN, "interrupted", None, 0),
        settings=AUTO,
        now=NOW,
        click_attempted=True,
        clicked=True,
    )
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    assert lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account)) is not None


async def test_the_hold_is_written_when_recording_the_outcome_fails(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database gone")

    monkeypatch.setattr(message_send, "record_prefill_outcome", broken)
    site = MessagingSite(ZEPHYRINE)
    site.send_answer = _answer(500)  # a bubble is left open
    a = Auto(lane, site)
    await a.execute()
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    assert lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account)) is not None


async def test_a_failing_refund_still_records_the_outcome(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database gone")

    monkeypatch.setattr(message_send, "_release_auto_send", broken)
    a = Auto(lane, MessagingSite(ZEPHYRINE, profile_html="<main><h1>No controls</h1></main>"))
    await a.execute()
    assert a.run().status is not SyncRunStatus.RUNNING
    assert a.message() is None  # not_typed was recorded: the claim is given back


# --- #458 re-review, reviewer B ----------------------------------------------------------


class _TypedThenNotTyped(_SentButNothingTyped):
    keys_sent = 3
    send_attempted = False


async def test_no_refund_once_a_key_was_typed(lane: Lane) -> None:
    a = Auto(lane)
    prepared = message_send.prepare(
        lane.factory, lane.user_id, a.run_id, settings=AUTO, clock=lambda: NOW
    )
    assert isinstance(prepared, message_send.PreparedPrefill)
    await message_send.run_prefill(
        lane.factory,
        lane.user_id,
        prepared,
        cast(Any, _TypedThenNotTyped()),
        settings=AUTO,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 1


async def test_a_refusal_before_the_message_click_holds_nothing(lane: Lane) -> None:
    a = Auto(lane, MessagingSite(ZEPHYRINE, profile_html="<main><h1>No controls</h1></main>"))
    await a.execute()
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    assert lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account)) is None


def test_release_floors_at_zero_counts_the_week_and_uses_the_spend_day(lane: Lane) -> None:
    budget = AUTO.linkedin.budget

    def go(session: Session, user: User) -> list[Any]:
        account = ensure_account(session, user).id
        yesterday = NOW - timedelta(days=1)
        budgets.consume(
            session, user, account, ActionClass.PROFILE_VISITS, now=yesterday, settings=budget
        )
        budgets.consume(
            session, user, account, ActionClass.PROFILE_VISITS, now=NOW, settings=budget
        )
        released = budgets.release(
            session,
            user,
            account,
            ActionClass.PROFILE_VISITS,
            spent_at=yesterday,
            settings=budget,
        )
        today = budgets.status(
            session, user, account, ActionClass.PROFILE_VISITS, now=NOW, settings=budget
        )
        floor = budgets.release(
            session, user, account, ActionClass.LI_MESSAGES_AUTO, spent_at=NOW, settings=budget
        )
        return [released, today, floor]

    released, today, floor = lane.write(go)
    assert released.day.count == 0  # yesterday's unit came off yesterday
    assert today.day.count == 1  # today's is untouched
    assert released.week is not None and released.week.count == 1
    assert floor.day.count == 0  # never below zero


def test_the_claim_takes_the_oldest_due_auto_send_first(lane: Lane) -> None:
    newer = lane.enroll(contact=dict(CONTACT), next_action_at=NOW - timedelta(minutes=5))
    older = lane.enroll(
        contact={"li_urn": "urn:li:fsd_profile:ACoAAOlder1", "li_public_id": "older"},
        next_action_at=NOW - timedelta(hours=5),
    )
    assert newer != older
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and claim.enrollment_id == older


def test_posture_passes_the_hold_through(lane: Lane) -> None:
    from netkeeper.services.posture import posture

    account = lane.read(lambda s, u: ensure_account(s, u).id)
    lane.write(
        lambda s, u: linkedin_steps.hold_auto_send(
            s, u, account, reason=linkedin_steps.AUTO_SEND_HOLD_BUBBLE, now=NOW, run_id=1
        )
    )
    report = lane.read(
        lambda s, u: posture(
            s, u, account, now=NOW, settings=AUTO, browser_mode="attach", probe=None
        )
    )
    [row] = [p for p in report.protections if p.name == "manual linkedin sends"]
    text = " ".join(row.warnings)
    assert "auto-send is held" in text
    assert "resume auto-send" in text and "netkeeper linkedin auto-send-resume" in text


# --- #457 (Try again) with auto-send ------------------------------------------------------


def _try_again_ids(lane: Lane) -> list[int]:
    rows = lane.read(lambda s, u: linkedin_steps.try_again(s, u, now=NOW, settings=AUTO))
    return [row.enrollment.id for row in rows]


async def test_an_unknown_auto_send_is_never_offered_as_try_again(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE)
    site.send_error = RuntimeError("detached")  # the Send click raised: unknown
    a = Auto(lane, site)
    await a.execute()
    message = a.message()
    assert message is not None and (message.error or "").startswith("unknown:")
    assert a.enrollment_id not in _try_again_ids(lane)
    retry = lane.write(
        lambda s, u: claim_prefill(
            s, u, a.enrollment_id, now=NOW, settings=AUTO, retry=True, no_bubble_open=True
        )
    )
    assert not retry.claimed


async def test_an_interrupted_auto_send_is_never_offered_as_try_again(lane: Lane) -> None:
    a = Auto(lane)  # claimed: the message is scheduled and its run running
    lane.write(
        lambda s, u: runs.finish_run(
            s, u, a.run_id, status=SyncRunStatus.FAILED, now=NOW, stop_reason="interrupted"
        )
    )
    assert a.message() is not None  # still claimed: nobody knows what it typed
    assert a.enrollment_id not in _try_again_ids(lane)
    retry = lane.write(
        lambda s, u: claim_prefill(
            s, u, a.enrollment_id, now=NOW, settings=AUTO, retry=True, no_bubble_open=True
        )
    )
    assert not retry.claimed


async def test_a_failure_after_the_send_click_is_not_offered_as_try_again(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Auto(lane)

    async def boom(self: BrowserRun, recipient: Any, **kwargs: Any) -> Any:
        raise RuntimeError("after send")

    monkeypatch.setattr(BrowserRun, "close_sent_bubble", boom)
    await a.execute()
    assert a.enrollment_id not in _try_again_ids(lane)


# --- #458 final review: the claim read again, live, just before Send -----------------------


def _change_enrollment(field: str, value: Any) -> Callable[[Session, User], None]:
    def act(session: Session, user: User) -> None:
        from netkeeper.models import Enrollment

        for enrollment in session.scalars(scoped(user, Enrollment)):
            setattr(enrollment, field, value)

    return act


def _pause_campaign(session: Session, user: User) -> None:
    from netkeeper.models import Campaign, CampaignStatus

    for campaign in session.scalars(scoped(user, Campaign)):
        campaign.status = CampaignStatus.PAUSED


def _reply(session: Session, user: User) -> None:
    import factories

    from netkeeper.models import Enrollment, MessageDirection

    for enrollment in session.scalars(scoped(user, Enrollment)):
        factories.make_message(
            session,
            enrollment,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            sent_at=NOW,
        )


def _do_not_contact(session: Session, user: User) -> None:
    from netkeeper.models import Contact

    for contact in session.scalars(scoped(user, Contact)):
        contact.do_not_contact = True


def _message_discarded(session: Session, user: User) -> None:
    for message in session.scalars(scoped(user, Message)):
        message.status = MessageStatus.DISCARDED


def _to_prefill(session: Session, user: User) -> None:
    for step in session.scalars(scoped(user, CampaignStep)):
        step.mode = StepMode.PREFILL


@pytest.mark.parametrize(
    ("act", "why"),
    [
        (_pause_campaign, "the step no longer passes its guards"),
        (
            _change_enrollment("status", EnrollmentStatus.REMOVED),
            "the step no longer passes its guards",
        ),
        (_reply, "a reply arrived"),
        (_do_not_contact, "the step no longer passes its guards"),
        (_to_prefill, "the step is not an auto-send step"),
        (_message_discarded, "the claimed message is no longer waiting to be sent"),
    ],
    ids=[
        "campaign paused",
        "enrollment removed",
        "reply arrived",
        "do not contact",
        "prefill",
        "message discarded",
    ],
)
async def test_a_change_to_the_claim_after_the_last_key_stops_the_click(
    lane: Lane, act: Callable[[Session, User], Any], why: str
) -> None:
    a = Auto(lane)
    _last_key_does(a, act)
    await a.execute()
    _no_send(a)
    run = a.run()
    if act is _message_discarded:
        # Nothing claimed is left to record the reason on; the run fails as no_claim.
        assert run.stop_reason == "no_claim"
    else:
        assert run.notes == f"not sent: {why}"
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    assert lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account)) is not None


async def test_a_change_to_the_claim_before_the_navigation_opens_nothing(lane: Lane) -> None:
    a = Auto(lane)
    lane.write(_pause_campaign)
    await a.execute()
    assert a.site.navigations == []
    _no_send(a)
    assert a.message() is None
    assert a.run().notes == "not started: the step no longer passes its guards"


# --- #458 final review: the hold when record_quietly's record fails; the counts -----------


def test_record_quietly_holds_when_recording_fails(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netkeeper.linkedin.messaging import MessageOutcome

    lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and claim.run_id is not None

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database gone")

    monkeypatch.setattr(message_send, "record", broken)
    message_send.record_quietly(
        lane.factory,
        lane.user_id,
        claim.run_id,
        MessageOutcome(MessageOutcomeKind.UNKNOWN, "interrupted", None, 0),
        settings=AUTO,
        now=NOW,
        click_attempted=True,
        clicked=True,
    )
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    hold = lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account))
    assert hold is not None and hold.run_id == claim.run_id


async def test_an_auto_send_leaves_li_prefills_spent_out_so_a_retry_asks_to_confirm(
    lane: Lane,
) -> None:
    a = Auto(lane, MessagingSite(ZEPHYRINE, bubble=Bubble(ZEPHYRINE, compose=None)))
    await a.execute()  # clicked Message, then no compose option answered: not_typed
    counts = a.run().counts_json or {}
    assert "li_prefills_spent" not in counts and counts["message_click_attempted"] is True
    enrollment = lane.enrollment(a.enrollment_id)
    last = lane.read(lambda s, u: linkedin_steps.last_try(s, u, enrollment, now=NOW))
    assert last.budget_spent is None and last.needs_confirmation


# --- #458 final review: a refusal before any navigation keeps the step due ----------------


async def test_a_busy_browser_keeps_the_auto_send_step_due(lane: Lane) -> None:
    from netkeeper.linkedin.activity_lock import account_key

    a = Auto(lane)
    account = a.run().linkedin_account_id
    async with a.provider.locks.hold(account_key(account)):
        await a.execute()
    assert a.site.navigations == []
    _given_back_unopened(a, "the browser was busy")


async def test_a_lapsed_auto_send_claim_keeps_the_step_due(lane: Lane) -> None:
    a = Auto(lane, clock=Clock(NOW + timedelta(seconds=61)))
    await a.execute()
    _given_back_unopened(a, "the claim lapsed")


async def test_a_flagged_session_at_the_worker_keeps_the_auto_send_step_due(lane: Lane) -> None:
    a = Auto(lane)
    lane.write(lambda s, u: flag_session(s, u, Outcome.CHECKPOINT, url="https://x.test/"))
    await a.execute()
    _given_back_unopened(a, "the LinkedIn session is flagged")


# --- #458 final review: a run that stopped mid-run with nothing recorded -----------------


def test_an_interrupted_auto_send_holds_auto_send_at_the_next_claim(lane: Lane) -> None:
    a_enrollment = lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and claim.run_id is not None and claim.enrollment_id == a_enrollment
    run_id = claim.run_id
    lane.write(
        lambda s, u: runs.finish_run(
            s, u, run_id, status=SyncRunStatus.FAILED, now=NOW, stop_reason="interrupted"
        )
    )
    later = NOW + timedelta(hours=1)
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=later, settings=AUTO)) is None
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    hold = lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account))
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_INTERRUPTED
    assert hold.run_id == run_id


# --- #458 focused re-review ----------------------------------------------------------------


def _claimed_run(lane: Lane) -> int:
    lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))
    claim = lane.write(lambda s, u: claim_auto_send(s, u, now=NOW, settings=AUTO))
    assert claim is not None and claim.run_id is not None
    return claim.run_id


def _hold_of(lane: Lane) -> Any:
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    return lane.read(lambda s, u: linkedin_steps.auto_send_hold(s, u, account))


def test_a_live_running_auto_send_writes_no_hold(lane: Lane) -> None:
    _claimed_run(lane)
    soon = NOW + runs.STALE_AFTER - timedelta(seconds=10)
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=soon, settings=AUTO)) is None
    assert _hold_of(lane) is None


def test_a_stale_running_auto_send_holds_after_a_restart(lane: Lane) -> None:
    run_id = _claimed_run(lane)
    later = NOW + runs.STALE_AFTER + timedelta(seconds=10)
    assert lane.write(lambda s, u: claim_auto_send(s, u, now=later, settings=AUTO)) is None
    hold = _hold_of(lane)
    assert hold is not None and hold.run_id == run_id
    assert hold.reason == linkedin_steps.AUTO_SEND_HOLD_INTERRUPTED


def test_an_interrupted_manual_prefill_does_not_hold_auto_send(lane: Lane) -> None:
    enrollment = lane.enroll(contact=dict(CONTACT))
    claim = lane.claim(enrollment)  # a person's prefill
    assert claim.claimed and claim.run_id is not None
    run_id = claim.run_id
    lane.write(
        lambda s, u: runs.finish_run(
            s, u, run_id, status=SyncRunStatus.FAILED, now=NOW, stop_reason="interrupted"
        )
    )
    later = NOW + timedelta(hours=1)
    lane.write(lambda s, u: claim_auto_send(s, u, now=later, settings=AUTO))
    assert _hold_of(lane) is None


async def test_run_prefills_second_lapse_check_gives_the_step_back_unopened(lane: Lane) -> None:
    a = Auto(lane)
    prepared = message_send.prepare(
        lane.factory, lane.user_id, a.run_id, settings=AUTO, clock=lambda: NOW
    )
    assert isinstance(prepared, message_send.PreparedPrefill)
    await message_send.run_prefill(
        lane.factory,
        lane.user_id,
        prepared,
        cast(Any, _SentButNothingTyped()),
        settings=AUTO,
        clock=lambda: NOW + timedelta(seconds=61),
    )
    _given_back_unopened(a, "the claim lapsed")


# --- #444's pre-click refusals hold auto-send (ADR 0008) ----------------------------------


@pytest.mark.parametrize(
    ("site", "reason"),
    [
        (
            lambda: MessagingSite(ZEPHYRINE, before=existing_bubble_html(ZEPHYRINE)),
            linkedin_steps.AUTO_SEND_HOLD_BUBBLE,
        ),
        (
            lambda: MessagingSite(
                ZEPHYRINE,
                before=(
                    '<div hidden><div contenteditable="true" role="textbox" '
                    'aria-label="Write a message…"><p><br></p></div></div>'
                ),
            ),
            linkedin_steps.AUTO_SEND_HOLD_BUBBLE,
        ),
    ],
    ids=["bubble already open", "a minimized composer"],
)
async def test_a_pre_click_bubble_refusal_holds_auto_send(
    lane: Lane, site: Callable[[], MessagingSite], reason: str
) -> None:
    a = Auto(lane, site())
    await a.execute()
    _no_send(a)
    assert a.site.tab.clicks == []  # nothing clicked
    run = a.run()
    assert (run.counts_json or {}).get("message_click_attempted") is False
    # After the navigation, so not given back unopened: the not_typed path, Try again.
    assert "opened" not in (run.counts_json or {})
    assert a.message() is None and lane.enrollment(a.enrollment_id).not_sent_count == 1
    hold = _hold_of(lane)
    assert hold is not None and hold.reason == reason and hold.run_id == a.run_id
    # Nothing was typed, so the li_messages_auto unit came back; the visit didn't.
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 0
    assert a.spent(ActionClass.PROFILE_VISITS) == 1


async def test_a_bubble_check_that_cannot_read_the_page_holds_auto_send(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreadable(self: BrowserRun, tab: Any) -> bool:
        raise RuntimeError("read failed")

    monkeypatch.setattr(BrowserRun, "_bubble_already_open", unreadable)
    a = Auto(lane)
    await a.execute()
    _no_send(a)
    hold = _hold_of(lane)
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_BUBBLE


async def test_a_covered_message_control_holds_auto_send(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netkeeper.linkedin import browser

    monkeypatch.setattr(browser, "choose_message_target", lambda *args, **kwargs: None)
    a = Auto(lane)
    await a.execute()
    _no_send(a)
    assert a.site.tab.clicks == []
    hold = _hold_of(lane)
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_COVERED
    assert a.spent(ActionClass.LI_MESSAGES_AUTO) == 0


async def test_a_pre_click_refusal_on_a_manual_prefill_holds_nothing(lane: Lane) -> None:
    site = MessagingSite(ZEPHYRINE, before=existing_bubble_html(ZEPHYRINE))
    provider, _ = fake_provider(site)
    worker = BrowserWorker(
        provider,
        lane.factory,
        AUTO.linkedin,
        clock=Clock(),
        sleep=no_sleep,
        prefill_sources=fast_source,
        campaign_settings=AUTO,
    )
    enrollment = lane.enroll(contact=dict(CONTACT))
    claim = lane.claim(enrollment)
    assert claim.claimed and claim.run_id is not None
    await worker.execute(claim.run_id, lane.user_id)
    assert _hold_of(lane) is None


async def test_another_pre_click_refusal_holds_nothing(lane: Lane) -> None:
    """Only #444's bubble and cover refusals hold: a profile with no Message control
    refuses before the click and holds nothing (the next contact's profile differs)."""
    a = Auto(lane, MessagingSite(ZEPHYRINE, profile_html="<main><h1>No controls</h1></main>"))
    await a.execute()
    assert _hold_of(lane) is None


async def test_a_pre_click_hold_is_written_when_the_final_record_fails(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database gone")

    monkeypatch.setattr(message_send, "record_prefill_outcome", broken)
    a = Auto(lane, MessagingSite(ZEPHYRINE, before=existing_bubble_html(ZEPHYRINE)))
    await a.execute()
    hold = _hold_of(lane)
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_BUBBLE


def test_record_quietly_writes_a_pre_click_hold_when_recording_fails(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from netkeeper.linkedin.messaging import MessageOutcome, PreClickHold

    run_id = _claimed_run(lane)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database gone")

    monkeypatch.setattr(message_send, "record", broken)
    message_send.record_quietly(
        lane.factory,
        lane.user_id,
        run_id,
        MessageOutcome(MessageOutcomeKind.NOT_TYPED, "covered", None, 0),
        settings=AUTO,
        now=NOW,
        click_attempted=False,
        clicked=False,
        pre_click_hold=PreClickHold.COVERED,
    )
    hold = _hold_of(lane)
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_COVERED


async def test_a_cancel_during_the_refund_still_records_the_pre_click_hold(
    lane: Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    def cancelled(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(message_send, "_release_auto_send", cancelled)
    a = Auto(lane, MessagingSite(ZEPHYRINE, before=existing_bubble_html(ZEPHYRINE)))
    with pytest.raises(asyncio.CancelledError):
        await a.execute()
    hold = _hold_of(lane)
    assert hold is not None and hold.reason == linkedin_steps.AUTO_SEND_HOLD_BUBBLE


def test_the_opened_in_front_note_is_not_part_of_the_not_sent_reason() -> None:
    """#195: a run's note that its tab opened in front never reads as part of why an
    auto-send wasn't sent."""
    from netkeeper.services import runs
    from netkeeper.web.api.linkedin_steps import _not_sent_reason

    notes = f"not sent: the schedule is paused {runs.OPENED_IN_FRONT_NOTE}"
    assert _not_sent_reason(notes) == "the schedule is paused"


# --- #343: the Settings page's values reach auto-send ---------------------------------


async def test_the_handler_claims_by_the_settings_page_budget(lane: Lane) -> None:
    """The file allows 15 auto-sends a day; the Settings page says 1, and 1 is spent.
    The claim reads the page's value, per claim, so nothing is claimed."""
    lane.enroll(contact=dict(CONTACT))
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(minutes=1)))

    def page_and_spend(session: Session, user: User) -> None:
        set_setting(session, user, "config.linkedin.budget.li_messages_auto_per_day", 1)
        account = ensure_account(session, user)
        key = budgets._day_key(account.id, ActionClass.LI_MESSAGES_AUTO, NOW.date())
        set_setting(session, user, key, 1)

    lane.write(page_and_spend)
    handler = scheduled_runs._auto_send_handler(
        lane.factory, cast(Any, None), cast(Any, _NoTasks()), AUTO, clock=lambda: NOW
    )
    account = lane.read(lambda s, u: ensure_account(s, u).id)
    ctx = scheduler.JobContext(lane.user_id, account, scheduler.JobKind.AUTO_SEND, NOW, False)
    assert await handler(ctx) is scheduler.JobOutcome.NOTHING_TO_SEND
    assert lane.runs() == []


async def test_a_browser_error_after_the_runner_started_is_not_given_back_unopened(
    lane: Lane,
) -> None:
    """The browser fails once the runner has started (here, building its page source):
    whether anything was opened is unknown, so the step is not handed back as unopened
    (``opened`` None, #458), unlike a failure while attaching."""
    a = Auto(lane)

    def unavailable(run: BrowserRun, *, sleep: Any, clock: Any) -> PagePrefill:
        raise BrowserUnavailable("Chrome went away")

    a.worker._prefill_sources = unavailable
    assert await a.execute() is runs.RunOutcome.RETRY_LATER
    _no_send(a)
    run = a.run()
    assert not (run.notes or "").startswith(linkedin_steps.NOT_STARTED_NOTE)
    assert (run.counts_json or {}).get("opened") is not False
