"""netkeeper.campaigns.templates (spec 8.5, 11.1; item P3-03): storage, versions, the gate."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns import templates as service
from netkeeper.campaigns.render import (
    CONTACT_FIELDS,
    LintIssue,
    LintRule,
    Severity,
    TemplateRenderError,
)
from netkeeper.campaigns.templates import (
    UNSET,
    DuplicateTemplateName,
    InvalidTemplateValue,
    TemplateInUse,
    TemplateNotFound,
    TemplateSuperseded,
    activation_errors,
    contact_fields,
    create_template,
    delete_template,
    get_template,
    is_superseded,
    list_templates,
    render_preview,
    update_template,
    versions,
)
from netkeeper.db import session_scope
from netkeeper.models import Campaign, CampaignStatus, Template, TemplateChannel, User, UserKind
from netkeeper.scoping import scoped

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
BODY = "Hi {{ first_name }}"


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller of the service must use: it reads, then writes."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def other(writer: Session) -> User:
    return factories.make_user(writer, kind=UserKind.HOSTED)


@pytest.fixture
def in_use(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Template ids to treat as used by an active campaign, until P3-04 provides campaigns."""
    ids: set[int] = set()
    monkeypatch.setattr(service, "is_in_use", lambda _session, _user, row: row.id in ids)
    return ids


def _create(session: Session, user: User, name: str = "reconnect", **fields: Any) -> Template:
    values: dict[str, Any] = {"channel": EMAIL, "subject": "Hello", "body": BODY}
    values.update(fields)
    return create_template(session, user, name=name, **values)


# --- create, read ---------------------------------------------------------------


def test_create_stores_version_one_with_its_lint(writer: Session, user: User) -> None:
    row = _create(writer, user)
    assert (row.name, row.channel, row.subject, row.body) == ("reconnect", EMAIL, "Hello", BODY)
    assert (row.version, row.previous_id, row.lint_json) == (1, None, [])
    assert row.user_id == user.id


def test_lint_errors_are_stored_but_never_block_a_save(writer: Session, user: User) -> None:
    row = _create(writer, user, subject=None, body="Hi {{ nickname }}")
    rules = [LintIssue.from_json(item).rule for item in row.lint_json]
    assert rules == [
        LintRule.MISSING_SUBJECT,
        LintRule.UNDEFINED_VARIABLE,
        LintRule.NO_CONTACT_FIELD,
    ]


def test_names_are_trimmed_and_blank_subjects_are_no_subject(writer: Session, user: User) -> None:
    row = _create(writer, user, "  spaced  ", channel=LINKEDIN, subject="   ")
    assert row.name == "spaced" and row.subject is None


def test_a_linkedin_template_never_stores_a_subject(writer: Session, user: User) -> None:
    """#448: a LinkedIn message has no subject, so a save drops one rather than keeping it."""
    row = _create(writer, user, channel=LINKEDIN, subject="Hello")
    assert row.subject is None
    assert row.lint_json == []


def test_switching_a_template_to_linkedin_drops_its_subject(writer: Session, user: User) -> None:
    row = _create(writer, user, channel=EMAIL, subject="Hello")
    changed = update_template(writer, user, row.id, channel=LINKEDIN)
    assert (changed.channel, changed.subject, changed.lint_json) == (LINKEDIN, None, [])
    again = update_template(writer, user, row.id, subject="Back again")
    assert again.subject is None


def test_a_save_clears_the_subject_a_linkedin_template_had_before(
    writer: Session, user: User
) -> None:
    """A row from before #448 still loads and lints; the next save heals it."""
    row = _create(writer, user, channel=LINKEDIN, subject=None)
    row.subject = "Left over"
    writer.flush()
    assert [i.rule for i in activation_errors(row)] == [LintRule.LINKEDIN_SUBJECT]
    healed = update_template(writer, user, row.id, body="Hi {{ first_name }}, again")
    assert healed.subject is None
    assert activation_errors(healed) == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"name": "  "}, "name is empty"),
        ({"name": "x" * 201}, "longer than 200"),
        ({"subject": "s" * 501}, "longer than 500"),
        ({"body": "b" * 20_001}, "longer than 20000"),
    ],
)
def test_values_that_cannot_be_stored_are_refused(
    writer: Session, user: User, fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(InvalidTemplateValue, match=message):
        _create(writer, user, **fields)


def test_names_are_unique_per_user(writer: Session, user: User, other: User) -> None:
    _create(writer, user)
    with pytest.raises(DuplicateTemplateName):
        _create(writer, user)
    _create(writer, other)  # another user's name is theirs


def test_writes_need_a_writer_session(session: Session, writer: Session, user: User) -> None:
    writer.commit()
    with pytest.raises(RuntimeError, match="writer session"):
        _create(session, user)


def test_get_is_scoped_to_the_user(writer: Session, user: User, other: User) -> None:
    row = _create(writer, user)
    assert get_template(writer, user, row.id) is row
    with pytest.raises(TemplateNotFound):
        get_template(writer, other, row.id)
    with pytest.raises(TemplateNotFound):
        update_template(writer, other, row.id, body="x")
    with pytest.raises(TemplateNotFound):
        delete_template(writer, other, row.id)


def test_list_is_by_name_and_only_the_users_own(writer: Session, user: User, other: User) -> None:
    _create(writer, user, "b")
    _create(writer, user, "a")
    _create(writer, other, "c")
    assert [row.name for row in list_templates(writer, user)] == ["a", "b"]
    assert [row.name for row in list_templates(writer, other)] == ["c"]


# --- update and versions ----------------------------------------------------------


def test_an_edit_of_a_template_not_in_use_changes_it_in_place(writer: Session, user: User) -> None:
    row = _create(writer, user)
    edited = update_template(writer, user, row.id, name="renamed", body="Yo {{ nickname }}")
    assert edited is row
    assert (row.name, row.body, row.subject, row.version) == ("renamed", "Yo {{ nickname }}",
                                                              "Hello", 1)  # fmt: skip
    assert [LintIssue.from_json(i).field for i in row.lint_json] == ["nickname", None]
    assert list_templates(writer, user) == [row]


def test_subject_unset_is_left_alone_and_none_clears_it(writer: Session, user: User) -> None:
    row = _create(writer, user)
    update_template(writer, user, row.id, subject=UNSET, body="Hey {{ title }}")
    assert row.subject == "Hello"
    cleared = update_template(writer, user, row.id, subject=None)
    assert cleared is row and cleared.subject is None
    assert [LintIssue.from_json(i).rule for i in cleared.lint_json] == [LintRule.MISSING_SUBJECT]


def test_changing_the_channel_relints(writer: Session, user: User) -> None:
    row = _create(writer, user, channel=LINKEDIN, subject=None)
    assert row.lint_json == []
    update_template(writer, user, row.id, channel=EMAIL)
    assert [LintIssue.from_json(i).rule for i in row.lint_json] == [LintRule.MISSING_SUBJECT]


def test_an_edit_that_changes_nothing_is_a_no_op(
    writer: Session, user: User, in_use: set[int]
) -> None:
    row = _create(writer, user)
    in_use.add(row.id)
    assert update_template(writer, user, row.id, name="reconnect") is row
    assert list(writer.scalars(scoped(user, Template))) == [row]


def test_a_rename_onto_another_template_is_refused(writer: Session, user: User) -> None:
    _create(writer, user, "taken")
    row = _create(writer, user, "mine")
    with pytest.raises(DuplicateTemplateName):
        update_template(writer, user, row.id, name="taken")


def test_an_edit_of_a_template_in_use_makes_a_new_version(
    writer: Session, user: User, in_use: set[int]
) -> None:
    """Spec 8.5: the campaign keeps pointing at the old row until someone upgrades it."""
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, body="Hey {{ first_name }}")

    assert new is not old
    assert (new.version, new.previous_id, new.name) == (2, old.id, "reconnect")
    assert (new.body, new.subject) == ("Hey {{ first_name }}", "Hello")
    assert old.body == BODY and old.version == 1  # left exactly as it was
    assert is_superseded(writer, user, old) and not is_superseded(writer, user, new)
    assert list_templates(writer, user) == [new]
    assert versions(writer, user, new) == [new, old]

    # The name is the template's, not a clash with its own older version.
    in_use.add(new.id)
    third = update_template(writer, user, new.id, name="renamed")
    assert (third.version, third.previous_id, third.name) == (3, new.id, "renamed")
    assert versions(writer, user, third) == [third, new, old]


def test_an_older_version_is_read_only(writer: Session, user: User, in_use: set[int]) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, body="Hey {{ first_name }}")
    assert get_template(writer, user, old.id) is old  # still readable
    with pytest.raises(TemplateSuperseded, match="edit the newest"):
        update_template(writer, user, old.id, body="Yo {{ first_name }}")
    with pytest.raises(TemplateSuperseded, match="delete the newest"):
        delete_template(writer, user, old.id)
    assert list_templates(writer, user) == [new]


def test_a_new_template_cannot_take_the_name_of_a_versioned_one(
    writer: Session, user: User, in_use: set[int]
) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    update_template(writer, user, old.id, body="Hey {{ first_name }}")
    with pytest.raises(DuplicateTemplateName):
        _create(writer, user)


def test_a_renamed_versioned_template_frees_its_old_name(
    writer: Session, user: User, in_use: set[int]
) -> None:
    """The old name stays on the superseded row, which lists nowhere, so it is free again."""
    old = _create(writer, user, "reconnect")
    in_use.add(old.id)
    renamed = update_template(writer, user, old.id, name="catch up")
    assert (renamed.name, old.name) == ("catch up", "reconnect")

    reused = _create(writer, user, "reconnect")
    assert reused.version == 1 and reused.previous_id is None
    assert [row.name for row in list_templates(writer, user)] == ["catch up", "reconnect"]
    assert versions(writer, user, renamed) == [renamed, old]
    assert versions(writer, user, reused) == [reused]


def test_two_edits_of_one_version_in_use_the_second_is_superseded(
    writer: Session, user: User, in_use: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both edits can pass is_superseded() before either inserts (PostgreSQL's concurrent
    writers). UNIQUE(user_id, previous_id) lets one win; the other is a 409, not a 500."""
    old = _create(writer, user)
    in_use.add(old.id)
    first = update_template(writer, user, old.id, body="Hey {{ first_name }}")
    # The second edit read the row before the first one's insert, so it saw no successor.
    monkeypatch.setattr(service, "is_superseded", lambda _session, _user, _row: False)
    with pytest.raises(TemplateSuperseded, match="replaced by another edit just now"):
        update_template(writer, user, old.id, body="Yo {{ first_name }}")

    # The savepoint kept the session usable, and the first edit stands.
    monkeypatch.undo()
    assert list_templates(writer, user) == [first]
    assert versions(writer, user, first) == [first, old]
    assert _create(writer, user, "after").id is not None


# --- delete -----------------------------------------------------------------


def test_delete_removes_the_whole_chain(writer: Session, user: User, in_use: set[int]) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, body="Hey {{ first_name }}")
    in_use.clear()
    keep = _create(writer, user, "keep")
    delete_template(writer, user, new.id)
    assert list(writer.scalars(scoped(user, Template))) == [keep]


def test_delete_is_refused_while_any_version_is_in_use(
    writer: Session, user: User, in_use: set[int]
) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, body="Hey {{ first_name }}")
    with pytest.raises(TemplateInUse):
        delete_template(writer, user, new.id)
    assert versions(writer, user, new) == [new, old]


def _use(session: Session, user: User, row: Template, status: CampaignStatus) -> Campaign:
    """A campaign in ``status`` whose one step sends ``row``."""
    campaign = factories.make_campaign(session, user, status=status)
    campaign.steps[0].template_id = row.id
    session.flush()
    return campaign


@pytest.mark.parametrize("status", sorted(CampaignStatus))
def test_a_version_is_in_use_once_a_campaign_naming_it_leaves_draft(
    writer: Session, user: User, status: CampaignStatus
) -> None:
    row = _create(writer, user)
    assert service.is_in_use(writer, user, row) is False
    _use(writer, user, row, status)
    assert service.is_in_use(writer, user, row) is (status is not CampaignStatus.DRAFT)
    assert service.is_referenced(writer, user, row) is True


def test_in_use_ids_answers_for_many_templates_at_once(writer: Session, user: User) -> None:
    active, draft, free = (_create(writer, user, name) for name in ("active", "draft", "free"))
    _use(writer, user, active, CampaignStatus.ACTIVE)
    _use(writer, user, draft, CampaignStatus.DRAFT)
    assert service.in_use_ids(writer, user, [active.id, draft.id, free.id]) == {active.id}
    assert service.in_use_ids(writer, user, []) == set()


def test_the_frozen_statuses_are_every_status_past_draft() -> None:
    assert {s.value for s in service.IN_USE_STATUSES} == {
        "reviewing",
        "active",
        "paused",
        "completed",
        "archived",
    }


def test_an_edit_while_only_a_draft_campaign_names_it_is_in_place(
    writer: Session, user: User
) -> None:
    row = _create(writer, user)
    draft = _use(writer, user, row, CampaignStatus.DRAFT)
    same = update_template(writer, user, row.id, body="Hey {{ first_name }}")
    assert same is row and draft.steps[0].template_id == row.id

    draft.status = CampaignStatus.REVIEWING
    writer.flush()
    new = update_template(writer, user, row.id, body="Yo {{ first_name }}")
    assert new.id != row.id and new.previous_id == row.id
    assert row.body == "Hey {{ first_name }}"  # what the campaign under review sends


def test_delete_is_refused_while_even_a_draft_campaign_names_it(
    writer: Session, user: User
) -> None:
    row = _create(writer, user)
    _use(writer, user, row, CampaignStatus.DRAFT)
    with pytest.raises(TemplateInUse):
        delete_template(writer, user, row.id)
    assert get_template(writer, user, row.id) is row


def test_another_users_campaign_never_puts_a_template_in_use(writer: Session, user: User) -> None:
    row = _create(writer, user)
    stranger = factories.make_user(writer)
    campaign = factories.make_campaign(writer, stranger)
    # A cross-user reference the service never makes; the scoped lookups must ignore it.
    campaign.steps[0].template_id = row.id
    writer.flush()
    assert service.is_in_use(writer, user, row) is False
    assert service.is_referenced(writer, user, row) is False


# --- the activation gate -------------------------------------------------------


def test_activation_errors_are_empty_for_a_clean_template(writer: Session, user: User) -> None:
    assert activation_errors(_create(writer, user)) == []


def test_activation_errors_list_every_lint_error(writer: Session, user: User) -> None:
    row = _create(writer, user, subject=None, body="{{ nickname }} http:/x.example")
    errors = activation_errors(row)
    assert [e.rule for e in errors] == [
        LintRule.MISSING_SUBJECT,
        LintRule.UNDEFINED_VARIABLE,
        LintRule.BAD_LINK,
        LintRule.NO_CONTACT_FIELD,
    ]
    assert all(e.severity is Severity.ERROR for e in errors)


def test_a_template_saved_with_a_me_field_before_342_is_blocked_by_activation(
    writer: Session, user: User
) -> None:
    """Stored lint from before #342 is clean; activation lints again and finds the removed
    field, so the template stays out of an active campaign until it is edited."""
    row = _create(writer, user, body="{{ first_name }} {{ me.signature }}")
    row.lint_json = []  # as a save before #342 stored it
    writer.flush()
    errors = activation_errors(row)
    assert [(e.rule, e.field) for e in errors] == [(LintRule.REMOVED_FIELD, "me.signature")]


# --- contact fields and the preview ---------------------------------------------------


def test_contact_fields_map_the_contact_onto_the_spec_names(writer: Session, user: User) -> None:
    contact = factories.make_contact(
        writer,
        user,
        first_name="Robert",
        preferred_name="Bob",
        last_name="Fixture",
        current_company="Fixture Co",
        current_title="Engineer",
        location="Springfield",
        connected_on=date(2019, 9, 27),
        positions=[
            {"title": "Engineer", "started_on": date(2025, 3, 1)},
            {"title": "Intern", "started_on": date(2018, 6, 1), "ended_on": date(2025, 2, 1)},
            {"title": "Undated"},
        ],
    )
    fields = contact_fields(contact, date(2026, 9, 26))
    assert list(fields) == list(CONTACT_FIELDS)
    assert fields == {
        "first_name": "Bob",
        "last_name": "Fixture",
        "company": "Fixture Co",
        "title": "Engineer",
        "location": "Springfield",
        "connected_year": 2019,
        "years_since_connected": 6,  # 27 September has not come round yet
        "last_position_change": date(2025, 3, 1),
    }
    assert contact_fields(contact, date(2026, 9, 27))["years_since_connected"] == 7


TODAY = date(2026, 9, 26)


def _last_change(writer: Session, user: User, *positions: dict[str, object]) -> object:
    contact = factories.make_contact(writer, user, positions=positions)
    return contact_fields(contact, TODAY)["last_position_change"]


def test_last_position_change_counts_an_end_later_than_every_start(
    writer: Session, user: User
) -> None:
    """A departure with nothing newer to go to is still a change (#232)."""
    assert _last_change(
        writer,
        user,
        {"title": "Lead", "started_on": date(2020, 1, 1), "ended_on": date(2026, 5, 1)},
        {"title": "Engineer", "started_on": date(2016, 1, 1), "ended_on": date(2019, 12, 1)},
    ) == date(2026, 5, 1)


def test_last_position_change_sees_a_departure_from_the_main_job_under_an_old_side_role(
    writer: Session, user: User
) -> None:
    """Leaving the main job while keeping an advisory role taken years ago: start
    dates alone would report the advisory start."""
    assert _last_change(
        writer,
        user,
        {"title": "Advisor", "started_on": date(2019, 4, 1)},
        {"title": "VP", "started_on": date(2021, 2, 1), "ended_on": date(2026, 8, 15)},
    ) == date(2026, 8, 15)


def test_last_position_change_counts_the_end_of_an_overlapping_side_role(
    writer: Session, user: User
) -> None:
    """Only the side role ended; the main job is untouched. That still counts, which
    #232 accepts as the rule's known limit."""
    assert _last_change(
        writer,
        user,
        {"title": "Engineer", "started_on": date(2018, 3, 1)},
        {"title": "Board member", "started_on": date(2022, 1, 1), "ended_on": date(2026, 2, 1)},
    ) == date(2026, 2, 1)


def test_last_position_change_ignores_an_end_date_after_today(writer: Session, user: User) -> None:
    """An announced departure has not happened yet."""
    assert _last_change(
        writer,
        user,
        {"title": "Engineer", "started_on": date(2021, 6, 1), "ended_on": date(2026, 12, 31)},
    ) == date(2021, 6, 1)


def test_last_position_change_ignores_an_end_date_of_tomorrow(writer: Session, user: User) -> None:
    """The boundary is today itself, not some grace period after it."""
    assert _last_change(
        writer,
        user,
        {
            "title": "Engineer",
            "started_on": date(2021, 6, 1),
            "ended_on": TODAY + timedelta(days=1),
        },
    ) == date(2021, 6, 1)


def test_last_position_change_counts_an_end_date_with_no_start_date(
    writer: Session, user: User
) -> None:
    assert _last_change(
        writer,
        user,
        {"title": "Engineer", "ended_on": date(2025, 11, 1)},
    ) == date(2025, 11, 1)


def test_last_position_change_ignores_a_start_date_after_today(writer: Session, user: User) -> None:
    """An announced new job has not happened yet either (#255): "congrats on the
    move" before the move is wrong."""
    assert _last_change(
        writer,
        user,
        {"title": "Engineer", "started_on": date(2021, 6, 1), "ended_on": date(2026, 8, 31)},
        {"title": "Lead", "started_on": TODAY + timedelta(days=1)},
    ) == date(2026, 8, 31)


def test_last_position_change_counts_a_start_date_of_today(writer: Session, user: User) -> None:
    assert _last_change(writer, user, {"title": "Lead", "started_on": TODAY}) == TODAY


def test_last_position_change_measures_against_the_today_it_is_given(
    writer: Session, user: User
) -> None:
    """Far from the real date, so reading the clock instead of ``today`` shows."""
    contact = factories.make_contact(
        writer,
        user,
        positions=[
            {"title": "Engineer", "started_on": date(2010, 1, 1), "ended_on": date(2012, 5, 1)},
            {"title": "Lead", "started_on": date(2012, 6, 1), "ended_on": date(2030, 1, 1)},
        ],
    )
    assert contact_fields(contact, date(2012, 5, 15))["last_position_change"] == date(2012, 5, 1)
    assert contact_fields(contact, date(2040, 1, 1))["last_position_change"] == date(2030, 1, 1)


def test_last_position_change_counts_an_end_date_of_today(writer: Session, user: User) -> None:
    assert (
        _last_change(
            writer,
            user,
            {"title": "Engineer", "started_on": date(2021, 6, 1), "ended_on": TODAY},
        )
        == TODAY
    )


def test_last_position_change_is_missing_when_no_position_has_a_date(
    writer: Session, user: User
) -> None:
    assert _last_change(writer, user, {"title": "Engineer"}, {"title": "Intern"}) is None


def test_last_position_change_is_missing_without_positions(writer: Session, user: User) -> None:
    """Missing, so a template renders it empty with a warning (covered below)."""
    assert _last_change(writer, user) is None


def test_first_name_falls_back_when_there_is_no_preferred_name(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user, first_name="Robert", preferred_name="")
    assert contact_fields(contact, date(2026, 9, 26))["first_name"] == "Robert"


def test_preview_renders_for_one_contact(writer: Session, user: User) -> None:
    row = _create(writer, user, subject="Hi {{ first_name }}", body="{{ company }} / Ada")
    contact = factories.make_contact(writer, user, preferred_name="Bo", current_company="Co")
    rendered = render_preview(row, contact, today=date(2026, 9, 26))
    assert (rendered.subject, rendered.body, rendered.issues) == ("Hi Bo", "Co / Ada", ())


def test_preview_of_a_contact_with_missing_fields_warns_and_does_not_raise(
    writer: Session, user: User
) -> None:
    """Done when (P3-03): missing fields are a lint warning, not an exception."""
    row = _create(
        writer,
        user,
        body="Hi {{ first_name }} from {{ company }}, since {{ connected_year }}. "
        "{{ last_position_change | ago }} {{ previous_send_date | ago }} {{ personal_line }}",
    )
    contact = factories.make_contact(
        writer, user, first_name="", preferred_name="", current_company=None
    )
    rendered = render_preview(row, contact, today=date(2026, 9, 26))
    assert rendered.body == "Hi  from , since .   "
    assert [(i.rule, i.severity, i.field) for i in rendered.issues] == [
        (LintRule.MISSING_VALUE, Severity.WARNING, name)
        for name in (
            "first_name",
            "company",
            "connected_year",
            "last_position_change",
            "previous_send_date",
            "personal_line",
        )
    ]


def test_preview_fills_a_personal_line_when_given(writer: Session, user: User) -> None:
    row = _create(writer, user, body="{{ personal_line }}")
    contact = factories.make_contact(writer, user)
    rendered = render_preview(row, contact, personal_line="Loved your talk.")
    assert rendered.body == "Loved your talk." and rendered.issues == ()


def test_preview_refuses_a_sandbox_escape(writer: Session, user: User) -> None:
    row = _create(writer, user, body="{{ first_name.__class__ }}")
    contact = factories.make_contact(writer, user)
    with pytest.raises(TemplateRenderError):
        render_preview(row, contact)


def test_preview_refuses_another_users_contact(writer: Session, user: User, other: User) -> None:
    row = _create(writer, user)
    contact = factories.make_contact(writer, other)
    with pytest.raises(ValueError, match="its own user's contact"):
        render_preview(row, contact)


def test_preview_uses_the_users_local_date(
    writer: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#357: at 03:00 UTC on 21 September it is still the 20th in Los Angeles, so a job
    starting on the 21st has not started for this user, as the engine sees it."""
    monkeypatch.setattr(service, "utcnow", lambda: datetime(2026, 9, 21, 3, 0, tzinfo=UTC))
    row = _create(writer, user, body="Changed {{ last_position_change }}")
    contact = factories.make_contact(
        writer,
        user,
        positions=[
            {"title": "Lead", "started_on": date(2026, 9, 21)},
            {"title": "Engineer", "started_on": date(2021, 6, 1), "ended_on": date(2026, 9, 1)},
        ],
    )
    local = render_preview(row, contact, timezone="America/Los_Angeles")
    assert local.body == "Changed 2026-09-01"
    assert render_preview(row, contact, timezone="UTC").body == "Changed 2026-09-21"
    # An unreadable zone falls back to UTC rather than failing the preview.
    assert render_preview(row, contact, timezone="Nowhere/Land").body == "Changed 2026-09-21"
    examples = {
        e.field.name: e.example
        for e in service.field_examples(contact, timezone="America/Los_Angeles")
    }
    assert examples["last_position_change"] == "2026-09-01"
