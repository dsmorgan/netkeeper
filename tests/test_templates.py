"""netkeeper.campaigns.templates (spec 8.5, 11.1; item P3-03): storage, versions, the gate."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
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
from netkeeper.models import Template, TemplateChannel, User, UserKind
from netkeeper.scoping import scoped

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
ME_KEYS = ("name", "website", "scheduling_link", "signature", "city")
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
    return create_template(session, user, name=name, me_keys=ME_KEYS, **values)


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
        update_template(writer, other, row.id, me_keys=ME_KEYS, body="x")
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
    edited = update_template(
        writer, user, row.id, me_keys=ME_KEYS, name="renamed", body="Yo {{ nickname }}"
    )
    assert edited is row
    assert (row.name, row.body, row.subject, row.version) == ("renamed", "Yo {{ nickname }}",
                                                              "Hello", 1)  # fmt: skip
    assert [LintIssue.from_json(i).field for i in row.lint_json] == ["nickname", None]
    assert list_templates(writer, user) == [row]


def test_subject_unset_is_left_alone_and_none_clears_it(writer: Session, user: User) -> None:
    row = _create(writer, user)
    update_template(writer, user, row.id, me_keys=ME_KEYS, subject=UNSET, body="Hey {{ title }}")
    assert row.subject == "Hello"
    cleared = update_template(writer, user, row.id, me_keys=ME_KEYS, subject=None)
    assert cleared is row and cleared.subject is None
    assert [LintIssue.from_json(i).rule for i in cleared.lint_json] == [LintRule.MISSING_SUBJECT]


def test_changing_the_channel_relints(writer: Session, user: User) -> None:
    row = _create(writer, user, channel=LINKEDIN, subject=None)
    assert row.lint_json == []
    update_template(writer, user, row.id, me_keys=ME_KEYS, channel=EMAIL)
    assert [LintIssue.from_json(i).rule for i in row.lint_json] == [LintRule.MISSING_SUBJECT]


def test_an_edit_that_changes_nothing_is_a_no_op(
    writer: Session, user: User, in_use: set[int]
) -> None:
    row = _create(writer, user)
    in_use.add(row.id)
    assert update_template(writer, user, row.id, me_keys=ME_KEYS, name="reconnect") is row
    assert list(writer.scalars(scoped(user, Template))) == [row]


def test_a_rename_onto_another_template_is_refused(writer: Session, user: User) -> None:
    _create(writer, user, "taken")
    row = _create(writer, user, "mine")
    with pytest.raises(DuplicateTemplateName):
        update_template(writer, user, row.id, me_keys=ME_KEYS, name="taken")


def test_an_edit_of_a_template_in_use_makes_a_new_version(
    writer: Session, user: User, in_use: set[int]
) -> None:
    """Spec 8.5: the campaign keeps pointing at the old row until someone upgrades it."""
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, me_keys=ME_KEYS, body="Hey {{ first_name }}")

    assert new is not old
    assert (new.version, new.previous_id, new.name) == (2, old.id, "reconnect")
    assert (new.body, new.subject) == ("Hey {{ first_name }}", "Hello")
    assert old.body == BODY and old.version == 1  # left exactly as it was
    assert is_superseded(writer, user, old) and not is_superseded(writer, user, new)
    assert list_templates(writer, user) == [new]
    assert versions(writer, user, new) == [new, old]

    # The name is the template's, not a clash with its own older version.
    in_use.add(new.id)
    third = update_template(writer, user, new.id, me_keys=ME_KEYS, name="renamed")
    assert (third.version, third.previous_id, third.name) == (3, new.id, "renamed")
    assert versions(writer, user, third) == [third, new, old]


def test_an_older_version_is_read_only(writer: Session, user: User, in_use: set[int]) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, me_keys=ME_KEYS, body="Hey {{ first_name }}")
    assert get_template(writer, user, old.id) is old  # still readable
    with pytest.raises(TemplateSuperseded, match="edit the newest"):
        update_template(writer, user, old.id, me_keys=ME_KEYS, body="Yo {{ first_name }}")
    with pytest.raises(TemplateSuperseded, match="delete the newest"):
        delete_template(writer, user, old.id)
    assert list_templates(writer, user) == [new]


def test_a_new_template_cannot_take_the_name_of_a_versioned_one(
    writer: Session, user: User, in_use: set[int]
) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    update_template(writer, user, old.id, me_keys=ME_KEYS, body="Hey {{ first_name }}")
    with pytest.raises(DuplicateTemplateName):
        _create(writer, user)


def test_a_renamed_versioned_template_frees_its_old_name(
    writer: Session, user: User, in_use: set[int]
) -> None:
    """The old name stays on the superseded row, which lists nowhere, so it is free again."""
    old = _create(writer, user, "reconnect")
    in_use.add(old.id)
    renamed = update_template(writer, user, old.id, me_keys=ME_KEYS, name="catch up")
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
    first = update_template(writer, user, old.id, me_keys=ME_KEYS, body="Hey {{ first_name }}")
    # The second edit read the row before the first one's insert, so it saw no successor.
    monkeypatch.setattr(service, "is_superseded", lambda _session, _user, _row: False)
    with pytest.raises(TemplateSuperseded, match="replaced by another edit just now"):
        update_template(writer, user, old.id, me_keys=ME_KEYS, body="Yo {{ first_name }}")

    # The savepoint kept the session usable, and the first edit stands.
    monkeypatch.undo()
    assert list_templates(writer, user) == [first]
    assert versions(writer, user, first) == [first, old]
    assert _create(writer, user, "after").id is not None


# --- delete -----------------------------------------------------------------


def test_delete_removes_the_whole_chain(writer: Session, user: User, in_use: set[int]) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, me_keys=ME_KEYS, body="Hey {{ first_name }}")
    in_use.clear()
    keep = _create(writer, user, "keep")
    delete_template(writer, user, new.id)
    assert list(writer.scalars(scoped(user, Template))) == [keep]


def test_delete_is_refused_while_any_version_is_in_use(
    writer: Session, user: User, in_use: set[int]
) -> None:
    old = _create(writer, user)
    in_use.add(old.id)
    new = update_template(writer, user, old.id, me_keys=ME_KEYS, body="Hey {{ first_name }}")
    with pytest.raises(TemplateInUse):
        delete_template(writer, user, new.id)
    assert versions(writer, user, new) == [new, old]


def test_nothing_is_in_use_until_campaigns_exist(writer: Session, user: User) -> None:
    row = _create(writer, user)
    assert service.is_in_use(writer, user, row) is False


# --- the activation gate -------------------------------------------------------


def test_activation_errors_are_empty_for_a_clean_template(writer: Session, user: User) -> None:
    assert activation_errors(_create(writer, user), ME_KEYS) == []


def test_activation_errors_list_every_lint_error(writer: Session, user: User) -> None:
    row = _create(writer, user, subject=None, body="{{ me.podcast }} http:/x.example")
    errors = activation_errors(row, ME_KEYS)
    assert [e.rule for e in errors] == [
        LintRule.MISSING_SUBJECT,
        LintRule.UNDEFINED_VARIABLE,
        LintRule.BAD_LINK,
        LintRule.NO_CONTACT_FIELD,
    ]
    assert all(e.severity is Severity.ERROR for e in errors)


def test_activation_lints_again_against_the_config_of_the_moment(
    writer: Session, user: User
) -> None:
    """A ``me`` key removed from the config after the save must block activation."""
    row = _create(writer, user, body="{{ first_name }} {{ me.podcast }}")
    # Saved while [me] had a podcast key:
    update_template(writer, user, row.id, me_keys=(*ME_KEYS, "podcast"), body=row.body + " ")
    assert row.lint_json == []
    assert [e.field for e in activation_errors(row, ME_KEYS)] == ["me.podcast"]
    assert activation_errors(row, (*ME_KEYS, "podcast")) == []


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


def test_first_name_falls_back_when_there_is_no_preferred_name(writer: Session, user: User) -> None:
    contact = factories.make_contact(writer, user, first_name="Robert", preferred_name="")
    assert contact_fields(contact, date(2026, 9, 26))["first_name"] == "Robert"


def test_preview_renders_for_one_contact(writer: Session, user: User) -> None:
    row = _create(writer, user, subject="Hi {{ first_name }}", body="{{ company }} / {{ me.name }}")
    contact = factories.make_contact(writer, user, preferred_name="Bo", current_company="Co")
    rendered = render_preview(row, contact, me={"name": "Ada"}, today=date(2026, 9, 26))
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
    rendered = render_preview(row, contact, me={}, today=date(2026, 9, 26))
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
    rendered = render_preview(row, contact, me={}, personal_line="Loved your talk.")
    assert rendered.body == "Loved your talk." and rendered.issues == ()


def test_preview_refuses_a_sandbox_escape(writer: Session, user: User) -> None:
    row = _create(writer, user, body="{{ first_name.__class__ }}")
    contact = factories.make_contact(writer, user)
    with pytest.raises(TemplateRenderError):
        render_preview(row, contact, me={})


def test_preview_refuses_another_users_contact(writer: Session, user: User, other: User) -> None:
    row = _create(writer, user)
    contact = factories.make_contact(writer, other)
    with pytest.raises(ValueError, match="its own user's contact"):
        render_preview(row, contact, me={})
