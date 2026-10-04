"""``netkeeper campaigns`` and ``netkeeper simulate --campaign`` (#291, P3-13).

Every command runs against a migrated scratch database under ``tmp_path``. Nothing
here reaches Gmail: the review's test send is recorded straight through the service,
as ``POST .../review/test-send`` records it after the Gmail call.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import factories
import pytest
from campaign_fakes import ARMED_FOR_SEND, make_mailbox
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.campaigns import templates as template_service
from netkeeper.cli import app as cli
from netkeeper.crm import lists as list_service
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    Enrollment,
    EnrollmentStatus,
    ListKind,
    Mailbox,
    Message,
    StepCondition,
    StepMode,
    Template,
    TemplateChannel,
    TestSend,
    User,
    UserKind,
)
from netkeeper.scoping import get_scoped, install_scope_guard, scoped
from netkeeper.services import campaign_engine, campaign_results, campaign_review, sending_hours
from netkeeper.services import campaigns as campaign_service
from netkeeper.services.settings_kv import set_setting
from netkeeper.services.users import ensure_local_user
from netkeeper.web.api.campaigns import CampaignResultsOut, results_out

runner = CliRunner()
BODY = "Hi {{ first_name }}, it has been a while."


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, pointed to by ``NETKEEPER_DATABASE_URL``."""
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


@dataclass
class World:
    factory: sessionmaker[Session]
    mailbox_email: str
    templates: list[int]
    contacts: list[int]
    list_name: str = "Reconnect people"


def _local(session: Session) -> User:
    return session.scalars(select(User).where(User.kind == UserKind.LOCAL)).one()


@pytest.fixture
def world(cli_db: sessionmaker[Session]) -> World:
    """An armed mailbox, two email templates, four contacts (one do-not-contact) in a list."""
    with session_scope(cli_db, write=True) as session:
        user = _local(session)
        mailbox = make_mailbox(session, user, **ARMED_FOR_SEND)
        templates = [
            template_service.create_template(
                session,
                user,
                name=f"step {n}",
                channel=TemplateChannel.EMAIL,
                subject="Catching up",
                body=BODY,
            ).id
            for n in (1, 2)
        ]
        contacts = [
            factories.make_contact(
                session, user, emails=[f"person{n}@contacts.example"], do_not_contact=n == 3
            ).id
            for n in range(4)
        ]
        row = list_service.create_list(session, user, "Reconnect people", ListKind.STATIC)
        list_service.add_members(session, user, row.id, contacts)
        return World(cli_db, mailbox.email, templates, contacts)


def _run(*args: str, input: str | None = None) -> Any:
    return runner.invoke(cli, list(args), input=input)


def _ok(*args: str) -> str:
    result = _run(*args)
    assert result.exit_code == 0, result.output
    return str(result.output)


def _created_id(output: str) -> int:
    match = re.search(r"campaign (\d+)", output)
    assert match, output
    return int(match.group(1))


def _create(world: World, name: str = "Reconnect") -> int:
    return _created_id(
        _ok(
            "campaigns",
            "create",
            name,
            "--mailbox",
            world.mailbox_email,
            "--step",
            str(world.templates[0]),
            "--step",
            f"{world.templates[1]}:5:send",
            "--list",
            world.list_name,
        )
    )


def _campaign(world: World, campaign_id: int) -> Campaign:
    with session_scope(world.factory) as session:
        campaign = session.scalars(
            scoped(_local(session), Campaign).where(Campaign.id == campaign_id)
        ).one()
        session.expunge(campaign)
        return campaign


def _enrollment_statuses(world: World, campaign_id: int) -> list[EnrollmentStatus]:
    with session_scope(world.factory) as session:
        return list(
            session.scalars(
                scoped(_local(session), Enrollment)
                .with_only_columns(Enrollment.status)
                .where(Enrollment.campaign_id == campaign_id)
            )
        )


def _reviewing(world: World) -> int:
    """A campaign created, enrolled and in review, through the CLI and the service."""
    campaign_id = _create(world)
    _ok("campaigns", "enroll", str(campaign_id))
    with session_scope(world.factory, write=True) as session:
        campaign_review.start_review(session, _local(session), campaign_id)
    return campaign_id


def _complete_review(world: World, campaign_id: int) -> None:
    """Record every requirement: the steps approved through the CLI, the rest as the
    review screens would through the API."""
    for position in ("1", "2"):
        approved = _run("campaigns", "approve-step", str(campaign_id), position, input="y\n")
        assert approved.exit_code == 0, approved.output
    now = datetime.now(UTC)
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        steps = session.scalars(
            scoped(user, CampaignStep).where(CampaignStep.campaign_id == campaign_id)
        ).all()
        for step in steps:
            plan = campaign_review.prepare_test_send(
                session, user, campaign_id, step.id, today=now.date()
            )
            campaign_review.record_test_send(
                session, user, plan, gmail_message_id=f"fake-{step.id}", now=now
            )
        assert campaign_review.record_lint(session, user, campaign_id, now=now).clean


# --- create -------------------------------------------------------------------------


def test_create_makes_a_draft_with_the_steps_in_order(world: World) -> None:
    campaign_id = _create(world)

    campaign = _campaign(world, campaign_id)
    assert campaign.status is CampaignStatus.DRAFT
    assert campaign.name == "Reconnect"
    assert campaign.source_list_id is not None
    with session_scope(world.factory) as session:
        steps = session.scalars(
            scoped(_local(session), CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position)
        ).all()
        shape = [(s.template_id, s.delay_days, s.mode, s.condition, s.same_thread) for s in steps]
    assert shape == [
        (world.templates[0], 0, StepMode.DRAFT, StepCondition.ALWAYS, False),
        (world.templates[1], 5, StepMode.SEND, StepCondition.NO_REPLY, True),
    ]
    assert _enrollment_statuses(world, campaign_id) == []


def test_create_refuses_an_unknown_template_and_a_bad_step(world: World) -> None:
    unknown = _run("campaigns", "create", "X", "--mailbox", world.mailbox_email, "--step", "999")
    assert unknown.exit_code == 1
    assert "error: step 1: no template 999" in unknown.output

    bad = _run("campaigns", "create", "X", "--step", f"{world.templates[0]}:1:shout")
    assert bad.exit_code == 1
    assert "expected TEMPLATE_ID[:DELAY_DAYS[:MODE]]" in bad.output

    no_mailbox = _run("campaigns", "create", "X", "--step", str(world.templates[0]))
    assert no_mailbox.exit_code == 1
    assert "needs a mailbox" in no_mailbox.output
    with session_scope(world.factory) as session:
        assert session.scalars(scoped(_local(session), Campaign)).all() == []


# --- enroll -------------------------------------------------------------------------


def test_enroll_takes_the_list_through_the_guards(world: World) -> None:
    campaign_id = _create(world)

    output = _ok("campaigns", "enroll", str(campaign_id))

    assert f"campaign {campaign_id}: 3 enrolled, 0 already in, 1 excluded" in output
    assert "3 will start, 1 skipped (1 do-not-contact)" in output
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.PENDING] * 3
    again = _ok("campaigns", "enroll", str(campaign_id))
    assert "0 enrolled, 3 already in" in again


# --- list and status ----------------------------------------------------------------


def test_list_shows_each_campaign_with_its_enrollments(world: World) -> None:
    assert "no campaigns" in _ok("campaigns", "list")
    campaign_id = _create(world)
    _ok("campaigns", "enroll", str(campaign_id))

    output = _ok("campaigns", "list")

    assert output.splitlines()[0].split() == ["ID", "NAME", "STATUS", "STEPS", "ENROLLMENTS"]
    assert output.splitlines()[1].split() == [
        str(campaign_id),
        "Reconnect",
        "draft",
        "2",
        "3",
        "pending",
    ]


def test_status_shows_steps_enrollments_and_what_review_needs(world: World) -> None:
    campaign_id = _reviewing(world)

    output = _ok("campaigns", "status", str(campaign_id))

    assert f"campaign {campaign_id}: Reconnect" in output
    assert "status: reviewing" in output
    assert f"mailbox: {world.mailbox_email}" in output
    assert "enrollments: 3 pending" in output
    assert "step 1 v1" in output and "step 2 v1" in output
    assert "review: activation still needs" in output
    for requirement in ("step_approvals", "test_sends", "lint"):
        assert f"- {requirement}:" in output

    missing = _run("campaigns", "status", "999")
    assert missing.exit_code == 1
    assert "error: no campaign 999" in missing.output


# --- activate -----------------------------------------------------------------------


def test_activate_refused_prints_what_is_missing_and_exits_non_zero(world: World) -> None:
    campaign_id = _reviewing(world)

    result = _run("campaigns", "activate", str(campaign_id), "--yes")

    assert result.exit_code == 1
    assert f"error: campaign {campaign_id} cannot be activated" in result.output
    for requirement in ("step_approvals", "test_sends", "lint"):
        assert f"- {requirement}:" in result.output
    assert "(steps 1, 2)" in result.output  # the test sends missing, by step
    assert _campaign(world, campaign_id).status is CampaignStatus.REVIEWING
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.PENDING] * 3


def test_review_step_shows_one_message_at_a_time_and_the_blocked_ones(world: World) -> None:
    campaign_id = _reviewing(world)
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        first = session.scalars(
            scoped(user, Enrollment)
            .where(Enrollment.campaign_id == campaign_id)
            .order_by(Enrollment.id)
        ).first()
        assert first is not None
        blocked_id = first.id
        contact = get_scoped(session, user, Contact, first.contact_id)
        assert contact is not None
        contact.do_not_contact = True

    output = _ok("campaigns", "review-step", str(campaign_id), "1", "--index", "2")

    assert f"step 1 of campaign {campaign_id}: email, step 1 v1" in output
    assert "approval: not approved" in output
    assert "message 2 of 2:" in output
    assert "subject: Catching up" in output
    assert "it has been a while." in output
    assert "blocked, never sent (1):" in output
    asked = _run("campaigns", "approve-step", str(campaign_id), "1", input="n\n").output
    assert "blocked, never sent (1):" in asked.split("approve step 1")[0]
    assert f"enrollment {blocked_id}:" in output and "do-not-contact" in output

    past = _ok("campaigns", "review-step", str(campaign_id), "1", "--index", "9")
    assert "no message 9: the step has 2" in past
    assert _run("campaigns", "review-step", str(campaign_id), "7").exit_code == 1


def test_approve_step_asks_then_approves_the_step(world: World) -> None:
    campaign_id = _reviewing(world)

    cancelled = _run("campaigns", "approve-step", str(campaign_id), "1", input="n\n")
    assert cancelled.exit_code == 1
    # What is approved is shown in full before the question.
    shown = cancelled.output.split("approve step 1 for all")[0]
    assert "message 1 of 3" in shown
    assert "subject: Catching up" in shown and "it has been a while." in shown
    assert "approve step 1 for all 3 messages that can be sent" in cancelled.output
    assert "cancelled: step 1 is not approved" in cancelled.output
    assert "approval: not approved" in _ok("campaigns", "review-step", str(campaign_id), "1")

    approved = _run("campaigns", "approve-step", str(campaign_id), "1", input="y\n")
    assert approved.exit_code == 0, approved.output
    assert f"step 1 of campaign {campaign_id} approved" in approved.output
    assert "--yes" not in _run("campaigns", "approve-step", "--help").output
    assert "approval: approved" in _ok("campaigns", "review-step", str(campaign_id), "1")
    status = _ok("campaigns", "status", str(campaign_id))
    assert "- step_approvals: steps not approved (steps 2)" in status


def test_a_personal_line_step_is_approved_message_by_message(world: World) -> None:
    campaign_id = _reviewing(world)
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        step = campaign_review.step_at(session, user, campaign_id, 1)
        template = get_scoped(session, user, Template, step.template_id)
        assert template is not None
        template.body = BODY + " {{ personal_line }}"
        ids = list(
            session.scalars(
                scoped(user, Enrollment)
                .with_only_columns(Enrollment.id)
                .where(Enrollment.campaign_id == campaign_id)
                .order_by(Enrollment.id)
            )
        )

    review = _ok("campaigns", "review-step", str(campaign_id), "1")
    assert "each message is approved on its own; 3 not approved yet" in review
    whole = _run("campaigns", "approve-step", str(campaign_id), "1", input="y\n")
    assert whole.exit_code == 1
    assert "approve each message with --enrollment ID" in whole.output

    one = _run(
        "campaigns", "approve-step", str(campaign_id), "1", "--enrollment", str(ids[0]), input="y\n"
    )
    assert one.exit_code == 0, one.output
    shown = one.output.split("approve 1 messages")[0]
    assert f"message 1 of 1 to approve:\nenrollment {ids[0]}:" in shown
    assert "it has been a while." in shown
    assert "2 not approved yet" in _ok("campaigns", "review-step", str(campaign_id), "1")
    status = _ok("campaigns", "status", str(campaign_id))
    assert "- message_approvals: messages of steps that use" in status
    assert f"(enrollments {ids[1]}, {ids[2]})" in status

    wrong = _run(
        "campaigns", "approve-step", str(campaign_id), "2", "--enrollment", str(ids[0]), input="y\n"
    )
    assert wrong.exit_code == 1 and "approved as a whole" in wrong.output


def test_activate_refuses_a_draft_with_the_reviewing_requirement(world: World) -> None:
    campaign_id = _create(world)
    _ok("campaigns", "enroll", str(campaign_id))

    result = _run("campaigns", "activate", str(campaign_id), "--yes")

    assert result.exit_code == 1
    assert "- reviewing: the campaign is draft, not reviewing" in result.output
    assert _campaign(world, campaign_id).status is CampaignStatus.DRAFT


def test_activate_after_a_complete_review_asks_then_activates(world: World) -> None:
    campaign_id = _reviewing(world)
    _complete_review(world, campaign_id)

    declined = _run("campaigns", "activate", str(campaign_id), input="n\n")
    assert declined.exit_code == 1
    assert "stays in review" in declined.output
    # #346: the guard summary is shown, and gates nothing; no acknowledgement was made.
    assert "guards: 3 will start, 1 skipped (1 do-not-contact)" in declined.output
    assert _campaign(world, campaign_id).status is CampaignStatus.REVIEWING

    result = _run("campaigns", "activate", str(campaign_id), input="y\n")

    assert result.exit_code == 0, result.output
    assert f"campaign {campaign_id} 'Reconnect' is active" in result.output
    campaign = _campaign(world, campaign_id)
    assert campaign.status is CampaignStatus.ACTIVE
    assert campaign.approved_at is not None
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.ACTIVE] * 3


def test_guards_lists_each_skipped_contact_with_every_reason(world: World) -> None:
    campaign_id = _reviewing(world)

    output = _ok("campaigns", "guards", str(campaign_id))

    assert output.splitlines()[0] == "guards: 3 will start, 1 skipped (1 do-not-contact)"
    assert output.splitlines()[1].split() == ["CONTACT", "NAME", "SKIPPED", "BECAUSE"]
    [row] = output.splitlines()[2:]
    assert row.startswith(str(world.contacts[3])) and row.endswith("do-not-contact")
    assert "note:" not in output  # nobody the old tool emailed
    assert "showing the first" not in output

    with session_scope(world.factory, write=True) as session:
        for contact_id in world.contacts[:2]:
            contact = get_scoped(session, _local(session), Contact, contact_id)
            assert contact is not None
            contact.archived_at = datetime.now(UTC)
    cut = _ok("campaigns", "guards", str(campaign_id), "--limit", "1")
    assert cut.splitlines()[0] == "guards: 1 will start, 3 skipped (2 archived, 1 do-not-contact)"
    assert len(cut.splitlines()) == 4  # the line, the header, one row, the note
    assert cut.splitlines()[-1] == "showing the first 1 of 3 skipped contacts"

    missing = _run("campaigns", "guards", "999")
    assert missing.exit_code == 1
    assert "error: no campaign 999" in missing.output


# --- pause and resume ---------------------------------------------------------------


def test_pause_and_resume_go_through_the_engine(world: World) -> None:
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        mailbox_id = session.scalars(scoped(user, Mailbox).with_only_columns(Mailbox.id)).one()
        campaign_id = factories.make_campaign(session, user, mailbox_id=mailbox_id).id  # active

    assert f"campaign {campaign_id} paused" in _ok("campaigns", "pause", str(campaign_id))
    assert _campaign(world, campaign_id).status is CampaignStatus.PAUSED
    again = _run("campaigns", "pause", str(campaign_id))
    assert again.exit_code == 1
    assert "is paused, not active" in again.output

    assert f"campaign {campaign_id} resumed" in _ok("campaigns", "resume", str(campaign_id))
    assert _campaign(world, campaign_id).status is CampaignStatus.ACTIVE
    twice = _run("campaigns", "resume", str(campaign_id))
    assert twice.exit_code == 1
    assert "is active, not paused" in twice.output


def test_pause_refuses_a_campaign_in_review(world: World) -> None:
    campaign_id = _reviewing(world)

    result = _run("campaigns", "pause", str(campaign_id))

    assert result.exit_code == 1
    assert "is reviewing, not active" in result.output


# --- the lifecycle (#345) ------------------------------------------------------------


def _active(world: World) -> int:
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        mailbox_id = session.scalars(scoped(user, Mailbox).with_only_columns(Mailbox.id)).one()
        campaign = factories.make_campaign(session, user, mailbox_id=mailbox_id)
        contact = get_scoped(session, user, Contact, world.contacts[0])
        assert contact is not None
        factories.make_message(session, factories.make_enrollment(session, campaign, contact))
        return campaign.id


def test_end_asks_then_ends_and_archive_and_unarchive_follow(world: World) -> None:
    campaign_id = _active(world)
    ident = str(campaign_id)

    declined = _run("campaigns", "end", ident, input="n\n")
    assert declined.exit_code == 1
    assert "is not ended" in declined.output
    assert _campaign(world, campaign_id).status is CampaignStatus.ACTIVE
    refused = _run("campaigns", "archive", ident)
    assert refused.exit_code == 1
    assert "end it first" in refused.output

    ended = _run("campaigns", "end", ident, input="y\n")
    assert ended.exit_code == 0, ended.output
    assert "cannot be resumed" in ended.output
    assert f"campaign {campaign_id} ended" in ended.output
    assert _campaign(world, campaign_id).status is CampaignStatus.COMPLETED
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.ACTIVE]
    assert _run("campaigns", "end", ident, "--yes").exit_code == 1

    assert f"campaign {campaign_id} archived" in _ok("campaigns", "archive", ident)
    assert ident not in [line.split()[0] for line in _ok("campaigns", "list").splitlines()[1:]]
    listed = _ok("campaigns", "list", "--archived")
    assert [line.split()[0] for line in listed.splitlines()[1:]] == [ident]
    assert "archived" in listed

    assert "it is completed" in _ok("campaigns", "unarchive", ident)
    assert _campaign(world, campaign_id).status is CampaignStatus.COMPLETED
    assert "no archived campaigns" in _ok("campaigns", "list", "--archived")


def test_delete_lists_the_drafts_left_asks_then_deletes(world: World) -> None:
    campaign_id = _create(world)
    _ok("campaigns", "enroll", str(campaign_id))
    now = datetime.now(UTC)
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        step = session.scalars(
            scoped(user, CampaignStep).where(
                CampaignStep.campaign_id == campaign_id, CampaignStep.position == 2
            )
        ).one()
        session.add(
            TestSend(
                user_id=user.id,
                campaign_id=campaign_id,
                step_id=step.id,
                fingerprint="f" * 64,
                to_address=world.mailbox_email,
                gmail_draft_id="draft-left",
                sent_at=now,
            )
        )
    ident = str(campaign_id)

    declined = _run("campaigns", "delete", ident, input="n\n")
    assert declined.exit_code == 1
    assert "is not deleted" in declined.output
    assert "deletes campaign" in declined.output and "2 steps, 3 enrollments" in declined.output
    assert "delete them by hand" in declined.output
    assert f"step 2 test to {world.mailbox_email}" in declined.output
    assert "draft-left" in declined.output
    assert _campaign(world, campaign_id).status is CampaignStatus.DRAFT

    deleted = _run("campaigns", "delete", ident, input="y\n")
    assert deleted.exit_code == 0, deleted.output
    assert f"campaign {campaign_id} deleted" in deleted.output
    with session_scope(world.factory) as session:
        assert get_scoped(session, _local(session), Campaign, campaign_id) is None
    assert _run("campaigns", "delete", ident, "--yes").exit_code == 1


def test_delete_refuses_a_campaign_that_sent_without_asking(world: World) -> None:
    campaign_id = _active(world)
    result = _run("campaigns", "delete", str(campaign_id))
    assert result.exit_code == 1
    assert "only a campaign that was never activated can be deleted" in result.output
    assert "delete campaign" not in result.output  # never asked
    assert _campaign(world, campaign_id).status is CampaignStatus.ACTIVE


# --- the gate: no CLI path reaches `active` without it ------------------------------


def test_the_cli_never_names_the_engine_gate() -> None:
    """Only ``campaign_review.activate`` may pass ``REVIEW_GATE``; the CLI and the
    campaign service and API it mirrors never name it or the engine's activate."""
    for module in (cli_module, campaign_service, _api_module()):
        source = inspect.getsource(module)
        assert "REVIEW_GATE" not in source, module.__name__
        assert "campaign_engine.activate" not in source, module.__name__
        assert "gate=" not in source, module.__name__


def _api_module() -> Any:
    from netkeeper.web.api import campaigns

    return campaigns


def test_no_command_activates_without_a_complete_review(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every campaigns command, run on an incomplete review, leaves nothing active; the
    one activation after the review is complete comes through the review gate."""
    calls: list[tuple[object, bool]] = []
    real = campaign_engine.activate

    def spy(*args: Any, **kwargs: Any) -> Campaign:
        via_review = any(
            frame.function == "activate" and frame.filename.endswith("campaign_review.py")
            for frame in inspect.stack()
        )
        calls.append((kwargs.get("gate"), via_review))
        return real(*args, **kwargs)

    monkeypatch.setattr(campaign_engine, "activate", spy)
    campaign_id = _reviewing(world)
    ident = str(campaign_id)
    for args in (
        ("campaigns", "list"),
        ("campaigns", "status", ident),
        ("campaigns", "enroll", ident),
        ("campaigns", "pause", ident),
        ("campaigns", "resume", ident),
        ("campaigns", "end", ident, "--yes"),
        ("campaigns", "archive", ident),
        ("campaigns", "unarchive", ident),
        ("campaigns", "activate", ident, "--yes"),
        ("simulate", "--campaign", ident, "--days", "2", "--start", "2026-09-29T08:00"),
    ):
        _run(*args)
    with session_scope(world.factory) as session:
        statuses = set(
            session.scalars(scoped(_local(session), Campaign).with_only_columns(Campaign.status))
        )
    assert CampaignStatus.ACTIVE not in statuses
    assert calls == []

    _complete_review(world, campaign_id)
    _ok("campaigns", "activate", ident, "--yes")

    assert calls == [(campaign_engine.REVIEW_GATE, True)]
    assert _campaign(world, campaign_id).status is CampaignStatus.ACTIVE


# --- the scheduled start (#338) ------------------------------------------------------


def _ready(world: World) -> int:
    campaign_id = _reviewing(world)
    _complete_review(world, campaign_id)
    return campaign_id


def _user_zone(world: World) -> ZoneInfo:
    with session_scope(world.factory) as session:
        return ZoneInfo(_local(session).timezone)


def test_activate_defaults_to_the_next_tuesday_at_nine_and_says_so(world: World) -> None:
    campaign_id = _ready(world)
    before = datetime.now(UTC)
    output = _ok("campaigns", "activate", str(campaign_id), "--yes")
    starts_at = _campaign(world, campaign_id).starts_at
    assert starts_at is not None and starts_at > before
    local = starts_at.astimezone(_user_zone(world))
    assert (local.strftime("%a"), local.hour, local.minute) == ("Tue", 9, 0)
    assert starts_at - before <= timedelta(days=7)
    assert "starts: Tue" in output
    assert "Most effective: Tue–Thu mornings." in output  # noqa: RUF001
    assert "netkeeper sends only while `serve` is running and this Mac is awake." in output
    assert "warning:" not in output


def test_activate_at_an_explicit_time_outside_the_slots_warns_and_keeps_it(
    world: World,
) -> None:
    campaign_id = _ready(world)
    output = _ok(
        "campaigns", "activate", str(campaign_id), "--start", "2099-01-03T22:00", "--yes"
    )  # a Saturday night
    assert "warning: That is outside the suggested slots" in output
    starts_at = _campaign(world, campaign_id).starts_at
    assert starts_at is not None
    assert starts_at.astimezone(_user_zone(world)).replace(tzinfo=None) == datetime(
        2099, 1, 3, 22, 0
    )


def test_activate_with_a_start_already_past_says_it_starts_now(world: World) -> None:
    """#338 review, N3: a past --start starts the campaign now, and the prompt says so."""
    campaign_id = _ready(world)
    output = _ok("campaigns", "activate", str(campaign_id), "--start", "2020-01-07T10:00", "--yes")
    assert "starts: now" in output


def test_activate_now_starts_at_once(world: World) -> None:
    campaign_id = _ready(world)
    before = datetime.now(UTC)
    output = _ok("campaigns", "activate", str(campaign_id), "--now", "--yes")
    starts_at = _campaign(world, campaign_id).starts_at
    assert starts_at is not None and before <= starts_at <= datetime.now(UTC)
    assert "starts: now" in output


def test_activate_refuses_both_a_start_and_now(world: World) -> None:
    campaign_id = _ready(world)
    result = _run(
        "campaigns", "activate", str(campaign_id), "--now", "--start", "2099-01-03T22:00", "--yes"
    )
    assert result.exit_code == 1 and "not both" in result.output
    assert _campaign(world, campaign_id).status is CampaignStatus.REVIEWING


def test_the_start_moves_until_the_first_send(world: World) -> None:
    campaign_id = _ready(world)
    _ok("campaigns", "activate", str(campaign_id), "--start", "2099-01-05T09:00", "--yes")
    output = _ok("campaigns", "start", str(campaign_id), "--start", "2099-01-06T10:30")
    assert "now starts Tue Jan 6, 10:30" in output
    status = _ok("campaigns", "status", str(campaign_id))
    assert "starts: Tue Jan 6, 10:30" in status
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        enrollment = session.scalars(
            scoped(user, Enrollment).where(Enrollment.campaign_id == campaign_id)
        ).first()
        assert enrollment is not None
        factories.make_message(session, enrollment, position=1)
    result = _run("campaigns", "start", str(campaign_id), "--now")
    assert result.exit_code == 1 and "already sent" in result.output


def test_step_time_sets_a_steps_day_offset_and_time_of_day(world: World) -> None:
    campaign_id = _create(world)
    output = _ok(
        "campaigns", "step-time", str(campaign_id), "2", "--delay-days", "3", "--at", "22:00"
    )
    assert f"campaign {campaign_id} step 2: +3d, at 22:00" in output
    assert "+3d at 22:00" in _ok("campaigns", "status", str(campaign_id))
    _ok("campaigns", "step-time", str(campaign_id), "2", "--delay-days", "5")
    assert "+5d suggested slot" in _ok("campaigns", "status", str(campaign_id))
    bad = _run("campaigns", "step-time", str(campaign_id), "2", "--delay-days", "5", "--at", "9pm")
    assert bad.exit_code == 1 and "HH:MM" in bad.output


# --- sending hours (#338) -------------------------------------------------------------


def test_sending_hours_show_set_and_any_time(world: World) -> None:
    assert "sending hours are: Mon to Fri, 09:00 to 17:00" in _ok("campaigns", "sending-hours")
    changed = _ok(
        "campaigns", "sending-hours", "--days", "mon,wed,friday", "--from", "08:00", "--to", "12:30"
    )
    assert "sending hours now: Mon, Wed, Fri, 08:00 to 12:30" in changed
    later = _ok("campaigns", "sending-hours", "--to", "15:00")  # the rest are kept
    assert "Mon, Wed, Fri, 08:00 to 15:00" in later
    assert "sending hours now: any time" in _ok("campaigns", "sending-hours", "--any-time")
    assert "sending hours are: any time" in _ok("campaigns", "sending-hours")


@pytest.mark.parametrize(
    "args",
    [
        ("--from", "17:00", "--to", "09:00"),
        ("--days", ""),
        ("--days", "someday"),
        ("--from", "9am"),
        ("--any-time", "--days", "mon"),
    ],
)
def test_sending_hours_that_cannot_be_used_are_refused(world: World, args: tuple[str, ...]) -> None:
    result = _run("campaigns", "sending-hours", *args)
    assert result.exit_code == 1, result.output
    assert "Mon to Fri, 09:00 to 17:00" in _ok("campaigns", "sending-hours")


def test_unreadable_sending_hours_are_said_not_shown_as_the_defaults(world: World) -> None:
    """#338 review N2: with no options, say nothing sends, rather than print the defaults."""
    with session_scope(world.factory, write=True) as session:
        set_setting(session, _local(session), sending_hours.KEY, {"enabled": "yes"})
    result = _run("campaigns", "sending-hours")
    assert result.exit_code == 1
    assert "cannot be read" in result.output and "no campaign sends" in result.output
    assert "Mon to Fri" not in result.output
    fixed = _ok("campaigns", "sending-hours", "--days", "mon", "--from", "09:00", "--to", "12:00")
    assert "sending hours now: Mon, 09:00 to 12:00" in fixed


def test_step_time_warns_outside_the_sending_hours(world: World) -> None:
    campaign_id = _create(world)
    late = _ok(
        "campaigns", "step-time", str(campaign_id), "2", "--delay-days", "3", "--at", "22:00"
    )
    assert "warning: 22:00 is outside the sending hours (Mon to Fri, 09:00 to 17:00)" in late
    inside = _ok(
        "campaigns", "step-time", str(campaign_id), "2", "--delay-days", "3", "--at", "10:00"
    )
    assert "warning" not in inside
    _ok("campaigns", "sending-hours", "--any-time")
    anytime = _ok(
        "campaigns", "step-time", str(campaign_id), "2", "--delay-days", "3", "--at", "22:00"
    )
    assert "warning" not in anytime


def test_activate_shows_the_sending_hours(world: World) -> None:
    campaign_id = _ready(world)
    output = _ok("campaigns", "activate", str(campaign_id), "--yes")
    assert "Sending hours: Mon to Fri, 09:00 to 17:00." in output


# --- simulate --campaign ------------------------------------------------------------


def test_simulate_replays_the_campaign_schedule_and_changes_nothing(world: World) -> None:
    campaign_id = _create(world)
    _ok("campaigns", "enroll", str(campaign_id))

    output = _ok(
        "simulate",
        "--campaign",
        str(campaign_id),
        "--start",
        "2026-09-29T09:00",  # the default start, a Tuesday, in the user's zone (UTC)
        "--days",
        "14",
        "--seed",
        "1",
    )

    lines = output.splitlines()
    assert (
        f"campaign {campaign_id} 'Reconnect' (draft): 3 contacts from its enrollments" in lines[0]
    )
    header = next(line for line in lines if line.startswith("DATE"))
    assert header.split() == ["DATE", "DAY", "STEP", "1", "STEP", "2", "TOTAL"]
    # Step 1 at the start (Tuesday), step 2 five days after each send (a Sunday), in the
    # next suggested slot (the following Tuesday).
    assert any(line.split() == ["2026-09-29", "Tue", "3", "0", "3"] for line in lines), output
    assert any(line.split() == ["2026-10-06", "Tue", "0", "3", "3"] for line in lines), output
    assert "per step: step 1 3, step 2 3" in output
    assert "3 of 3 contacts got every step" in output
    assert "draft steps count as sent the moment they are drafted" in output
    assert _campaign(world, campaign_id).status is CampaignStatus.DRAFT
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.PENDING] * 3
    with session_scope(world.factory) as session:
        assert session.scalars(scoped(_local(session), Message)).all() == []

    repeat = _ok(
        "simulate",
        "--campaign",
        str(campaign_id),
        "--start",
        "2026-09-29T09:00",
        "--days",
        "14",
        "--seed",
        "1",
    )
    assert repeat == output


def test_simulate_counts_the_source_before_anyone_is_enrolled(world: World) -> None:
    campaign_id = _create(world)

    output = _ok("simulate", "--campaign", str(campaign_id), "--start", "2026-09-28", "--days", "3")

    assert "4 contacts from its source" in output


def test_simulate_refuses_an_unknown_campaign_and_a_stray_start(world: World) -> None:
    unknown = _run("simulate", "--campaign", "999")
    assert unknown.exit_code == 1
    assert "error: no campaign 999" in unknown.output

    stray = _run("simulate", "--start", "2026-09-28")
    assert stray.exit_code == 1
    assert "--start applies only with --campaign" in stray.output


# --- after review (#292) --------------------------------------------------------------


def test_a_refused_activate_never_asks(world: World) -> None:
    campaign_id = _reviewing(world)

    result = _run("campaigns", "activate", str(campaign_id), input="y\n")

    assert result.exit_code == 1
    assert "cannot be activated" in result.output
    assert "activate campaign" not in result.output  # the confirm prompt
    assert "[y/N]" not in result.output
    assert _campaign(world, campaign_id).status is CampaignStatus.REVIEWING


def test_enroll_with_a_new_list_reports_what_it_removed(world: World) -> None:
    campaign_id = _create(world)
    _ok("campaigns", "enroll", str(campaign_id))
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        row = list_service.create_list(session, user, "Just one", ListKind.STATIC)
        list_service.add_members(session, user, row.id, world.contacts[:1])

    output = _ok("campaigns", "enroll", str(campaign_id), "--list", "Just one")

    assert "0 enrolled, 1 already in, 0 excluded, 2 removed; 1 pending" in output
    assert "1 will start, none skipped" in output
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.PENDING]


def test_simulate_warns_when_the_campaign_is_partway_through(world: World) -> None:
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user, channels=(TemplateChannel.EMAIL,) * 2)
        contact = factories.make_contact(session, user, emails=["x@contacts.example"])
        factories.make_message(session, factories.make_enrollment(session, campaign, contact))
        campaign_id = campaign.id

    output = _ok("simulate", "--campaign", str(campaign_id), "--start", "2026-09-28", "--days", "2")

    assert (
        "warning: this campaign is active and partway through (1 messages fired so far)" in output
    )

    fresh = _create(world)
    _ok("campaigns", "enroll", str(fresh))
    quiet = _ok("simulate", "--campaign", str(fresh), "--start", "2026-09-28", "--days", "2")
    assert "warning:" not in quiet


# --- results (#350) -----------------------------------------------------------------


def _campaign_with_results(world: World) -> int:
    """Two steps sent to one contact, who replied after step 2; step 1 sent to another."""
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        campaign = factories.make_campaign(session, user, channels=(TemplateChannel.EMAIL,) * 2)
        at = datetime.now(UTC) - timedelta(days=3)
        replied = factories.make_enrollment(
            session,
            campaign,
            factories.make_contact(session, user),
            replied_at=at + timedelta(days=2),
        )
        factories.make_message(session, replied, position=1, sent_at=at)
        factories.make_message(session, replied, position=2, sent_at=at + timedelta(days=1))
        quiet = factories.make_enrollment(session, campaign, factories.make_contact(session, user))
        factories.make_message(session, quiet, position=1, sent_at=at)
        return campaign.id


def test_status_shows_the_results(world: World) -> None:
    campaign_id = _campaign_with_results(world)

    output = _ok("campaigns", "status", str(campaign_id))

    assert "results: 3 sent to 2, 1 replied (reply rate 50%), 0 bounced, 0 opted out" in output
    assert "REPLIED" in output and "BOUNCED" in output and "OPTED OUT" in output
    assert re.search(r"^\s*2\s.*\s1\s+0\s+0\s*$", output, re.MULTILINE), output
    assert "sends per day (" in output
    assert len(re.findall(r"^  \d{4}-\d{2}-\d{2}  \d+$", output, re.MULTILINE)) >= 4


def test_status_json_matches_the_api(world: World) -> None:
    campaign_id = _campaign_with_results(world)

    printed = CampaignResultsOut.model_validate_json(
        _ok("campaigns", "status", str(campaign_id), "--json")
    )

    with session_scope(world.factory) as session:
        expected = results_out(
            campaign_results.campaign_results(
                session, _local(session), campaign_id, now=datetime.now(UTC)
            )
        )
    assert printed == expected
    assert [s.replied for s in printed.steps] == [0, 1]
    assert printed.totals.sent == 3

    missing = _run("campaigns", "status", "999", "--json")
    assert missing.exit_code == 1
    assert "error: no campaign 999" in missing.output
