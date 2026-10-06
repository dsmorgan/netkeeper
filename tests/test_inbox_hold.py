"""Hold LinkedIn steps while the LinkedIn inbox poll is stale (#417).

The rule is pure and pinned at each boundary. The holds run through the real claim
(:func:`claim_prefill`), the real tick, and the real inbox poll against
:class:`inbox_fakes.FakeInboxSource`: nothing loads a page, and every URN is invented.
Each safety check has a test that fails when the check is weakened (see the PR's mutants).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import factories
import pytest
from campaign_fakes import NOW
from inbox_fakes import FakeInboxSource, delta, record_poll
from sqlalchemy.orm import Session, sessionmaker
from test_campaign_engine import World, make_world
from test_linkedin_steps import EMAIL, LANE_SETTINGS, LINKEDIN, Lane, make_lane

from netkeeper.config import LinkedInSettings, Settings
from netkeeper.models import (
    Campaign,
    Enrollment,
    EnrollmentStatus,
    LiConversation,
    MessageDirection,
    MessageStatus,
    SyncRunKind,
    SyncRunStatus,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import inbox_hold, runs
from netkeeper.services import poll_status as poll_status_service
from netkeeper.services.campaign_engine import Skip
from netkeeper.services.inbox_hold import (
    INTERVAL,
    SLACK,
    STALE_AFTER_POLLS,
    is_stale,
    stale_after,
)
from netkeeper.services.inbox_poll import INCOMPLETE, poll_inbox
from netkeeper.services.linkedin_steps import USER_REFUSALS, Refusal
from netkeeper.services.posture import Protection, Status, posture
from netkeeper.services.scheduler import DEFAULT_SCHEDULES, RETRY_MAX_MINUTES, JobKind

LIMIT = stale_after()


@pytest.fixture(autouse=True)
def _message_send_has_a_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """``message_send`` has no runner until P4-03; the claim records its run through the
    real ``create_run`` (as ``test_linkedin_steps`` does)."""
    monkeypatch.setattr(runs, "RUNNABLE_KINDS", runs.RUNNABLE_KINDS | {SyncRunKind.MESSAGE_SEND})


# --- the rule ------------------------------------------------------------------------------


def test_the_rule_constants_are_pinned() -> None:
    """Safety constants against numbers written out here (CLAUDE.md)."""
    assert STALE_AFTER_POLLS == 2  # Gmail's number
    assert timedelta(hours=3) == INTERVAL == DEFAULT_SCHEDULES[JobKind.INBOX].interval
    assert timedelta(minutes=50) == SLACK
    assert RETRY_MAX_MINUTES == 50.0
    assert timedelta(hours=6, minutes=50) == LIMIT


def test_a_poll_exactly_at_the_limit_is_fresh_and_a_second_past_it_is_stale() -> None:
    assert not is_stale(NOW - LIMIT, now=NOW)
    assert is_stale(NOW - LIMIT - timedelta(seconds=1), now=NOW)
    assert not is_stale(NOW - timedelta(seconds=1), now=NOW)
    assert not is_stale(NOW, now=NOW)


def test_a_poll_from_the_future_is_not_stale() -> None:
    """A clock set back must not lock the user out: only an old poll is stale."""
    assert not is_stale(NOW + timedelta(days=1), now=NOW)


def test_no_poll_is_stale_while_linkedin_is_live_and_not_otherwise() -> None:
    assert is_stale(None, now=NOW)
    assert is_stale(None, now=NOW, linkedin_live=True)
    assert not is_stale(None, now=NOW, linkedin_live=False)


def test_the_limit_follows_the_interval() -> None:
    assert stale_after(timedelta(hours=1)) == timedelta(hours=2) + SLACK
    assert is_stale(NOW - timedelta(hours=3), now=NOW, interval=timedelta(hours=1))
    assert not is_stale(NOW - timedelta(hours=2), now=NOW, interval=timedelta(hours=1))


def test_an_old_complete_poll_is_stale_even_though_one_ever_completed() -> None:
    assert is_stale(NOW - timedelta(days=30), now=NOW, linkedin_live=False)


# --- which poll counts -----------------------------------------------------------------------


def test_only_a_complete_poll_counts_and_the_newest_by_start(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, polled=False)

    def read() -> datetime | None:
        return lane.read(lambda s, u: inbox_hold.last_complete_poll(s, u, now=NOW))

    assert read() is None
    lane.write(
        lambda s, u: record_poll(
            s, u, NOW - timedelta(hours=9), status=SyncRunStatus.ABORTED, reason=INCOMPLETE
        )
    )
    lane.write(
        lambda s, u: record_poll(
            s, u, NOW - timedelta(hours=8), status=SyncRunStatus.ABORTED, reason="throttled"
        )
    )
    lane.write(
        lambda s, u: record_poll(
            s, u, NOW - timedelta(hours=6), status=SyncRunStatus.FAILED, reason="heat"
        )
    )
    assert read() is None  # none of them read the inbox
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(hours=5)))
    assert read() == NOW - timedelta(hours=5)
    lane.write(
        lambda s, u: record_poll(
            s, u, NOW - timedelta(hours=1), status=SyncRunStatus.ABORTED, reason=INCOMPLETE
        )
    )
    assert read() == NOW - timedelta(hours=5)  # the newer, part-way poll does not refresh it


# --- who is watched --------------------------------------------------------------------------


def _conversation(session: Session, user: User, contact_id: int, name: str = "A") -> None:
    session.add(
        LiConversation(
            user_id=user.id,
            contact_id=contact_id,
            conversation_urn=f"urn:li:msg_conversation:INVENTED{name}",
            last_activity_at=NOW,
            polled_at=NOW,
        )
    )
    session.flush()


def test_a_contact_is_watched_by_a_conversation_or_a_claimed_linkedin_message(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory, channels=(EMAIL, LINKEDIN))
    by_conversation, by_message, discarded, typed, email_only, bare = (
        world.enroll_new() for _ in range(6)
    )

    def setup(session: Session) -> None:
        def contact_of(enrollment_id: int) -> int:
            row = get_scoped(session, world.user, Enrollment, enrollment_id)
            assert row is not None
            return row.contact_id

        _conversation(session, world.user, contact_of(by_conversation))
        for enrollment_id, status in (
            (by_message, MessageStatus.PREFILLED),
            (discarded, MessageStatus.DISCARDED),
        ):
            enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
            assert enrollment is not None
            factories.make_message(session, enrollment, position=2, status=status, sent_at=None)
        # A discarded prefill that was typed may have been sent anyway: it watches the
        # contact with no time limit, since the next step is due a delay after the discard.
        for enrollment_id, typed_at in ((typed, NOW - timedelta(days=90)),):
            enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
            assert enrollment is not None
            factories.make_message(
                session,
                enrollment,
                position=2,
                status=MessageStatus.DISCARDED,
                sent_at=None,
                prefilled_at=typed_at,
            )
        enrollment = get_scoped(session, world.user, Enrollment, email_only)
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1)  # an email, sent

    world.write(setup)

    def watched(enrollment_id: int) -> bool:
        def check(session: Session) -> bool:
            row = get_scoped(session, world.user, Enrollment, enrollment_id)
            assert row is not None
            return inbox_hold.contact_watched(session, world.user, row.contact_id)

        return world.read(check)

    assert watched(by_conversation)
    assert watched(by_message)
    assert not watched(discarded)  # never typed
    assert watched(typed)
    assert not watched(email_only)
    assert not watched(bare)


def test_another_users_conversation_does_not_watch_my_contact(
    session_factory: sessionmaker[Session],
) -> None:
    mine = make_world(session_factory)
    theirs = make_world(session_factory)
    mine_id = mine.enroll_new()

    def link(session: Session) -> None:
        row = get_scoped(session, mine.user, Enrollment, mine_id)
        assert row is not None
        # The other user holds a conversation under the same contact id: never mine.
        _conversation(session, theirs.user, row.contact_id)

    mine.write(link)

    def check(session: Session) -> bool:
        row = get_scoped(session, mine.user, Enrollment, mine_id)
        assert row is not None
        return inbox_hold.contact_watched(session, mine.user, row.contact_id)

    assert not mine.read(check)


def test_another_users_claimed_message_does_not_watch_my_contact(
    session_factory: sessionmaker[Session],
) -> None:
    mine = make_world(session_factory, channels=(EMAIL, LINKEDIN))
    theirs = make_world(session_factory, channels=(EMAIL, LINKEDIN))
    mine_id = mine.enroll_new()
    theirs_id = theirs.enroll_new()

    def link(session: Session) -> int:
        row = get_scoped(session, mine.user, Enrollment, mine_id)
        other = get_scoped(session, theirs.user, Enrollment, theirs_id)
        assert row is not None and other is not None
        # Their LinkedIn message, carrying my contact's id: never mine to watch.
        message = factories.make_message(
            session, other, position=2, status=MessageStatus.PREFILLED, sent_at=None
        )
        message.contact_id = row.contact_id
        session.flush()
        return row.contact_id

    contact_id = mine.write(link)
    assert not mine.read(lambda s: inbox_hold.contact_watched(s, mine.user, contact_id))


def test_another_users_fresh_poll_does_not_release_my_hold(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, polled=False)
    other = make_world(session_factory)
    other.write(lambda s: record_poll(s, other.user, NOW - timedelta(minutes=1)))
    assert lane.read(lambda s, u: inbox_hold.last_complete_poll(s, u, now=NOW)) is None
    assert lane.claim(lane.enroll(), polled=False).reasons == ("linkedin_inbox_stale",)


def test_another_users_linkedin_campaign_does_not_put_me_in_use(
    session_factory: sessionmaker[Session],
) -> None:
    mine = make_world(session_factory)
    mine.enroll_new()
    theirs = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    theirs.enroll_new()
    assert not mine.read(lambda s: inbox_hold.linkedin_in_use(s, mine.user))
    assert theirs.read(lambda s: inbox_hold.linkedin_in_use(s, theirs.user))
    row = _row(mine)
    assert row is not None and row.notes == ()  # no hold text for me
    check = _inbox_check(mine, poll_status_service.Serving())
    assert check.reason is not None and "held" not in check.reason


def test_a_future_dated_poll_is_not_the_newest_and_cannot_stay_fresh(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, polled=False)
    lane.write(lambda s, u: record_poll(s, u, NOW + timedelta(days=30)))
    assert lane.read(lambda s, u: inbox_hold.last_complete_poll(s, u, now=NOW)) is None
    assert lane.read(lambda s, u: inbox_hold.stale(s, u, now=NOW))
    # An older real poll is what counts, not the newest row.
    lane.write(lambda s, u: record_poll(s, u, NOW - LIMIT - timedelta(minutes=1)))
    assert lane.read(lambda s, u: inbox_hold.last_complete_poll(s, u, now=NOW)) == (
        NOW - LIMIT - timedelta(minutes=1)
    )
    assert lane.claim(lane.enroll(), polled=False).reasons == ("linkedin_inbox_stale",)


def test_a_poll_that_ran_long_is_aged_from_its_start(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, polled=False)
    started = NOW - LIMIT - timedelta(minutes=1)
    lane.write(lambda s, u: record_poll(s, u, started, ended=NOW - timedelta(minutes=1)))
    assert lane.read(lambda s, u: inbox_hold.last_complete_poll(s, u, now=NOW)) == started
    assert lane.claim(lane.enroll(), polled=False).reasons == ("linkedin_inbox_stale",)


# --- a LinkedIn prefill claim ----------------------------------------------------------------


def test_a_claim_is_held_with_its_reason_while_no_poll_has_completed(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, polled=False)
    enrollment_id = lane.enroll()
    claim = lane.claim(enrollment_id, polled=False)

    assert not claim.claimed
    assert claim.reasons == (Refusal.INBOX_STALE.value,) == ("linkedin_inbox_stale",)
    assert claim.detail is not None
    assert "no poll has completed yet" in claim.detail
    assert "run `netkeeper linkedin inbox` by hand; scheduled polls wait for a first one" in (
        claim.detail
    )
    assert "or wait for the next scheduled poll" not in claim.detail
    assert "netkeeper linkedin inbox" in claim.detail
    # Wait, never skip or fail: nothing was written, and the enrollment is still due.
    assert lane.messages(enrollment_id) == []
    assert lane.runs() == []
    enrollment = lane.enrollment(enrollment_id)
    assert (enrollment.status, enrollment.next_action_at) == (EnrollmentStatus.ACTIVE, NOW)
    assert Refusal.INBOX_STALE in USER_REFUSALS  # "prefill next" stops at the first


def test_a_claim_is_held_on_an_old_complete_poll_and_goes_on_a_fresh_one(
    session_factory: sessionmaker[Session],
) -> None:
    lane = make_lane(session_factory, polled=False)
    enrollment_id = lane.enroll()
    lane.write(lambda s, u: record_poll(s, u, NOW - LIMIT - timedelta(minutes=1)))
    held = lane.claim(enrollment_id, polled=False)
    assert held.reasons == ("linkedin_inbox_stale",)
    assert held.detail is not None and "hours ago" in held.detail
    assert "or wait for the next scheduled poll" in held.detail  # an older poll exists

    lane.write(lambda s, u: record_poll(s, u, NOW - LIMIT))
    assert lane.claim(enrollment_id, polled=False).claimed


async def test_a_held_claim_is_released_only_by_a_complete_poll(
    session_factory: sessionmaker[Session],
) -> None:
    """The real poll: a part-way one (``inbox_incomplete``) keeps the hold, a complete one
    ends it, and nothing else changed on the enrollment in between."""
    lane = make_lane(session_factory, polled=False)
    enrollment_id = lane.enroll()
    lane.write(lambda s, u: record_poll(s, u, NOW - timedelta(days=2)))  # complete, long ago
    assert lane.claim(enrollment_id, polled=False).reasons == ("linkedin_inbox_stale",)

    part_way = FakeInboxSource(delta(complete=False))
    report = await poll_inbox(
        session_factory,
        lane.user_id,
        part_way,
        settings=LinkedInSettings(),
        clock=lambda: NOW,
    )
    assert report.stop_reason == INCOMPLETE
    again = lane.claim(enrollment_id, polled=False)
    assert again.reasons == ("linkedin_inbox_stale",), (
        "a poll that stopped part way must not release"
    )
    assert lane.messages(enrollment_id) == []

    complete = FakeInboxSource(delta())
    report = await poll_inbox(
        session_factory,
        lane.user_id,
        complete,
        settings=LinkedInSettings(),
        clock=lambda: NOW,
    )
    assert report.stop_reason == "inbox_read"
    claim = lane.claim(enrollment_id, polled=False)
    assert claim.claimed, claim.reasons
    [message] = lane.messages(enrollment_id)
    assert message.status is MessageStatus.SCHEDULED
    assert message.direction is MessageDirection.OUT


def test_the_hold_adds_to_the_claims_other_refusals_and_never_replaces_one(
    session_factory: sessionmaker[Session],
) -> None:
    """An enrollment that is not due is refused for that, first: the hold only ever adds."""
    lane = make_lane(session_factory, polled=False)
    not_due = lane.enroll(next_action_at=NOW + timedelta(days=1))
    assert lane.claim(not_due, polled=False).reasons == (Skip.NOT_DUE.value,)


def test_a_claim_with_a_fresh_poll_is_not_held(session_factory: sessionmaker[Session]) -> None:
    lane: Lane = make_lane(session_factory)  # records a poll an hour before NOW
    assert lane.claim(lane.enroll()).claimed
    assert lane.settings is LANE_SETTINGS


def test_a_reply_ends_a_held_claim_the_hold_never_hides_it(
    session_factory: sessionmaker[Session],
) -> None:
    """The claim's twin of the email test: the reply is read before the hold, so a reply
    ends the enrollment even while the inbox is stale."""
    lane = make_lane(session_factory, polled=False)
    enrollment_id = lane.enroll()

    def reply(session: Session, user: User) -> None:
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(
            session,
            enrollment,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            sent_at=NOW - timedelta(hours=1),
        )

    lane.write(reply)
    claim = lane.claim(enrollment_id, polled=False)
    assert claim.reasons == (Skip.ENDED.value, Skip.REPLIED.value)
    assert lane.enrollment(enrollment_id).status is EnrollmentStatus.REPLIED


# --- an email step ---------------------------------------------------------------------------


def _watch(world: World, enrollment_id: int) -> None:
    def link(session: Session) -> None:
        row = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert row is not None
        _conversation(session, world.user, row.contact_id, name=str(enrollment_id))

    world.write(link)


def _poll(world: World, at: datetime, **kwargs: object) -> None:
    def record(session: Session) -> None:
        record_poll(session, world.user, at, **kwargs)  # type: ignore[arg-type]

    world.write(record)


def test_an_email_step_for_a_watched_contact_is_held_while_an_unwatched_ones_goes(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory)
    watched = world.enroll_new(email="watched@example.test")
    free = world.enroll_new(email="free@example.test")
    _watch(world, watched)

    first = world.tick()
    assert [f.enrollment_id for f, _ in first.fired] == [free]
    assert first.skipped()[watched] == (Skip.LINKEDIN_INBOX_STALE.value,)
    assert Skip.LINKEDIN_INBOX_STALE.value == "linkedin_inbox_stale"
    # Wait, never skip or fail: still active, still due, nothing written for it.
    assert world.messages(watched) == []
    row = world.enrollment(watched)
    assert (row.status, row.next_action_at, row.not_sent_error) == (
        EnrollmentStatus.ACTIVE,
        NOW,
        None,
    )
    assert world.tick().fired == []  # and the next tick holds it again


def test_a_held_email_step_goes_once_a_complete_poll_has_read_the_inbox(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory)
    watched = world.enroll_new()
    _watch(world, watched)
    assert world.tick().fired == []

    _poll(world, NOW - timedelta(minutes=5))
    [(firing, _)] = world.tick().fired
    assert firing.enrollment_id == watched


def test_a_poll_that_stopped_part_way_keeps_an_email_step_held(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory)
    watched = world.enroll_new()
    _watch(world, watched)
    _poll(world, NOW - timedelta(days=1))  # complete, but old
    _poll(world, NOW - timedelta(minutes=1), status=SyncRunStatus.ABORTED, reason=INCOMPLETE)
    result = world.tick()
    assert result.fired == []
    assert result.skipped()[watched] == (Skip.LINKEDIN_INBOX_STALE.value,)
    _poll(world, NOW - timedelta(seconds=30), status=SyncRunStatus.ABORTED, reason="throttled")
    assert world.tick().fired == []


def test_the_email_hold_edge_is_the_rules_limit(session_factory: sessionmaker[Session]) -> None:
    world = make_world(session_factory)
    watched = world.enroll_new()
    _watch(world, watched)
    _poll(world, NOW - LIMIT - timedelta(seconds=1))
    assert world.tick().fired == []
    _poll(world, NOW - LIMIT)
    assert [f.enrollment_id for f, _ in world.tick().fired] == [watched]


def test_a_reply_ends_a_held_enrollment_the_hold_never_hides_it(
    session_factory: sessionmaker[Session],
) -> None:
    """The hold comes after every check that already ends or holds: a reply is recorded
    and ends the enrollment even while the inbox is stale."""
    world = make_world(session_factory)
    watched = world.enroll_new()
    _watch(world, watched)

    def reply(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, watched)
        assert enrollment is not None
        factories.make_message(
            session,
            enrollment,
            direction=MessageDirection.IN,
            status=MessageStatus.RECEIVED,
            sent_at=NOW - timedelta(hours=1),
        )

    world.write(reply)
    result = world.tick()
    assert result.skipped()[watched] == (Skip.ENDED.value, Skip.REPLIED.value)
    assert world.enrollment(watched).status is EnrollmentStatus.REPLIED


def test_an_existing_hold_still_holds_when_the_inbox_is_fresh_or_stale(
    session_factory: sessionmaker[Session],
) -> None:
    """Outside the sending hours the step is deferred for that reason, hold or no hold."""
    world = make_world(session_factory)
    world.hours(days=["Sat"], start="09:00", end="17:00")  # NOW is a Tuesday
    watched = world.enroll_new()
    _watch(world, watched)
    result = world.tick()
    assert Skip.LINKEDIN_INBOX_STALE.value not in result.skipped()[watched]
    assert result.fired == []


@pytest.mark.parametrize("age", [timedelta(days=7, hours=1), timedelta(days=30)])
def test_a_typed_then_discarded_prefill_still_holds_the_email_step_a_full_delay_later(
    session_factory: sessionmaker[Session], age: timedelta
) -> None:
    """Step 1 (LinkedIn) was typed, then discarded; step 2 (email) is due its 7-day delay
    later. The person may have sent it anyway, so while no complete poll has read the
    inbox since, the email step waits, and a complete poll releases it."""
    world = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    discarded_at = NOW - age
    enrollment_id = world.enroll_new(current_step=1, next_action_at=NOW)

    def discard(session: Session) -> None:
        enrollment = get_scoped(session, world.user, Enrollment, enrollment_id)
        assert enrollment is not None
        factories.make_message(
            session,
            enrollment,
            position=1,
            status=MessageStatus.DISCARDED,
            sent_at=None,
            prefilled_at=discarded_at - timedelta(minutes=5),
            discarded_at=discarded_at,
        )

    world.write(discard)
    held = world.tick()
    assert held.fired == []
    assert held.skipped()[enrollment_id] == (Skip.LINKEDIN_INBOX_STALE.value,)

    _poll(world, NOW - timedelta(minutes=1))
    [(firing, _)] = world.tick().fired
    assert firing.enrollment_id == enrollment_id


def _forged_world(session_factory: sessionmaker[Session]) -> tuple[World, World]:
    mine = make_world(session_factory)
    mine.enroll_new()
    return mine, make_world(session_factory)


def _my_contact(world: World, session: Session) -> int:
    row = session.scalars(scoped(world.user, Enrollment)).first()
    assert row is not None
    return row.contact_id


def test_a_forged_conversation_row_of_another_user_does_not_put_me_in_use(
    session_factory: sessionmaker[Session],
) -> None:
    mine, theirs = _forged_world(session_factory)

    def forge(session: Session) -> None:
        _conversation(session, theirs.user, _my_contact(mine, session))

    mine.write(forge)
    assert not mine.read(lambda s: inbox_hold.linkedin_in_use(s, mine.user))


def test_a_forged_message_row_of_another_user_does_not_put_me_in_use(
    session_factory: sessionmaker[Session],
) -> None:
    mine, _ = _forged_world(session_factory)
    theirs_campaign = make_world(session_factory, channels=(EMAIL, LINKEDIN))
    other_id = theirs_campaign.enroll_new()

    def forge(session: Session) -> None:
        other = get_scoped(session, theirs_campaign.user, Enrollment, other_id)
        assert other is not None
        message = factories.make_message(
            session, other, position=2, status=MessageStatus.PREFILLED, sent_at=None
        )
        message.contact_id = _my_contact(mine, session)

    mine.write(forge)
    assert not mine.read(lambda s: inbox_hold.linkedin_in_use(s, mine.user))


def test_a_forged_enrollment_row_of_another_user_does_not_put_me_in_use(
    session_factory: sessionmaker[Session],
) -> None:
    """My campaign has a LinkedIn step; the only enrollment in it belongs to someone else."""
    mine = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    theirs = make_world(session_factory)

    def forge(session: Session) -> None:
        contact = factories.make_contact(session, theirs.user, emails=["x@example.test"])
        campaign = get_scoped(session, mine.user, Campaign, mine.campaign.id)
        assert campaign is not None
        factories.make_enrollment(session, campaign, contact, user_id=theirs.user.id)

    mine.write(forge)
    assert not mine.read(lambda s: inbox_hold.linkedin_in_use(s, mine.user))


def test_my_enrollment_in_another_users_linkedin_campaign_does_not_put_me_in_use(
    session_factory: sessionmaker[Session],
) -> None:
    """A forged row: my enrollment points at a LinkedIn campaign owned by someone else."""
    mine = make_world(session_factory)
    theirs = make_world(session_factory, channels=(LINKEDIN, EMAIL))

    def forge(session: Session) -> None:
        contact = factories.make_contact(session, mine.user, emails=["y@example.test"])
        campaign = get_scoped(session, theirs.user, Campaign, theirs.campaign.id)
        assert campaign is not None
        factories.make_enrollment(session, campaign, contact, user_id=mine.user.id)

    mine.write(forge)
    assert not mine.read(lambda s: inbox_hold.linkedin_in_use(s, mine.user))


# --- posture and poll status ------------------------------------------------------------------


def _linkedin_world(session_factory: sessionmaker[Session]) -> World:
    world = make_world(session_factory, channels=(LINKEDIN, EMAIL))
    world.enroll_new()
    return world


def _row(world: World, at: datetime = NOW) -> Protection | None:
    def read(session: Session) -> Protection | None:
        user = session.get(User, world.user.id)
        assert user is not None
        report = posture(session, user, 1, now=at, settings=Settings())
        rows = [p for p in report.protections if p.name == "linkedin reply poll"]
        return rows[0] if rows else None

    return world.write(read)


def test_posture_row_has_no_hold_text_when_linkedin_is_not_in_use(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory)
    world.enroll_new()
    row = _row(world)
    assert row is not None and row.notes == ()  # nothing waits, so nothing is held
    _poll(world, NOW - timedelta(hours=1))
    polled = _row(world)
    assert polled is not None and "holds past" not in polled.value


def test_posture_explains_the_hold_when_no_poll_has_completed(
    session_factory: sessionmaker[Session],
) -> None:
    row = _row(_linkedin_world(session_factory))
    assert row is not None
    assert row.status is Status.ON
    # #433's first poll by hand, and #417's hold, both on the one row.
    assert "no complete poll yet" in row.value
    assert "run by hand: `netkeeper linkedin inbox`" in row.value
    (note,) = row.notes
    assert "no poll has completed yet" in note
    assert "netkeeper linkedin inbox" in note
    assert "wait until a poll completes" in note
    assert "nothing is skipped or failed" in note
    assert row.warnings == ()


def test_posture_explains_an_old_poll_and_then_a_fresh_one(
    session_factory: sessionmaker[Session],
) -> None:
    world = _linkedin_world(session_factory)
    _poll(world, NOW - timedelta(hours=8))
    old = _row(world)
    assert old is not None
    assert old.value.startswith("last complete poll 2026-09-29 06:00 UTC (8 h ago)")
    assert "its last complete poll was 8.0 hours ago" in old.notes[0]
    assert "older than 6.8 hours" in old.notes[0]

    _poll(world, NOW - timedelta(minutes=90))
    fresh = _row(world)
    assert fresh is not None
    assert fresh.value == "last complete poll 2026-09-29 12:30 UTC (1 h ago); holds past 6.8 h"
    assert fresh.notes == ()


def _inbox_check(world: World, serving: poll_status_service.Serving) -> poll_status_service.Check:
    def read(session: Session) -> poll_status_service.Check:
        user = session.get(User, world.user.id)
        assert user is not None
        status = poll_status_service.poll_status(
            session, user, now=NOW, settings=Settings(), serving=serving
        )
        [check] = [c for c in status.checks if c.key == poll_status_service.LINKEDIN_INBOX]
        return check

    return world.read(read)


def test_poll_status_says_why_the_inbox_holds_steps(
    session_factory: sessionmaker[Session],
) -> None:
    world = _linkedin_world(session_factory)
    check = _inbox_check(world, poll_status_service.Serving(scheduler=True, campaign_engine=True))
    assert check.reason is not None
    assert check.reason.startswith("Scheduled LinkedIn runs are disarmed")  # still says so
    assert "steps for a contact you are watching on LinkedIn (a conversation the poll has seen" in (
        check.reason
    )
    assert "or a prefill you claimed), are held until a poll completes. The LinkedIn inbox" in (
        check.reason
    )
    assert "no poll has completed yet" in check.reason
    # The same sentence when serve is not running: a poll that is not running is the usual cause.
    stopped = _inbox_check(world, poll_status_service.Serving())
    assert stopped.reason is not None and "are held until a poll completes" in stopped.reason

    _poll(world, NOW - timedelta(hours=1))
    fresh = _inbox_check(world, poll_status_service.Serving(scheduler=True, campaign_engine=True))
    assert fresh.reason == "Scheduled LinkedIn runs are disarmed"
    assert fresh.last_at is not None


def test_poll_status_is_silent_when_nothing_is_held(
    session_factory: sessionmaker[Session],
) -> None:
    world = make_world(session_factory)
    world.enroll_new()
    check = _inbox_check(world, poll_status_service.Serving())
    assert check.reason is not None and "held" not in check.reason


def test_posture_names_how_the_newest_poll_ended_with_the_hold_text(
    session_factory: sessionmaker[Session],
) -> None:
    world = _linkedin_world(session_factory)
    _poll(world, NOW - timedelta(hours=9))
    _poll(world, NOW - timedelta(hours=1), status=SyncRunStatus.ABORTED, reason="owner_mismatch")
    row = _row(world)
    assert row is not None
    (note,) = row.notes
    assert "the newest poll ended owner_mismatch" in note
    assert "run `netkeeper linkedin inbox-forget-owner`" in note
    assert "wait until a poll completes" in note  # the hold, in the same note
