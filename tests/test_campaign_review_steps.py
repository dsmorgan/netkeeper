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

from netkeeper.campaigns.render import me_fields
from netkeeper.db import session_scope
from netkeeper.models import (
    CampaignStatus,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageStatus,
    StepMode,
    Template,
    TemplateChannel,
    User,
)
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_review
from netkeeper.services.campaign_engine import run_tick

ME = me_fields(SETTINGS.me)


@dataclass
class Reviewed:
    factory: sessionmaker[Session]
    user: User
    campaign_id: int
    step_id: int
    ok: list[int]
    no_subject: int
    excluded: int


def _reviewing(factory: sessionmaker[Session], *, subject: str, body: str) -> Reviewed:
    """A one-step email campaign under review: two contacts whose message can be sent,
    one whose subject renders empty, and one a guard excludes."""
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

        def enroll(email: str, **contact: Any) -> int:
            row = factories.make_contact(session, user, emails=[email], **contact)
            return factories.make_enrollment(
                session, campaign, row, status=EnrollmentStatus.PENDING
            ).id

        ok = [enroll("ada@contacts.example"), enroll("bob@contacts.example")]
        no_subject = enroll("nameless@contacts.example", first_name="", preferred_name=None)
        excluded = enroll("dnc@contacts.example", do_not_contact=True)
        return Reviewed(factory, user, campaign.id, step.id, ok, no_subject, excluded)


def _review(r: Reviewed) -> campaign_review.StepReview:
    with session_scope(r.factory) as session:
        return campaign_review.review_step(
            session, r.user, r.campaign_id, r.step_id, me=ME, now=NOW
        )


def _complete_and_activate(r: Reviewed, review: campaign_review.StepReview) -> None:
    with session_scope(r.factory, write=True) as session:
        user = r.user
        campaign_review.approve_step(
            session,
            user,
            r.campaign_id,
            r.step_id,
            fingerprint_seen=review.fingerprint,
            me=ME,
            now=NOW,
        )
        plan = campaign_review.prepare_test_send(
            session, user, r.campaign_id, r.step_id, enrollment_id=r.ok[0], me=ME, today=NOW.date()
        )
        campaign_review.record_test_send(session, user, plan, gmail_message_id="fake", now=NOW)
        assert campaign_review.record_lint(session, user, r.campaign_id, me=ME, now=NOW).clean
        campaign = campaign_review.get_campaign(session, user, r.campaign_id)
        campaign_review.acknowledge_guards(
            session,
            user,
            r.campaign_id,
            summary_seen=campaign_review.guard_summary(session, user, campaign, now=NOW),
            audience_fingerprint_seen=campaign_review.audience_fingerprint(session, user, campaign),
            now=NOW,
        )
        campaign_review.activate(
            session, user, r.campaign_id, settings=SETTINGS, me=ME, now=NOW, starts_at=NOW
        )


def _tick_until_quiet(r: Reviewed, sender: FakeSender) -> None:
    for n in range(12):
        at = NOW + timedelta(minutes=1 + 10 * n)
        sender.now = at
        run_tick(
            r.factory,
            settings=SETTINGS,
            sender=sender,
            clock=partial(_at, at),
            rng=random.Random(n),
        )


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
            me=ME,
            now=NOW,
        )
        campaign = campaign_review.get_campaign(session, r.user, r.campaign_id)
        gaps = campaign_review.missing(session, r.user, campaign, me=ME, now=NOW)
        assert "lint" in {g.requirement for g in gaps}
        with pytest.raises(campaign_review.ReviewIncomplete):
            campaign_review.activate(
                session, r.user, r.campaign_id, settings=SETTINGS, me=ME, now=NOW, starts_at=NOW
            )
