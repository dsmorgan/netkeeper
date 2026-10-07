"""A campaign step adopts the newest version of its template (#397).

The rule this file holds above all: a message that exists keeps the text it was
rendered with. Adoption re-points the step; it never renders again, or writes, a
claimed, drafted, prefilled or sent message, and an enrollment the step already
fired for is never sent the step again.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import NOW, SETTINGS, FakeSender, make_mailbox
from fastapi import FastAPI
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.campaigns import templates as template_service
from netkeeper.campaigns.templates import REMOVED_FIELD_BLOCK
from netkeeper.cli import app as cli
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Enrollment,
    EnrollmentStatus,
    Message,
    MessageStatus,
    StepApproval,
    StepMode,
    Template,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.models.base import UserOwned
from netkeeper.scoping import get_scoped, install_scope_guard, scoped
from netkeeper.services import campaign_review
from netkeeper.services import campaigns as campaign_service
from netkeeper.services import template_adoption as service
from netkeeper.services.campaign_engine import run_tick
from netkeeper.services.campaigns import CampaignConflict, CampaignNotFound
from netkeeper.services.template_adoption import AdoptionStale
from netkeeper.services.users import ensure_local_user

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
OLD_TEXT = "Hi First, the text it was rendered with."
CSRF = {"X-Netkeeper-Client": "1"}


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


def _campaign(
    session: Session,
    user: User,
    *,
    channels: tuple[TemplateChannel, ...] = (EMAIL,),
    status: CampaignStatus = CampaignStatus.ACTIVE,
    mode: StepMode | None = None,
) -> Campaign:
    mailbox = make_mailbox(session, user)
    campaign = factories.make_campaign(
        session, user, channels=channels, status=status, mailbox_id=mailbox.id
    )
    for step in campaign.steps:
        if step.channel is EMAIL:
            step.mode = StepMode.SEND
        if mode is not None:
            step.mode = mode
    session.flush()
    return campaign


def _new_version(session: Session, user: User, step: CampaignStep, body: str) -> Template:
    """Edit the step's template: in use, so the edit is a new version (spec 8.5)."""
    row = template_service.update_template(session, user, step.template_id, body=body)
    assert row.id != step.template_id
    return row


_counter = iter(range(1, 1_000_000))


def _enroll(session: Session, campaign: Campaign, **fields: Any) -> Enrollment:
    user = session.get(User, campaign.user_id)
    assert user is not None
    contact = factories.make_contact(session, user, emails=[f"person{next(_counter)}@example.test"])
    return factories.make_enrollment(session, campaign, contact, **fields)


def _message(session: Session, enrollment: Enrollment, status: MessageStatus) -> Message:
    return factories.make_message(
        session,
        enrollment,
        status=status,
        body_rendered=OLD_TEXT,
        subject="Hello" if enrollment.campaign.steps[0].channel is EMAIL else None,
        sent_at=NOW if status is MessageStatus.SENT else None,
        scheduled_at=NOW - timedelta(minutes=1),
        prefilled_at=NOW if status is MessageStatus.PREFILLED else None,
    )


def _row(obj: Any) -> dict[str, Any]:
    """Every column of a row, as stored."""
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs}


def _changed(before: dict[str, Any], after: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """The columns that differ, with both values: empty when nothing changed."""
    return {k: (v, after[k]) for k, v in before.items() if after[k] != v}


def _fresh[T: UserOwned](session: Session, user: User, model: type[T], row_id: int) -> T:
    session.expire_all()
    row = get_scoped(session, user, model, row_id)
    assert row is not None
    return row


def _adopt(session: Session, user: User, campaign: Campaign, position: int = 1) -> Any:
    step = campaign.steps[position - 1]
    shown = service.preview(session, user, campaign.id, step.id, now=NOW)
    assert shown.refusal is None, shown.refusal
    return service.adopt(
        session, user, campaign.id, step.id, fingerprint_seen=shown.fingerprint, now=NOW
    )


# --- the preview ---------------------------------------------------------------------------


def test_the_preview_shows_the_change_and_who_gets_it(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(EMAIL, EMAIL))
    step = campaign.steps[1]
    waiting = _enroll(writer, campaign, current_step=1, next_action_at=NOW)
    done = _enroll(writer, campaign, current_step=1)
    _message(writer, done, MessageStatus.SENT)
    fired_step_2 = _message(writer, done, MessageStatus.SENT)
    fired_step_2.step_id = step.id
    replied = _enroll(writer, campaign, status=EnrollmentStatus.REPLIED, current_step=1)
    newest = _new_version(writer, user, step, "Hello {{ first_name }}, still around?")

    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)

    assert shown.refusal is None
    assert (shown.current.version, shown.newest.version) == (1, 2)
    assert shown.newest.template_id == newest.id
    assert "-Hi {{ first_name }}" in shown.diff
    assert "+Hello {{ first_name }}, still around?" in shown.diff
    assert shown.errors == ()
    assert [e.enrollment_id for e in shown.affected] == [waiting.id]
    assert shown.affected_total == 1
    assert replied.id not in {e.enrollment_id for e in shown.affected}
    assert shown.kept == {MessageStatus.SENT: 1}
    [sample] = shown.samples
    assert sample.enrollment_id == waiting.id
    assert sample.body is not None and sample.body.startswith("Hello First")
    # Reads only.
    assert writer.get(CampaignStep, step.id).template_id != newest.id  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "status", [CampaignStatus.DRAFT, CampaignStatus.REVIEWING, CampaignStatus.COMPLETED]
)
def test_only_an_active_or_paused_campaign_adopts(writer: Session, status: CampaignStatus) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    _new_version(writer, user, step, "Hello {{ first_name }}")
    campaign.status = status
    writer.flush()
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    assert shown.refusal is not None and "only an active or paused" in shown.refusal
    with pytest.raises(CampaignConflict, match="only an active or paused"):
        service.adopt(
            writer, user, campaign.id, step.id, fingerprint_seen=shown.fingerprint, now=NOW
        )


def test_a_step_on_the_newest_version_has_nothing_to_adopt(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    assert shown.refusal == "step 1 already uses the newest version (v1)"
    assert shown.diff == "" and shown.samples == ()


def test_the_newest_version_of_a_chain_is_the_one_adopted(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    second = _new_version(writer, user, step, "Hello {{ first_name }}")
    # Another campaign uses v2, so an edit of it is v3.
    third = Template(
        user_id=user.id,
        name=second.name,
        channel=EMAIL,
        subject="Hello",
        body="Hey {{ first_name }}",
        lint_json=[],
        version=3,
        previous_id=second.id,
    )
    writer.add(third)
    writer.flush()
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    assert (shown.newest.template_id, shown.newest.version) == (third.id, 3)
    assert "+Hey {{ first_name }}" in shown.diff


@pytest.mark.parametrize("mode", [StepMode.PREFILL, StepMode.AUTO_SEND])
@pytest.mark.parametrize(
    ("subject", "body", "rule"),
    [
        (None, "Hi {{ first_name }} " + "x" * 9000, "linkedin_too_long"),
        (None, "Hi {{ first_name }},\tthanks", "linkedin_untypable"),
        (None, "Hi {{ first_name }}, {{ me.signature }}", "removed_field"),
    ],
)
def test_a_linkedin_version_with_a_lint_error_is_refused_for_either_mode(
    writer: Session, mode: StepMode, subject: str | None, body: str, rule: str
) -> None:
    """The activation gate's checks, LinkedIn's included, whatever the step's mode: an
    auto-send step (#458) takes exactly what a prefill step takes."""
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(LINKEDIN,), mode=mode)
    step = campaign.steps[0]
    # Written directly: a save drops a LinkedIn subject (#448), but a row from before
    # that keeps one.
    newer = Template(
        user_id=user.id,
        name=step.template.name,
        channel=LINKEDIN,
        subject=subject,
        body=body,
        lint_json=[],
        version=2,
        previous_id=step.template_id,
    )
    writer.add(newer)
    writer.flush()
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    assert rule in {i.rule.value for i in shown.errors}
    assert shown.refusal is not None and rule in shown.refusal
    with pytest.raises(CampaignConflict, match=rule):
        service.adopt(
            writer, user, campaign.id, step.id, fingerprint_seen=shown.fingerprint, now=NOW
        )
    assert _fresh(writer, user, CampaignStep, step.id).template_id != newer.id


@pytest.mark.parametrize("mode", [StepMode.PREFILL, StepMode.AUTO_SEND])
def test_linkedin_warnings_are_shown_and_a_message_too_long_to_type_is_blocked(
    writer: Session, mode: StepMode
) -> None:
    """As at activation: a subject (#448, never rendered) and a long typing time are
    warnings in the template, shown and never a refusal. A message that renders too long
    to type is an error for its contact: listed as blocked, and never sent."""
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(LINKEDIN,), mode=mode)
    step = campaign.steps[0]
    waiting = _enroll(writer, campaign, next_action_at=NOW)
    newer = Template(
        user_id=user.id,
        name=step.template.name,
        channel=LINKEDIN,
        subject="Catching up",  # a row from before #448 keeps one
        body="Hi {{ first_name }}, " + "a fairly long line. " * 60,
        lint_json=[],
        version=2,
        previous_id=step.template_id,
    )
    writer.add(newer)
    writer.flush()
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    assert shown.errors == ()
    assert {i.rule.value for i in shown.warnings} == {"linkedin_subject", "linkedin_typing_time"}
    assert shown.refusal is None
    [blocked] = shown.blocked
    assert (blocked.enrollment_id, shown.blocked_total, shown.samples) == (waiting.id, 1, ())
    assert blocked.blocked is not None and "to type" in blocked.blocked
    assert blocked.subject is None


def test_a_version_on_another_channel_is_refused(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    template_service.update_template(writer, user, step.template_id, channel=LINKEDIN)
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    assert shown.refusal is not None and "keeps its channel" in shown.refusal


def test_a_personal_line_version_is_refused(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    _new_version(writer, user, step, "Hi {{ first_name }}, {{ personal_line }}")
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    assert shown.refusal is not None and "personal_line" in shown.refusal


# --- adopting -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [CampaignStatus.ACTIVE, CampaignStatus.PAUSED])
def test_adopting_repoints_the_step_records_who_and_approves_it(
    writer: Session, status: CampaignStatus
) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, status=status)
    step = campaign.steps[0]
    newest = _new_version(writer, user, step, "Hello {{ first_name }}")

    result = _adopt(writer, user, campaign)

    assert (result.from_version, result.to_version, result.template_id) == (1, 2, newest.id)
    row = _fresh(writer, user, CampaignStep, step.id)
    assert row.template_id == newest.id
    assert row.template_adopted_at == NOW
    assert row.template_adopted_by == user.id
    assert row.template_adopted_from_version == 1
    # The campaign keeps its status: the confirm was the approval.
    assert _fresh(writer, user, Campaign, campaign.id).status is status
    # The review reflects it: the step is approved in the new version, and named by it.
    review = campaign_review.review_step(writer, user, campaign.id, step.id, now=NOW)
    assert review.approved
    assert review.template_name.endswith("v2")
    [approval] = writer.scalars(scoped(user, StepApproval)).all()
    assert approval.fingerprint == review.fingerprint
    # So do the campaign's status and its lint record.
    detail = campaign_service.campaign_status(writer, user, campaign.id, now=NOW)
    assert (detail.steps[0].template_version, detail.steps[0].newest_version) == (2, None)
    current = campaign_review.content_fingerprint(writer, user, campaign)
    assert _fresh(writer, user, Campaign, campaign.id).lint_fingerprint == current


def test_the_status_offers_a_newer_version(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    _new_version(writer, user, campaign.steps[0], "Hello {{ first_name }}")
    detail = campaign_service.campaign_status(writer, user, campaign.id, now=NOW)
    assert (detail.steps[0].template_version, detail.steps[0].newest_version) == (1, 2)


def test_a_stale_preview_is_refused(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    second = _new_version(writer, user, step, "Hello {{ first_name }}")
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    template_service.update_template(writer, user, second.id, body="Hey {{ first_name }}")
    with pytest.raises(AdoptionStale):
        service.adopt(
            writer, user, campaign.id, step.id, fingerprint_seen=shown.fingerprint, now=NOW
        )
    assert _fresh(writer, user, CampaignStep, step.id).template_id == shown.current.template_id


def test_another_users_campaign_is_not_found(writer: Session) -> None:
    owner = factories.make_user(writer)
    other = factories.make_user(writer)
    campaign = _campaign(writer, owner)
    step = campaign.steps[0]
    _new_version(writer, owner, step, "Hello {{ first_name }}")
    shown = service.preview(writer, owner, campaign.id, step.id, now=NOW)
    with pytest.raises(CampaignNotFound):
        service.preview(writer, other, campaign.id, step.id, now=NOW)
    with pytest.raises(CampaignNotFound):
        service.adopt(
            writer, other, campaign.id, step.id, fingerprint_seen=shown.fingerprint, now=NOW
        )
    assert _fresh(writer, owner, CampaignStep, step.id).template_adopted_at is None


# --- the rule: a message that exists keeps its text ----------------------------------------


EMAIL_STATUSES = (
    MessageStatus.SCHEDULED,  # claimed: its send is under way
    MessageStatus.DRAFTED,
    MessageStatus.SENT,
    MessageStatus.FAILED,
    MessageStatus.DISCARDED,
    MessageStatus.BOUNCED,
)


def test_no_email_message_of_the_step_changes(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    enrollments = [_enroll(writer, campaign) for _ in EMAIL_STATUSES]
    messages = [_message(writer, e, s) for e, s in zip(enrollments, EMAIL_STATUSES, strict=True)]
    for e in enrollments:
        e.next_action_at = None
    _new_version(writer, user, campaign.steps[0], "Hello {{ first_name }}, all new")
    before_messages = [_row(_fresh(writer, user, Message, m.id)) for m in messages]
    before_enrollments = [_row(_fresh(writer, user, Enrollment, e.id)) for e in enrollments]

    shown = service.preview(writer, user, campaign.id, campaign.steps[0].id, now=NOW)
    assert shown.affected_total == 0
    assert {m.message_id for m in shown.open_messages} == {messages[0].id, messages[1].id}
    _adopt(writer, user, campaign)

    after = [_row(_fresh(writer, user, Message, m.id)) for m in messages]
    assert [_changed(b, a) for b, a in zip(before_messages, after, strict=True)] == [{}] * len(
        messages
    )
    assert all(m["body_rendered"] == OLD_TEXT for m in after)
    after_enrollments = [_row(_fresh(writer, user, Enrollment, e.id)) for e in enrollments]
    assert [_changed(b, a) for b, a in zip(before_enrollments, after_enrollments, strict=True)] == [
        {}
    ] * len(enrollments)


@pytest.mark.parametrize(
    "status",
    [
        MessageStatus.SCHEDULED,  # a prefill or an auto-send in progress
        MessageStatus.PREFILLED,  # typed, waiting for the person
        MessageStatus.STALE,
        MessageStatus.SENT,
    ],
)
@pytest.mark.parametrize("mode", [StepMode.PREFILL, StepMode.AUTO_SEND])
def test_an_open_or_sent_linkedin_message_keeps_its_text(
    writer: Session, status: MessageStatus, mode: StepMode
) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(LINKEDIN,), mode=mode)
    enrollment = _enroll(writer, campaign)
    message = _message(writer, enrollment, status)
    enrollment.next_action_at = None
    enrollment.not_sent_error = None
    _new_version(writer, user, campaign.steps[0], "Hello {{ first_name }}, all new")
    before = (
        _row(_fresh(writer, user, Message, message.id)),
        _row(_fresh(writer, user, Enrollment, enrollment.id)),
    )

    _adopt(writer, user, campaign)

    after = (
        _row(_fresh(writer, user, Message, message.id)),
        _row(_fresh(writer, user, Enrollment, enrollment.id)),
    )
    assert after == before
    assert after[0]["body_rendered"] == OLD_TEXT


def test_a_parked_enrollment_with_a_message_of_the_step_is_not_released(
    writer: Session,
) -> None:
    """A ``blocked:`` reason on an enrollment that already has the step's message (a merge
    can leave one) is not the step's to release: the message decides, never twice."""
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(EMAIL, EMAIL))
    enrollment = _enroll(writer, campaign, next_action_at=None, not_sent_error=REMOVED_FIELD_BLOCK)
    message = _message(writer, enrollment, MessageStatus.SENT)
    _new_version(writer, user, campaign.steps[0], "Hello {{ first_name }}")
    before = _row(_fresh(writer, user, Enrollment, enrollment.id))

    result = _adopt(writer, user, campaign)

    assert result.released == 0
    assert _row(_fresh(writer, user, Enrollment, enrollment.id)) == before
    assert _fresh(writer, user, Message, message.id).body_rendered == OLD_TEXT


def test_only_enrollments_parked_on_this_step_are_released(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(EMAIL, EMAIL))
    parked = _enroll(
        writer,
        campaign,
        next_action_at=None,
        not_sent_error=REMOVED_FIELD_BLOCK,
        not_sent_count=0,
    )
    on_step_2 = _enroll(
        writer, campaign, current_step=1, next_action_at=None, not_sent_error=REMOVED_FIELD_BLOCK
    )
    other_reason = _enroll(writer, campaign, next_action_at=None, not_sent_error="not_typed: x")
    paused = _enroll(
        writer,
        campaign,
        status=EnrollmentStatus.PAUSED,
        next_action_at=None,
        not_sent_error=REMOVED_FIELD_BLOCK,
    )
    _new_version(writer, user, campaign.steps[0], "Hello {{ first_name }}")
    untouched = {
        e.id: _row(_fresh(writer, user, Enrollment, e.id))
        for e in (on_step_2, other_reason, paused)
    }

    shown = service.preview(writer, user, campaign.id, campaign.steps[0].id, now=NOW)
    assert shown.released == 1
    result = _adopt(writer, user, campaign)

    assert result.released == 1
    row = _fresh(writer, user, Enrollment, parked.id)
    assert (row.next_action_at, row.not_sent_error, row.not_sent_count) == (NOW, None, 0)
    for enrollment_id, before in untouched.items():
        assert _row(_fresh(writer, user, Enrollment, enrollment_id)) == before


# --- with the engine: what fires, and what never fires again --------------------------------


def _tick(factory: sessionmaker[Session], user: User, sender: FakeSender, now: Any = NOW) -> Any:
    sender.now = now
    [result] = [
        r
        for r in run_tick(
            factory, settings=SETTINGS, sender=sender, clock=lambda: now, rng=random.Random(1)
        )
        if r.user_id == user.id
    ]
    return result


def _user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    assert user is not None
    return user


def test_a_parked_step_fires_in_the_new_version_only_after_adoption_and_never_twice(
    session_factory: sessionmaker[Session],
) -> None:
    sender = FakeSender()
    with session_scope(session_factory, write=True) as s:
        user = factories.make_user(s)
        campaign = _campaign(s, user, status=CampaignStatus.PAUSED)
        step = campaign.steps[0]
        step.template.body = "Hi {{ first_name }}, {{ me.signature }}"  # #342: blocked
        fired = _enroll(s, campaign, next_action_at=NOW)
        sent = _message(s, fired, MessageStatus.SENT)  # the step already went to this one
        # Long enough ago for the mailbox's spacing.
        sent.scheduled_at = sent.sent_at = NOW - timedelta(days=60)
        blocked = _enroll(s, campaign, next_action_at=NOW)
        campaign_id, step_id, user_id = campaign.id, step.id, user.id
        fired_id, blocked_id = fired.id, blocked.id
    with session_scope(session_factory, write=True) as s:
        user = _user(s, user_id)
        campaign_service.resume(s, user, campaign_id)

    # Before: the template blocks the step, and the enrollment is parked with why.
    first = _tick(session_factory, user, sender)
    assert first.fired == [], first.decisions
    with session_scope(session_factory, write=True) as s:
        user = _user(s, user_id)
        row = _fresh(s, user, Enrollment, blocked_id)
        assert (row.next_action_at, row.not_sent_error) == (None, REMOVED_FIELD_BLOCK)
        step = _fresh(s, user, CampaignStep, step_id)
        _new_version(s, user, step, "Hello {{ first_name }}, fixed")
        campaign_service.pause(s, user, campaign_id)
        shown = service.preview(s, user, campaign_id, step_id, now=NOW)
        assert shown.released == 1
        assert fired_id not in {e.enrollment_id for e in shown.affected}
        service.adopt(s, user, campaign_id, step_id, fingerprint_seen=shown.fingerprint, now=NOW)

    # Paused: adoption makes it due, and nothing fires until the campaign resumes.
    assert _tick(session_factory, user, sender, NOW + timedelta(minutes=1)).fired == []
    with session_scope(session_factory, write=True) as s:
        user = _user(s, user_id)
        campaign_service.resume(s, user, campaign_id)
    later = NOW + timedelta(minutes=2)
    [(firing, _)] = _tick(session_factory, user, sender, later).fired
    assert firing.enrollment_id == blocked_id
    assert firing.body.startswith("Hello First") and firing.body.endswith("fixed")
    for minutes in (3, 4, 5):
        assert _tick(session_factory, user, sender, NOW + timedelta(minutes=minutes)).fired == []
    with session_scope(session_factory) as s:
        user = _user(s, user_id)
        mine = list(s.scalars(scoped(user, Message).where(Message.enrollment_id == fired_id)))
        assert [m.body_rendered for m in mine] == [OLD_TEXT]


# --- the API ---------------------------------------------------------------------------------


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _seed_api(app: FastAPI, *, owner: str = "local") -> tuple[int, int]:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session) if owner == "local" else factories.make_user(session)
        campaign = _campaign(session, user)
        _enroll(session, campaign, next_action_at=None, not_sent_error=REMOVED_FIELD_BLOCK)
        _new_version(session, user, campaign.steps[0], "Hello {{ first_name }}")
        return campaign.id, campaign.steps[0].id


async def test_the_api_previews_then_adopts_with_a_confirm(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    campaign_id, step_id = _seed_api(running_app)
    base = f"/api/v1/campaigns/{campaign_id}/steps/{step_id}"
    before = (await client.get(f"/api/v1/campaigns/{campaign_id}")).json()
    assert before["steps"][0]["newest_template_version"] == 2
    shown = (await client.get(f"{base}/adoption")).json()
    assert shown["refusal"] is None
    assert (shown["current"]["version"], shown["newest"]["version"]) == (1, 2)
    assert shown["released"] == 1 and shown["affected_total"] == 1

    unconfirmed = await client.post(
        f"{base}/adopt", json={"fingerprint": shown["fingerprint"]}, headers=CSRF
    )
    assert unconfirmed.status_code == 422
    stale = await client.post(
        f"{base}/adopt", json={"fingerprint": "0" * 64, "confirm": True}, headers=CSRF
    )
    assert stale.status_code == 409

    done = await client.post(
        f"{base}/adopt", json={"fingerprint": shown["fingerprint"], "confirm": True}, headers=CSRF
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert (body["from_version"], body["to_version"], body["released"]) == (1, 2, 1)
    step = body["campaign"]["steps"][0]
    assert step["template_version"] == 2 and step["newest_template_version"] is None
    assert step["template_adopted_from_version"] == 1
    assert step["template_adopted_at"] is not None and step["template_adopted_by"] is not None

    again = await client.post(
        f"{base}/adopt", json={"fingerprint": shown["fingerprint"], "confirm": True}, headers=CSRF
    )
    assert again.status_code == 409


async def test_the_api_answers_404_for_another_users_campaign(
    running_app: FastAPI, client: httpx.AsyncClient
) -> None:
    campaign_id, step_id = _seed_api(running_app, owner="other")
    base = f"/api/v1/campaigns/{campaign_id}/steps/{step_id}"
    assert (await client.get(f"{base}/adoption")).status_code == 404
    adopted = await client.post(
        f"{base}/adopt", json={"fingerprint": "0" * 64, "confirm": True}, headers=CSRF
    )
    assert adopted.status_code == 404


# --- the CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    yield factory
    engine.dispose()


def test_the_cli_shows_the_change_and_asks_first(cli_db: sessionmaker[Session]) -> None:
    with session_scope(cli_db, write=True) as session:
        user = _local(session)
        campaign = _campaign(session, user)
        _enroll(session, campaign, next_action_at=None, not_sent_error=REMOVED_FIELD_BLOCK)
        _new_version(session, user, campaign.steps[0], "Hello {{ first_name }}")
        campaign_id, step_id = campaign.id, campaign.steps[0].id
    runner = CliRunner()

    declined = runner.invoke(
        cli, ["campaigns", "adopt-template", str(campaign_id), "1"], input="n\n"
    )
    assert declined.exit_code == 1
    assert "v1 -> v2" in declined.output
    assert "+Hello {{ first_name }}" in declined.output
    assert "1 enrollments the step has not fired for" in declined.output
    assert "cancelled: step 1 still uses v1" in declined.output

    accepted = runner.invoke(
        cli, ["campaigns", "adopt-template", str(campaign_id), "1"], input="y\n"
    )
    assert accepted.exit_code == 0, accepted.output
    assert "now uses v2 (was v1); 1 parked enrollments are due again" in accepted.output
    with session_scope(cli_db) as session:
        user = _local(session)
        step = get_scoped(session, user, CampaignStep, step_id)
        assert step is not None and step.template_adopted_from_version == 1

    nothing = runner.invoke(cli, ["campaigns", "adopt-template", str(campaign_id), "1"])
    assert nothing.exit_code == 1
    assert "already uses the newest version (v2)" in nothing.output


def test_adopting_a_later_step_releases_nobody_parked_on_an_earlier_one(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(EMAIL, EMAIL))
    on_step_1 = _enroll(writer, campaign, next_action_at=None, not_sent_error=REMOVED_FIELD_BLOCK)
    _new_version(writer, user, campaign.steps[1], "Hello {{ first_name }}")
    before = _row(_fresh(writer, user, Enrollment, on_step_1.id))

    result = _adopt(writer, user, campaign, position=2)

    assert result.released == 0 and result.affected_total == 1  # it gets v2 later
    assert _changed(before, _row(_fresh(writer, user, Enrollment, on_step_1.id))) == {}


def test_a_due_enrollment_keeps_its_due_time(writer: Session) -> None:
    """Only a parked enrollment (no due time) is made due: one already waiting for a later
    time, such as a guard's re-check, keeps it."""
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    later = NOW + timedelta(days=1)
    waiting = _enroll(writer, campaign, next_action_at=later, not_sent_error=REMOVED_FIELD_BLOCK)
    _new_version(writer, user, campaign.steps[0], "Hello {{ first_name }}")
    before = _row(_fresh(writer, user, Enrollment, waiting.id))

    assert _adopt(writer, user, campaign).released == 0
    assert _changed(before, _row(_fresh(writer, user, Enrollment, waiting.id))) == {}


@pytest.mark.parametrize("mode", [StepMode.PREFILL, StepMode.AUTO_SEND])
def test_a_prefill_refused_as_too_long_is_released(writer: Session, mode: StepMode) -> None:
    """A ``too_long`` refusal is decided before the lock, the budget and any navigation, so
    no bubble is open: the shorter version makes it due again, and the claim lints it."""
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(LINKEDIN,), mode=mode)
    too_long = _enroll(
        writer,
        campaign,
        next_action_at=None,
        not_sent_error="too_long: the message takes too long to type",
        not_sent_count=1,
        not_sent_since=NOW - timedelta(hours=1),
    )
    not_typed = _enroll(
        writer, campaign, next_action_at=None, not_sent_error="not_typed: x", not_sent_count=1
    )
    _new_version(writer, user, campaign.steps[0], "Hi {{ first_name }}, shorter now")
    before = _row(_fresh(writer, user, Enrollment, not_typed.id))

    shown = service.preview(writer, user, campaign.id, campaign.steps[0].id, now=NOW)
    assert shown.released == 1
    assert _adopt(writer, user, campaign).released == 1

    row = _fresh(writer, user, Enrollment, too_long.id)
    assert (row.next_action_at, row.not_sent_error, row.not_sent_count, row.not_sent_since) == (
        NOW,
        None,
        0,
        None,
    )
    # A prefill that typed nothing waits for Try again (#445), never for a new version.
    assert _changed(before, _row(_fresh(writer, user, Enrollment, not_typed.id))) == {}


def test_adopt_renders_nothing_and_shares_the_previews_fingerprint(
    writer: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the write lock, adopt needs only the refusal and the fingerprint: the
    fingerprint is the same with and without the renders, and adopt renders nothing."""
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    _enroll(writer, campaign, next_action_at=NOW)
    _new_version(writer, user, step, "Hello {{ first_name }}")
    rendered = service.preview(writer, user, campaign.id, step.id, now=NOW)
    bare = service.preview(writer, user, campaign.id, step.id, now=NOW, render=False)
    assert rendered.samples and not bare.samples
    assert rendered.fingerprint == bare.fingerprint

    def no_render(*_: object, **__: object) -> object:
        raise AssertionError("adopt rendered the messages")

    monkeypatch.setattr(campaign_review, "_render_messages", no_render)
    result = service.adopt(
        writer, user, campaign.id, step.id, fingerprint_seen=rendered.fingerprint, now=NOW
    )
    assert result.to_version == 2


def test_a_campaign_status_change_since_the_preview_is_stale(writer: Session) -> None:
    user = factories.make_user(writer)
    campaign = _campaign(writer, user)
    step = campaign.steps[0]
    _new_version(writer, user, step, "Hello {{ first_name }}")
    shown = service.preview(writer, user, campaign.id, step.id, now=NOW)
    campaign_service.pause(writer, user, campaign.id)
    with pytest.raises(AdoptionStale):
        service.adopt(
            writer, user, campaign.id, step.id, fingerprint_seen=shown.fingerprint, now=NOW
        )
    assert _fresh(writer, user, CampaignStep, step.id).template_adopted_at is None


def test_an_enrollment_already_past_the_step_is_not_affected(writer: Session) -> None:
    """``current_step`` at or past the step, even with no message of it: never reached."""
    user = factories.make_user(writer)
    campaign = _campaign(writer, user, channels=(EMAIL, EMAIL))
    past = _enroll(
        writer, campaign, current_step=1, next_action_at=None, not_sent_error=REMOVED_FIELD_BLOCK
    )
    _new_version(writer, user, campaign.steps[0], "Hello {{ first_name }}")
    shown = service.preview(writer, user, campaign.id, campaign.steps[0].id, now=NOW)
    assert (shown.affected_total, shown.released) == (0, 0)
    assert past.id not in {e.enrollment_id for e in shown.affected}


def test_adopt_needs_a_writer_session(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory, write=True) as s:
        user = factories.make_user(s)
        campaign = _campaign(s, user)
        _new_version(s, user, campaign.steps[0], "Hello {{ first_name }}")
        ids = (user.id, campaign.id, campaign.steps[0].id)
    with session_scope(session_factory) as s:
        user = _user(s, ids[0])
        shown = service.preview(s, user, ids[1], ids[2], now=NOW)
        with pytest.raises(RuntimeError, match="writer session"):
            service.adopt(s, user, ids[1], ids[2], fingerprint_seen=shown.fingerprint, now=NOW)
