"""netkeeper.services.campaign_guards (spec 11.9; item P3-05): who may be enrolled and sent to."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm.interactions import add_interaction
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    Contact,
    ContactEmail,
    EmailStatus,
    EnrollmentStatus,
    InteractionKind,
    TemplateChannel,
    User,
)
from netkeeper.services import campaign_guards as guards
from netkeeper.services.campaign_guards import (
    GUARDS,
    ChannelReason,
    ChannelState,
    ContactFacts,
    GuardPolicy,
    Reason,
    Verdict,
    check_channel,
    check_contact,
    check_enrollment,
    check_step,
    excluded_summary,
    load_facts,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
POLICY = GuardPolicy(contacted_within_days=30)


def facts(**changes: Any) -> ContactFacts:
    """A contact every guard lets through, with ``changes``."""
    eligible = ContactFacts(
        contact_id=1,
        merged=False,
        archived=False,
        needs_review=False,
        do_not_contact=False,
        disconnected=False,
        email_statuses=(EmailStatus.OK,),
        sendable_email="ada@example.test",
        has_linkedin=True,
        other_campaigns=frozenset(),
        last_outbound_at=None,
    )
    return dataclasses.replace(eligible, **changes)


def reasons(
    subject: ContactFacts | None,
    channel: TemplateChannel = EMAIL,
    policy: GuardPolicy = POLICY,
) -> tuple[Reason, ...]:
    return check_contact(subject, 1, channel, policy, now=NOW).reasons


# --- the constants are pinned ----------------------------------------------------------


def test_the_guard_sets_are_pinned() -> None:
    """Safety constants against numbers and words written out here (CLAUDE.md)."""
    assert {s.value for s in guards.UNSENDABLE_EMAIL_STATUSES} == {"bounced", "invalid"}
    assert {s.value for s in guards.LIVE_ENROLLMENT_STATUSES} == {"pending", "active", "paused"}
    assert {s.value for s in guards.RUNNING_CAMPAIGN_STATUSES} == {
        "reviewing",
        "active",
        "paused",
    }
    assert [r.value for r in guards.REASON_ORDER] == [
        "unknown_contact",
        "merged",
        "archived",
        "needs_review",
        "do_not_contact",
        "disconnected",
        "unknown_channel",
        "no_email",
        "email_bounced",
        "email_invalid",
        "no_linkedin",
        "in_another_campaign",
        "contacted_recently",
    ]


def test_every_guard_runs() -> None:
    assert [guard.__name__ for guard in GUARDS] == [
        "not_merged",
        "not_archived",
        "not_waiting_for_review",
        "not_do_not_contact",
        "not_disconnected",
        "has_channel_address",
        "not_in_another_campaign",
        "not_contacted_recently",
    ]


# --- one test per guard ------------------------------------------------------------------


def test_an_eligible_contact_passes_on_both_channels() -> None:
    assert reasons(facts(), EMAIL) == ()
    assert reasons(facts(), LINKEDIN) == ()
    verdict = check_contact(facts(), 1, EMAIL, POLICY, now=NOW)
    assert verdict.eligible and verdict.reason is None


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("merged", Reason.MERGED),
        ("archived", Reason.ARCHIVED),
        ("needs_review", Reason.NEEDS_REVIEW),
        ("do_not_contact", Reason.DO_NOT_CONTACT),
        ("disconnected", Reason.DISCONNECTED),
    ],
)
def test_each_contact_state_guard_excludes_on_every_channel(field: str, reason: Reason) -> None:
    for channel in (EMAIL, LINKEDIN):
        assert reasons(facts(**{field: True}), channel) == (reason,)


def test_an_email_step_needs_an_address() -> None:
    assert reasons(facts(email_statuses=(), sendable_email=None)) == (Reason.NO_EMAIL,)


def test_an_email_step_skips_a_bounced_or_invalid_address_for_a_good_one() -> None:
    """Facts carry the first sendable address; a bounced primary with a good second passes."""
    statuses = (EmailStatus.BOUNCED, EmailStatus.INVALID, EmailStatus.OK)
    assert reasons(facts(email_statuses=statuses, sendable_email="b@example.test")) == ()


@pytest.mark.parametrize(
    ("statuses", "reason"),
    [
        ((EmailStatus.BOUNCED,), Reason.EMAIL_BOUNCED),
        ((EmailStatus.INVALID,), Reason.EMAIL_INVALID),
        ((EmailStatus.INVALID, EmailStatus.BOUNCED), Reason.EMAIL_BOUNCED),
    ],
)
def test_an_email_step_with_no_sendable_address_says_why(
    statuses: tuple[EmailStatus, ...], reason: Reason
) -> None:
    assert reasons(facts(email_statuses=statuses, sendable_email=None)) == (reason,)


def test_a_bounce_leaves_the_contact_eligible_for_linkedin() -> None:
    """Spec 11.5: only the channel being sent on is checked."""
    bounced = facts(email_statuses=(EmailStatus.BOUNCED,), sendable_email=None)
    assert reasons(bounced, LINKEDIN) == ()
    assert reasons(facts(has_linkedin=False), EMAIL) == ()


def test_a_linkedin_step_needs_a_profile() -> None:
    assert reasons(facts(has_linkedin=False), LINKEDIN) == (Reason.NO_LINKEDIN,)


def test_a_channel_with_no_guard_excludes() -> None:
    """A channel added later without an address guard must send nothing, not everything."""
    fax = cast("TemplateChannel", "fax")
    assert reasons(facts(), fax) == (Reason.UNKNOWN_CHANNEL,)


def test_another_running_campaign_excludes_unless_allowed() -> None:
    busy = facts(other_campaigns=frozenset({7}))
    assert reasons(busy) == (Reason.IN_ANOTHER_CAMPAIGN,)
    allowing = GuardPolicy(contacted_within_days=30, allow_other_campaigns=True)
    assert reasons(busy, policy=allowing) == ()


@pytest.mark.parametrize(
    ("ago", "recent"),
    [
        (timedelta(days=30), False),  # exactly the window: outside it
        (timedelta(days=30) - timedelta(seconds=1), True),
        (timedelta(days=1), True),
        (timedelta(days=31), False),
        (-timedelta(hours=1), True),  # a time in the future counts
    ],
)
def test_contact_within_the_window_excludes(ago: timedelta, recent: bool) -> None:
    got = reasons(facts(last_outbound_at=NOW - ago))
    assert got == ((Reason.CONTACTED_RECENTLY,) if recent else ())


def test_a_zero_window_turns_the_recency_guard_off() -> None:
    off = GuardPolicy(contacted_within_days=0)
    assert reasons(facts(last_outbound_at=NOW), policy=off) == ()


def test_a_contact_nobody_found_is_excluded() -> None:
    """Safety: a guard that cannot decide excludes."""
    assert reasons(None) == (Reason.UNKNOWN_CONTACT,)


def test_every_reason_is_listed_in_order_and_the_first_is_the_reason() -> None:
    everything = facts(
        merged=True,
        do_not_contact=True,
        email_statuses=(),
        sendable_email=None,
        other_campaigns=frozenset({2}),
        last_outbound_at=NOW,
    )
    verdict = check_contact(everything, 1, EMAIL, POLICY, now=NOW)
    assert verdict.reasons == (
        Reason.MERGED,
        Reason.DO_NOT_CONTACT,
        Reason.NO_EMAIL,
        Reason.IN_ANOTHER_CAMPAIGN,
        Reason.CONTACTED_RECENTLY,
    )
    assert verdict.reason is Reason.MERGED and not verdict.eligible


def test_now_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        check_contact(facts(), 1, EMAIL, POLICY, now=NOW.replace(tzinfo=None))


# --- the channel guard -------------------------------------------------------------------

HEALTHY = ChannelState(
    mailbox_ok=True,
    mailbox_sent_today=79,
    mailbox_daily_cap=80,
    browser_ok=True,
    browser_budget_left=1,
)


def test_a_healthy_channel_under_its_cap_may_send() -> None:
    assert check_channel(EMAIL, HEALTHY) == ()
    assert check_channel(LINKEDIN, HEALTHY) == ()


@pytest.mark.parametrize(
    ("channel", "changes", "expected"),
    [
        (EMAIL, {"mailbox_ok": False}, (ChannelReason.MAILBOX_UNHEALTHY,)),
        (EMAIL, {"mailbox_sent_today": 80}, (ChannelReason.MAILBOX_AT_CAP,)),
        (
            EMAIL,
            {"mailbox_ok": False, "mailbox_sent_today": 81},
            (ChannelReason.MAILBOX_UNHEALTHY, ChannelReason.MAILBOX_AT_CAP),
        ),
        (EMAIL, {"mailbox_ok": None}, (ChannelReason.MAILBOX_UNKNOWN,)),
        (EMAIL, {"mailbox_sent_today": None}, (ChannelReason.MAILBOX_UNKNOWN,)),
        (EMAIL, {"mailbox_daily_cap": None}, (ChannelReason.MAILBOX_UNKNOWN,)),
        (LINKEDIN, {"browser_ok": False}, (ChannelReason.BROWSER_UNHEALTHY,)),
        (LINKEDIN, {"browser_budget_left": 0}, (ChannelReason.BROWSER_OUT_OF_BUDGET,)),
        (LINKEDIN, {"browser_ok": None}, (ChannelReason.BROWSER_UNKNOWN,)),
        (LINKEDIN, {"browser_budget_left": None}, (ChannelReason.BROWSER_UNKNOWN,)),
        # Each channel reads only its own side.
        (EMAIL, {"browser_ok": False}, ()),
        (LINKEDIN, {"mailbox_ok": False}, ()),
    ],
)
def test_each_channel_guard_excludes(
    channel: TemplateChannel, changes: dict[str, Any], expected: tuple[ChannelReason, ...]
) -> None:
    assert check_channel(channel, dataclasses.replace(HEALTHY, **changes)) == expected


def test_nothing_known_about_a_channel_excludes() -> None:
    assert check_channel(EMAIL, ChannelState()) == (ChannelReason.MAILBOX_UNKNOWN,)
    assert check_channel(LINKEDIN, ChannelState()) == (ChannelReason.BROWSER_UNKNOWN,)
    fax = cast("TemplateChannel", "fax")
    assert check_channel(fax, HEALTHY) == (ChannelReason.UNKNOWN_CHANNEL,)


# --- the summary -------------------------------------------------------------------------


def _verdicts(*groups: tuple[int, Reason | None]) -> list[Verdict]:
    out: list[Verdict] = []
    for count, reason in groups:
        out += [Verdict(len(out) + i, () if reason is None else (reason,)) for i in range(count)]
    return out


def test_the_summary_is_the_spec_example() -> None:
    """Spec 11.8's line, generated from the reasons."""
    verdicts = _verdicts(
        (175, None),
        (3, Reason.CONTACTED_RECENTLY),
        (30, Reason.NO_EMAIL),
        (4, Reason.DO_NOT_CONTACT),
    )
    assert excluded_summary(verdicts, contacted_within_days=30) == (
        "212 in audience, 37 excluded: 30 no email, 4 do-not-contact,"
        " 3 contacted in the last 30 days"
    )


def test_the_summary_counts_each_contact_once_under_its_first_reason() -> None:
    both = Verdict(1, (Reason.DO_NOT_CONTACT, Reason.NO_EMAIL))
    assert excluded_summary([both, Verdict(2, ())], contacted_within_days=30) == (
        "2 in audience, 1 excluded: 1 do-not-contact"
    )


def test_the_summary_breaks_a_tie_in_reason_order() -> None:
    verdicts = _verdicts((2, Reason.NO_LINKEDIN), (2, Reason.ARCHIVED), (1, Reason.MERGED))
    assert excluded_summary(verdicts, contacted_within_days=30) == (
        "5 in audience, 5 excluded: 2 archived, 2 no LinkedIn profile,"
        " 1 merged into another contact"
    )


def test_the_summary_with_nobody_excluded_and_nobody_at_all() -> None:
    assert excluded_summary(_verdicts((3, None)), contacted_within_days=30) == (
        "3 in audience, none excluded"
    )
    assert excluded_summary([], contacted_within_days=30) == "0 in audience, none excluded"


def test_every_reason_has_its_label() -> None:
    """The words on the review screen, written out (spec 11.8)."""
    labels = {r.value: guards.reason_label(r, contacted_within_days=1) for r in Reason}
    assert labels == {
        "unknown_contact": "not found",
        "merged": "merged into another contact",
        "archived": "archived",
        "needs_review": "waiting for review",
        "do_not_contact": "do-not-contact",
        "disconnected": "disconnected",
        "unknown_channel": "no guard for the channel",
        "no_email": "no email",
        "email_bounced": "bounced email",
        "email_invalid": "invalid email",
        "no_linkedin": "no LinkedIn profile",
        "in_another_campaign": "in another campaign",
        "contacted_recently": "contacted in the last 1 day",
    }


# --- reading the facts from the database ------------------------------------------------


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def campaign(writer: Session, user: User) -> Campaign:
    """This campaign: email, then LinkedIn."""
    return factories.make_campaign(writer, user, channels=(EMAIL, LINKEDIN))


def _contact(session: Session, user: User, **overrides: Any) -> Contact:
    return factories.make_contact(session, user, emails=["ada@example.test"], **overrides)


def test_facts_read_the_contact_as_it_is(writer: Session, user: User, campaign: Campaign) -> None:
    contact = factories.make_contact(
        writer, user, emails=["a@example.test", "b@example.test", "c@example.test"]
    )
    contact.emails[0].status = EmailStatus.BOUNCED
    contact.emails[1].status = EmailStatus.INVALID
    contact.do_not_contact = True
    contact.archived_at = contact.needs_review_at = contact.li_disconnected_at = NOW
    writer.flush()
    got = load_facts(writer, user, [contact.id], campaign_id=campaign.id)[contact.id]
    assert got == ContactFacts(
        contact_id=contact.id,
        merged=False,
        archived=True,
        needs_review=True,
        do_not_contact=True,
        disconnected=True,
        email_statuses=(EmailStatus.BOUNCED, EmailStatus.INVALID, EmailStatus.OK),
        sendable_email="c@example.test",
        has_linkedin=True,
        other_campaigns=frozenset(),
        last_outbound_at=None,
    )


def test_facts_see_a_merge_and_a_contact_with_no_linkedin(
    writer: Session, user: User, campaign: Campaign
) -> None:
    survivor = _contact(writer, user)
    loser = _contact(writer, user, li_urn=None, li_public_id=None)
    loser.merged_into_id = survivor.id
    writer.flush()
    got = load_facts(writer, user, [loser.id], campaign_id=campaign.id)[loser.id]
    assert got.merged and not got.has_linkedin


def test_another_users_contact_is_not_found_and_so_excluded(
    writer: Session, user: User, campaign: Campaign
) -> None:
    stranger = factories.make_user(writer)
    theirs = _contact(writer, stranger)
    assert load_facts(writer, user, [theirs.id], campaign_id=campaign.id) == {}
    [verdict] = check_enrollment(writer, user, campaign, [theirs.id], now=NOW)
    assert verdict.reasons == (Reason.UNKNOWN_CONTACT,)


@pytest.mark.parametrize(
    ("campaign_status", "enrollment_status", "counts"),
    [
        (CampaignStatus.ACTIVE, EnrollmentStatus.ACTIVE, True),
        (CampaignStatus.REVIEWING, EnrollmentStatus.PENDING, True),
        (CampaignStatus.PAUSED, EnrollmentStatus.PAUSED, True),
        (CampaignStatus.DRAFT, EnrollmentStatus.PENDING, False),  # not sending yet
        (CampaignStatus.COMPLETED, EnrollmentStatus.COMPLETED, False),
        (CampaignStatus.ACTIVE, EnrollmentStatus.REPLIED, False),  # done with them
        (CampaignStatus.ACTIVE, EnrollmentStatus.REMOVED, False),
    ],
)
def test_another_campaign_counts_while_it_has_steps_to_send(
    writer: Session,
    user: User,
    campaign: Campaign,
    campaign_status: CampaignStatus,
    enrollment_status: EnrollmentStatus,
    counts: bool,
) -> None:
    contact = _contact(writer, user)
    other = factories.make_campaign(writer, user, status=campaign_status)
    factories.make_enrollment(writer, other, contact, status=enrollment_status)
    [verdict] = check_enrollment(writer, user, campaign, [contact.id], now=NOW)
    assert verdict.reasons == ((Reason.IN_ANOTHER_CAMPAIGN,) if counts else ())


def test_two_campaigns_never_exclude_each_other_both_at_once(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """At a step fire, only an enrollment that came first counts: the older one proceeds."""
    contact = _contact(writer, user)
    later = factories.make_campaign(writer, user)
    first = factories.make_enrollment(writer, campaign, contact)
    second = factories.make_enrollment(writer, later, contact)
    assert check_step(writer, user, first, campaign.steps[0], now=NOW).eligible
    blocked = check_step(writer, user, second, later.steps[0], now=NOW)
    assert blocked.reasons == (Reason.IN_ANOTHER_CAMPAIGN,)


def test_its_own_enrollment_is_not_another_campaign(
    writer: Session, user: User, campaign: Campaign
) -> None:
    contact = _contact(writer, user)
    enrollment = factories.make_enrollment(writer, campaign, contact)
    assert check_step(writer, user, enrollment, campaign.steps[0], now=NOW).eligible


@pytest.mark.parametrize(
    ("kind", "counts"),
    [
        (InteractionKind.EMAIL_OUT, True),
        (InteractionKind.LI_OUT, True),
        (InteractionKind.CALL, True),
        (InteractionKind.MEETING, True),
        (InteractionKind.EMAIL_IN, False),  # them reaching you
        (InteractionKind.NOTE, False),
        (InteractionKind.LI_VIEW, False),
    ],
)
def test_recent_outbound_contact_of_any_kind_excludes(
    writer: Session, user: User, campaign: Campaign, kind: InteractionKind, counts: bool
) -> None:
    contact = _contact(writer, user)
    add_interaction(writer, user, contact.id, kind, NOW - timedelta(days=3))
    [verdict] = check_enrollment(writer, user, campaign, [contact.id], now=NOW)
    assert verdict.reasons == ((Reason.CONTACTED_RECENTLY,) if counts else ())


def test_old_contact_is_outside_the_campaigns_own_window(writer: Session, user: User) -> None:
    short = factories.make_campaign(writer, user, contacted_within_days_guard=7)
    contact = _contact(writer, user)
    add_interaction(writer, user, contact.id, InteractionKind.EMAIL_OUT, NOW - timedelta(days=10))
    [verdict] = check_enrollment(writer, user, short, [contact.id], now=NOW)
    assert verdict.eligible


def test_the_next_step_of_the_same_campaign_is_not_recent_contact(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """Spec 11.9: "unless the message is the next step of this same campaign"."""
    contact = _contact(writer, user)
    enrollment = factories.make_enrollment(writer, campaign, contact, current_step=1)
    step_one = factories.make_message(writer, enrollment, sent_at=NOW - timedelta(days=7))
    add_interaction(
        writer, user, contact.id, InteractionKind.EMAIL_OUT, step_one.sent_at or NOW, None,
        step_one.id,
    )  # fmt: skip
    step_two = campaign.steps[1]
    assert check_step(writer, user, enrollment, step_two, now=NOW).eligible


def test_another_campaigns_message_is_recent_contact_even_without_an_interaction(
    writer: Session, user: User, campaign: Campaign
) -> None:
    contact = _contact(writer, user)
    other = factories.make_campaign(writer, user, status=CampaignStatus.COMPLETED)
    theirs = factories.make_enrollment(writer, other, contact, status=EnrollmentStatus.COMPLETED)
    factories.make_message(writer, theirs, sent_at=NOW - timedelta(days=2))
    [verdict] = check_enrollment(writer, user, campaign, [contact.id], now=NOW)
    assert verdict.reasons == (Reason.CONTACTED_RECENTLY,)


def test_another_campaigns_message_interaction_is_recent_contact(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """An interaction linked to another campaign's message counts, however old the message row."""
    contact = _contact(writer, user)
    ours = factories.make_enrollment(writer, campaign, contact, current_step=1)
    other = factories.make_campaign(writer, user, status=CampaignStatus.COMPLETED)
    theirs = factories.make_enrollment(writer, other, contact, status=EnrollmentStatus.COMPLETED)
    message = factories.make_message(writer, theirs, sent_at=NOW - timedelta(days=90))
    add_interaction(
        writer, user, contact.id, InteractionKind.EMAIL_OUT, NOW - timedelta(days=1), None,
        message.id,
    )  # fmt: skip
    verdict = check_step(writer, user, ours, campaign.steps[1], now=NOW)
    assert verdict.reasons == (Reason.CONTACTED_RECENTLY,)


def test_a_state_change_after_enrollment_is_caught_at_the_step(
    writer: Session, user: User, campaign: Campaign
) -> None:
    """Spec 11.9 checks again at every step fire, because state changes in between."""
    contact = _contact(writer, user)
    [at_enrollment] = check_enrollment(writer, user, campaign, [contact.id], now=NOW)
    assert at_enrollment.eligible
    enrollment = factories.make_enrollment(writer, campaign, contact)
    contact.do_not_contact = True
    writer.flush()
    verdict = check_step(writer, user, enrollment, campaign.steps[0], now=NOW)
    assert verdict.reasons == (Reason.DO_NOT_CONTACT,)


def test_enrollment_checks_the_first_steps_channel(writer: Session, user: User) -> None:
    """A LinkedIn-first campaign enrolls a contact with no email; an email-first one does not."""
    no_email = factories.make_contact(writer, user)
    linkedin_first = factories.make_campaign(writer, user, channels=(LINKEDIN, EMAIL))
    email_first = factories.make_campaign(writer, user, channels=(EMAIL, LINKEDIN))
    [li] = check_enrollment(writer, user, linkedin_first, [no_email.id], now=NOW)
    [em] = check_enrollment(writer, user, email_first, [no_email.id], now=NOW)
    assert li.eligible
    assert em.reasons == (Reason.NO_EMAIL,)


def test_enrollment_verdicts_come_in_id_order_with_the_summary(
    writer: Session, user: User, campaign: Campaign
) -> None:
    good = _contact(writer, user)
    no_email = factories.make_contact(writer, user)
    blocked = _contact(writer, user, do_not_contact=True)
    verdicts = check_enrollment(
        writer, user, campaign, [blocked.id, good.id, no_email.id, good.id], now=NOW
    )
    assert [v.contact_id for v in verdicts] == sorted([good.id, no_email.id, blocked.id])
    assert excluded_summary(verdicts, contacted_within_days=30) == (
        "3 in audience, 2 excluded: 1 do-not-contact, 1 no email"
    )


def test_a_campaign_with_no_steps_enrolls_nobody(writer: Session, user: User) -> None:
    empty = factories.make_campaign(writer, user, channels=())
    contact = _contact(writer, user)
    [verdict] = check_enrollment(writer, user, empty, [contact.id], now=NOW)
    assert verdict.reasons == (Reason.UNKNOWN_CHANNEL,)


def test_the_checks_refuse_another_users_campaign_and_mismatched_steps(
    writer: Session, user: User, campaign: Campaign
) -> None:
    stranger = factories.make_user(writer)
    theirs = factories.make_campaign(writer, stranger)
    with pytest.raises(ValueError, match="own user"):
        check_enrollment(writer, user, theirs, [], now=NOW)
    contact = _contact(writer, user)
    enrollment = factories.make_enrollment(writer, campaign, contact)
    unrelated = factories.make_campaign(writer, user)
    with pytest.raises(ValueError, match="not one of"):
        check_step(writer, user, enrollment, unrelated.steps[0], now=NOW)


def test_the_guards_only_read(writer: Session, user: User, campaign: Campaign) -> None:
    contact = _contact(writer, user)
    enrollment = factories.make_enrollment(writer, campaign, contact)
    writer.flush()
    check_enrollment(writer, user, campaign, [contact.id], now=NOW)
    check_step(writer, user, enrollment, campaign.steps[0], now=NOW)
    assert not writer.dirty and not writer.new and not writer.deleted


def test_a_bounced_address_row_is_what_the_facts_skip(writer: Session, user: User) -> None:
    """The guards reuse crm.contacts.sendable_email, refusing invalid as well as bounced."""
    contact = factories.make_contact(writer, user, emails=["x@example.test"])
    row: ContactEmail = contact.emails[0]
    row.status = EmailStatus.INVALID
    writer.flush()
    got = load_facts(writer, user, [contact.id], campaign_id=None)[contact.id]
    assert got.sendable_email is None
    assert reasons(got) == (Reason.EMAIL_INVALID,)
