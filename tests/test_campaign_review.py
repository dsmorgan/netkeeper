"""The review gate and the test send over the API (spec 11.8; #288, P3-09).

Every Gmail call goes to a :class:`FakeGmail` set as ``app.state.gmail_opener``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import factories
import httpx
import pytest
from campaign_fakes import ARMED_FOR_SEND, make_mailbox
from fastapi import FastAPI
from sqlalchemy import func, select
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
    Message,
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
from netkeeper.services.campaign_guards import Reason, check_enrollment

CSRF = {"X-Netkeeper-Client": "1"}
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
    ids = [e["enrollment_id"] for e in sample["enrollments"]]
    await _ok(
        await client.post(
            f"{s.base}/review/approve",
            json={"enrollment_ids": ids, "content_fingerprint": sample["content_fingerprint"]},
            headers=CSRF,
        )
    )
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


async def _approve(client: httpx.AsyncClient, s: Setup, ids: list[int], fingerprint: str) -> None:
    await _ok(
        await client.post(
            f"{s.base}/review/approve",
            json={"enrollment_ids": ids, "content_fingerprint": fingerprint},
            headers=CSRF,
        )
    )


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
    other, viewed = await _search_one(client, s, sample)
    if skip != "searched_previews":
        await _approve(client, s, [other], viewed["content_fingerprint"])
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
    await _approve(client, s, ids[1:], sample["content_fingerprint"])
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
    ids = [e["enrollment_id"] for e in sample["enrollments"]]
    response = await client.post(
        f"{s.base}/review/approve",
        json={"enrollment_ids": ids, "content_fingerprint": sample["content_fingerprint"]},
        headers=CSRF,
    )
    assert response.status_code == 409


async def test_previews_never_viewed_cannot_be_approved(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app)
    review = await _review(client, s)
    response = await client.post(
        f"{s.base}/review/approve",
        json={
            "enrollment_ids": [s.enrollment_ids[0]],
            "content_fingerprint": review["content_fingerprint"],
        },
        headers=CSRF,
    )
    assert response.status_code == 409


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
    assert review["guard_summary"] == "12 in audience, none excluded"


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


async def test_a_test_send_is_never_counted_and_never_advances_an_enrollment(
    client: httpx.AsyncClient, running_app: FastAPI
) -> None:
    s = _build(running_app, people=1)
    await _test_send_all(client, s)
    assert len(s.gmail.sent()) == 2
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


@pytest.mark.parametrize(
    "arm",
    [
        pytest.param({}, id="disarmed"),
        pytest.param({"armed_at": utcnow(), "armed_by": "test"}, id="armed-for-drafts"),
        pytest.param(
            {"armed_at": utcnow(), "armed_by": "test", "message_id_verified_at": utcnow()},
            id="verified-but-drafts-only",
        ),
    ],
)
async def test_a_test_send_is_refused_unless_the_mailbox_is_armed_for_send(
    client: httpx.AsyncClient, running_app: FastAPI, arm: dict[str, Any]
) -> None:
    s = _build(running_app, arm=arm)
    response = await client.post(
        f"{s.base}/review/test-send", json={"step_id": s.step_ids[0]}, headers=CSRF
    )
    assert response.status_code == 409
    assert "armed for send" in response.json()["detail"]
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
        ("POST", "/review/approve", {"enrollment_ids": [1], "content_fingerprint": "x"}),
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
