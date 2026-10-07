"""Overriding the recent-contact guard for chosen contacts at enrollment (#446).

The override sets aside :func:`~netkeeper.services.campaign_guards.not_contacted_recently`
for exactly the contacts a person names, and for contact made before the override only.
Every other guard still decides, and every contact not named is still skipped. The web
and CLI ends are in ``test_web_campaigns.py`` and ``test_cli_campaigns.py``.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.crm import do_not_send
from netkeeper.crm import lists as list_service
from netkeeper.crm.contacts import merge_contacts
from netkeeper.crm.interactions import add_interaction
from netkeeper.crm.self_contact import ensure_self_contact
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    Contact,
    ContactEmail,
    DoNotSendReason,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    InteractionKind,
    ListKind,
    TemplateChannel,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services import campaign_engine, campaign_review
from netkeeper.services import campaigns as campaign_service
from netkeeper.services.campaign_guards import (
    ContactFacts,
    GuardPolicy,
    Reason,
    check_contact,
    check_enrollment,
    check_step,
    last_contact,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
POLICY = GuardPolicy(contacted_within_days=30)
RECENT = NOW - timedelta(days=5)
_addresses = itertools.count(1)


def facts(**changes: Any) -> ContactFacts:
    """A contact every guard but the recent-contact one lets through, with ``changes``."""
    base = ContactFacts(
        contact_id=1,
        merged=False,
        archived=False,
        is_self=False,
        needs_review=False,
        do_not_contact=False,
        disconnected=False,
        email_statuses=(EmailStatus.OK,),
        sendable_email="ada@example.test",
        has_linkedin=True,
        other_campaigns=frozenset(),
        last_outbound_at=RECENT,
        address_bounced_elsewhere=False,
        duplicate_address=False,
        do_not_send=None,
    )
    return dataclasses.replace(base, **changes)


def reasons(subject: ContactFacts, channel: TemplateChannel = EMAIL) -> tuple[Reason, ...]:
    return check_contact(subject, 1, channel, POLICY, now=NOW).reasons


# --- the guard itself ------------------------------------------------------------------


def test_without_an_override_recent_contact_excludes() -> None:
    assert reasons(facts()) == (Reason.CONTACTED_RECENTLY,)


def test_an_override_sets_aside_contact_at_or_before_it() -> None:
    assert reasons(facts(recent_contact_override_at=RECENT)) == ()
    assert reasons(facts(recent_contact_override_at=NOW)) == ()


def test_contact_after_the_override_counts_again() -> None:
    later = facts(last_outbound_at=NOW - timedelta(days=1), recent_contact_override_at=RECENT)
    assert reasons(later) == (Reason.CONTACTED_RECENTLY,)


# Every other reason a contact can have, as the facts that give it, and the channel.
NEVER_OVERRIDDEN: list[tuple[dict[str, Any], TemplateChannel, Reason]] = [
    ({"is_self": True}, EMAIL, Reason.SELF),
    ({"merged": True}, EMAIL, Reason.MERGED),
    ({"archived": True}, EMAIL, Reason.ARCHIVED),
    ({"needs_review": True}, EMAIL, Reason.NEEDS_REVIEW),
    ({"do_not_contact": True}, EMAIL, Reason.DO_NOT_CONTACT),
    ({"disconnected": True}, EMAIL, Reason.DISCONNECTED),
    ({"sendable_email": None, "email_statuses": ()}, EMAIL, Reason.NO_EMAIL),
    (
        {"sendable_email": None, "email_statuses": (EmailStatus.BOUNCED,)},
        EMAIL,
        Reason.EMAIL_BOUNCED,
    ),
    (
        {"sendable_email": None, "email_statuses": (EmailStatus.INVALID,)},
        EMAIL,
        Reason.EMAIL_INVALID,
    ),
    ({"do_not_send": DoNotSendReason.OPTED_OUT}, EMAIL, Reason.DO_NOT_SEND),
    ({"do_not_send": DoNotSendReason.OPTED_OUT}, LINKEDIN, Reason.DO_NOT_SEND),
    ({"address_bounced_elsewhere": True}, EMAIL, Reason.ADDRESS_BOUNCED_ELSEWHERE),
    ({"has_linkedin": False}, LINKEDIN, Reason.NO_LINKEDIN),
    ({"duplicate_address": True}, EMAIL, Reason.DUPLICATE_ADDRESS),
    ({"other_campaigns": frozenset({9})}, EMAIL, Reason.IN_ANOTHER_CAMPAIGN),
]


def test_every_contact_reason_but_recent_contact_is_listed_here() -> None:
    """A guard added later must be added to ``NEVER_OVERRIDDEN`` too."""
    step_fire = {Reason.CAMPAIGN_NOT_ACTIVE, Reason.ENROLLMENT_NOT_ACTIVE}
    unreachable = {Reason.UNKNOWN_CONTACT, Reason.UNKNOWN_CHANNEL, Reason.CONTACTED_RECENTLY}
    assert {r for _, _, r in NEVER_OVERRIDDEN} == set(Reason) - step_fire - unreachable


@pytest.mark.parametrize(("changes", "channel", "reason"), NEVER_OVERRIDDEN)
def test_an_override_never_sets_aside_another_guard(
    changes: dict[str, Any], channel: TemplateChannel, reason: Reason
) -> None:
    overridden = facts(recent_contact_override_at=NOW, **changes)
    assert reasons(overridden, channel) == (reason,)


def test_an_unknown_contact_stays_excluded() -> None:
    assert check_contact(None, 1, EMAIL, POLICY, now=NOW).reasons == (Reason.UNKNOWN_CONTACT,)


# --- against the database ----------------------------------------------------------------


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _draft(session: Session, user: User, *channels: TemplateChannel) -> Campaign:
    return factories.make_campaign(
        session, user, channels=channels or (EMAIL,), status=CampaignStatus.DRAFT
    )


def _contacted(session: Session, user: User, at: datetime = RECENT, **overrides: Any) -> Contact:
    overrides.setdefault("emails", [f"c{next(_addresses)}@example.test"])
    contact = factories.make_contact(session, user, **overrides)
    add_interaction(session, user, contact.id, InteractionKind.EMAIL_OUT, at)
    return contact


def _enrollment(session: Session, user: User, campaign: Campaign, contact: Contact) -> Enrollment:
    return session.scalars(
        scoped(user, Enrollment).where(
            Enrollment.campaign_id == campaign.id, Enrollment.contact_id == contact.id
        )
    ).one()


def test_only_the_named_contacts_are_overridden(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    named, unnamed = _contacted(writer, user), _contacted(writer, user)
    fresh = factories.make_contact(writer, user, emails=["fresh@example.test"])

    verdicts = {
        v.contact_id: v
        for v in check_enrollment(
            writer,
            user,
            campaign,
            [named.id, unnamed.id, fresh.id],
            now=NOW,
            override_recent_contact=[named.id, fresh.id],
        )
    }

    assert (verdicts[named.id].reasons, verdicts[named.id].overridden) == ((), True)
    assert verdicts[unnamed.id].reasons == (Reason.CONTACTED_RECENTLY,)
    assert not verdicts[unnamed.id].overridden
    # Named, but the guard passed it anyway: nothing was overridden.
    assert (verdicts[fresh.id].reasons, verdicts[fresh.id].overridden) == ((), False)


def test_contact_in_the_future_is_not_overridden(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    ahead = _contacted(writer, user, at=NOW + timedelta(hours=1))
    [verdict] = check_enrollment(
        writer, user, campaign, [ahead.id], now=NOW, override_recent_contact=[ahead.id]
    )
    assert verdict.reasons == (Reason.CONTACTED_RECENTLY,)


def _never(session: Session, user: User, campaign: Campaign) -> dict[str, Contact]:
    """Contacts contacted recently that another guard excludes as well, by what it is."""
    opted = _contacted(session, user, emails=["opted@example.test"])
    do_not_send.add(session, user, "opted@example.test", DoNotSendReason.OPTED_OUT)
    elsewhere = _contacted(session, user)
    other = factories.make_campaign(session, user)  # active
    factories.make_enrollment(session, other, elsewhere)
    you = ensure_self_contact(session, user)
    you.emails.append(ContactEmail(user_id=user.id, email="me@example.test", is_primary=True))
    survivor = _contacted(session, user)
    merged = _contacted(session, user)
    merge_contacts(session, user, survivor.id, merged.id)
    dnc = _contacted(session, user, do_not_contact=True)
    session.flush()
    return {
        "opted_out": opted,
        "in_another_campaign": elsewhere,
        "self": you,
        "merged": merged,
        "do_not_contact": dnc,
    }


def test_the_override_never_enrolls_a_contact_another_guard_excludes(
    writer: Session, user: User
) -> None:
    campaign = _draft(writer, user)
    never = _never(writer, user, campaign)
    ids = [c.id for c in never.values()]

    result = campaign_engine.enroll(
        writer, user, campaign.id, ids, now=NOW, override_recent_contact=ids
    )

    assert result.enrolled == () and result.overridden == ()
    got = {v.contact_id: v.reasons for v in result.verdicts}
    assert Reason.DO_NOT_SEND in got[never["opted_out"].id]
    assert Reason.IN_ANOTHER_CAMPAIGN in got[never["in_another_campaign"].id]
    assert Reason.SELF in got[never["self"].id]
    assert Reason.MERGED in got[never["merged"].id]
    assert Reason.DO_NOT_CONTACT in got[never["do_not_contact"].id]


def test_the_override_never_enrolls_a_contact_without_a_linkedin_member_id(
    writer: Session, user: User
) -> None:
    campaign = _draft(writer, user, LINKEDIN)
    slug_only = _contacted(writer, user, li_urn=None)

    result = campaign_engine.enroll(
        writer, user, campaign.id, [slug_only.id], now=NOW, override_recent_contact=[slug_only.id]
    )

    assert result.enrolled == ()
    [verdict] = result.verdicts
    assert verdict.reasons == (Reason.NO_LINKEDIN,)


def test_the_enrollment_records_who_overrode_and_when(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    named, plain = _contacted(writer, user), factories.make_contact(writer, user, emails=["p@x.t"])

    result = campaign_engine.enroll(
        writer, user, campaign.id, [named.id, plain.id], now=NOW, override_recent_contact=[named.id]
    )

    assert sorted(result.enrolled) == sorted([named.id, plain.id])
    assert result.overridden == (named.id,)
    overridden = _enrollment(writer, user, campaign, named)
    assert (overridden.recent_contact_override_at, overridden.recent_contact_override_by) == (
        NOW,
        user.id,
    )
    passed = _enrollment(writer, user, campaign, plain)
    assert (passed.recent_contact_override_at, passed.recent_contact_override_by) == (None, None)


def test_the_step_fire_keeps_the_override_for_earlier_contact_only(
    writer: Session, user: User
) -> None:
    campaign = _draft(writer, user)
    named = _contacted(writer, user)
    campaign_engine.enroll(
        writer, user, campaign.id, [named.id], now=NOW, override_recent_contact=[named.id]
    )
    enrollment = _enrollment(writer, user, campaign, named)
    campaign.status = CampaignStatus.ACTIVE
    enrollment.status = EnrollmentStatus.ACTIVE
    writer.flush()
    step = campaign.steps[0]

    assert check_step(writer, user, enrollment, step, now=NOW + timedelta(hours=1)).eligible

    add_interaction(writer, user, named.id, InteractionKind.CALL, NOW + timedelta(minutes=30))
    verdict = check_step(writer, user, enrollment, step, now=NOW + timedelta(hours=1))
    assert verdict.reasons == (Reason.CONTACTED_RECENTLY,)


def test_the_step_fire_still_runs_every_other_guard(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    named = _contacted(writer, user)
    campaign_engine.enroll(
        writer, user, campaign.id, [named.id], now=NOW, override_recent_contact=[named.id]
    )
    enrollment = _enrollment(writer, user, campaign, named)
    campaign.status = CampaignStatus.ACTIVE
    enrollment.status = EnrollmentStatus.ACTIVE
    named.do_not_contact = True
    writer.flush()

    verdict = check_step(writer, user, enrollment, campaign.steps[0], now=NOW)
    assert verdict.reasons == (Reason.DO_NOT_CONTACT,)


# --- the service and the review ----------------------------------------------------------


def _audience(session: Session, user: User, campaign: Campaign, contacts: list[Contact]) -> None:
    row = list_service.create_list(session, user, f"people {campaign.id}", ListKind.STATIC)
    list_service.add_members(session, user, row.id, [c.id for c in contacts])
    campaign.source_list_id = row.id
    session.flush()


def test_the_review_lists_who_the_recent_contact_guard_skips(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    recent = _contacted(writer, user)
    both = _contacted(writer, user, do_not_contact=True)
    by_linkedin = factories.make_contact(writer, user, emails=["li@example.test"])
    add_interaction(writer, user, by_linkedin.id, InteractionKind.LI_OUT, RECENT)
    add_interaction(writer, user, by_linkedin.id, InteractionKind.EMAIL_OUT, RECENT - timedelta(1))
    _audience(writer, user, campaign, [recent, both, by_linkedin])

    report = campaign_review.guard_report(writer, user, campaign, now=NOW)

    rows = {c.contact_id: c for c in report.skipped}
    assert rows[recent.id].overridable
    assert rows[recent.id].reason_codes == ("contacted_recently",)
    assert (rows[recent.id].last_contacted_at, rows[recent.id].last_contacted_channel) == (
        RECENT,
        "email",
    )
    assert rows[by_linkedin.id].last_contacted_channel == "linkedin"
    # Another guard skips it too: never overridable here.
    assert not rows[both.id].overridable
    assert rows[both.id].reason_codes == ("do_not_contact", "contacted_recently")


def test_an_overridden_enrollment_will_start_in_the_review(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    named, unnamed = _contacted(writer, user), _contacted(writer, user)
    _audience(writer, user, campaign, [named, unnamed])

    outcome = campaign_service.enroll(
        writer, user, campaign.id, now=NOW, override_recent_contact=[named.id], confirm=True
    )

    assert (outcome.enrolled, outcome.overridden, outcome.excluded) == (1, 1, 1)
    assert outcome.summary == "1 will start, 1 skipped (1 contacted in the last 30 days)"
    report = campaign_review.guard_report(writer, user, campaign, now=NOW)
    assert [c.contact_id for c in report.skipped] == [unnamed.id]


def test_the_service_needs_confirm(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    named = _contacted(writer, user)
    _audience(writer, user, campaign, [named])

    with pytest.raises(campaign_service.InvalidCampaign, match="needs confirm"):
        campaign_service.enroll(
            writer, user, campaign.id, now=NOW, override_recent_contact=[named.id]
        )
    assert writer.scalars(scoped(user, Enrollment)).all() == []


def test_the_service_refuses_a_new_source_with_an_override(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    named = _contacted(writer, user)
    _audience(writer, user, campaign, [named])

    with pytest.raises(campaign_service.InvalidCampaign, match="without changing the source"):
        campaign_service.enroll(
            writer,
            user,
            campaign.id,
            now=NOW,
            list_id=campaign.source_list_id,
            override_recent_contact=[named.id],
            confirm=True,
        )


def test_the_service_refuses_a_contact_outside_the_audience(writer: Session, user: User) -> None:
    campaign = _draft(writer, user)
    inside, outside = _contacted(writer, user), _contacted(writer, user)
    _audience(writer, user, campaign, [inside])

    with pytest.raises(campaign_service.InvalidCampaign, match=f"audience: {outside.id}"):
        campaign_service.enroll(
            writer,
            user,
            campaign.id,
            now=NOW,
            override_recent_contact=[inside.id, outside.id],
            confirm=True,
        )
    assert writer.scalars(scoped(user, Enrollment)).all() == []


def test_last_contact_counts_sent_campaign_messages(writer: Session, user: User) -> None:
    active = factories.make_campaign(writer, user, channels=(LINKEDIN,))
    contact = factories.make_contact(writer, user, emails=["m@example.test"])
    enrollment = factories.make_enrollment(writer, active, contact)
    factories.make_message(writer, enrollment, sent_at=RECENT)
    add_interaction(writer, user, contact.id, InteractionKind.CALL, RECENT - timedelta(days=1))

    found = last_contact(writer, user, [contact.id])[contact.id]
    assert (found.at, found.channel) == (RECENT, "linkedin")
