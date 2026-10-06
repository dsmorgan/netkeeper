"""PageInbox: the inbox poll's source over the page's own answers (P4-01, #380, ADR 0006).

Driven through the real :class:`~netkeeper.linkedin.browser.BrowserRun` over
:mod:`inbox_site`'s fake messaging page. The attacks this file makes on the source, each a
test below: does it stop at the bound? Does it open more threads than five, or one it may not?
Can it skip a conversation that holds a real reply? Does an unknown shape read as "no replies"
instead of stopping the poll? Does a poll that read part of the list call itself complete?
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import inbox_site
import messaging_pages as mp
import pytest
from browser_fakes import FakeContext
from inbox_site import Behavior, InboxSite, Reply, conv, people
from run_fakes import fake_provider
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import LinkedInSettings
from netkeeper.db import session_scope
from netkeeper.linkedin import page_connections, page_inbox
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.inbox import (
    MAX_THREADS_OPENED,
    InboxDelta,
    InboxJobSpec,
    InboxReadStopped,
)
from netkeeper.linkedin.observe import ObservationFailed
from netkeeper.linkedin.pacing import ScrollProfile
from netkeeper.linkedin.page_inbox import PageInbox
from netkeeper.models import Interaction, InteractionKind, SyncRunStatus, User
from netkeeper.scoping import scoped
from netkeeper.services.inbox_poll import poll_inbox

T0 = datetime.fromtimestamp(mp.T0 / 1000, tz=UTC)

ONE_WHEEL = ScrollProfile(steps_range=(1, 1), back_up_p=0.0)


async def no_sleep(seconds: float) -> None:
    return None


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def spec(
    *,
    since: datetime | None = None,
    max_conversations: int = 40,
    open_for: frozenset[str] = frozenset(),
) -> InboxJobSpec:
    return InboxJobSpec(
        since=since,
        watched_urns=frozenset(),
        max_conversations=max_conversations,
        open_threads_for=open_for,
    )


def _last(c: mp.Conv) -> mp.Msg:
    assert c.last is not None
    return c.last


def descending(count: int, *, start: int = 100, step: int = 5, **kwargs: Any) -> list[mp.Conv]:
    """``count`` one-to-one conversations, newest first, ``step`` minutes apart."""
    return [conv(100 + i, who, start - step * i, **kwargs) for i, who in enumerate(people(count))]


async def read(site: FakeContext, job: InboxJobSpec, **kwargs: Any) -> tuple[InboxDelta, PageInbox]:
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageInbox(
            run,
            sleep=no_sleep,
            rng=random.Random(1),
            scroll_profile=ONE_WHEEL,
            landing_wait_s=0.05,
            response_wait_s=0.05,
            thread_wait_s=0.05,
            thread_pause_s=(0.0, 0.0),
            **kwargs,
        )
        return await source.read(job), source


async def stopped(site: FakeContext, job: InboxJobSpec, **kwargs: Any) -> InboxReadStopped:
    with pytest.raises(InboxReadStopped) as caught:
        await read(site, job, **kwargs)
    return caught.value


def category_requests(site: InboxSite) -> int:
    return sum(1 for _, url in site.requests if "lastUpdatedBefore" in url or "nextCursor" in url)


# --- the list: pagination to the bound ------------------------------------------------


async def test_a_short_list_is_read_whole_and_complete() -> None:
    site = InboxSite(descending(3), behavior=Behavior(first=3))
    delta, _ = await read(site, spec())

    assert len(delta.conversations) == 3 and delta.complete
    assert category_requests(site) == 1  # one scroll, to see the page say the list ends
    first = delta.conversations[0]
    assert first.counterpart_urn == people(3)[0].urn
    assert [m.outbound for m in first.messages] == [False]


async def test_it_scrolls_for_older_pages_until_it_passes_since_and_stops() -> None:
    site = InboxSite(descending(12), behavior=Behavior(first=3, page_size=3))
    # minutes: 100, 95, ..., 45. since = 70 is the 7th item, on the second older page.
    delta, _ = await read(site, spec(since=at(70)))

    assert delta.complete
    assert [c.last_activity_at for c in delta.conversations][-1] == at(70)
    assert len(delta.conversations) == 7  # strictly older than since are not handed on
    assert category_requests(site) == 2  # the third older page is never asked for


async def test_stopping_at_the_bound_before_since_is_incomplete() -> None:
    site = InboxSite(descending(12), behavior=Behavior(first=3, page_size=3))
    delta, _ = await read(site, spec(since=at(0), max_conversations=4))

    assert not delta.complete
    assert len(delta.conversations) == 4
    assert category_requests(site) == 1


async def test_without_since_the_end_of_the_list_is_what_proves_it() -> None:
    site = InboxSite(descending(5), behavior=Behavior(first=3, page_size=3))
    delta, _ = await read(site, spec(since=None, max_conversations=40))
    assert delta.complete and len(delta.conversations) == 5

    capped, _ = await read(
        InboxSite(descending(9), behavior=Behavior(first=3, page_size=3)),
        spec(since=None, max_conversations=4),
    )
    assert not capped.complete  # the core decides what a bounded first poll counts as


async def test_a_list_that_ends_past_the_bound_is_still_incomplete() -> None:
    """The page proved the end, but the bound was passed first: only the bounded part was read."""
    site = InboxSite(descending(6), behavior=Behavior(first=3, page_size=3))
    delta, _ = await read(site, spec(since=None, max_conversations=4))

    assert len(delta.conversations) == 4 and not delta.complete


async def test_an_empty_older_page_is_also_the_end() -> None:
    site = InboxSite(descending(4), behavior=Behavior(first=3, page_size=3, end="empty"))
    delta, _ = await read(site, spec())
    assert delta.complete and len(delta.conversations) == 4


async def test_a_page_asked_for_twice_is_read_once() -> None:
    site = InboxSite(
        descending(8), behavior=Behavior(first=3, page_size=3, repeat_older=frozenset({0}))
    )
    delta, _ = await read(site, spec())
    assert delta.complete and len(delta.conversations) == 8


async def test_a_list_that_stops_loading_is_route_changed_after_the_connections_limit() -> None:
    assert page_inbox.MAX_IDLE_SCROLLS == page_connections.MAX_IDLE_SCROLLS == 6
    site = InboxSite(descending(9), behavior=Behavior(first=3, page_size=3, end="stall"))
    stop = await stopped(site, spec(since=at(0)))

    assert stop.outcome is Outcome.ROUTE_CHANGED
    # Two pages answered, then six scrolls brought nothing.
    assert len(site.pages[0].mouse.wheels) == 8


async def test_a_gap_before_an_older_page_is_a_stop_not_a_skipped_page() -> None:
    def skip(index: int, url: str) -> str:
        if index == 0:
            return mp.conversations_category_url(last_updated_before=mp.T0 - 500 * mp.MINUTE_MS)
        return url

    site = InboxSite(descending(9), behavior=Behavior(first=3, page_size=3, older_url=skip))
    assert (await stopped(site, spec(since=at(0)))).outcome is Outcome.ROUTE_CHANGED


async def test_a_skipped_cursor_is_a_stop() -> None:
    def skip(index: int, url: str) -> str:
        return mp.conversations_category_url(next_cursor="invented-cursor-9") if index == 1 else url

    site = InboxSite(descending(12), behavior=Behavior(first=3, page_size=3, older_url=skip))
    assert (await stopped(site, spec(since=at(0)))).outcome is Outcome.ROUTE_CHANGED


async def test_a_list_that_is_not_newest_first_is_a_stop() -> None:
    ascending = [conv(100 + i, who, 10 + 5 * i) for i, who in enumerate(people(3))]
    site = InboxSite(ascending, behavior=Behavior(first=3))
    assert (await stopped(site, spec())).outcome is Outcome.ROUTE_CHANGED


async def test_a_second_mailbox_owner_is_a_stop() -> None:
    other = mp.Member(2, "Other", "Owner", "Invented", "SELF")

    def elsewhere(index: int, url: str) -> str:
        return url.replace(mp.OWNER.urn.replace(":", "%3A"), other.urn.replace(":", "%3A"))

    site = InboxSite(descending(9), behavior=Behavior(first=3, page_size=3, older_url=elsewhere))
    assert (await stopped(site, spec(since=at(0)))).outcome is Outcome.ROUTE_CHANGED


# --- the kinds ----------------------------------------------------------------------------


async def test_kinds_are_counted_and_skipped_and_an_accepted_inmail_is_kept() -> None:
    everything = [*mp.INBOX_FIRST_PAGE, *mp.INBOX_OLDER_PAGE]
    site = InboxSite(everything, behavior=Behavior(first=4, page_size=4))
    delta, _ = await read(site, spec())

    kept = {c.conversation_urn for c in delta.conversations}
    assert kept == {c.urn for c in mp.READ_AS_ONE_TO_ONE}  # the accepted InMail among them
    assert mp.INMAIL_ACCEPTED.urn in kept
    assert delta.skipped_group == len(mp.SKIPPED_GROUP)
    assert delta.skipped_other == len(mp.SKIPPED_OTHER)
    assert mp.NO_MESSAGES.urn not in kept  # nothing to report
    assert delta.complete


async def test_a_third_sender_in_a_one_to_one_thread_is_a_stop_not_a_skipped_group() -> None:
    """The list said two participants: a third voice is an unknown shape, and a reply of the
    contact's could be hiding in it."""
    who = people(1)[0]
    c = conv(100, who, 50)
    thread = [
        mp.Msg(100, 0, who, "Invented.", mp.T0 + 40 * mp.MINUTE_MS),
        mp.Msg(100, 2, mp.THADDEUS, "Invented third voice.", mp.T0 + 45 * mp.MINUTE_MS),
        _last(c),
    ]
    site = InboxSite([c], {100: thread}, Behavior(first=1))
    stop = await stopped(site, spec(open_for=frozenset({c.urn})))
    assert stop.outcome is Outcome.ROUTE_CHANGED


# --- opening threads ---------------------------------------------------------------------


def _with_threads(count: int) -> tuple[InboxSite, list[mp.Conv]]:
    convs = descending(count)
    threads = {
        c.n: [
            mp.Msg(c.n, 0, mp.OWNER, "Invented opener.", mp.T0 + 10 * mp.MINUTE_MS),
            _last(c),
        ]
        for c in convs
    }
    return InboxSite(convs, threads, Behavior(first=count)), convs


async def test_a_thread_opens_by_navigation_only_for_the_conversations_asked_for() -> None:
    site, convs = _with_threads(6)
    wanted = frozenset({convs[1].urn, convs[3].urn})
    delta, source = await read(site, spec(open_for=wanted))

    assert site.thread_navigations == [
        f"{inbox_site.ORIGIN}/messaging/thread/{mp.thread_id(convs[i].n)}/" for i in (1, 3)
    ]
    assert source.threads_opened == 2
    by_urn = {c.conversation_urn: c for c in delta.conversations}
    assert len(by_urn[convs[1].urn].messages) == 2  # the thread's older message came too
    assert by_urn[convs[1].urn].messages[0].outbound
    assert len(by_urn[convs[0].urn].messages) == 1  # not opened: the last message only


async def test_no_thread_opens_when_none_is_asked_for() -> None:
    site, _ = _with_threads(4)
    await read(site, spec())
    assert site.thread_navigations == []


async def test_at_most_five_threads_open_even_if_asked_for_more() -> None:
    site, convs = _with_threads(8)
    job = spec()
    # The spec itself refuses more than five; force six past it to test the source's own cap.
    object.__setattr__(job, "open_threads_for", frozenset(c.urn for c in convs[:6]))
    _, source = await read(site, job)

    assert len(site.thread_navigations) == MAX_THREADS_OPENED == 5
    assert source.threads_opened == 5
    # The newest five, not an arbitrary five.
    opened = {u.rstrip("/").rsplit("/", 1)[1] for u in site.thread_navigations}
    assert opened == {mp.thread_id(c.n) for c in convs[:5]}


async def test_a_conversation_quiet_since_the_last_poll_is_not_opened() -> None:
    site, convs = _with_threads(4)  # 100, 95, 90, 85 minutes
    job = spec(since=at(90), open_for=frozenset(c.urn for c in convs))
    delta, _ = await read(site, job)

    opened = {u.rstrip("/").rsplit("/", 1)[1] for u in site.thread_navigations}
    assert opened == {mp.thread_id(convs[0].n), mp.thread_id(convs[1].n)}
    assert delta.complete


async def test_a_group_or_an_ad_is_never_opened() -> None:
    everything = [*mp.INBOX_FIRST_PAGE, *mp.INBOX_OLDER_PAGE]
    site = InboxSite(everything, behavior=Behavior(first=len(everything)))
    job = spec(open_for=frozenset({mp.SPONSORED.urn, mp.GROUP.urn, mp.INMAIL_PENDING.urn}))
    await read(site, job)
    assert site.thread_navigations == []


async def test_a_thread_the_page_loaded_on_its_own_costs_no_navigation() -> None:
    site, convs = _with_threads(3)
    site.b.auto_thread = convs[0].n
    delta, source = await read(site, spec(open_for=frozenset({convs[0].urn})))

    assert site.thread_navigations == [] and source.threads_opened == 0
    assert len(delta.conversations[0].messages) == 2


async def test_a_thread_that_loads_nothing_stops_the_poll() -> None:
    site, convs = _with_threads(3)
    site.b.silent_threads = frozenset({convs[0].n})
    stop = await stopped(site, spec(open_for=frozenset({convs[0].urn})))
    assert stop.outcome is Outcome.ROUTE_CHANGED


async def test_a_thread_whose_body_is_lost_stops_the_poll() -> None:
    site, convs = _with_threads(3)
    site.b.thread_replies = {
        convs[0].n: Reply(body_error=Exception("No resource with given identifier found"))
    }
    with pytest.raises(ObservationFailed):
        await read(site, spec(open_for=frozenset({convs[0].urn})))


async def test_a_thread_answered_with_an_error_status_stops_the_poll() -> None:
    site, convs = _with_threads(3)
    site.b.thread_replies = {convs[0].n: Reply(status=500)}
    stop = await stopped(site, spec(open_for=frozenset({convs[0].urn})))
    assert stop.outcome is Outcome.ROUTE_CHANGED


async def test_a_thread_answer_under_a_renamed_field_stops_the_poll() -> None:
    site, convs = _with_threads(3)
    renamed = mp.messages_by_sync_token(site.threads[convs[0].n]).replace(
        mp.MESSAGES_BY_SYNC_TOKEN, "messengerMessagesByRenamedQuery"
    )
    site.b.thread_replies = {convs[0].n: Reply(body=renamed)}
    stop = await stopped(site, spec(open_for=frozenset({convs[0].urn})))
    assert stop.outcome is Outcome.ROUTE_CHANGED


async def test_an_item_with_no_message_whose_thread_was_not_opened_is_incomplete() -> None:
    convs = descending(6)
    bare = mp.Conv(
        **{
            **{f: getattr(convs[5], f) for f in mp.Conv.__dataclass_fields__},
            "last": None,
            "has_messages": False,
        }
    )
    convs[5] = bare
    threads = {
        c.n: [mp.Msg(c.n, 0, mp.OWNER, "Invented opener.", mp.T0), _last(c)] for c in convs[:5]
    }
    site = InboxSite(convs, threads, Behavior(first=6))
    job = spec()
    object.__setattr__(job, "open_threads_for", frozenset(c.urn for c in convs))
    delta, source = await read(site, job)

    assert source.threads_opened == 5  # the cap: the bare one, the oldest, was not opened
    assert not delta.complete and len(delta.conversations) == 5


async def test_an_item_with_no_message_nobody_asked_about_leaves_the_read_complete() -> None:
    convs = descending(2)
    convs[1] = mp.Conv(
        **{
            **{f: getattr(convs[1], f) for f in mp.Conv.__dataclass_fields__},
            "last": None,
            "has_messages": False,
        }
    )
    delta, _ = await read(InboxSite(convs, behavior=Behavior(first=2)), spec())
    assert delta.complete and len(delta.conversations) == 1


async def test_an_error_on_a_list_refresh_stops_the_poll_even_though_the_list_read() -> None:
    """The list itself arrived whole; a failed answer of ours is still a stop."""
    site = InboxSite(descending(3), behavior=Behavior(first=3, refresh_reply=Reply(status=500)))
    assert (await stopped(site, spec())).outcome is Outcome.ROUTE_CHANGED


async def test_an_older_page_of_another_category_is_a_stop() -> None:
    def secondary(index: int, url: str) -> str:
        return url.replace(mp.PRIMARY_INBOX, mp.SECONDARY_INBOX)

    site = InboxSite(descending(9), behavior=Behavior(first=3, page_size=3, older_url=secondary))
    assert (await stopped(site, spec(since=at(0)))).outcome is Outcome.ROUTE_CHANGED


async def test_a_wall_at_a_thread_stops_with_the_outcome() -> None:
    site, convs = _with_threads(3)
    site.b.thread_landing = inbox_site.CHECKPOINT_URL
    stop = await stopped(site, spec(open_for=frozenset({convs[0].urn})))

    assert stop.outcome is Outcome.CHECKPOINT and stop.final_url == inbox_site.CHECKPOINT_URL


async def test_a_thread_answer_that_does_not_parse_stops_the_poll() -> None:
    site, convs = _with_threads(3)
    site.b.thread_replies = {convs[0].n: Reply(body=json.dumps({"data": {"x": 1}}))}
    site.b.auto_thread = None
    # The reply names no variant, so it is not a thread answer; a thread answer with a
    # broken message is.
    broken = _mutated_thread(convs[0].n)
    site.b.thread_replies = {convs[0].n: Reply(body=broken)}
    stop = await stopped(site, spec(open_for=frozenset({convs[0].urn})))
    assert stop.outcome is Outcome.ROUTE_CHANGED


def _mutated_thread(n: int) -> str:
    doc = json.loads(mp.messages_by_sync_token([mp.Msg(n, 1, mp.OWNER, "Invented.", mp.T0)]))
    del doc["data"][mp.MESSAGES_BY_SYNC_TOKEN]["elements"][0]["deliveredAt"]
    return json.dumps(doc)


# --- send matching -----------------------------------------------------------------------


async def test_a_send_the_person_made_arrives_as_an_outbound_message_after_the_hand_over() -> None:
    who = people(1)[0]
    handed_over = at(30)
    c = conv(100, who, 50, outbound=True, text="Invented sent line.")
    thread = [
        mp.Msg(100, 0, who, "Invented earlier note.", mp.T0 + 10 * mp.MINUTE_MS),
        mp.Msg(100, 1, mp.OWNER, "Invented sent line.", mp.T0 + 40 * mp.MINUTE_MS),
        _last(c),
    ]
    site = InboxSite([c], {100: thread}, Behavior(first=1))
    delta, _ = await read(site, spec(since=at(0), open_for=frozenset({c.urn})))

    [conversation] = delta.conversations
    sends = [m for m in conversation.messages if m.outbound and m.at > handed_over]
    assert len(sends) == 2  # the thread's own and the list's last (a second send)
    assert all(m.sender_urn == mp.OWNER.urn for m in sends)
    assert conversation.conversation_urn == c.urn
    assert [m.outbound for m in conversation.messages] == [False, True, True]
    assert all(m.message_urn.startswith("urn:li:msg_message:(") for m in sends)


async def test_a_contacts_message_is_never_a_send_even_at_the_same_time() -> None:
    who = people(1)[0]
    c = conv(100, who, 50)
    delta, _ = await read(InboxSite([c], behavior=Behavior(first=1)), spec())
    [message] = delta.conversations[0].messages
    assert not message.outbound and message.sender_urn == who.urn


# --- walls, statuses, shapes --------------------------------------------------------------


@pytest.mark.parametrize(
    ("landing", "outcome"),
    [
        (inbox_site.CHECKPOINT_URL, Outcome.CHECKPOINT),
        (inbox_site.LOGIN_URL, Outcome.LOGGED_OUT),
        ("https://www.linkedin.com/feed/", Outcome.ROUTE_CHANGED),
    ],
)
async def test_a_wall_or_another_page_at_messaging_stops_with_its_outcome(
    landing: str, outcome: Outcome
) -> None:
    site = InboxSite(descending(3), behavior=Behavior(landing=landing))
    stop = await stopped(site, spec())
    assert stop.outcome is outcome and stop.final_url == landing


async def test_a_throttled_list_stops_as_throttled() -> None:
    site = InboxSite(descending(3), behavior=Behavior(list_reply=Reply(status=429)))
    assert (await stopped(site, spec())).outcome is Outcome.THROTTLED


async def test_a_page_that_loads_no_list_is_a_stop_not_an_empty_inbox() -> None:
    site = InboxSite(descending(3), behavior=Behavior(no_list=True))
    assert (await stopped(site, spec())).outcome is Outcome.ROUTE_CHANGED


def _broken_list() -> str:
    doc = json.loads(mp.conversations_by_sync_token(descending(3)))
    del doc["data"][mp.BY_SYNC_TOKEN]["elements"][1]["lastActivityAt"]
    return json.dumps(doc)


async def test_an_unknown_list_shape_stops_and_hands_nothing_on() -> None:
    site = InboxSite(descending(3), behavior=Behavior(list_reply=Reply(body=_broken_list())))
    stop = await stopped(site, spec())
    assert stop.outcome is Outcome.ROUTE_CHANGED


async def test_an_older_page_that_does_not_parse_stops_the_poll() -> None:
    doc = json.loads(mp.conversations_by_category(descending(3)[:0] or [], next_cursor="x"))
    doc["data"][mp.BY_CATEGORY]["elements"] = [{"entityUrn": "urn:li:nothing"}]
    site = InboxSite(
        descending(9),
        behavior=Behavior(first=3, older_replies={0: Reply(body=json.dumps(doc))}),
    )
    assert (await stopped(site, spec(since=at(0)))).outcome is Outcome.ROUTE_CHANGED


async def test_no_message_text_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    secret = "Lorem ipsum nobody may log"
    site = InboxSite(
        [conv(100, people(1)[0], 50, text=secret)], behavior=Behavior(first=1, no_list=False)
    )
    await read(site, spec())
    site2 = InboxSite(descending(3), behavior=Behavior(list_reply=Reply(body=_broken_list())))
    await stopped(site2, spec())
    assert secret not in caplog.text


def test_only_linkedin_or_loopback_is_an_origin() -> None:
    with pytest.raises(ValueError, match="never"):
        PageInbox(object(), origin="https://evil.example")  # type: ignore[arg-type]
    PageInbox(object(), origin="http://127.0.0.1:4000")  # type: ignore[arg-type]


# --- the whole poll: a parse failure is an aborted poll, never a quiet complete one -------


def _watched(factory: sessionmaker[Session]) -> int:
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        contact = factories.make_contact(session, user, li_urn=people(1)[0].urn)
        factories.make_enrollment(session, factories.make_campaign(session, user), contact)
        return user.id


async def _poll(
    factory: sessionmaker[Session], user_id: int, site: FakeContext
) -> tuple[SyncRunStatus, str | None]:
    provider, _ = fake_provider(site)
    now = datetime.now(UTC)
    async with provider.run("account-1") as run:
        source = PageInbox(
            run,
            sleep=no_sleep,
            rng=random.Random(1),
            scroll_profile=ONE_WHEEL,
            landing_wait_s=0.05,
            response_wait_s=0.05,
            thread_wait_s=0.05,
            thread_pause_s=(0.0, 0.0),
        )
        report = await poll_inbox(
            factory, user_id, source, settings=LinkedInSettings(), clock=lambda: now
        )
    with session_scope(factory) as session:
        from netkeeper.services import runs

        user = session.get(User, user_id)
        assert user is not None
        run_row = runs.get_run(session, user, report.run_id)
        return run_row.status, run_row.stop_reason


def _interactions(factory: sessionmaker[Session], user_id: int) -> list[Interaction]:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        rows = list(session.scalars(scoped(user, Interaction).order_by(Interaction.id)))
        session.expunge_all()
        return rows


async def test_a_healthy_page_completes_and_records_the_contacts_reply(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _watched(session_factory)
    who = people(1)[0]
    site = InboxSite([conv(100, who, 50)], behavior=Behavior(first=1))
    # Make the conversation recent enough to be read by a first poll with no since.
    status, reason = await _poll(session_factory, user_id, site)

    assert (status, reason) == (SyncRunStatus.COMPLETED, "inbox_read")
    rows = _interactions(session_factory, user_id)
    assert [r.kind for r in rows] == [InteractionKind.LI_IN]


async def test_an_unknown_shape_aborts_the_poll_and_writes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _watched(session_factory)
    site = InboxSite(
        [conv(100, people(1)[0], 50)],
        behavior=Behavior(first=1, list_reply=Reply(body=_broken_list())),
    )
    status, reason = await _poll(session_factory, user_id, site)

    assert status is SyncRunStatus.ABORTED and reason == "route_changed"
    assert _interactions(session_factory, user_id) == []


async def test_a_wall_aborts_the_poll_and_flags_the_session(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _watched(session_factory)
    site = InboxSite([], behavior=Behavior(landing=inbox_site.CHECKPOINT_URL))
    status, reason = await _poll(session_factory, user_id, site)

    assert status is SyncRunStatus.ABORTED and reason == "checkpoint"


# --- whose mailbox -------------------------------------------------------------------------


def _set_self_urn(factory: sessionmaker[Session], user_id: int, urn: str | None) -> None:
    from netkeeper.crm.self_contact import ensure_self_contact

    with session_scope(factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        me = ensure_self_contact(session, user)
        me.li_urn = urn


async def test_a_page_showing_another_mailbox_than_the_self_contacts_aborts_and_writes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _watched(session_factory)
    _set_self_urn(session_factory, user_id, mp.ZEPHYRINE.urn)
    site = InboxSite([conv(100, people(1)[0], 50)], behavior=Behavior(first=1))
    status, reason = await _poll(session_factory, user_id, site)

    assert (status, reason) == (SyncRunStatus.ABORTED, "owner_mismatch")
    assert _interactions(session_factory, user_id) == []


async def test_a_page_showing_the_self_contacts_mailbox_completes(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _watched(session_factory)
    _set_self_urn(session_factory, user_id, mp.OWNER.urn)
    site = InboxSite([conv(100, people(1)[0], 50)], behavior=Behavior(first=1))
    status, reason = await _poll(session_factory, user_id, site)
    assert (status, reason) == (SyncRunStatus.COMPLETED, "inbox_read")


async def test_with_no_self_urn_the_first_polls_owner_is_recorded_and_held_to(
    session_factory: sessionmaker[Session],
) -> None:
    user_id = _watched(session_factory)
    site = InboxSite([conv(100, people(1)[0], 50)], behavior=Behavior(first=1))
    assert (await _poll(session_factory, user_id, site))[0] is SyncRunStatus.COMPLETED
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        from netkeeper.services.settings_kv import get_setting

        assert get_setting(session, user, "linkedin.inbox.owner_urn") == mp.OWNER.urn

    # Later the recorded owner is somebody else's: the page shows another account.
    before = len(_interactions(session_factory, user_id))
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        from netkeeper.services.settings_kv import set_setting

        set_setting(session, user, "linkedin.inbox.owner_urn", mp.ZEPHYRINE.urn)
    status, reason = await _poll(session_factory, user_id, site)
    assert (status, reason) == (SyncRunStatus.ABORTED, "owner_mismatch")
    assert len(_interactions(session_factory, user_id)) == before
