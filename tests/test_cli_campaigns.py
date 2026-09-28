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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import factories
import pytest
from campaign_fakes import ARMED_FOR_SEND, make_mailbox
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.campaigns import templates as template_service
from netkeeper.campaigns.render import me_fields
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.crm import lists as list_service
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Campaign,
    CampaignStatus,
    CampaignStep,
    Enrollment,
    EnrollmentStatus,
    ListKind,
    Message,
    StepCondition,
    StepMode,
    TemplateChannel,
    User,
    UserKind,
)
from netkeeper.scoping import install_scope_guard, scoped
from netkeeper.services import campaign_engine, campaign_review
from netkeeper.services import campaigns as campaign_service
from netkeeper.services.users import ensure_local_user

runner = CliRunner()
BODY = "Hi {{ first_name }}, it has been a while."
ME = me_fields(Settings().me)  # what the CLI renders with, from the default config


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
                me_keys=(),
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
    """Record every requirement, as the review screens would through the API."""
    now = datetime.now(UTC)
    with session_scope(world.factory, write=True) as session:
        user = _local(session)
        sample = campaign_review.draw_sample(session, user, campaign_id, me=ME, now=now)
        campaign_review.approve(
            session,
            user,
            campaign_id,
            {e.enrollment_id: e.fingerprint for e in sample.enrollments},
            me=ME,
            now=now,
        )
        steps = session.scalars(
            scoped(user, CampaignStep).where(CampaignStep.campaign_id == campaign_id)
        ).all()
        for step in steps:
            plan = campaign_review.prepare_test_send(
                session, user, campaign_id, step.id, enrollment_id=None, me=ME, today=now.date()
            )
            campaign_review.record_test_send(
                session, user, plan, gmail_message_id=f"fake-{step.id}", now=now
            )
        assert campaign_review.record_lint(session, user, campaign_id, me=ME, now=now).clean
        campaign = campaign_review.get_campaign(session, user, campaign_id)
        campaign_review.acknowledge_guards(
            session,
            user,
            campaign_id,
            summary_seen=campaign_review.guard_summary(session, user, campaign, now=now),
            audience_fingerprint_seen=campaign_review.audience_fingerprint(session, user, campaign),
            now=now,
        )


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
    assert "4 in audience, 1 excluded: 1 do-not-contact" in output
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
    for requirement in ("sample_previews", "test_sends", "lint", "guards"):
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
    for requirement in ("sample_previews", "test_sends", "lint", "guards"):
        assert f"- {requirement}:" in result.output
    assert "(steps 1, 2)" in result.output  # the test sends missing, by step
    assert _campaign(world, campaign_id).status is CampaignStatus.REVIEWING
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.PENDING] * 3


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
    assert _campaign(world, campaign_id).status is CampaignStatus.REVIEWING

    result = _run("campaigns", "activate", str(campaign_id), input="y\n")

    assert result.exit_code == 0, result.output
    assert f"campaign {campaign_id} 'Reconnect' is active" in result.output
    campaign = _campaign(world, campaign_id)
    assert campaign.status is CampaignStatus.ACTIVE
    assert campaign.approved_at is not None
    assert _enrollment_statuses(world, campaign_id) == [EnrollmentStatus.ACTIVE] * 3


# --- pause and resume ---------------------------------------------------------------


def test_pause_and_resume_go_through_the_engine(world: World) -> None:
    with session_scope(world.factory, write=True) as session:
        campaign_id = factories.make_campaign(session, _local(session)).id  # active

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


# --- simulate --campaign ------------------------------------------------------------


def test_simulate_replays_the_campaign_schedule_and_changes_nothing(world: World) -> None:
    campaign_id = _create(world)
    _ok("campaigns", "enroll", str(campaign_id))

    output = _ok(
        "simulate",
        "--campaign",
        str(campaign_id),
        "--start",
        "2026-09-28T08:00",  # a Monday, in the user's zone (UTC)
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
    # Step 1 on the first window day (Tuesday), step 2 five days after each send, pushed to
    # the next window day (the following Tuesday).
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
        "2026-09-28T08:00",
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
