"""One approval per step, through the engine (spec 11.8; #339).

A step approval is given once and covers the step's messages, but a message that
is blocked when the step is reviewed (it renders with no subject, or a guard
excludes the contact) is never covered by it, and never sends once the campaign
is active.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import factories
import pytest
from campaign_fakes import ARMED_FOR_SEND, NOW, SETTINGS, FakeSender, make_mailbox
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.render import LintRule
from netkeeper.campaigns.templates import activation_errors
from netkeeper.crm import do_not_send
from netkeeper.crm import lists as list_service
from netkeeper.crm.self_contact import update_self_contact
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    EmailStatus,
    Enrollment,
    EnrollmentStatus,
    ListKind,
    Message,
    MessageStatus,
    StepMode,
    Template,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine, campaign_review
from netkeeper.services.campaign_engine import Decision, Skip, run_tick
from netkeeper.services.campaign_guards import Reason, check_step


@dataclass
class Reviewed:
    factory: sessionmaker[Session]
    user: User
    campaign_id: int
    step_id: int
    ok: list[int]
    no_subject: int
    excluded: int


def _reviewing(
    factory: sessionmaker[Session], *, subject: str, body: str, bob_elsewhere: bool = False
) -> Reviewed:
    """A one-step email campaign under review: two contacts whose message can be sent,
    one whose subject renders empty, and one a guard excludes. ``bob_elsewhere``: bob
    is first enrolled in a draft campaign, an enrollment older than this one's."""
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
        campaign = factories.make_campaign(
            session,
            user,
            channels=(TemplateChannel.EMAIL,),
            status=CampaignStatus.REVIEWING,
            mailbox_id=mailbox.id,
        )
        step = campaign.steps[0]
        step.mode = StepMode.SEND
        template = get_scoped(session, user, Template, step.template_id)
        assert template is not None
        template.subject, template.body = subject, body

        other = factories.make_campaign(session, user, status=CampaignStatus.DRAFT)

        def enroll(email: str, **contact: Any) -> int:
            row = factories.make_contact(session, user, emails=[email], **contact)
            if bob_elsewhere and email.startswith("bob@"):
                factories.make_enrollment(session, other, row, status=EnrollmentStatus.PENDING)
            return factories.make_enrollment(
                session, campaign, row, status=EnrollmentStatus.PENDING
            ).id

        ok = [enroll("ada@contacts.example"), enroll("bob@contacts.example")]
        no_subject = enroll("nameless@contacts.example", first_name="", preferred_name=None)
        excluded = enroll("dnc@contacts.example", do_not_contact=True)
        return Reviewed(factory, user, campaign.id, step.id, ok, no_subject, excluded)


def _review(r: Reviewed) -> campaign_review.StepReview:
    with session_scope(r.factory) as session:
        return campaign_review.review_step(session, r.user, r.campaign_id, r.step_id, now=NOW)


def _complete_and_activate(r: Reviewed, review: campaign_review.StepReview) -> None:
    with session_scope(r.factory, write=True) as session:
        user = r.user
        campaign_review.approve_step(
            session,
            user,
            r.campaign_id,
            r.step_id,
            fingerprint_seen=review.fingerprint,
            now=NOW,
        )
        update_self_contact(session, user, {"first_name": "Selfie"})  # the test renders it
        plan = campaign_review.prepare_test_send(
            session, user, r.campaign_id, r.step_id, today=NOW.date()
        )
        campaign_review.record_test_send(session, user, plan, gmail_message_id="fake", now=NOW)
        assert campaign_review.record_lint(session, user, r.campaign_id, now=NOW).clean
        campaign_review.activate(
            session, user, r.campaign_id, settings=SETTINGS, now=NOW, starts_at=NOW
        )


def _tick_until_quiet(r: Reviewed, sender: FakeSender) -> list[Decision]:
    """Tick until nothing more fires; every decision the ticks made."""
    decisions: list[Decision] = []
    for n in range(12):
        at = NOW + timedelta(minutes=1 + 10 * n)
        sender.now = at
        for result in run_tick(
            r.factory,
            settings=SETTINGS,
            sender=sender,
            clock=partial(_at, at),
            rng=random.Random(n),
        ):
            decisions.extend(result.decisions)
    return decisions


def _at(when: datetime) -> datetime:
    return when


def test_blocked_messages_are_listed_apart_and_never_sent(
    session_factory: sessionmaker[Session],
) -> None:
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")
    review = _review(r)
    assert [m.enrollment_id for m in review.messages] == r.ok
    blocked = {m.enrollment_id: m.blocked for m in review.blocked}
    assert blocked == {
        r.no_subject: "renders with no subject",
        r.excluded: "excluded by a guard: do-not-contact",
    }
    _complete_and_activate(r, review)
    assert all(m.approved for m in _review(r).messages)
    assert not any(m.approved for m in _review(r).blocked)

    sender = FakeSender()
    _tick_until_quiet(r, sender)

    sent_to = sorted(f.to_address or "" for f in sender.firings)
    assert sent_to == ["ada@contacts.example", "bob@contacts.example"]
    with session_scope(r.factory) as session:
        for enrollment_id in (r.no_subject, r.excluded):
            assert not list(
                session.scalars(
                    scoped(r.user, Message).where(
                        Message.enrollment_id == enrollment_id,
                        Message.status == MessageStatus.SENT,
                    )
                )
            )
            enrollment = get_scoped(session, r.user, Enrollment, enrollment_id)
            assert enrollment is not None and enrollment.current_step is None


def _exclude_bob(
    session: Session, user: User, campaign_id: int, contact: Contact, how: Reason
) -> None:
    """Make a guard exclude bob, after activation, for ``how``."""
    if how is Reason.DO_NOT_CONTACT:
        contact.do_not_contact = True
    elif how is Reason.NO_EMAIL:
        contact.emails.clear()
    elif how is Reason.EMAIL_INVALID:
        contact.emails[0].status = EmailStatus.INVALID
    elif how is Reason.NEEDS_REVIEW:
        contact.needs_review_at = NOW
    elif how is Reason.ARCHIVED:
        contact.archived_at = NOW
    elif how is Reason.MERGED:
        contact.merged_into_id = factories.make_contact(session, user).id
    elif how is Reason.DO_NOT_SEND:
        do_not_send.add_by_hand(session, user, "bob@contacts.example")
    elif how is Reason.ADDRESS_BOUNCED_ELSEWHERE:
        twin = factories.make_contact(session, user, emails=["bob@contacts.example"])
        twin.emails[0].status = EmailStatus.BOUNCED
    elif how is Reason.IN_ANOTHER_CAMPAIGN:
        # Bob's older enrollment, in a campaign that was a draft, now runs.
        for campaign in session.scalars(scoped(user, Campaign).where(Campaign.id != campaign_id)):
            campaign.status = CampaignStatus.REVIEWING
    else:
        raise AssertionError(how)
    session.flush()


@pytest.mark.parametrize(
    "how",
    [
        Reason.DO_NOT_CONTACT,
        Reason.NO_EMAIL,
        Reason.EMAIL_INVALID,
        Reason.NEEDS_REVIEW,
        Reason.DO_NOT_SEND,
        Reason.IN_ANOTHER_CAMPAIGN,
        Reason.ARCHIVED,
        Reason.MERGED,
        Reason.ADDRESS_BOUNCED_ELSEWHERE,
    ],
    ids=lambda r: r.value,
)
def test_a_contact_a_guard_excludes_after_activation_is_never_sent(
    session_factory: sessionmaker[Session], how: Reason
) -> None:
    """#346: activation needs no guard acknowledgement, and the guards still apply when
    the step fires: a contact a guard excludes only after the review, the approval and
    activation is sent nothing, whatever the guard. The campaign is one email step, so
    each address guard is the one deciding, and the tick's decision names it."""
    r = _reviewing(
        session_factory,
        subject="{{ first_name }}",
        body="Hi {{ first_name }}",
        bob_elsewhere=how is Reason.IN_ANOTHER_CAMPAIGN,
    )
    with session_scope(r.factory) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        assert campaign_review.guard_summary(session, r.user, campaign, now=NOW) == (
            "3 will start, 1 skipped (1 do-not-contact)"
        )
    _complete_and_activate(r, _review(r))
    with session_scope(r.factory, write=True) as session:
        enrollment = get_scoped(session, r.user, Enrollment, r.ok[1])
        assert enrollment is not None
        contact = get_scoped(session, r.user, Contact, enrollment.contact_id)
        assert contact is not None
        _exclude_bob(session, r.user, r.campaign_id, contact, how)
        step = get_scoped(session, r.user, CampaignStep, r.step_id)
        assert step is not None
        assert step.channel is TemplateChannel.EMAIL
        assert check_step(session, r.user, enrollment, step, now=NOW).reasons == (how,)

    sender = FakeSender()
    decisions = _tick_until_quiet(r, sender)

    skipped = [d for d in decisions if d.enrollment_id == r.ok[1]]
    assert skipped and all(not d.fired for d in skipped)
    # Other decisions only wait for the mailbox's spacing; the guard's names its reason,
    # and that reason alone.
    [guarded] = [d.reasons for d in skipped if d.reasons != (Skip.SPACING,)]
    assert guarded in {(Skip.GUARD_EXCLUDED, how.value), (Skip.ENDED, how.value)}

    assert sorted(f.to_address or "" for f in sender.firings) == ["ada@contacts.example"]
    with session_scope(r.factory) as session:
        assert not list(
            session.scalars(
                scoped(r.user, Message).where(
                    Message.enrollment_id == r.ok[1], Message.status == MessageStatus.SENT
                )
            )
        )


def test_a_contact_pending_in_two_campaigns_starts_only_in_the_older_enrollment(
    session_factory: sessionmaker[Session],
) -> None:
    """#346 review: another campaign counts only for an older enrollment, in the summary
    as in check_step. The older campaign counts the contact as starting; the newer one
    skips it as in another campaign."""
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session)
        mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
        x = factories.make_contact(session, user, emails=["x@contacts.example"])
        older, newer = (
            factories.make_campaign(
                session,
                user,
                channels=(TemplateChannel.EMAIL,),
                status=CampaignStatus.REVIEWING,
                mailbox_id=mailbox.id,
            )
            for _ in range(2)
        )
        in_older = factories.make_enrollment(session, older, x, status=EnrollmentStatus.PENDING)
        in_newer = factories.make_enrollment(session, newer, x, status=EnrollmentStatus.PENDING)
        assert in_older.id < in_newer.id

        def report(campaign: Campaign) -> campaign_review.GuardReport:
            return campaign_review.guard_report(session, user, campaign, now=NOW)

        def at_fire(enrollment: Enrollment, campaign: Campaign) -> set[Reason]:
            verdict = check_step(session, user, enrollment, campaign.steps[0], now=NOW)
            # Pending and reviewing: what check_step adds for not being live yet.
            return set(verdict.reasons) - {
                Reason.ENROLLMENT_NOT_ACTIVE,
                Reason.CAMPAIGN_NOT_ACTIVE,
            }

        first, second = report(older), report(newer)
        assert (first.summary, first.will_start, first.skipped) == (
            "1 will start, none skipped",
            1,
            (),
        )
        assert at_fire(in_older, older) == set()
        assert second.summary == "0 will start, 1 skipped (1 in another campaign)"
        assert [(c.contact_id, c.reasons) for c in second.skipped] == [
            (x.id, ("in another campaign",))
        ]
        assert at_fire(in_newer, newer) == {Reason.IN_ANOTHER_CAMPAIGN}


def _summary(r: Reviewed) -> campaign_review.GuardReport:
    with session_scope(r.factory) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        return campaign_review.guard_report(session, r.user, campaign, now=NOW)


def test_of_two_pending_enrollments_sharing_an_address_only_the_newer_is_skipped(
    session_factory: sessionmaker[Session],
) -> None:
    """#346 review: the summary judges a pending enrollment as the engine judges its
    first step, so it counts the older of two sharing an address as starting, and the
    engine sends to it, once."""
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")
    with session_scope(r.factory, write=True) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        twin = factories.make_contact(session, r.user, emails=["ada@contacts.example"])
        newer = factories.make_enrollment(
            session, campaign, twin, status=EnrollmentStatus.PENDING
        ).id

    report = _summary(r)
    assert report.summary == (
        "3 will start, 2 skipped (1 do-not-contact, 1 address already in this campaign)"
    )
    assert [(c.contact_id, c.reasons) for c in report.skipped if c.contact_id == twin.id] == [
        (twin.id, ("address already in this campaign",))
    ]
    review = _review(r)
    blocked = {m.enrollment_id: m.blocked for m in review.blocked}
    assert blocked[newer] == "excluded by a guard: address already in this campaign"
    assert r.ok[0] not in blocked  # the older one sends, in the review as in the engine

    _complete_and_activate(r, review)
    sender = FakeSender()
    _tick_until_quiet(r, sender)

    assert sorted(f.to_address or "" for f in sender.firings) == [
        "ada@contacts.example",
        "bob@contacts.example",
    ]
    with session_scope(r.factory) as session:
        sent = set(
            session.scalars(
                scoped(r.user, Message)
                .with_only_columns(Message.enrollment_id)
                .where(Message.status == MessageStatus.SENT)
            )
        )
    assert sent == set(r.ok)


def test_a_source_contact_not_enrolled_is_judged_against_the_pending_enrollments(
    session_factory: sessionmaker[Session],
) -> None:
    """#346 review: a source contact sharing a pending enrollment's address is skipped,
    as enrolling it would be; the pending one still starts."""
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")
    with session_scope(r.factory, write=True) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        twin = factories.make_contact(session, r.user, emails=["bob@contacts.example"])
        loner = factories.make_contact(session, r.user, emails=["cy@contacts.example"])
        members = [
            *session.scalars(
                scoped(r.user, Enrollment)
                .with_only_columns(Enrollment.contact_id)
                .where(Enrollment.campaign_id == r.campaign_id)
            ),
            twin.id,
            loner.id,
        ]
        source = list_service.create_list(session, r.user, "Source", ListKind.STATIC)
        list_service.add_members(session, r.user, source.id, members)
        campaign.source_list_id = source.id

    report = _summary(r)
    assert report.summary == (
        "3 will start, 2 skipped (1 do-not-contact, 1 address already in this campaign),"
        " 1 not enrolled"
    )
    assert twin.id in {c.contact_id for c in report.skipped}
    with session_scope(r.factory, write=True) as session:
        result = campaign_engine.enroll(
            session, r.user, r.campaign_id, [twin.id, loner.id], now=NOW
        )
        assert [v.contact_id for v in result.verdicts if v.eligible] == [loner.id]
        assert {v.contact_id: v.reasons for v in result.verdicts}[twin.id] == (
            Reason.DUPLICATE_ADDRESS,
        )


def test_a_lint_error_blocks_every_message_and_activation(
    session_factory: sessionmaker[Session],
) -> None:
    """A template with a lint error blocks each message and the gate; approving the step
    does not cover them."""
    r = _reviewing(session_factory, subject="Hello", body="Hi {{ nope }} {{ first_name }}")
    review = _review(r)
    assert review.total == 0
    assert {m.enrollment_id for m in review.blocked} == {*r.ok, r.no_subject, r.excluded}
    assert all(
        m.blocked is not None and m.blocked.startswith("lint error:")
        for m in review.blocked
        if m.enrollment_id != r.excluded
    )
    with session_scope(r.factory, write=True) as session:
        campaign_review.approve_step(
            session,
            r.user,
            r.campaign_id,
            r.step_id,
            fingerprint_seen=review.fingerprint,
            now=NOW,
        )
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        gaps = campaign_review.missing(session, r.user, campaign, now=NOW)
        assert "lint" in {g.requirement for g in gaps}
        with pytest.raises(campaign_review.ReviewIncomplete):
            campaign_review.activate(
                session, r.user, r.campaign_id, settings=SETTINGS, now=NOW, starts_at=NOW
            )


def test_the_summary_alone_never_reads_the_old_tools_history(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _reviewing(session_factory, subject="{{ first_name }}", body="Hi {{ first_name }}")

    def unwanted(*_: object) -> None:
        pytest.fail("the note was read for a caller that does not show it")

    monkeypatch.setattr(campaign_review, "prior_contact", unwanted)
    with session_scope(r.factory) as session:
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        assert campaign_review.guard_summary(session, r.user, campaign, now=NOW) == (
            "3 will start, 1 skipped (1 do-not-contact)"
        )
        assert campaign_review.guard_summary_and_note(
            session, r.user, campaign, now=NOW, include_note=False
        ) == ("3 will start, 1 skipped (1 do-not-contact)", None)


def _linkedin_reviewing(
    factory: sessionmaker[Session],
    *,
    subject: str | None,
    body: str,
    locations: tuple[str, ...] = ("Lisbon",),
) -> Any:
    """A one-step LinkedIn campaign under review, with a contact enrolled for each of
    ``locations``."""
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session)
        campaign = factories.make_campaign(
            session, user, channels=(TemplateChannel.LINKEDIN,), status=CampaignStatus.REVIEWING
        )
        template = get_scoped(session, user, Template, campaign.steps[0].template_id)
        assert template is not None
        template.subject, template.body = subject, body
        for location in locations:
            contact = factories.make_contact(session, user, location=location)
            factories.make_enrollment(session, campaign, contact, status=EnrollmentStatus.PENDING)
        return user, campaign.id


@pytest.fixture
def newlines_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The newline flag as it was before P4-03 (#382), and as it goes back to if a later
    capture shows Shift+Enter sending: the tests that use it pin the refusing branch."""
    from netkeeper.campaigns import render as render_module

    monkeypatch.setattr(render_module, "SHIFT_ENTER_NEWLINES_ALLOWED", False)


@pytest.mark.parametrize(
    ("subject", "body", "rule"),
    [
        ("Hello", "Hi {{ first_name }}", LintRule.LINKEDIN_SUBJECT),
        (None, "Hi {{ first_name }},\nthanks", LintRule.LINKEDIN_NEWLINE),
        (None, "Hi {{ first_name }} " + "x" * 8000, LintRule.LINKEDIN_TOO_LONG),
    ],
    ids=["subject", "newline", "too_long"],
)
@pytest.mark.usefixtures("newlines_refused")
def test_a_linkedin_step_that_fails_lint_blocks_activation(
    session_factory: sessionmaker[Session], subject: str | None, body: str, rule: LintRule
) -> None:
    """P4-11: what the prefill would refuse fails lint, and activation needs a clean lint."""
    user, campaign_id = _linkedin_reviewing(session_factory, subject=subject, body=body)
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert not result.clean
        [(_position, issues)] = result.steps
        assert [issue.rule for issue in issues] == [rule]
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        gaps = campaign_review.missing(session, user, campaign, now=NOW)
        assert "lint" in {g.requirement for g in gaps}
        with pytest.raises(campaign_review.ReviewIncomplete):
            campaign_review.activate(
                session, user, campaign_id, settings=SETTINGS, now=NOW, starts_at=NOW
            )


def test_a_long_linkedin_message_is_a_warning_that_does_not_block_lint(
    session_factory: sessionmaker[Session],
) -> None:
    user, campaign_id = _linkedin_reviewing(
        session_factory, subject=None, body="Hi {{ first_name }} " + "x" * 1100
    )
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert result.clean
        assert result.steps == ((1, ()),)


def test_a_linkedin_subject_with_a_long_body_gives_only_the_subject_error(
    session_factory: sessionmaker[Session],
) -> None:
    """The long-message warning is never listed among the errors that block activation."""
    user, campaign_id = _linkedin_reviewing(
        session_factory, subject="Hello", body="Hi {{ first_name }} " + "x" * 1100
    )
    with session_scope(session_factory, write=True) as session:
        result = campaign_review.record_lint(session, user, campaign_id, now=NOW)
        assert not result.clean
        [(_position, issues)] = result.steps
        assert [issue.rule for issue in issues] == [LintRule.LINKEDIN_SUBJECT]
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        template = campaign.steps[0].template
        assert [i.rule for i in activation_errors(template)] == [LintRule.LINKEDIN_SUBJECT]


@pytest.mark.usefixtures("newlines_refused")
def test_a_message_whose_render_has_a_lint_error_cannot_be_approved(
    session_factory: sessionmaker[Session],
) -> None:
    """P4-11: a merge value that makes a LinkedIn message multi-line blocks it, and a
    per-message approval of it is refused, not stored."""
    user, campaign_id = _linkedin_reviewing(
        session_factory,
        subject=None,
        body="Hi {{ first_name }} {{ personal_line }} in {{ location }}",
        locations=("Lisbon", "Two\nlines"),
    )
    with session_scope(session_factory) as session:
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        step_id = campaign.steps[0].id
        review = campaign_review.review_step(session, user, campaign_id, step_id, now=NOW)
    every = {m.enrollment_id: m for m in (*review.messages, *review.blocked)}
    bad = [m for m in every.values() if m.blocked is not None]
    good = [m for m in every.values() if m.blocked is None]
    assert len(bad) == 1 and len(good) == 1
    assert bad[0].blocked == (
        "lint error: LinkedIn messages must be one paragraph: the prefill never presses Enter"
    )
    refused = pytest.raises(campaign_review.ReviewConflict, match="can't be approved: lint error")
    with session_scope(session_factory, write=True) as session, refused:
        campaign_review.approve_messages(
            session,
            user,
            campaign_id,
            step_id,
            {bad[0].enrollment_id: bad[0].fingerprint},
            now=NOW,
        )
    with session_scope(session_factory, write=True) as session:
        approved = campaign_review.approve_messages(
            session,
            user,
            campaign_id,
            step_id,
            {good[0].enrollment_id: good[0].fingerprint},
            now=NOW,
        )
        assert [row.enrollment_id for row in approved] == [good[0].enrollment_id]
