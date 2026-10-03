"""The review gate and the test send over the API (spec 11.8; #288, P3-09).

Every Gmail call goes to a :class:`FakeGmail` set as ``app.state.gmail_opener``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from netkeeper.db import session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentStatus,
    Interaction,
    Mailbox,
    MailboxStatus,
    Message,
    ReviewPreview,
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
REQUIREMENTS = ("sample_previews", "searched_previews", "test_sends", "lint", "guards")


def test_the_sample_is_ten() -> None:
    assert campaign_review.SAMPLE_SIZE == 10


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


async def _approve_sample(client: httpx.AsyncClient, s: Setup) -> Any:
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    await _approve(client, s, sample["enrollments"])
    return sample


async def _search_one(client: httpx.AsyncClient, s: Setup, sample: Any) -> tuple[int, Any]:
    sampled = {e["enrollment_id"] for e in sample["enrollments"]}
    other = next(i for i in s.enrollment_ids if i not in sampled)
    viewed = await _ok(
        await client.post(
            f"{s.base}/review/previews", json={"enrollment_ids": [other]}, headers=CSRF
        )
    )
    return other, viewed


def _approval(previews: list[Any]) -> dict[str, Any]:
    return {
        "previews": [
            {"enrollment_id": p["enrollment_id"], "fingerprint": p["fingerprint"]} for p in previews
        ]
    }


async def _approve(client: httpx.AsyncClient, s: Setup, previews: list[Any]) -> None:
    await _ok(await client.post(f"{s.base}/review/approve", json=_approval(previews), headers=CSRF))


async def _test_send_all(client: httpx.AsyncClient, s: Setup) -> None:
    for step_id in s.email_step_ids:
        await _ok(
            await client.post(f"{s.base}/review/test-send", json={"step_id": step_id}, headers=CSRF)
        )


async def _lint(client: httpx.AsyncClient, s: Setup) -> Any:
    return await _ok(await client.post(f"{s.base}/review/lint", headers=CSRF))


async def _ack(client: httpx.AsyncClient, s: Setup) -> None:
    review = await _review(client, s)
    await _ok(
        await client.post(
            f"{s.base}/review/guards/acknowledge",
            json={
                "summary": review["guard_summary"],
                "audience_fingerprint": review["audience_fingerprint"],
            },
            headers=CSRF,
        )
    )


async def _complete(client: httpx.AsyncClient, s: Setup, *, skip: str | None = None) -> None:
    """Record every requirement but ``skip``. A searched preview is always viewed, and
    approved unless ``skip`` is ``searched_previews``."""
    if skip != "sample_previews":
        sample = await _approve_sample(client, s)
    else:
        sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    _other, viewed = await _search_one(client, s, sample)
    if skip != "searched_previews":
        await _approve(client, s, viewed["enrollments"])
    if skip != "test_sends":
        await _test_send_all(client, s)
    if skip != "lint":
        assert (await _lint(client, s))["clean"] is True
    if skip != "guards":
        await _ack(client, s)


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
    assert _missing(body) == {"sample_previews", "test_sends", "lint", "guards"}
    tests = next(m for m in body["missing"] if m["requirement"] == "test_sends")
    assert tests["step_positions"] == [1, 2]


async def test_one_sampled_preview_left_unapproved_is_named(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s, skip="sample_previews")
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    ids = [e["enrollment_id"] for e in sample["enrollments"]]
    await _approve(client, s, sample["enrollments"][1:])
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    [gap] = response.json()["missing"]
    assert (gap["requirement"], gap["enrollment_ids"]) == ("sample_previews", [ids[0]])


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
        ("template", {"sample_previews", "searched_previews", "test_sends", "lint"}, [1]),
        ("template_version", {"sample_previews", "searched_previews", "test_sends", "lint"}, [1]),
        ("step_mode", {"sample_previews", "searched_previews", "test_sends", "lint"}, [2]),
        ("step_added", {"sample_previews", "searched_previews", "test_sends", "lint"}, [3]),
        ("enrolled", {"sample_previews", "guards"}, None),
        ("removed", {"sample_previews", "guards"}, None),
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
        tests = next(m for m in response.json()["missing"] if m["requirement"] == "test_sends")
        assert tests["step_positions"] == steps
    assert _campaign(s)[0] is CampaignStatus.REVIEWING


async def test_guard_results_that_change_undo_the_acknowledgement(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        contact_id = session.scalars(unscoped(select(Enrollment.contact_id))).first()
        assert contact_id is not None
        contact = get_scoped(session, user, Contact, contact_id)
        assert contact is not None
        contact.do_not_contact = True
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    assert _missing(response.json()) == {"guards"}


async def test_an_approval_for_previews_that_changed_since_is_refused(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    _edit(s, "template")
    response = await client.post(
        f"{s.base}/review/approve", json=_approval(sample["enrollments"]), headers=CSRF
    )
    assert response.status_code == 409
    assert "changed since they were viewed" in response.json()["detail"]
    assert response.json()["code"] == "stale"  # show it again and retry (#299)


async def test_previews_never_viewed_cannot_be_approved(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    response = await client.post(
        f"{s.base}/review/approve",
        json={"previews": [{"enrollment_id": s.enrollment_ids[0], "fingerprint": "x"}]},
        headers=CSRF,
    )
    assert response.status_code == 409
    assert "not viewed" in response.json()["detail"]
    assert response.json().get("code") is None  # a real refusal, not a stale one (#299)


async def test_an_enrollment_no_longer_pending_cannot_be_approved(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    first = sample["enrollments"][0]
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        enrollment = get_scoped(session, user, Enrollment, first["enrollment_id"])
        assert enrollment is not None
        enrollment.status = EnrollmentStatus.REMOVED
    response = await client.post(f"{s.base}/review/approve", json=_approval([first]), headers=CSRF)
    assert response.status_code == 409
    assert "not pending" in response.json()["detail"]
    assert response.json().get("code") is None  # a real refusal, not a stale one (#299)
    with session_scope(s.factory) as session:
        row = session.scalars(
            unscoped(select(ReviewPreview)).where(
                ReviewPreview.enrollment_id == first["enrollment_id"]
            )
        ).one()
        assert row.approved_at is None


async def test_a_summary_that_is_not_the_current_one_is_not_acknowledged(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    review = await _review(client, s)
    response = await client.post(
        f"{s.base}/review/guards/acknowledge",
        json={
            "summary": "1 in audience, none excluded",
            "audience_fingerprint": review["audience_fingerprint"],
        },
        headers=CSRF,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "stale"
    assert review["guard_summary"] == "12 in audience, none excluded"


async def test_an_approval_on_a_campaign_no_longer_reviewing_is_a_real_refusal(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    """Only a stale fingerprint answers ``code: stale``; a campaign moved on is not one
    that showing the previews again can fix (#299)."""
    s = _build(running_app)
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    with session_scope(s.factory, write=True) as session:
        campaign = session.scalars(unscoped(select(Campaign))).one()
        campaign.status = CampaignStatus.DRAFT
    approve = await client.post(
        f"{s.base}/review/approve", json=_approval(sample["enrollments"]), headers=CSRF
    )
    review = await _review(client, s)
    ack = await client.post(
        f"{s.base}/review/guards/acknowledge",
        json={
            "summary": review["guard_summary"],
            "audience_fingerprint": review["audience_fingerprint"],
        },
        headers=CSRF,
    )
    for response in (approve, ack):
        assert response.status_code == 409
        assert "not reviewing" in response.json()["detail"]
        assert response.json().get("code") is None


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


# --- the sample, lint, the transition -----------------------------------------------


async def test_the_sample_is_ten_drawn_once_per_audience(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    first = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    again = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    ids = [e["enrollment_id"] for e in first["enrollments"]]
    assert len(ids) == 10 and set(ids) <= set(s.enrollment_ids)
    assert [e["enrollment_id"] for e in again["enrollments"]] == ids
    step = first["enrollments"][0]["steps"][0]
    assert step["subject"] == "Hello" and step["body"].startswith("Hi First")
    assert step["to_address"] in s.contact_emails
    _edit(s, "enrolled")
    redrawn = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    assert len(redrawn["enrollments"]) == 10
    assert all(not e["approved"] for e in redrawn["enrollments"])


async def test_a_small_audience_is_sampled_whole(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=3)
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    assert sorted(e["enrollment_id"] for e in sample["enrollments"]) == s.enrollment_ids


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
        ("POST", "/review/sample", None),
        ("POST", "/review/previews", {"enrollment_ids": [1]}),
        ("POST", "/review/approve", {"previews": [{"enrollment_id": 1, "fingerprint": "x"}]}),
        ("POST", "/review/lint", None),
        ("POST", "/review/guards/acknowledge", {"summary": "x", "audience_fingerprint": "x"}),
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
        campaign_id = campaign.id
    running_app.state.gmail_opener = lambda *_: pytest.fail("no Gmail for another user")
    response = await client.request(
        method, f"/api/v1/campaigns/{campaign_id}{path}", json=body, headers=CSRF
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
async def test_editing_an_approved_contact_undoes_its_approval(
    client: httpx.AsyncClient, running_app: FastAPI, edit: str
) -> None:
    s = _build(running_app)
    await _complete(client, s)
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    enrollment_id = sample["enrollments"][0]["enrollment_id"]
    with session_scope(s.factory, write=True) as session:
        user = _local(session)
        enrollment = get_scoped(session, user, Enrollment, enrollment_id)
        assert enrollment is not None
        contact = get_scoped(session, user, Contact, enrollment.contact_id)
        assert contact is not None
        if edit == "renamed":
            contact.preferred_name = "Somebody Else"
        else:
            contact.emails[0].email = "moved@contacts.example"
    response = await client.post(f"{s.base}/activate", headers=CSRF)
    assert response.status_code == 409
    [gap] = response.json()["missing"]
    assert (gap["requirement"], gap["enrollment_ids"]) == ("sample_previews", [enrollment_id])
    assert _campaign(s)[0] is CampaignStatus.REVIEWING


async def test_an_old_audience_fingerprint_is_not_acknowledged(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    before = await _review(client, s)
    _edit(s, "removed")
    _edit(s, "enrolled")  # the same count, so the same summary, for another audience
    after = await _review(client, s)
    assert after["guard_summary"] == before["guard_summary"]
    response = await client.post(
        f"{s.base}/review/guards/acknowledge",
        json={
            "summary": after["guard_summary"],
            "audience_fingerprint": before["audience_fingerprint"],
        },
        headers=CSRF,
    )
    assert response.status_code == 409
    assert "audience changed" in response.json()["detail"]
    assert response.json()["code"] == "stale"


async def test_previews_viewed_in_an_earlier_sample_must_still_be_approved(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    await _complete(client, s, skip="sample_previews")
    first = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    _edit(s, "enrolled")
    await _ack(client, s)
    second = await _approve_sample(client, s)
    dropped = {e["enrollment_id"] for e in first["enrollments"]} - {
        e["enrollment_id"] for e in second["enrollments"]
    }
    body = await _review(client, s)
    searched = [m for m in body["missing"] if m["requirement"] == "searched_previews"]
    if dropped:
        assert _missing(body) == {"searched_previews"}
        assert set(searched[0]["enrollment_ids"]) == dropped
    else:  # every earlier draw was drawn again (about 1 in 286)
        assert _missing(body) == set()


async def test_a_test_send_renders_for_the_first_sampled_enrollment(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    sample = await _ok(await client.post(f"{s.base}/review/sample", headers=CSRF))
    await _ok(
        await client.post(
            f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
        )
    )
    [sent] = s.gmail.sent()
    first = sample["enrollments"][0]
    assert first["steps"][0]["body"] in s.gmail.raw(sent.id).get_content()


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
