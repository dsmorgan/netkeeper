"""The review gate and the test send over the API (spec 11.8; #288, P3-09).

Every Gmail call goes to a :class:`FakeGmail` set as ``app.state.gmail_opener``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import factories
import httpx
import pytest
from campaign_fakes import ARMED_FOR_SEND, FakeSender, make_mailbox
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.gmail import GmailRateLimited
from netkeeper.campaigns.gmail_fake import FakeGmail
from netkeeper.crm import lists as list_service
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentStatus,
    HistoryCampaign,
    HistoryRecipient,
    Interaction,
    ListKind,
    Mailbox,
    MailboxStatus,
    Message,
    StepApproval,
    StepMode,
    Template,
    TemplateChannel,
    TestSend,
    User,
    UserKind,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, unscoped
from netkeeper.services import campaign_engine, campaign_review
from netkeeper.services import mailboxes as mailbox_service
from netkeeper.services.campaign_guards import Reason, check_enrollment
from netkeeper.services.campaign_sender import GmailSender

CSRF = {"X-Netkeeper-Client": "1"}
ARMED_FOR_DRAFTS: dict[str, Any] = {"armed_at": utcnow() - timedelta(days=1), "armed_by": "test"}
REQUIREMENTS = ("step_approvals", "test_sends", "lint")


def test_the_step_review_limits_are_pinned() -> None:
    assert (campaign_review.STEP_PAGE, campaign_review.STEP_PAGE_MAX) == (20, 50)
    assert campaign_review.APPROVE_MAX == 50


@dataclass
class Setup:
    factory: sessionmaker[Session]
    campaign_id: int
    mailbox_email: str
    enrollment_ids: list[int]
    contact_emails: list[str]
    step_ids: list[int]
    gmail: FakeGmail
    email_step_ids: list[int] = field(default_factory=list)
    opened: list[tuple[int, int]] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"/api/v1/campaigns/{self.campaign_id}"


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


def _build(
    app: FastAPI,
    *,
    people: int = 12,
    arm: dict[str, Any] | None = None,
    channels: tuple[TemplateChannel, ...] = (TemplateChannel.EMAIL, TemplateChannel.EMAIL),
    status: CampaignStatus = CampaignStatus.REVIEWING,
) -> Setup:
    factory: sessionmaker[Session] = app.state.session_factory
    with session_scope(factory, write=True) as session:
        user = _local(session)
        mailbox = make_mailbox(session, user, **(ARMED_FOR_SEND if arm is None else arm))
        campaign = factories.make_campaign(
            session, user, channels=channels, status=status, mailbox_id=mailbox.id
        )
        for step in campaign.steps:
            if step.channel is TemplateChannel.EMAIL:
                step.mode = StepMode.SEND
        emails, enrollment_ids = [], []
        for n in range(people):
            email = f"person{n}@contacts.example"
            contact = factories.make_contact(session, user, emails=[email])
            enrollment = factories.make_enrollment(
                session, campaign, contact, status=EnrollmentStatus.PENDING
            )
            emails.append(email)
            enrollment_ids.append(enrollment.id)
        session.flush()
        setup = Setup(
            factory,
            campaign.id,
            mailbox.email,
            enrollment_ids,
            emails,
            [s.id for s in campaign.steps],
            FakeGmail(mailbox.email, mailbox_id=mailbox.id),
            [s.id for s in campaign.steps if s.channel is TemplateChannel.EMAIL],
        )

    def opener(user_id: int, mailbox_id: int) -> FakeGmail:
        setup.opened.append((user_id, mailbox_id))
        return setup.gmail

    app.state.gmail_opener = opener
    return setup


async def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


async def _review(client: httpx.AsyncClient, s: Setup) -> Any:
    return await _ok(await client.get(f"{s.base}/review"))


async def _step_review(
    client: httpx.AsyncClient, s: Setup, step_id: int, *, offset: int = 0, limit: int = 20
) -> Any:
    return await _ok(
        await client.get(
            f"{s.base}/review/steps/{step_id}", params={"offset": offset, "limit": limit}
        )
    )


async def _approve_step(client: httpx.AsyncClient, s: Setup, step_id: int) -> Any:
    review = await _step_review(client, s, step_id)
    return await _ok(
        await client.post(
            f"{s.base}/review/steps/{step_id}/approve",
            json={"fingerprint": review["fingerprint"]},
            headers=CSRF,
        )
    )


async def _approve_steps(client: httpx.AsyncClient, s: Setup) -> None:
    for step_id in s.step_ids:
        await _approve_step(client, s, step_id)


async def _test_send_all(client: httpx.AsyncClient, s: Setup) -> None:
    for step_id in s.email_step_ids:
        await _ok(
            await client.post(f"{s.base}/review/test-send", json={"step_id": step_id}, headers=CSRF)
        )


async def _lint(client: httpx.AsyncClient, s: Setup) -> Any:
    return await _ok(await client.post(f"{s.base}/review/lint", headers=CSRF))


async def _complete(client: httpx.AsyncClient, s: Setup, *, skip: str | None = None) -> None:
    """Record every requirement but ``skip``."""
    if skip != "step_approvals":
        await _approve_steps(client, s)
    if skip != "test_sends":
        await _test_send_all(client, s)
    if skip != "lint":
        assert (await _lint(client, s))["clean"] is True


def _missing(body: Any) -> set[str]:
    return {m["requirement"] for m in body["missing"]}


def _campaign(s: Setup) -> tuple[CampaignStatus, Any, list[EnrollmentStatus]]:
    with session_scope(s.factory) as session:
        campaign = session.scalars(unscoped(select(Campaign))).one()
        statuses = list(
            session.scalars(unscoped(select(Enrollment.status).order_by(Enrollment.id)))
        )
        return campaign.status, campaign.approved_at, statuses


# --- activation ---------------------------------------------------------------------


async def test_a_campaign_with_every_requirement_recorded_activates(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    assert _missing(await _review(client, s)) == set()
    body = await _ok(await client.post(f"{s.base}/activate", headers=CSRF))
    assert body["status"] == "active"
    status, approved_at, enrollments = _campaign(s)
    assert status is CampaignStatus.ACTIVE and approved_at is not None
    assert set(enrollments) == {EnrollmentStatus.ACTIVE}


def _at(when: datetime) -> datetime:
    return when


def _starts_at(s: Setup) -> tuple[datetime | None, list[datetime | None]]:
    with session_scope(s.factory) as session:
        campaign = session.scalars(unscoped(select(Campaign))).one()
        due = list(
            session.scalars(unscoped(select(Enrollment.next_action_at).order_by(Enrollment.id)))
        )
        return campaign.starts_at, due


async def test_activation_without_a_start_defaults_to_the_next_tuesday_at_nine(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """#338: the API's default is the safe one, never "now"."""
    s = _build(running_app)
    await _complete(client, s)
    before = utcnow()
    await _ok(await client.post(f"{s.base}/activate", headers=CSRF))
    starts_at, due = _starts_at(s)
    assert starts_at is not None and starts_at > before
    with session_scope(s.factory) as session:
        zone = ZoneInfo(
            session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one().timezone
        )
    local = starts_at.astimezone(zone)
    assert (local.weekday(), local.hour, local.minute) == (1, 9, 0)
    assert set(due) == {starts_at}


async def test_activation_takes_an_explicit_start_and_sends_nothing_before_it(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    start = utcnow() + timedelta(days=2)
    await _ok(
        await client.post(f"{s.base}/activate", json={"starts_at": start.isoformat()}, headers=CSRF)
    )
    starts_at, due = _starts_at(s)
    assert starts_at == start
    assert set(due) == {starts_at}
    sender = FakeSender()
    for at in (utcnow(), start - timedelta(seconds=1)):
        results = campaign_engine.run_tick(
            s.factory, settings=running_app.state.settings, sender=sender, clock=partial(_at, at)
        )
        assert all(r.fired == [] for r in results)
    assert sender.firings == []
    with session_scope(s.factory) as session:
        assert session.scalar(unscoped(select(func.count(Message.id)))) == 0
    results = campaign_engine.run_tick(
        s.factory, settings=running_app.state.settings, sender=sender, clock=lambda: start
    )
    assert [len(r.fired) for r in results] == [1]


async def test_activation_refuses_a_start_with_no_time_zone(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    response = await client.post(
        f"{s.base}/activate", json={"starts_at": "2099-01-05T09:00:00"}, headers=CSRF
    )
    assert response.status_code == 422
    assert _campaign(s)[0] is CampaignStatus.REVIEWING


@pytest.mark.parametrize("requirement", REQUIREMENTS)
async def test_activation_is_refused_without_each_requirement(
    client: httpx.AsyncClient, running_app: FastAPI, requirement: str
) -> None:
    s = _build(running_app)
    await _complete(client, s, skip=requirement)
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    assert _missing(response.json()) == {requirement}
    assert response.json()["detail"].startswith("the review is not complete")
    status, approved_at, enrollments = _campaign(s)
    assert (status, approved_at) == (CampaignStatus.REVIEWING, None)
    assert set(enrollments) == {EnrollmentStatus.PENDING}


async def test_nothing_recorded_lists_every_requirement(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    body = response.json()
    assert _missing(body) == {"step_approvals", "test_sends", "lint"}  # no guards (#346)
    for requirement in ("step_approvals", "test_sends"):
        gap = next(m for m in body["missing"] if m["requirement"] == requirement)
        assert gap["step_positions"] == [1, 2]


@pytest.mark.parametrize("unapproved", [0, 1])
async def test_activation_is_refused_while_any_step_is_unapproved(
    client: httpx.AsyncClient, running_app: FastAPI, unapproved: int
) -> None:
    s = _build(running_app)
    await _complete(client, s, skip="step_approvals")
    for n, step_id in enumerate(s.step_ids):
        if n != unapproved:
            await _approve_step(client, s, step_id)
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    [gap] = response.json()["missing"]
    assert (gap["requirement"], gap["step_positions"]) == ("step_approvals", [unapproved + 1])
    status, approved_at, enrollments = _campaign(s)
    assert (status, approved_at) == (CampaignStatus.REVIEWING, None)
    assert set(enrollments) == {EnrollmentStatus.PENDING}


async def test_a_draft_campaign_is_not_activated(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, status=CampaignStatus.DRAFT)
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    assert "reviewing" in _missing(response.json())
    assert _campaign(s)[0] is CampaignStatus.DRAFT


async def test_activation_needs_the_csrf_header(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    assert (await client.post(f"{s.base}/activate")).status_code == 403
    assert _campaign(s)[0] is CampaignStatus.REVIEWING


# --- invalidation -------------------------------------------------------------------


def _edit(s: Setup, change: str) -> None:
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        campaign = session.scalars(unscoped(select(Campaign))).one()
        steps = sorted(campaign.steps, key=lambda step: step.position)
        if change == "template":
            template = get_scoped(session, user, Template, steps[0].template_id)
            assert template is not None
            template.body = "Hello again {{ first_name }}"
        elif change == "template_version":
            old = get_scoped(session, user, Template, steps[0].template_id)
            assert old is not None
            new = Template(
                user_id=user.id,
                name=old.name,
                channel=old.channel,
                subject=old.subject,
                body=old.body,
                lint_json=[],
                version=2,
                previous_id=old.id,
            )
            session.add(new)
            session.flush()
            steps[0].template_id = new.id
        elif change == "step_mode":
            steps[1].mode = StepMode.DRAFT
        elif change == "step_added":
            template = Template(
                user_id=user.id, name="step 3", channel=TemplateChannel.EMAIL,
                subject="Third", body="Hi {{ first_name }}", lint_json=[],
            )  # fmt: skip
            campaign.steps.append(
                CampaignStep(
                    user_id=user.id, position=3, channel=TemplateChannel.EMAIL,
                    template=template, delay_days=7, mode=StepMode.SEND,
                )
            )  # fmt: skip
        elif change == "enrolled":
            contact = factories.make_contact(session, user, emails=["late@contacts.example"])
            factories.make_enrollment(session, campaign, contact, status=EnrollmentStatus.PENDING)
        elif change == "removed":
            first = session.scalars(unscoped(select(Enrollment).order_by(Enrollment.id))).first()
            assert first is not None
            first.status = EnrollmentStatus.REMOVED
        else:
            raise AssertionError(change)


@pytest.mark.parametrize(
    ("change", "undone", "steps"),
    [
        ("template", {"step_approvals", "test_sends", "lint"}, [1]),
        ("template_version", {"step_approvals", "test_sends", "lint"}, [1]),
        ("step_mode", {"step_approvals", "test_sends", "lint"}, [2]),
        ("step_added", {"step_approvals", "test_sends", "lint"}, [3]),
    ],
)
async def test_a_change_after_the_review_undoes_the_affected_approvals(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    change: str,
    undone: set[str],
    steps: list[int] | None,
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    _edit(s, change)
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    assert _missing(response.json()) == undone
    if steps is not None:
        for requirement in ("step_approvals", "test_sends"):
            gap = next(m for m in response.json()["missing"] if m["requirement"] == requirement)
            assert gap["step_positions"] == steps
    assert _campaign(s)[0] is CampaignStatus.REVIEWING


@pytest.mark.parametrize("change", ["enrolled", "removed"])
async def test_an_audience_change_after_the_review_undoes_nothing(
    client: httpx.AsyncClient, running_app: FastAPI, change: str
) -> None:
    """#346: no record counts for the audience; a step approval covers contacts enrolled
    later (#339), and the guards apply again at every fire."""
    s = _build(running_app)
    await _complete(client, s)
    _edit(s, change)
    assert _missing(await _review(client, s)) == set()
    assert (await client.post(f"{s.base}/activate", headers=CSRF)).status_code == 200


async def test_a_contact_a_guard_excludes_after_the_review_does_not_block_activation(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """#346: the guard summary is informational, so a change in it gates nothing. The
    contact is not sent anything: see the engine test in test_campaign_review_steps."""
    s = _build(running_app)
    await _complete(client, s)
    before = await _review(client, s)
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        contact_id = session.scalars(unscoped(select(Enrollment.contact_id))).first()
        assert contact_id is not None
        contact = get_scoped(session, user, Contact, contact_id)
        assert contact is not None
        contact.do_not_contact = True
    after = await _review(client, s)
    assert (before["guard_summary"], after["guard_summary"]) == (
        "12 will send, none skipped",
        "11 will send, 1 skipped (1 do-not-contact)",
    )
    assert _missing(after) == set()
    assert (await client.post(f"{s.base}/activate", headers=CSRF)).status_code == 200


async def test_the_guard_acknowledgement_is_gone(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    review = await _review(client, s)
    assert "guards_acknowledged" not in review
    response = await client.post(
        f"{s.base}/review/guards/acknowledge",
        json={"summary": review["guard_summary"], "audience_fingerprint": "x"},
        headers=CSRF,
    )
    assert response.status_code in (404, 405)


async def test_the_guard_details_list_each_skipped_contact_with_every_reason(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=4)
    _block(s, s.enrollment_ids[0], "do_not_contact")
    _block(s, s.enrollment_ids[0], "no_email")
    _block(s, s.enrollment_ids[1], "no_email")
    with session_scope(s.factory) as session:
        first, second = (
            session.scalars(unscoped(select(Enrollment.contact_id).where(Enrollment.id == e))).one()
            for e in s.enrollment_ids[:2]
        )
    details = await _ok(await client.get(f"{s.base}/review/guards"))
    summary = "2 will send, 2 skipped (1 do-not-contact, 1 no email)"
    assert details["summary"] == summary == (await _review(client, s))["guard_summary"]
    assert (details["will_send"], details["not_enrolled"]) == (2, 0)
    assert [(c["contact_id"], c["reasons"]) for c in details["skipped"]] == [
        (first, ["do-not-contact", "no email"]),  # every reason, not only the counted one
        (second, ["no email"]),
    ]
    assert all(c["name"] for c in details["skipped"])
    assert details["prior_contact_note"] is None


async def test_the_review_notes_who_the_old_tool_emailed(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """#65: informational, over the contacts that will send, and gating nothing."""
    s = _build(running_app, people=3)
    _block(s, s.enrollment_ids[2], "do_not_contact")
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        old = HistoryCampaign(
            user_id=user.id, name="Spring", started_on=date(2026, 3, 2), source_sha256="0" * 64
        )
        session.add(old)
        session.flush()
        for email in (s.contact_emails[0], s.contact_emails[2]):  # [2] is skipped
            session.add(HistoryRecipient(user_id=user.id, history_campaign_id=old.id, email=email))
    review = await _review(client, s)
    note = "1 was emailed by the old tool; last on 2026-03-02"
    assert review["prior_contact_note"] == note
    assert (await _ok(await client.get(f"{s.base}/review/guards")))["prior_contact_note"] == note
    await _complete(client, s)
    assert _missing(await _review(client, s)) == set()


async def test_a_source_contact_no_guard_skips_but_not_enrolled_is_counted_apart(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=2)
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        campaign = session.scalars(unscoped(select(Campaign))).one()
        members = [
            *session.scalars(unscoped(select(Enrollment.contact_id))),
            factories.make_contact(session, user, emails=["late@contacts.example"]).id,
        ]
        source = list_service.create_list(session, user, "Source", ListKind.STATIC)
        list_service.add_members(session, user, source.id, members)
        campaign.source_list_id = source.id
    details = await _ok(await client.get(f"{s.base}/review/guards"))
    assert details["summary"] == "2 will send, none skipped, 1 not enrolled"
    assert (details["will_send"], details["not_enrolled"], details["skipped"]) == (2, 1, [])


async def test_a_template_edited_after_the_review_was_shown_refuses_the_approval(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    review = await _step_review(client, s, s.step_ids[0])
    _edit(s, "template")
    response = await client.post(
        f"{s.base}/review/steps/{s.step_ids[0]}/approve",
        json={"fingerprint": review["fingerprint"]},
        headers=CSRF,
    )
    assert response.status_code == 409
    assert "changed since it was shown" in response.json()["detail"]
    assert response.json()["code"] == "stale"  # show it again and retry (#299)
    with session_scope(s.factory) as session:
        assert session.scalar(unscoped(select(func.count()).select_from(StepApproval))) == 0


async def test_a_template_edit_after_the_approval_undoes_it(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The approval covers the step only while its template is the one approved (#339)."""
    s = _build(running_app)
    await _approve_steps(client, s)
    assert (await _step_review(client, s, s.step_ids[0]))["approved"] is True
    _edit(s, "template")
    review = await _step_review(client, s, s.step_ids[0])
    assert review["approved"] is False
    assert not any(m["approved"] for m in review["messages"])
    assert (await _step_review(client, s, s.step_ids[1]))["approved"] is True
    gaps = (await _review(client, s))["missing"]
    [gap] = [m for m in gaps if m["requirement"] == "step_approvals"]
    assert gap["step_positions"] == [1]
    await _approve_step(client, s, s.step_ids[0])  # approved again, for the new text
    assert "step_approvals" not in _missing(await _review(client, s))


async def test_a_step_of_another_campaign_is_404(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    with session_scope(s.factory, write=True) as session:
        other = factories.make_campaign(session, _local(session), status=CampaignStatus.REVIEWING)
        other_step = other.steps[0].id
    assert (await client.get(f"{s.base}/review/steps/{other_step}")).status_code == 404
    response = await client.post(
        f"{s.base}/review/steps/{other_step}/approve", json={"fingerprint": "x"}, headers=CSRF
    )
    assert response.status_code == 404


async def test_an_approval_on_a_campaign_no_longer_reviewing_is_a_real_refusal(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """Only a stale fingerprint answers ``code: stale``; a campaign moved on is not one
    that showing the step again can fix (#299)."""
    s = _build(running_app)
    step = await _step_review(client, s, s.step_ids[0])
    with session_scope(s.factory, write=True) as session:
        campaign = session.scalars(unscoped(select(Campaign))).one()
        campaign.status = CampaignStatus.DRAFT
    approve = await client.post(
        f"{s.base}/review/steps/{s.step_ids[0]}/approve",
        json={"fingerprint": step["fingerprint"]},
        headers=CSRF,
    )
    assert approve.status_code == 409
    assert "not reviewing" in approve.json()["detail"]
    assert approve.json().get("code") is None


@pytest.mark.parametrize("status", [MailboxStatus.REAUTH_REQUIRED, MailboxStatus.DISABLED])
async def test_a_mailbox_that_broke_after_its_test_send_blocks_activation(
    client: httpx.AsyncClient, running_app: FastAPI, status: MailboxStatus
) -> None:
    """The gate reads the mailbox's health, not only the send path (#299)."""
    s = _build(running_app)
    await _complete(client, s)
    assert _missing(await _review(client, s)) == set()
    with session_scope(s.factory, write=True) as session:
        mailbox = session.scalars(unscoped(select(Mailbox))).one()
        mailbox.status = status
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    [gap] = response.json()["missing"]
    assert gap["requirement"] == "mailbox"
    assert gap["detail"] == f"{s.mailbox_email} is {status}, not ok"
    status_now, approved_at, enrollments = _campaign(s)
    assert (status_now, approved_at) == (CampaignStatus.REVIEWING, None)
    assert set(enrollments) == {EnrollmentStatus.PENDING}
    with session_scope(s.factory, write=True) as session:
        session.scalars(unscoped(select(Mailbox))).one().status = MailboxStatus.OK
    assert (await client.post(f"{s.base}/activate", headers=CSRF)).status_code == 200


async def test_an_email_campaign_with_no_mailbox_is_missing_one(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    with session_scope(s.factory, write=True) as session:
        # mailbox_id is locked outside draft, so the row is changed beneath the ORM.
        session.execute(
            unscoped(update(Campaign)).values(mailbox_id=None),
            execution_options={"synchronize_session": False},
        )
    gaps = [m for m in (await _review(client, s))["missing"] if m["requirement"] == "mailbox"]
    assert [g["detail"] for g in gaps] == ["the campaign has email steps and no mailbox"]


async def test_a_linkedin_only_campaign_needs_no_mailbox_health(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, channels=(TemplateChannel.LINKEDIN,))
    with session_scope(s.factory, write=True) as session:
        session.scalars(unscoped(select(Mailbox))).one().status = MailboxStatus.DISABLED
    assert "mailbox" not in _missing(await _review(client, s))


# --- the step review: paging, blocked messages, lint, the transition ------------------


async def test_the_step_review_pages_through_every_message(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    first = await _step_review(client, s, s.step_ids[0], limit=5)
    assert (first["total"], first["offset"], first["position"]) == (12, 0, 1)
    assert first["per_message"] is False and first["approved"] is False
    assert first["blocked"] == []
    pages = [first["messages"]]
    for offset in (5, 10):
        page = await _step_review(client, s, s.step_ids[0], offset=offset, limit=5)
        assert page["fingerprint"] == first["fingerprint"]
        pages.append(page["messages"])
    assert [len(p) for p in pages] == [5, 5, 2]
    seen = [m["enrollment_id"] for page in pages for m in page]
    assert seen == s.enrollment_ids  # every contact, once, in order
    message = pages[0][0]
    assert message["subject"] == "Hello" and message["body"].startswith("Hi First")
    assert message["to_address"] == s.contact_emails[0]
    assert message["blocked"] is None and message["approved"] is False
    past = await _step_review(client, s, s.step_ids[0], offset=12)
    assert (past["total"], past["messages"]) == (12, [])
    too_many = await client.get(f"{s.base}/review/steps/{s.step_ids[0]}", params={"limit": 51})
    assert too_many.status_code == 422


async def test_a_step_approval_covers_contacts_enrolled_after_it(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    _edit(s, "enrolled")
    review = await _step_review(client, s, s.step_ids[0], limit=50)
    assert review["approved"] is True and review["total"] == 13
    assert all(m["approved"] for m in review["messages"])
    assert _missing(await _review(client, s)) == set()
    assert (await client.post(f"{s.base}/activate", headers=CSRF)).status_code == 200


def _block(s: Setup, enrollment_id: int, how: str) -> None:
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        if how == "do_not_contact":
            contact.do_not_contact = True
        elif how == "no_email":
            contact.emails.clear()
        else:
            raise AssertionError(how)


async def test_blocked_messages_are_listed_apart_and_never_approved(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=4)
    _block(s, s.enrollment_ids[1], "do_not_contact")
    _block(s, s.enrollment_ids[2], "no_email")
    review = await _step_review(client, s, s.step_ids[0])
    assert review["total"] == 2
    assert [m["enrollment_id"] for m in review["messages"]] == [
        s.enrollment_ids[0],
        s.enrollment_ids[3],
    ]
    blocked = {m["enrollment_id"]: m["blocked"] for m in review["blocked"]}
    assert blocked == {
        s.enrollment_ids[1]: "excluded by a guard: do-not-contact",
        s.enrollment_ids[2]: "excluded by a guard: no email",
    }
    await _approve_steps(client, s)  # a step with blocked messages is approved for the rest
    review = await _step_review(client, s, s.step_ids[0])
    assert review["approved"] is True
    assert all(m["approved"] for m in review["messages"])
    assert not any(m["approved"] for m in review["blocked"])


async def test_lint_errors_are_answered_and_not_recorded(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        step = session.scalars(unscoped(select(CampaignStep).order_by(CampaignStep.id))).first()
        assert step is not None
        template = get_scoped(session, user, Template, step.template_id)
        assert template is not None
        template.subject = None  # an email with no subject is a lint error
    body = await _lint(client, s)
    assert body["clean"] is False
    assert body["steps"][0]["errors"] and not body["steps"][1]["errors"]
    assert "lint" in _missing(await _review(client, s))


@pytest.mark.parametrize("what", ["no_steps", "nobody"])
async def test_review_starts_only_with_a_step_and_an_audience(
    client: httpx.AsyncClient, running_app: FastAPI, what: str
) -> None:
    s = _build(
        running_app,
        status=CampaignStatus.DRAFT,
        people=0 if what == "nobody" else 2,
        channels=() if what == "no_steps" else (TemplateChannel.EMAIL,),
    )
    assert (await client.post(f"{s.base}/review/start", headers=CSRF)).status_code == 409
    assert _campaign(s)[0] is CampaignStatus.DRAFT


async def test_review_starts_from_draft(client: httpx.AsyncClient, running_app: FastAPI) -> None:
    s = _build(running_app, status=CampaignStatus.DRAFT)
    body = await _ok(await client.post(f"{s.base}/review/start", headers=CSRF))
    assert body["status"] == "reviewing"


# --- personal_line: each message approved on its own (#339) --------------------------


def _personal_line(s: Setup, position: int = 1) -> int:
    """Step ``position``'s template made to name ``{{ personal_line }}``; its step id."""
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        step = session.scalars(
            unscoped(select(CampaignStep)).where(CampaignStep.position == position)
        ).one()
        template = get_scoped(session, user, Template, step.template_id)
        assert template is not None
        template.body = "Hi {{ first_name }}. {{ personal_line }}"
        return step.id


async def _approve_messages(
    client: httpx.AsyncClient, s: Setup, step_id: int, messages: list[Any]
) -> httpx.Response:
    return await client.post(
        f"{s.base}/review/steps/{step_id}/messages/approve",
        json={
            "messages": [
                {"enrollment_id": m["enrollment_id"], "fingerprint": m["fingerprint"]}
                for m in messages
            ]
        },
        headers=CSRF,
    )


async def test_a_personal_line_step_cannot_be_approved_as_a_whole(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=3)
    step_id = _personal_line(s)
    review = await _step_review(client, s, step_id)
    assert review["per_message"] is True and review["approved"] is False
    assert (review["total"], review["unapproved"]) == (3, 3)
    response = await client.post(
        f"{s.base}/review/steps/{step_id}/approve",
        json={"fingerprint": review["fingerprint"]},
        headers=CSRF,
    )
    assert response.status_code == 409
    assert "approved on its own" in response.json()["detail"]
    assert response.json().get("code") is None  # a real refusal, not a stale one
    with session_scope(s.factory) as session:
        assert session.scalar(unscoped(select(func.count()).select_from(StepApproval))) == 0


async def test_a_personal_line_step_needs_every_message_approved_to_activate(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=3)
    step_id = _personal_line(s)
    await _approve_step(client, s, s.step_ids[1])
    await _complete(client, s, skip="step_approvals")
    review = await _step_review(client, s, step_id)
    await _ok(await _approve_messages(client, s, step_id, review["messages"][:2]))

    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    [gap] = response.json()["missing"]
    assert gap["requirement"] == "message_approvals"
    assert (gap["enrollment_ids"], gap["step_positions"]) == ([s.enrollment_ids[2]], [1])
    assert _campaign(s)[0] is CampaignStatus.REVIEWING

    review = await _step_review(client, s, step_id)
    assert [m["approved"] for m in review["messages"]] == [True, True, False]
    assert review["unapproved"] == 1
    await _ok(await _approve_messages(client, s, step_id, review["messages"][2:]))
    assert (await client.post(f"{s.base}/activate", headers=CSRF)).status_code == 200


async def test_editing_a_contact_undoes_its_personal_line_message_approval(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=2)
    step_id = _personal_line(s)
    review = await _step_review(client, s, step_id)
    await _ok(await _approve_messages(client, s, step_id, review["messages"]))
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        enrollment = get_scoped(session, user, Enrollment, s.enrollment_ids[0])
        assert enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        contact.preferred_name = "Somebody Else"
    gaps = (await _review(client, s))["missing"]
    [gap] = [m for m in gaps if m["requirement"] == "message_approvals"]
    assert gap["enrollment_ids"] == [s.enrollment_ids[0]]
    stale = await _approve_messages(client, s, step_id, review["messages"][:1])
    assert stale.status_code == 409 and stale.json()["code"] == "stale"


async def test_a_changed_personal_line_undoes_its_message_approval(
    client: httpx.AsyncClient, running_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The personal line a message renders with feeds its fingerprint, so a line written
    or edited after the approval undoes it (#339 review)."""
    s = _build(running_app, people=2)
    step_id = _personal_line(s)
    review = await _step_review(client, s, step_id)
    await _ok(await _approve_messages(client, s, step_id, review["messages"]))
    assert "message_approvals" not in _missing(await _review(client, s))
    edited = s.enrollment_ids[0]

    def line(enrollment_id: int, step: CampaignStep) -> str | None:
        return "Loved your talk on kettles." if enrollment_id == edited else None

    monkeypatch.setattr(campaign_review, "personal_line_for", line)
    gaps = (await _review(client, s))["missing"]
    [gap] = [m for m in gaps if m["requirement"] == "message_approvals"]
    assert gap["enrollment_ids"] == [edited]
    again = await _step_review(client, s, step_id)
    assert "Loved your talk on kettles." in again["messages"][0]["body"]
    assert [m["approved"] for m in again["messages"]] == [False, True]
    stale = await _approve_messages(client, s, step_id, review["messages"][:1])
    assert stale.status_code == 409 and stale.json()["code"] == "stale"
    await _ok(await _approve_messages(client, s, step_id, again["messages"][:1]))
    assert "message_approvals" not in _missing(await _review(client, s))


async def test_approving_a_step_again_keeps_one_whole_step_row(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _approve_step(client, s, s.step_ids[0])
    _edit(s, "template")
    await _approve_step(client, s, s.step_ids[0])  # approved again, in place
    with session_scope(s.factory) as session:
        rows = session.scalars(
            unscoped(select(StepApproval)).where(StepApproval.step_id == s.step_ids[0])
        ).all()
        assert [r.enrollment_id for r in rows] == [None]
    assert (await _step_review(client, s, s.step_ids[0]))["approved"] is True


async def test_a_template_edit_undoes_personal_line_message_approvals(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=2)
    step_id = _personal_line(s)
    review = await _step_review(client, s, step_id)
    await _ok(await _approve_messages(client, s, step_id, review["messages"]))
    with session_scope(s.factory, write=True) as session:
        step = session.scalars(
            unscoped(select(CampaignStep)).where(CampaignStep.id == step_id)
        ).one()
        template = get_scoped(session, _local(session), Template, step.template_id)
        assert template is not None
        template.body = "Hello {{ first_name }}. {{ personal_line }}"
    gaps = (await _review(client, s))["missing"]
    [gap] = [m for m in gaps if m["requirement"] == "message_approvals"]
    assert gap["enrollment_ids"] == s.enrollment_ids


async def test_messages_of_a_step_approved_as_a_whole_are_not_approved_one_by_one(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=2)
    review = await _step_review(client, s, s.step_ids[0])
    response = await _approve_messages(client, s, s.step_ids[0], review["messages"])
    assert response.status_code == 409
    assert "approved as a whole" in response.json()["detail"]


async def test_a_message_of_an_enrollment_no_longer_pending_is_not_approved(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=2)
    step_id = _personal_line(s)
    review = await _step_review(client, s, step_id)
    with session_scope(s.factory, write=True) as session:
        enrollment = get_scoped(session, _local(session), Enrollment, s.enrollment_ids[0])
        assert enrollment is not None
        enrollment.status = EnrollmentStatus.REMOVED
    response = await _approve_messages(client, s, step_id, review["messages"][:1])
    assert response.status_code == 409
    assert "not pending" in response.json()["detail"]
    assert response.json().get("code") is None
    with session_scope(s.factory) as session:
        assert session.scalar(unscoped(select(func.count()).select_from(StepApproval))) == 0


# --- the test send ------------------------------------------------------------------


async def test_a_test_send_goes_only_to_the_mailboxs_own_address(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=1)
    body = await _ok(
        await client.post(
            f"{s.base}/review/test-send",
            json={"step_id": s.step_ids[0], "enrollment_id": s.enrollment_ids[0]},
            headers=CSRF,
        )
    )
    assert body["to_address"] == s.mailbox_email
    [sent] = s.gmail.sent()
    raw = s.gmail.raw(sent.id)
    assert raw["To"] == s.mailbox_email
    assert raw["Subject"] == "[Test] Hello"
    assert "First" in raw.get_content()  # rendered for the enrollment's contact
    headers = " ".join(f"{k}: {v}" for k, v in raw.items())
    assert s.contact_emails[0] not in headers
    assert [method for method, _ in s.gmail.calls] == ["messages.send"]


async def test_a_mailbox_armed_for_drafts_gets_a_test_draft_and_never_a_send(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """#304: armed for drafts only, the test is a Gmail draft to the mailbox itself, in
    no thread, and ``messages.send`` is never called (#277)."""
    s = _build(running_app, people=1, arm=ARMED_FOR_DRAFTS)
    body = await _ok(
        await client.post(
            f"{s.base}/review/test-send",
            json={"step_id": s.step_ids[0], "enrollment_id": s.enrollment_ids[0]},
            headers=CSRF,
        )
    )
    assert (body["to_address"], body["drafted"]) == (s.mailbox_email, True)
    assert [method for method, _ in s.gmail.calls] == ["drafts.create"]
    assert s.gmail.sent() == []
    [(draft_id, ref)] = s.gmail.drafts().items()
    assert ref.thread_id == ref.id  # a thread of its own
    raw = s.gmail.raw(ref.id)
    assert raw["To"] == s.mailbox_email
    assert raw["Subject"] == "[Test] Hello"
    assert "First" in raw.get_content()
    assert s.contact_emails[0] not in " ".join(f"{k}: {v}" for k, v in raw.items())
    with session_scope(s.factory) as session:
        row = session.scalars(unscoped(select(TestSend))).one()
        assert (row.gmail_draft_id, row.gmail_message_id) == (draft_id, ref.id)
        assert row.rfc822_message_id == raw["Message-ID"]
    review = await _review(client, s)
    [untested] = [m for m in review["missing"] if m["requirement"] == "test_sends"]
    assert untested["step_positions"] == [2]  # step 1's test draft counts


@pytest.mark.parametrize("arm", [ARMED_FOR_SEND, ARMED_FOR_DRAFTS], ids=["sent", "drafted"])
async def test_a_test_send_is_never_counted_and_never_advances_an_enrollment(
    client: httpx.AsyncClient, running_app: FastAPI, arm: dict[str, Any]
) -> None:
    s = _build(running_app, people=1, arm=arm)
    await _test_send_all(client, s)
    tests = len(s.gmail.sent()) if arm is ARMED_FOR_SEND else len(s.gmail.drafts())
    assert tests == 2
    now = utcnow()
    with session_scope(s.factory) as session:
        user = _local(session)
        campaign = session.scalars(unscoped(select(Campaign))).one()
        for table in (Message, Interaction):
            assert session.scalar(unscoped(select(func.count()).select_from(table))) == 0
        assert session.scalar(unscoped(select(func.count()).select_from(TestSend))) == 2
        enrollment = session.scalars(unscoped(select(Enrollment))).one()
        assert (enrollment.status, enrollment.current_step, enrollment.next_action_at) == (
            EnrollmentStatus.PENDING,
            None,
            None,
        )
        day = timedelta(days=1)
        assert campaign.mailbox_id is not None
        assert (
            campaign_engine.mailbox_count(session, user, campaign.mailbox_id, now - day, now + day)
            == 0
        )
        assert campaign_engine.campaign_count(session, user, campaign.id, now - day, now + day) == 0
        [verdict] = check_enrollment(session, user, campaign, [enrollment.contact_id], now=now)
        assert Reason.CONTACTED_RECENTLY not in verdict.reasons


async def test_a_test_send_is_refused_on_a_disarmed_mailbox(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, arm={})
    response = await client.post(
        f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
    )
    assert response.status_code == 409
    assert "is not armed;" in response.json()["detail"]  # the first check
    assert (s.opened, s.gmail.calls) == ([], [])
    with session_scope(s.factory) as session:
        assert session.scalar(unscoped(select(func.count()).select_from(TestSend))) == 0


async def test_a_linkedin_step_has_no_test_send(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, channels=(TemplateChannel.EMAIL, TemplateChannel.LINKEDIN))
    response = await client.post(
        f"{s.base}/review/test-send", json={"step_id": s.step_ids[1]}, headers=CSRF
    )
    assert response.status_code == 409
    assert s.gmail.calls == []
    await _complete(client, s)  # the LinkedIn step needs none
    assert (await client.post(f"{s.base}/activate", headers=CSRF)).status_code == 200


async def test_a_test_send_gmail_refused_records_nothing(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)

    def refuse(*_: Any, **__: Any) -> Any:
        raise GmailRateLimited("slow down", code="rateLimitExceeded")

    s.gmail.send = refuse  # type: ignore[method-assign]
    response = await client.post(
        f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
    )
    assert response.status_code == 502
    with session_scope(s.factory) as session:
        assert session.scalar(unscoped(select(func.count()).select_from(TestSend))) == 0


# --- scope --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/review", None),
        ("POST", "/review/start", None),
        ("GET", "/review/steps/{step}", None),
        ("POST", "/review/steps/{step}/approve", {"fingerprint": "x"}),
        (
            "POST",
            "/review/steps/{step}/messages/approve",
            {"messages": [{"enrollment_id": 1, "fingerprint": "x"}]},
        ),
        ("POST", "/review/lint", None),
        ("GET", "/review/guards", None),
        ("POST", "/review/test-send", {"step_id": 1}),
        ("POST", "/activate", None),
    ],
)
async def test_another_users_campaign_is_404(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(factory, write=True) as session:
        other = factories.make_user(session)
        campaign = factories.make_campaign(session, other, status=CampaignStatus.REVIEWING)
        contact = factories.make_contact(session, other, emails=["them@contacts.example"])
        factories.make_enrollment(session, campaign, contact, status=EnrollmentStatus.PENDING)
        campaign_id, step_id = campaign.id, campaign.steps[0].id
    running_app.state.gmail_opener = lambda *_: pytest.fail("no Gmail for another user")
    response = await client.request(
        method,
        f"/api/v1/campaigns/{campaign_id}{path.format(step=step_id)}",
        json=body,
        headers=CSRF,
    )
    assert response.status_code == 404
    with session_scope(factory) as session:
        assert session.scalars(unscoped(select(Campaign.status))).one() is (
            CampaignStatus.REVIEWING
        )


def _change_arming_after_prepare(
    s: Setup, monkeypatch: pytest.MonkeyPatch, then: dict[str, Any]
) -> None:
    """Change the mailbox's arming between the first check and the Gmail call."""
    prepare = campaign_review.prepare_test_send

    def prepare_then_change(*args: Any, **kwargs: Any) -> campaign_review.TestSendPlan:
        plan = prepare(*args, **kwargs)
        with session_scope(s.factory, write=True) as session:
            mailbox = get_scoped(session, _local(session), Mailbox, plan.mailbox_id)
            assert mailbox is not None
            for name, value in then.items():
                setattr(mailbox, name, value)
        return plan

    monkeypatch.setattr(campaign_review, "prepare_test_send", prepare_then_change)


@pytest.mark.parametrize("arm", [ARMED_FOR_SEND, ARMED_FOR_DRAFTS], ids=["send", "drafts"])
async def test_a_disarm_after_the_first_check_sends_and_drafts_nothing(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    arm: dict[str, Any],
) -> None:
    """The recheck just before the Gmail call: disarmed after the test was prepared, it
    makes no Gmail call and records nothing."""
    s = _build(running_app, arm=arm)
    _change_arming_after_prepare(
        s, monkeypatch, {"armed_at": None, "send_armed_at": None, "armed_by": None}
    )
    response = await client.post(
        f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "the mailbox is no longer armed"
    assert s.gmail.calls == []
    with session_scope(s.factory) as session:
        assert session.scalar(unscoped(select(func.count()).select_from(TestSend))) == 0


@pytest.mark.parametrize(
    ("arm", "then", "method"),
    [
        pytest.param(ARMED_FOR_SEND, {"send_armed_at": None}, "drafts.create", id="send-to-drafts"),
        pytest.param(
            ARMED_FOR_DRAFTS,
            {"send_armed_at": utcnow(), "message_id_verified_at": utcnow()},
            "messages.send",
            id="drafts-to-send",
        ),
    ],
)
async def test_an_arming_changed_after_the_first_check_decides_the_test(
    client: httpx.AsyncClient,
    running_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    arm: dict[str, Any],
    then: dict[str, Any],
    method: str,
) -> None:
    """The arming read just before the Gmail call decides: taken back to drafts, the test
    is only drafted, never sent; armed to send meanwhile, it is sent, as the person now
    armed it to, and only ever to the mailbox itself."""
    s = _build(running_app, arm=arm)
    _change_arming_after_prepare(s, monkeypatch, then)
    body = await _ok(
        await client.post(
            f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
        )
    )
    assert [m for m, _ in s.gmail.calls] == [method]
    assert body["drafted"] is (method == "drafts.create")
    assert body["to_address"] == s.mailbox_email
    with session_scope(s.factory) as session:
        row = session.scalars(unscoped(select(TestSend))).one()
        assert (row.gmail_draft_id is not None) is (method == "drafts.create")


@pytest.mark.parametrize("edit", ["renamed", "new_email"])
async def test_editing_a_contact_keeps_the_step_approval(
    client: httpx.AsyncClient, running_app: FastAPI, edit: str
) -> None:
    """A step approval covers the step's messages as they render later (#339)."""
    s = _build(running_app)
    await _complete(client, s)
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        enrollment = get_scoped(session, user, Enrollment, s.enrollment_ids[0])
        assert enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        if edit == "renamed":
            contact.preferred_name = "Somebody Else"
        else:
            contact.emails[0].email = "moved@contacts.example"
    review = await _step_review(client, s, s.step_ids[0])
    assert review["messages"][0]["approved"] is True
    assert (await client.post(f"{s.base}/activate", headers=CSRF)).status_code == 200


async def test_a_test_send_renders_for_the_first_pending_enrollment(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    review = await _step_review(client, s, s.step_ids[0])
    await _ok(
        await client.post(
            f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
        )
    )
    [sent] = s.gmail.sent()
    first = review["messages"][0]
    assert first["enrollment_id"] == s.enrollment_ids[0]
    assert first["body"] in s.gmail.raw(sent.id).get_content()


# --- a fresh mailbox, end to end (#304) --------------------------------------------------


def _drafts_check(s: Setup, app: FastAPI, *, at: datetime) -> GmailSender:
    """The drafts check ``serve`` runs each tick: :meth:`GmailSender.reconcile`."""
    sender = GmailSender(
        s.factory,
        opener=lambda user_id, mailbox_id: s.gmail,
        clock=lambda: at,
        drafts_every=timedelta(0),
        replies_every=timedelta(days=3650),
    )
    with session_scope(s.factory) as session:
        user_id = _local(session).id
    sender.reconcile(s.factory, user_id, settings=app.state.settings, now=at)
    return sender


def _mailbox(s: Setup) -> Mailbox:
    with session_scope(s.factory) as session:
        mailbox = session.scalars(unscoped(select(Mailbox).order_by(Mailbox.id))).first()
        assert mailbox is not None
        session.expunge(mailbox)
        return mailbox


async def test_a_fresh_mailbox_goes_from_disarmed_to_armed_to_send_with_no_manual_step(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The CP6 deadlock (#304): connect, arm for drafts, a test draft of each step, the
    gate satisfied, activate, the drafts check verifies a test draft's Message-ID, and
    arming to send is allowed. Nothing is sent on the way."""
    s = _build(running_app, arm={})  # connected, and disarmed as every mailbox starts
    mailbox_id = _mailbox(s).id
    arm = f"/api/v1/mailboxes/{mailbox_id}/arm"
    refused = await client.post(arm, json={"mode": "send"}, headers=CSRF)
    assert refused.status_code == 409  # not armed for drafts yet
    await _ok(await client.post(arm, json={"mode": "draft"}, headers=CSRF))
    refused = await client.post(arm, json={"mode": "send"}, headers=CSRF)
    assert refused.status_code == 409  # no draft found by its Message-ID yet

    await _complete(client, s)  # each email step's test is a draft
    assert len(s.gmail.drafts()) == len(s.email_step_ids) == 2
    assert _missing(await _review(client, s)) == set()
    await _ok(await client.post(f"{s.base}/activate", headers=CSRF))
    assert _campaign(s)[0] is CampaignStatus.ACTIVE

    now = utcnow()
    _drafts_check(s, running_app, at=now)
    assert _mailbox(s).message_id_verified_at == now
    searches = [p for m, p in s.gmail.calls if m == "messages.list" and "test draft" in p]
    assert len(searches) == 1  # the newest test draft, found at once

    body = await _ok(await client.post(arm, json={"mode": "send"}, headers=CSRF))
    assert body["arm"] == "send"
    methods = {m for m, _ in s.gmail.calls}
    assert "messages.send" not in methods
    assert len(s.gmail.drafts()) == 2  # netkeeper deleted neither test draft
    with session_scope(s.factory) as session:
        for table in (Message, Interaction):
            assert session.scalar(unscoped(select(func.count()).select_from(table))) == 0


async def test_the_drafts_check_verifies_a_test_draft_before_any_campaign_is_active(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, arm=ARMED_FOR_DRAFTS)
    await _test_send_all(client, s)
    assert _campaign(s)[0] is CampaignStatus.REVIEWING
    now = utcnow()
    _drafts_check(s, running_app, at=now)
    assert _mailbox(s).message_id_verified_at == now


async def test_a_test_draft_not_found_leaves_the_mailbox_unverified(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """A test draft the person discarded, or Gmail's search not caught up: nothing is
    verified, nothing is deleted, and the next check tries again."""
    s = _build(running_app, arm=ARMED_FOR_DRAFTS)
    await _test_send_all(client, s)
    for draft_id in list(s.gmail.drafts()):
        s.gmail.discard_draft(draft_id)
    _drafts_check(s, running_app, at=utcnow())
    assert _mailbox(s).message_id_verified_at is None
    searches = [p for m, p in s.gmail.calls if m == "messages.list" and "test draft" in p]
    assert len(searches) == 2  # each test draft, newest first
    assert all(m in {"drafts.create", "messages.list"} for m, p in s.gmail.calls if "test" in p)


@pytest.mark.parametrize(
    "arm",
    [
        pytest.param({}, id="disarmed"),
        pytest.param({**ARMED_FOR_DRAFTS, "message_id_verified_at": utcnow()}, id="verified"),
    ],
)
async def test_the_drafts_check_skips_a_disarmed_or_verified_mailbox(
    client: httpx.AsyncClient, running_app: FastAPI, arm: dict[str, Any]
) -> None:
    s = _build(running_app, arm=ARMED_FOR_DRAFTS)
    await _test_send_all(client, s)
    with session_scope(s.factory, write=True) as session:
        mailbox = session.scalars(unscoped(select(Mailbox))).one()
        mailbox.armed_at = None
        mailbox.armed_by = None
        for name, value in arm.items():
            setattr(mailbox, name, value)
    with session_scope(s.factory) as session:
        assert campaign_review.test_drafts_to_verify(session, _local(session)) == []
    calls = len(s.gmail.calls)
    _drafts_check(s, running_app, at=utcnow())
    assert not [p for m, p in s.gmail.calls[calls:] if "test draft" in p]


async def test_a_sent_test_is_not_searched_for(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, arm=ARMED_FOR_SEND)
    await _test_send_all(client, s)
    with session_scope(s.factory, write=True) as session:
        mailbox = session.scalars(unscoped(select(Mailbox))).one()
        mailbox.message_id_verified_at = None
        mailbox.send_armed_at = None
    with session_scope(s.factory) as session:
        assert campaign_review.test_drafts_to_verify(session, _local(session)) == []


def test_the_drafts_check_searches_at_most_three_test_drafts() -> None:
    assert mailbox_service.TEST_DRAFTS_CHECKED == 3


async def test_an_arming_changed_while_the_client_opens_decides_the_test(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """The arming is read after the Gmail client is opened (its Keychain reads), just
    before the call: a mailbox taken back to drafts meanwhile only gets a draft."""
    s = _build(running_app, arm=ARMED_FOR_SEND)

    def open_then_take_back_to_drafts(user_id: int, mailbox_id: int) -> FakeGmail:
        with session_scope(s.factory, write=True) as session:
            mailbox = get_scoped(session, _local(session), Mailbox, mailbox_id)
            assert mailbox is not None
            mailbox.send_armed_at = None
        return s.gmail

    running_app.state.gmail_opener = open_then_take_back_to_drafts
    body = await _ok(
        await client.post(
            f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
        )
    )
    assert body["drafted"] is True
    assert [m for m, _ in s.gmail.calls] == ["drafts.create"]


async def _arm_to_send(client: httpx.AsyncClient, s: Setup) -> httpx.Response:
    mailbox_id = _mailbox(s).id
    return await client.post(
        f"/api/v1/mailboxes/{mailbox_id}/arm", json={"mode": "send"}, headers=CSRF
    )


async def test_a_failed_test_draft_search_verifies_nothing(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """A Gmail error in the test-draft search is not a find: the mailbox stays unverified,
    arming to send is still refused, and nothing is recorded as not found."""
    s = _build(running_app, arm=ARMED_FOR_DRAFTS)
    await _test_send_all(client, s)
    s.gmail.fail_next("messages.list", GmailRateLimited("slow down", code="rateLimitExceeded"))
    _drafts_check(s, running_app, at=utcnow())
    assert _mailbox(s).message_id_verified_at is None
    refused = await _arm_to_send(client, s)
    assert refused.status_code == 409
    assert "has been found by its Message-ID yet" in refused.json()["detail"]
    with session_scope(s.factory) as session:
        assert set(session.scalars(unscoped(select(TestSend.not_found_at)))) == {None}


async def test_a_test_draft_not_found_is_named_when_arming_to_send_is_refused(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, arm=ARMED_FOR_DRAFTS)
    await _test_send_all(client, s)
    before = (await _arm_to_send(client, s)).json()["detail"]
    assert "test draft was found" not in before  # not searched for yet
    for draft_id in list(s.gmail.drafts()):
        s.gmail.discard_draft(draft_id)
    _drafts_check(s, running_app, at=utcnow())
    refused = await _arm_to_send(client, s)
    assert refused.status_code == 409
    assert "no netkeeper test draft was found in the Drafts of" in refused.json()["detail"]
    assert "make a new test draft" in refused.json()["detail"]
    await _test_send_all(client, s)  # a new test draft: not searched for yet
    assert "test draft was found" not in (await _arm_to_send(client, s)).json()["detail"]
    _drafts_check(s, running_app, at=utcnow())
    assert (await _arm_to_send(client, s)).status_code == 200


async def test_a_mailboxs_test_drafts_are_searched_only_in_that_mailbox(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """Two armed, unverified mailboxes: A's test drafts (to A's own address) are never
    searched for in B's Gmail, and do not verify B."""
    s = _build(running_app, arm=ARMED_FOR_DRAFTS)
    await _test_send_all(client, s)
    with session_scope(s.factory, write=True) as session:
        other = make_mailbox(
            session, _local(session), email="other@example.test", **ARMED_FOR_DRAFTS
        )
        a_id, b_id = _mailbox_ids(session)
        assert b_id == other.id
    b_gmail = FakeGmail("other@example.test", mailbox_id=b_id)
    boxes = {a_id: s.gmail, b_id: b_gmail}
    with session_scope(s.factory) as session:
        checks = campaign_review.test_drafts_to_verify(session, _local(session))
    assert {c.mailbox_id for c in checks} == {a_id}
    sender = GmailSender(
        s.factory,
        opener=lambda user_id, mailbox_id: boxes[mailbox_id],
        drafts_every=timedelta(0),
        replies_every=timedelta(days=3650),
    )
    with session_scope(s.factory) as session:
        user_id = _local(session).id
    sender.reconcile(s.factory, user_id, settings=running_app.state.settings, now=utcnow())
    assert not [p for m, p in b_gmail.calls if "test draft" in p]
    with session_scope(s.factory) as session:
        rows = session.execute(unscoped(select(Mailbox.id, Mailbox.message_id_verified_at))).all()
    verified = {row[0]: row[1] for row in rows}
    assert verified[a_id] is not None
    assert verified[b_id] is None


def _mailbox_ids(session: Session) -> list[int]:
    return list(session.scalars(unscoped(select(Mailbox.id).order_by(Mailbox.id))))


def test_the_review_render_uses_the_users_local_date(session: Session) -> None:
    """#357: at 03:00 UTC on 21 September it is still the 20th in Los Angeles, so a job
    starting on the 21st has not started for this user, as the engine sees it. The review
    must show what the send will."""
    now = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
    user = factories.make_user(session)
    campaign = factories.make_campaign(session, user)
    step = campaign.steps[0]
    assert step.template is not None
    step.template.body = "Changed {{ last_position_change }}"
    contact = factories.make_contact(
        session,
        user,
        emails=["reach@example.test"],
        positions=[
            {"title": "Lead", "started_on": date(2026, 9, 21)},
            {"title": "Engineer", "started_on": date(2021, 6, 1), "ended_on": date(2026, 9, 1)},
        ],
    )
    enrollment = factories.make_enrollment(
        session, campaign, contact, status=EnrollmentStatus.PENDING
    )

    def body() -> str | None:
        (preview,) = campaign_review._render_messages(
            session,
            user,
            campaign,
            step,
            step.template,
            [enrollment],
            {},
            now,
            step_approved=False,
            approved_messages={},
        )
        return preview.body

    user.timezone = "America/Los_Angeles"
    assert body() == "Changed 2026-09-01"
    user.timezone = "UTC"
    assert body() == "Changed 2026-09-21"
