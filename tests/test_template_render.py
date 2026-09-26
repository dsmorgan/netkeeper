"""netkeeper.campaigns.render (spec 11.1; item P3-03): the sandbox, merge fields, ``ago``, lint."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from netkeeper.campaigns.render import (
    CONTACT_FIELDS,
    ME_FIELDS,
    LintIssue,
    LintRule,
    MergeValues,
    Part,
    Severity,
    TemplateRenderError,
    ago,
    has_errors,
    lint,
    me_fields,
    render,
)
from netkeeper.config import MeSettings
from netkeeper.models import TemplateChannel

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
TODAY = date(2026, 9, 26)
ME = {"name": "Ada Fixture", "website": "https://ada.example", "scheduling_link": "",
      "signature": "Ada", "city": ""}  # fmt: skip
GOOD_BODY = "Hi {{ first_name }}, {{ me.name }} here."


def _rules(issues: list[LintIssue]) -> list[tuple[LintRule, str | None]]:
    return [(issue.rule, issue.field) for issue in issues]


def _values(**contact: object) -> MergeValues:
    return MergeValues(contact=contact, me=ME)


# --- lint: a clean template -----------------------------------------------------------


def test_a_template_naming_every_merge_field_lints_clean() -> None:
    contact = " ".join(f"{{{{ {name} }}}}" for name in CONTACT_FIELDS)
    me = " ".join(f"{{{{ me.{name} }}}}" for name in ME_FIELDS)
    body = (
        f"{contact} {me} {{{{ campaign.name }}}} {{{{ step.number }}}} "
        "{{ previous_send_date | ago }} {{ personal_line }} {{ me['name'] }}"
        "{% if company %}at {{ company | upper }}{% endif %}"
        "{% for word in ['a', 'b'] %}{{ word }}{% endfor %}{% set x = 1 %}{{ x }}"
    )
    assert lint(EMAIL, "Hello {{ first_name }}", body, ME.keys()) == []
    assert not has_errors([])


def test_extra_me_keys_from_the_config_are_merge_fields() -> None:
    me = me_fields(MeSettings(name="Ada", extra={"podcast": "Fixture Hour"}))
    assert me["podcast"] == "Fixture Hour" and set(ME_FIELDS) <= set(me)
    body = "{{ first_name }}: {{ me.podcast }}"
    assert lint(LINKEDIN, None, body, me.keys()) == []
    assert _rules(lint(LINKEDIN, None, body, ME.keys())) == [
        (LintRule.UNDEFINED_VARIABLE, "me.podcast")
    ]


# --- lint: each rule is an error --------------------------------------------------------


def test_undefined_variables_are_errors() -> None:
    body = "{{ first_name }} {{ nickname }} {{ me.age }} {{ campaign.owner }} {{ step.when }}"
    issues = lint(LINKEDIN, None, body, ME.keys())
    assert _rules(issues) == [
        (LintRule.UNDEFINED_VARIABLE, "nickname"),
        (LintRule.UNDEFINED_VARIABLE, "me.age"),
        (LintRule.UNDEFINED_VARIABLE, "campaign.owner"),
        (LintRule.UNDEFINED_VARIABLE, "step.when"),
    ]
    assert all(issue.severity is Severity.ERROR for issue in issues)
    assert has_errors(issues)


def test_a_group_of_fields_used_whole_or_computed_is_an_error() -> None:
    body = "{{ first_name }} {{ me }} {% for k in me %}{% endfor %} {{ me[first_name] }}"
    issues = lint(LINKEDIN, None, body, ME.keys())
    assert [(issue.rule, issue.field, issue.message) for issue in issues] == [
        (
            LintRule.UNDEFINED_VARIABLE,
            "me",
            "`me` is a group of fields; name one, like `me.name`",
        ),
        (LintRule.UNDEFINED_VARIABLE, "me", "`me[...]` needs the field name written out"),
    ]


def test_the_globals_jinja_ships_are_not_merge_fields() -> None:
    body = "{{ first_name }}{% for i in range(3) %}{% endfor %}{{ cycler }}{{ lipsum() }}"
    assert {name for _, name in _rules(lint(LINKEDIN, None, body, ME.keys()))} == {
        "range",
        "cycler",
        "lipsum",
    }


def test_a_body_with_no_per_contact_field_is_an_error() -> None:
    issues = lint(LINKEDIN, None, "Hi, {{ me.name }} here. {{ campaign.name }}", ME.keys())
    assert _rules(issues) == [(LintRule.NO_CONTACT_FIELD, None)]
    assert issues[0].part is Part.BODY and issues[0].severity is Severity.ERROR


def test_a_contact_field_in_the_subject_alone_does_not_count() -> None:
    issues = lint(EMAIL, "Hi {{ first_name }}", "Hello there", ME.keys())
    assert _rules(issues) == [(LintRule.NO_CONTACT_FIELD, None)]


def test_personal_line_counts_as_a_per_contact_field() -> None:
    assert lint(LINKEDIN, None, "{{ personal_line }}", ME.keys()) == []


@pytest.mark.parametrize("subject", [None, "", "   "])
def test_an_email_with_no_subject_is_an_error(subject: str | None) -> None:
    issues = lint(EMAIL, subject, GOOD_BODY, ME.keys())
    assert _rules(issues) == [(LintRule.MISSING_SUBJECT, None)]
    assert issues[0].part is Part.SUBJECT


def test_a_linkedin_message_needs_no_subject() -> None:
    assert lint(LINKEDIN, None, GOOD_BODY, ME.keys()) == []


@pytest.mark.parametrize(
    "link",
    [
        "http:/example.com",
        "https:example.com",
        "https://",
        "https:///path",
        "https://..example.com",
        "http://example.com:99999",
        "https://[::1",
        "https://ex!ample.com",
    ],
)
def test_links_that_do_not_parse_are_errors(link: str) -> None:
    issues = lint(LINKEDIN, None, f"{{{{ first_name }}}}, see {link} today", ME.keys())
    assert [issue.rule for issue in issues] == [LintRule.BAD_LINK]
    assert issues[0].severity is Severity.ERROR


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com",
        "https://example.com/path?q=1#top.",  # trailing full stop is the sentence's
        "(https://example.com/a)",
        "HTTPS://EXAMPLE.COM",
        "http://localhost:8000/x",
        "https://[::1]:8443/",
        "https://bücher.example/",
        "https://{{ me.website }}",  # completed by a merge field
        "https://example.com/{{ first_name }}",
        "{{ me.website }}/calendar",
        "mailto:ada@example.test and ftp://files.example",  # not http(s); not checked
    ],
)
def test_links_that_parse_or_are_completed_by_a_field_pass(text: str) -> None:
    assert lint(LINKEDIN, None, f"{{{{ first_name }}}} {text}", ME.keys()) == []


def test_bad_links_in_the_subject_are_reported_against_it() -> None:
    issues = lint(EMAIL, "See http:/x.example", GOOD_BODY, ME.keys())
    assert [(i.rule, i.part) for i in issues] == [(LintRule.BAD_LINK, Part.SUBJECT)]


def test_a_template_that_does_not_compile_is_a_syntax_error() -> None:
    for body in ["{{ first_name ", "{% if first_name %}", "{{ first_name | no_such_filter }}"]:
        issues = lint(LINKEDIN, None, body, ME.keys())
        assert [issue.rule for issue in issues] == [LintRule.SYNTAX], body
        assert issues[0].message.startswith("line 1: ")


@pytest.mark.parametrize(
    "tag",
    [
        "{% include 'other' %}",
        "{% extends 'base' %}",
        "{% import 'macros' as m %}",
        "{% from 'macros' import greet %}",
    ],
)
def test_reaching_for_another_template_is_a_syntax_error(tag: str) -> None:
    """Each compiles and then fails at render, so it has to fail lint: lint is the gate."""
    body = "{{ first_name }}\n" + tag
    issues = lint(LINKEDIN, None, body, ME.keys())
    assert [issue.rule for issue in issues] == [LintRule.SYNTAX]
    assert issues[0].message == "line 2: a template cannot extend, include, or import another"
    with pytest.raises(TemplateRenderError):
        render(LINKEDIN, None, body, _values(first_name="A"), today=TODAY)


def test_lint_issues_round_trip_through_json() -> None:
    issue = LintIssue(LintRule.BAD_LINK, Severity.WARNING, Part.BODY, "broken", "http:/x")
    assert issue.to_json() == {
        "rule": "bad_link",
        "severity": "warning",
        "part": "body",
        "message": "broken",
        "field": "http:/x",
    }
    assert LintIssue.from_json(issue.to_json()) == issue


# --- the sandbox ----------------------------------------------------------------


ESCAPES = [
    "{{ ''.__class__ }}",
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    "{{ first_name.__class__ }}",
    "{{ first_name.__init__.__globals__ }}",
    "{{ me.__dict__ }}",
    "{{ me._private }}",
    "{{ me['__class__'] }}",
    "{{ first_name | attr('__class__') }}",
    "{{ (first_name|attr('upper')).__self__ }}",
]


@pytest.mark.parametrize("escape", ESCAPES)
def test_underscore_names_fail_lint(escape: str) -> None:
    issues = lint(LINKEDIN, None, f"{{{{ first_name }}}} {escape}", ME.keys())
    assert LintRule.UNSAFE_ATTRIBUTE in {issue.rule for issue in issues}
    assert has_errors(issues)


@pytest.mark.parametrize("escape", ESCAPES)
def test_underscore_names_fail_the_render(escape: str) -> None:
    with pytest.raises(TemplateRenderError):
        render(LINKEDIN, None, f"{{{{ first_name }}}} {escape}", _values(first_name="A"),
               today=TODAY)  # fmt: skip


@pytest.mark.parametrize(
    "escape",
    [
        "{{ first_name | attr(x) }}",  # the name is computed, so lint cannot see it
        "{{ me[x] }}",
    ],
)
def test_the_sandbox_itself_refuses_what_lint_cannot_see(escape: str) -> None:
    body = "{% set x = '_' ~ '_class__' %}{{ first_name }} " + escape
    with pytest.raises(TemplateRenderError, match="sandbox"):
        render(LINKEDIN, None, body, _values(first_name="A"), today=TODAY)


def test_a_method_that_mutates_is_refused() -> None:
    body = "{% set l = [1] %}{{ l.append(2) }}{{ first_name }}"
    with pytest.raises(TemplateRenderError, match="sandbox"):
        render(LINKEDIN, None, body, _values(first_name="A"), today=TODAY)


def test_merge_values_are_data_never_template_source() -> None:
    """Imported data is not trusted: a contact's name that looks like a template stays text."""
    values = _values(first_name="{{ me.name }}", company="{% for x in y %}")
    rendered = render(LINKEDIN, None, "Hi {{ first_name }} at {{ company }}", values, today=TODAY)
    assert rendered.body == "Hi {{ me.name }} at {% for x in y %}"


def test_autoescape_is_off_for_plain_text() -> None:
    rendered = render(
        LINKEDIN, None, "{{ first_name }}", _values(first_name="Ann & <Bo>"), today=TODAY
    )
    assert rendered.body == "Ann & <Bo>"


def test_an_error_inside_the_template_is_a_render_error_not_a_crash() -> None:
    with pytest.raises(TemplateRenderError, match="TypeError"):
        render(LINKEDIN, None, "{{ first_name + 1 }}", _values(first_name="A"), today=TODAY)
    with pytest.raises(TemplateRenderError, match=r"^body: line 1: "):
        render(LINKEDIN, None, "{{ first_name ", _values(first_name="A"), today=TODAY)


# --- rendering ------------------------------------------------------------------


def test_render_fills_every_kind_of_field() -> None:
    values = MergeValues(
        contact={"first_name": "Bo", "company": "Fixture Co", "connected_year": 2019},
        me=ME,
        campaign_name="First 100",
        step_number=2,
        previous_send_date=datetime(2026, 9, 19, 23, 30, tzinfo=UTC),
        personal_line="Congrats on the move.",
    )
    rendered = render(
        EMAIL,
        "Re: {{ campaign.name }}",
        "Hi {{ first_name }} ({{ company }}, since {{ connected_year }}). "
        "Step {{ step.number }}, {{ previous_send_date | ago }}. {{ personal_line }} "
        "{{ me.signature }}",
        values,
        today=TODAY,
    )
    assert rendered.subject == "Re: First 100"
    assert rendered.body == (
        "Hi Bo (Fixture Co, since 2019). Step 2, last week. Congrats on the move. Ada"
    )
    assert rendered.issues == ()


def test_missing_fields_render_empty_with_a_warning_not_an_exception() -> None:
    values = MergeValues(contact={"first_name": "Bo", "company": None, "title": "  "}, me=ME)
    rendered = render(
        EMAIL,
        "{{ first_name }}, {{ campaign.name }}",
        "Hi {{ first_name }} at {{ company }} as {{ title }}, {{ location }}. "
        "{{ previous_send_date | ago }} {{ me.city }} {{ step.number }}",
        values,
        today=TODAY,
    )
    assert rendered.body == "Hi Bo at  as , .   "
    assert rendered.subject == "Bo, "
    warnings = [(i.part, i.field) for i in rendered.issues if i.severity is Severity.WARNING]
    assert warnings == [
        (Part.SUBJECT, "campaign.name"),
        (Part.BODY, "company"),
        (Part.BODY, "title"),
        (Part.BODY, "location"),
        (Part.BODY, "previous_send_date"),
        (Part.BODY, "me.city"),
        (Part.BODY, "step.number"),
    ]
    assert {i.rule for i in rendered.issues} == {LintRule.MISSING_VALUE}
    assert not has_errors(list(rendered.issues))


def test_a_missing_field_still_works_with_default_and_if() -> None:
    rendered = render(
        LINKEDIN,
        None,
        "{{ first_name }} {{ company | default('your team') }}{% if title %}!{% endif %}",
        _values(first_name="Bo"),
        today=TODAY,
    )
    assert rendered.body == "Bo your team"


def test_render_carries_lint_errors_along_with_the_output() -> None:
    rendered = render(LINKEDIN, None, "Hi {{ nickname }}", _values(), today=TODAY)
    assert rendered.body == "Hi "
    assert _rules(list(rendered.issues)) == [
        (LintRule.UNDEFINED_VARIABLE, "nickname"),
        (LintRule.NO_CONTACT_FIELD, None),
    ]


def test_a_link_broken_by_a_merge_value_is_a_warning() -> None:
    values = MergeValues(contact={"first_name": "Bo"}, me={**ME, "website": "https://"})
    rendered = render(LINKEDIN, None, "{{ first_name }} {{ me.website }}", values, today=TODAY)
    assert [(i.rule, i.severity, i.field) for i in rendered.issues] == [
        (LintRule.BAD_LINK, Severity.WARNING, "https://")
    ]


def test_a_link_lint_already_refused_is_not_reported_twice() -> None:
    rendered = render(LINKEDIN, None, "{{ first_name }} http:/x.example", _values(first_name="B"),
                      today=TODAY)  # fmt: skip
    assert [i.rule for i in rendered.issues] == [LintRule.BAD_LINK]


# --- ago --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "words"),
    [
        (-3, "today"),
        (0, "today"),
        (1, "yesterday"),
        (2, "2 days ago"),
        (6, "6 days ago"),
        (7, "last week"),
        (13, "last week"),
        (14, "2 weeks ago"),
        (27, "3 weeks ago"),
        (28, "last month"),
        (59, "last month"),
        (60, "2 months ago"),
        (364, "12 months ago"),
        (365, "last year"),
        (729, "last year"),
        (730, "2 years ago"),
        (3650, "10 years ago"),
    ],
)
def test_ago_phrasing(days: int, words: str) -> None:
    assert ago(TODAY - timedelta(days=days), today=TODAY) == words


def test_ago_counts_utc_days_for_a_datetime() -> None:
    # 20:30 in New York on the 24th is 00:30 on the 25th in UTC: one day before the 26th.
    evening = datetime(2026, 9, 24, 20, 30, tzinfo=ZoneInfo("America/New_York"))
    assert ago(evening, today=TODAY) == "yesterday"


def test_ago_of_nothing_is_empty_and_of_a_non_date_is_an_error() -> None:
    assert ago(None, today=TODAY) == ""
    assert ago("", today=TODAY) == ""
    with pytest.raises(TemplateRenderError, match="ago needs a date"):
        render(LINKEDIN, None, "{{ first_name | ago }}", _values(first_name="A"), today=TODAY)
