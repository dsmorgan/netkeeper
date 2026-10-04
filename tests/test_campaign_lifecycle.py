"""The campaign lifecycle (#345): end, archive, unarchive and delete.

The maintainer's decision on #345: delete only a campaign never activated and with
no messages; archive one that ever sent, once it is ended; unarchive is allowed;
netkeeper never deletes a Gmail draft (ADR 0003), it lists them; a campaign is
concluded when every enrollment is terminal, or the person ended it.

The engine's side (an ended or archived campaign never fires) is in
``test_campaign_engine.py``; the routes in ``test_web_campaigns.py``; the commands
in ``test_cli_campaigns.py``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import factories
import pytest
from campaign_fakes import make_mailbox
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageDirection,
    MessageStatus,
    ReviewPreview,
    StepApproval,
    Template,
    TemplateChannel,
    TestSend,
    User,
)
from netkeeper.models.base import UserOwned
from netkeeper.scoping import get_scoped, scoped
from netkeeper.services import campaign_engine, campaign_results
from netkeeper.services import campaigns as service
from netkeeper.services.campaigns import CampaignConflict, CampaignNotFound

EMAIL2 = (TemplateChannel.EMAIL, TemplateChannel.EMAIL)
NOW = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
TERMINAL = (
    EnrollmentStatus.REPLIED,
    EnrollmentStatus.COMPLETED,
    EnrollmentStatus.BOUNCED,
    EnrollmentStatus.OPTED_OUT,
    EnrollmentStatus.REMOVED,
)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, rolled back at teardown unless a test commits."""
    with session_scope(session_factory, write=True) as session:
        yield session


def _user(session: Session) -> User:
    return factories.make_user(session)


def _campaign(session: Session, user: User, status: CampaignStatus, **fields: object) -> Campaign:
    return factories.make_campaign(session, user, channels=EMAIL2, status=status, **fields)


def _enroll(
    session: Session,
    user: User,
    campaign: Campaign,
    status: EnrollmentStatus = EnrollmentStatus.ACTIVE,
) -> Enrollment:
    contact = factories.make_contact(session, user)
    return factories.make_enrollment(session, campaign, contact, status=status)


def _status(campaign: Campaign) -> CampaignStatus:
    """The campaign's status, read fresh: unnarrowed by an earlier assert on it."""
    return campaign.status


_BY_CAMPAIGN: tuple[tuple[type[UserOwned], InstrumentedAttribute[int]], ...] = (
    (CampaignStep, CampaignStep.campaign_id),
    (Enrollment, Enrollment.campaign_id),
    (ReviewPreview, ReviewPreview.campaign_id),
    (StepApproval, StepApproval.campaign_id),
    (TestSend, TestSend.campaign_id),
)


def test_the_lifecycle_sets_are_pinned() -> None:
    """Which states end, and which count as over, against words written out here."""
    assert {s.value for s in campaign_engine.ENDABLE} == {"active", "paused"}
    assert {s.value for s in service.OVER} == {"completed", "archived"}
    assert {s.value for s in campaign_engine.LIVE_STATUSES} == {"pending", "active", "paused"}


# --- end ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [CampaignStatus.ACTIVE, CampaignStatus.PAUSED])
def test_end_completes_an_active_or_paused_campaign_and_keeps_its_enrollments(
    writer: Session, status: CampaignStatus
) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, status)
    live = _enroll(writer, user, campaign)
    live.next_action_at = NOW
    replied = _enroll(writer, user, campaign, EnrollmentStatus.REPLIED)

    ended = service.end(writer, user, campaign.id)

    assert ended.status is CampaignStatus.COMPLETED
    assert (live.status, live.next_action_at) == (EnrollmentStatus.ACTIVE, NOW)
    assert replied.status is EnrollmentStatus.REPLIED
    assert service.concluded(writer, user, ended)


@pytest.mark.parametrize(
    "status",
    [
        CampaignStatus.DRAFT,
        CampaignStatus.REVIEWING,
        CampaignStatus.COMPLETED,
        CampaignStatus.ARCHIVED,
    ],
)
def test_end_refuses_a_campaign_that_is_not_running(
    writer: Session, status: CampaignStatus
) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, status)
    with pytest.raises(CampaignConflict, match="only an active or paused"):
        service.end(writer, user, campaign.id)
    assert campaign.status is status


def test_an_ended_campaign_cannot_be_resumed(writer: Session) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, CampaignStatus.PAUSED)
    service.end(writer, user, campaign.id)
    with pytest.raises(CampaignConflict, match="not paused"):
        service.resume(writer, user, campaign.id)
    assert campaign.status is CampaignStatus.COMPLETED


# --- concluded and archive -----------------------------------------------------------


def test_concluded_means_ended_or_every_enrollment_terminal(writer: Session) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, CampaignStatus.ACTIVE)
    for status in TERMINAL:
        _enroll(writer, user, campaign, status)
    assert service.concluded(writer, user, campaign)

    for live in campaign_engine.LIVE_STATUSES:
        other = _campaign(writer, user, CampaignStatus.ACTIVE)
        _enroll(writer, user, other, EnrollmentStatus.REPLIED)
        _enroll(writer, user, other, live)
        assert not service.concluded(writer, user, other), live

    for never in (CampaignStatus.DRAFT, CampaignStatus.REVIEWING):
        never_sent = _campaign(writer, user, never)
        assert not service.concluded(writer, user, never_sent)


def test_archive_and_unarchive_an_ended_campaign(writer: Session) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, CampaignStatus.ACTIVE)
    enrollment = _enroll(writer, user, campaign)
    message = factories.make_message(writer, enrollment, sent_at=NOW - timedelta(days=2))
    service.end(writer, user, campaign.id)

    service.archive(writer, user, campaign.id)

    assert campaign.status is CampaignStatus.ARCHIVED
    # Hidden from the list, kept everywhere else.
    assert [s.campaign.id for s in service.list_campaigns(writer, user)] == []
    assert [s.campaign.id for s in service.list_campaigns(writer, user, archived=True)] == [
        campaign.id
    ]
    assert get_scoped(writer, user, Message, message.id) is not None
    results = campaign_results.campaign_results(writer, user, campaign.id, now=NOW)
    assert (results.totals.sent, results.totals.contacted) == (1, 1)
    detail = service.campaign_status(writer, user, campaign.id, now=NOW)
    assert (detail.concluded, detail.deletable) == (True, False)

    with pytest.raises(CampaignConflict, match="already archived"):
        service.archive(writer, user, campaign.id)

    service.unarchive(writer, user, campaign.id)
    assert _status(campaign) is CampaignStatus.COMPLETED
    assert [s.campaign.id for s in service.list_campaigns(writer, user)] == [campaign.id]
    with pytest.raises(CampaignConflict, match="not archived"):
        service.unarchive(writer, user, campaign.id)


@pytest.mark.parametrize("status", [CampaignStatus.ACTIVE, CampaignStatus.PAUSED])
def test_archive_refuses_a_running_campaign_with_live_enrollments(
    writer: Session, status: CampaignStatus
) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, status)
    _enroll(writer, user, campaign, EnrollmentStatus.PAUSED)
    with pytest.raises(CampaignConflict, match="end it first"):
        service.archive(writer, user, campaign.id)
    assert campaign.status is status


@pytest.mark.parametrize("status", [CampaignStatus.ACTIVE, CampaignStatus.PAUSED])
def test_archive_refuses_a_running_campaign_even_when_every_enrollment_finished(
    writer: Session, status: CampaignStatus
) -> None:
    """The maintainer's decision: archiving requires the campaign to be ended first.
    Concluded by its enrollments is not enough, and archive never ends it itself."""
    user = _user(writer)
    campaign = _campaign(writer, user, status)
    _enroll(writer, user, campaign, EnrollmentStatus.COMPLETED)
    _enroll(writer, user, campaign, EnrollmentStatus.REPLIED)
    assert service.concluded(writer, user, campaign)
    with pytest.raises(CampaignConflict, match="end it first"):
        service.archive(writer, user, campaign.id)
    assert campaign.status is status

    service.end(writer, user, campaign.id)
    service.archive(writer, user, campaign.id)
    assert _status(campaign) is CampaignStatus.ARCHIVED


@pytest.mark.parametrize("status", [CampaignStatus.DRAFT, CampaignStatus.REVIEWING])
def test_archive_refuses_a_campaign_never_activated(
    writer: Session, status: CampaignStatus
) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, status)
    with pytest.raises(CampaignConflict, match="delete it instead"):
        service.archive(writer, user, campaign.id)


# --- delete ---------------------------------------------------------------------------


def _review_records(session: Session, user: User, campaign: Campaign) -> None:
    """A preview, a step approval and two test sends: one a Gmail draft, one sent."""
    enrollment = _enroll(session, user, campaign, EnrollmentStatus.PENDING)
    step = campaign.steps[0]
    session.add_all(
        [
            ReviewPreview(
                user_id=user.id,
                campaign_id=campaign.id,
                enrollment_id=enrollment.id,
                viewed_at=NOW,
            ),
            StepApproval(
                user_id=user.id,
                campaign_id=campaign.id,
                step_id=step.id,
                fingerprint="f" * 64,
                approved_at=NOW,
            ),
            TestSend(
                user_id=user.id,
                campaign_id=campaign.id,
                step_id=campaign.steps[1].id,
                fingerprint="f" * 64,
                to_address="me@example.test",
                gmail_draft_id="draft-test-2",
                sent_at=NOW,
            ),
            TestSend(
                user_id=user.id,
                campaign_id=campaign.id,
                step_id=step.id,
                fingerprint="f" * 64,
                to_address="me@example.test",
                gmail_message_id="gm-sent",
                sent_at=NOW - timedelta(hours=1),
            ),
        ]
    )
    session.flush()


@pytest.mark.parametrize("status", [CampaignStatus.DRAFT, CampaignStatus.REVIEWING])
def test_delete_removes_a_campaign_never_activated_and_lists_the_drafts_left(
    session_factory: sessionmaker[Session], status: CampaignStatus
) -> None:
    with session_scope(session_factory, write=True) as session:
        user = _user(session)
        campaign = _campaign(session, user, status)
        _enroll(session, user, campaign, EnrollmentStatus.PENDING)
        _review_records(session, user, campaign)
        template_ids = [s.template_id for s in campaign.steps]
        campaign_id, user_id = campaign.id, user.id
        assert service.campaign_status(session, user, campaign_id, now=NOW).deletable

    with session_scope(session_factory, write=True) as session:
        user = session.get_one(User, user_id)
        plan = service.delete_campaign(session, user, campaign_id)

    assert (plan.deletable, plan.steps, plan.enrollments) == (True, 2, 2)
    assert plan.leftover_drafts == (
        service.LeftoverDraft(
            step_position=2,
            to_address="me@example.test",
            drafted_at=NOW,
            gmail_draft_id="draft-test-2",
        ),
    )
    with session_scope(session_factory) as session:
        user = session.get_one(User, user_id)
        assert get_scoped(session, user, Campaign, campaign_id) is None
        for model, column in _BY_CAMPAIGN:
            left = session.scalars(scoped(user, model).where(column == campaign_id)).all()
            assert left == [], model
        # The templates are the person's, not the campaign's.
        assert all(get_scoped(session, user, Template, t) is not None for t in template_ids)


@pytest.mark.parametrize(
    ("status", "with_step"),
    [(status, True) for status in MessageStatus] + [(MessageStatus.RECEIVED, False)],
)
def test_delete_refuses_a_campaign_with_any_message(
    writer: Session, status: MessageStatus, with_step: bool
) -> None:
    """Any message row, in any status and either direction, keeps the campaign; so does a
    reply whose step is not known (``step_id`` NULL), found by its enrollment alone."""
    user = _user(writer)
    campaign = _campaign(writer, user, CampaignStatus.DRAFT)
    enrollment = _enroll(writer, user, campaign, EnrollmentStatus.PENDING)
    direction = MessageDirection.IN if status is MessageStatus.RECEIVED else MessageDirection.OUT
    message = factories.make_message(
        writer, enrollment, status=status, direction=direction, sent_at=None
    )
    if not with_step:
        message.step_id = None
        writer.flush()

    plan = service.delete_plan(writer, user, campaign.id)
    assert plan.refusal == f"campaign {campaign.id} has 1 message, so it is kept"
    assert not service.campaign_status(writer, user, campaign.id, now=NOW).deletable
    with pytest.raises(CampaignConflict, match="has 1 message"):
        service.delete_campaign(writer, user, campaign.id)
    assert get_scoped(writer, user, Campaign, campaign.id) is not None


def test_delete_refuses_a_message_that_names_only_a_step(writer: Session) -> None:
    """A message of another campaign's enrollment that names this campaign's step keeps
    the campaign too: the step's foreign key would refuse the delete anyway."""
    user = _user(writer)
    campaign = _campaign(writer, user, CampaignStatus.DRAFT)
    other = _campaign(writer, user, CampaignStatus.ACTIVE)
    enrollment = _enroll(writer, user, other)
    message = factories.make_message(writer, enrollment)
    message.step_id = campaign.steps[0].id
    writer.flush()
    with pytest.raises(CampaignConflict, match="has 1 message"):
        service.delete_campaign(writer, user, campaign.id)


def test_the_database_refuses_to_delete_a_campaign_with_a_message(
    session_factory: sessionmaker[Session],
) -> None:
    """Below the service: a message's enrollment and step have no ON DELETE action."""
    with session_scope(session_factory, write=True) as session:
        user = _user(session)
        campaign = _campaign(session, user, CampaignStatus.DRAFT)
        enrollment = _enroll(session, user, campaign, EnrollmentStatus.PENDING)
        factories.make_message(session, enrollment, status=MessageStatus.DISCARDED)
        campaign_id, user_id = campaign.id, user.id
    with (
        pytest.raises(IntegrityError),
        session_scope(session_factory, write=True) as session,
    ):
        user = session.get_one(User, user_id)
        row = get_scoped(session, user, Campaign, campaign_id)
        assert row is not None
        session.delete(row)
        session.flush()


@pytest.mark.parametrize(
    "status",
    [
        CampaignStatus.ACTIVE,
        CampaignStatus.PAUSED,
        CampaignStatus.COMPLETED,
        CampaignStatus.ARCHIVED,
    ],
)
def test_delete_refuses_a_campaign_that_was_activated(
    writer: Session, status: CampaignStatus
) -> None:
    user = _user(writer)
    campaign = _campaign(writer, user, status)
    with pytest.raises(CampaignConflict, match="only a campaign that was never activated"):
        service.delete_campaign(writer, user, campaign.id)
    assert get_scoped(writer, user, Campaign, campaign.id) is not None


def test_delete_refuses_a_reviewing_campaign_that_was_activated_once(writer: Session) -> None:
    """Belt and braces: an approval or a start on record means it was activated."""
    user = _user(writer)
    campaign = _campaign(writer, user, CampaignStatus.REVIEWING, approved_at=NOW)
    with pytest.raises(CampaignConflict, match="was activated once"):
        service.delete_campaign(writer, user, campaign.id)


# --- another user's campaign ----------------------------------------------------------


def test_every_move_answers_not_found_for_another_users_campaign(writer: Session) -> None:
    owner, other = _user(writer), _user(writer)
    draft = _campaign(writer, owner, CampaignStatus.DRAFT)
    ended = _campaign(writer, owner, CampaignStatus.COMPLETED)
    archived = _campaign(writer, owner, CampaignStatus.ARCHIVED)
    for move, campaign in (
        (service.end, _campaign(writer, owner, CampaignStatus.ACTIVE)),
        (service.archive, ended),
        (service.unarchive, archived),
        (service.delete_plan, draft),
        (service.delete_campaign, draft),
    ):
        with pytest.raises(CampaignNotFound):
            move(writer, other, campaign.id)
    assert (draft.status, ended.status, archived.status) == (
        CampaignStatus.DRAFT,
        CampaignStatus.COMPLETED,
        CampaignStatus.ARCHIVED,
    )
    assert service.list_campaigns(writer, other, archived=True) == []


def test_the_delete_is_conditional_on_the_state_it_checked(
    writer: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete statement repeats the checks: a campaign activated after the plan was
    read (here, a plan forced to say yes) matches no row and is refused, not deleted."""
    user = _user(writer)
    campaign = _campaign(writer, user, CampaignStatus.ACTIVE)
    real = service.delete_plan

    def stale(session: Session, user: User, campaign_id: int) -> service.DeletePlan:
        return dataclasses.replace(real(session, user, campaign_id), refusal=None)

    monkeypatch.setattr(service, "delete_plan", stale)
    with pytest.raises(CampaignConflict, match="changed while it was being deleted"):
        service.delete_campaign(writer, user, campaign.id)
    assert get_scoped(writer, user, Campaign, campaign.id) is not None


def _test_draft(session: Session, user: User, campaign: Campaign, to: str) -> None:
    session.add(
        TestSend(
            user_id=user.id,
            campaign_id=campaign.id,
            step_id=campaign.steps[0].id,
            fingerprint="f" * 64,
            to_address=to,
            gmail_draft_id=f"draft-{campaign.id}",
            rfc822_message_id=f"<test-{campaign.id}@netkeeper.test>",
            sent_at=NOW,
        )
    )
    session.flush()


def test_the_plan_says_when_the_delete_takes_the_last_drafts_that_could_verify(
    writer: Session,
) -> None:
    """#345 review: a still-unverified mailbox is verified by finding a test draft's
    Message-ID (#304); deleting the only campaign holding one says so."""
    user = _user(writer)
    mailbox = make_mailbox(writer, user, message_id_verified_at=None)
    draft = _campaign(writer, user, CampaignStatus.DRAFT, mailbox_id=mailbox.id)
    _test_draft(writer, user, draft, mailbox.email.upper())
    assert service.delete_plan(writer, user, draft.id).unverifies == mailbox.email

    other = _campaign(writer, user, CampaignStatus.REVIEWING, mailbox_id=mailbox.id)
    _test_draft(writer, user, other, mailbox.email)
    assert service.delete_plan(writer, user, draft.id).unverifies is None

    verified = make_mailbox(writer, user, email="verified@example.test")
    verified.message_id_verified_at = NOW
    lone = _campaign(writer, user, CampaignStatus.DRAFT, mailbox_id=verified.id)
    _test_draft(writer, user, lone, verified.email)
    assert service.delete_plan(writer, user, lone.id).unverifies is None
