"""netkeeper.campaigns.render (spec 11.1; item P3-03): the sandbox, merge fields, ``ago``, lint."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import jinja2
import pytest
from jinja2 import nodes
from jinja2.exceptions import SecurityError
from time_limit import Stopwatch, scaled

from netkeeper.campaigns import render as render_module
from netkeeper.campaigns.render import (
    ALLOWED_FILTERS,
    ALLOWED_NODES,
    ALLOWED_TESTS,
    CONTACT_FIELDS,
    LINKEDIN_MESSAGE_MAX_CHARS,
    MAX_INT_BITS,
    MAX_LITERAL_CHARS,
    MAX_NESTING,
    MAX_OUTPUT_CHARS,
    MAX_TRUNCATE_LENGTH,
    PLACEHOLDER_EXAMPLES,
    FieldGroup,
    LintIssue,
    LintRule,
    MergeValues,
    Part,
    Severity,
    TemplateRenderError,
    ago,
    has_errors,
    lint,
    merge_fields,
    placeholder_example,
    render,
)
from netkeeper.linkedin import pacing
from netkeeper.linkedin.pacing import TYPING_LINT_SECONDS, TYPING_WARN_CHARS
from netkeeper.models import TemplateChannel

EMAIL = TemplateChannel.EMAIL
LINKEDIN = TemplateChannel.LINKEDIN
TODAY = date(2026, 9, 26)
GOOD_BODY = "Hi {{ first_name }}, Ada here."
# A subject that is clean on its own, for tests about the body that need multiple lines
# or length, which a LinkedIn message may not have (P4-11).
SUBJECT = "Hello"


def _rules(issues: list[LintIssue]) -> list[tuple[LintRule, str | None]]:
    return [(issue.rule, issue.field) for issue in issues]


def _values(**contact: object) -> MergeValues:
    return MergeValues(contact=contact)


# --- lint: a clean template -----------------------------------------------------------


def test_a_template_naming_every_merge_field_lints_clean() -> None:
    contact = " ".join(f"{{{{ {name} }}}}" for name in CONTACT_FIELDS)
    body = (
        f"{contact} {{{{ campaign.name }}}} {{{{ step.number }}}} "
        "{{ previous_send_date | ago }} {{ personal_line }}"
        "{% if company %}at {{ company | upper }}{% elif title %}!{% else %}?{% endif %}"
        "{{ years_since_connected + 1 }} {{ step.number * 2 }} {{ first_name ~ '!' }}"
        "{{ 'x' if company else 'y' }} {{ first_name in 'Ann Bob' }} {{ not company }}"
        "{{ company and title or location }} {{ connected_year is even }}"
        "{{ first_name | default('there') | trim | title | lower | capitalize }}"
        "{{ company | truncate(10, true, end='..', leeway=0) }} {{ none }} {{ true }}"
    )
    assert lint(EMAIL, "Hello {{ first_name }}", body) == []
    assert not has_errors([])


@pytest.mark.parametrize(
    ("body", "name"),
    [
        ("{{ first_name }}, {{ me.name }} here", "me.name"),
        ("{{ first_name }} {{ me.signature }}", "me.signature"),
        ("{{ first_name }} {{ me.podcast }}", "me.podcast"),  # was an extra [me] key
        ("{{ first_name }} {{ me }}", "me"),
        ("{{ first_name }} {{ me.name | upper }}", "me.name"),
    ],
)
def test_a_me_field_is_a_removed_field_error_that_says_what_to_do(body: str, name: str) -> None:
    """#320, #342: templates use contact fields only. A template written for ``me.*`` is told
    why, rather than seeing a bare "not a merge field"."""
    [issue] = lint(LINKEDIN, None, body)
    assert (issue.rule, issue.field, issue.severity) == (
        LintRule.REMOVED_FIELD,
        name,
        Severity.ERROR,
    )
    assert issue.line == 1
    assert "were removed" in issue.message
    assert "write your own details" in issue.message and "About you" in issue.message
    assert has_errors([issue])


def test_a_template_saved_with_a_me_field_renders_it_empty_with_the_error() -> None:
    """An old template still renders (a preview shows it), never sends: the error blocks
    activation, and the field is empty rather than a value from anywhere."""
    rendered = render(
        LINKEDIN,
        None,
        "Hi {{ first_name }}, {{ me.name }} here",
        _values(first_name="Bo"),
        today=TODAY,
    )
    assert rendered.body == "Hi Bo,  here"
    assert _rules(list(rendered.issues)) == [(LintRule.REMOVED_FIELD, "me.name")]


def test_the_removed_field_rule_is_pinned() -> None:
    """The rule's value is stored in ``lint_json`` and read by the editor."""
    assert LintRule.REMOVED_FIELD.value == "removed_field"


# --- lint: each rule is an error --------------------------------------------------------


def test_undefined_variables_are_errors() -> None:
    body = "{{ first_name }} {{ nickname }} {{ campaign.owner }} {{ step.when }}"
    issues = lint(LINKEDIN, None, body)
    assert _rules(issues) == [
        (LintRule.UNDEFINED_VARIABLE, "nickname"),
        (LintRule.UNDEFINED_VARIABLE, "campaign.owner"),
        (LintRule.UNDEFINED_VARIABLE, "step.when"),
    ]
    assert all(issue.severity is Severity.ERROR for issue in issues)
    assert has_errors(issues)


def test_a_group_of_fields_used_whole_or_computed_is_an_error() -> None:
    body = "{{ first_name }} {{ step }} {{ step | upper }} {{ step[first_name] }}"
    issues = lint(LINKEDIN, None, body)
    assert [(issue.rule, issue.field, issue.message) for issue in issues] == [
        (
            LintRule.UNDEFINED_VARIABLE,
            "step",
            "`step` is a group of fields; name one, like `step.number`",
        ),
        (
            LintRule.UNSUPPORTED,
            "subscript",
            "line 1: `subscript` is not available in a message template",
        ),
    ]


def test_the_globals_jinja_ships_are_not_merge_fields() -> None:
    body = "{{ first_name }}{{ range }}{{ cycler }}{{ lipsum }}"
    assert {name for _, name in _rules(lint(LINKEDIN, None, body))} == {
        "range",
        "cycler",
        "lipsum",
    }


def test_a_body_with_no_per_contact_field_is_an_error() -> None:
    issues = lint(LINKEDIN, None, "Hi, Ada here. {{ campaign.name }}")
    assert _rules(issues) == [(LintRule.NO_CONTACT_FIELD, None)]
    assert issues[0].part is Part.BODY and issues[0].severity is Severity.ERROR


def test_a_contact_field_in_the_subject_alone_does_not_count() -> None:
    issues = lint(EMAIL, "Hi {{ first_name }}", "Hello there")
    assert _rules(issues) == [(LintRule.NO_CONTACT_FIELD, None)]


def test_personal_line_counts_as_a_per_contact_field() -> None:
    assert lint(LINKEDIN, None, "{{ personal_line }}") == []


@pytest.mark.parametrize("subject", [None, "", "   "])
def test_an_email_with_no_subject_is_an_error(subject: str | None) -> None:
    issues = lint(EMAIL, subject, GOOD_BODY)
    assert _rules(issues) == [(LintRule.MISSING_SUBJECT, None)]
    assert issues[0].part is Part.SUBJECT


def test_a_linkedin_message_needs_no_subject() -> None:
    assert lint(LINKEDIN, None, GOOD_BODY) == []


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
    issues = lint(LINKEDIN, None, f"{{{{ first_name }}}}, see {link} today")
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
        "https://{{ company }}.example",  # completed by a merge field
        "https://example.com/{{ first_name }}",
        "{{ company }}/calendar",
        "mailto:ada@example.test and ftp://files.example",  # not http(s); not checked
    ],
)
def test_links_that_parse_or_are_completed_by_a_field_pass(text: str) -> None:
    assert lint(LINKEDIN, None, f"{{{{ first_name }}}} {text}") == []


def test_bad_links_in_the_subject_are_reported_against_it() -> None:
    issues = lint(EMAIL, "See http:/x.example", GOOD_BODY)
    assert [(i.rule, i.part) for i in issues] == [(LintRule.BAD_LINK, Part.SUBJECT)]


def test_a_template_that_does_not_parse_is_a_syntax_error() -> None:
    for body in ["{{ first_name ", "{% if first_name %}", "{{ first_name | }}"]:
        issues = lint(LINKEDIN, None, body)
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
def test_reaching_for_another_template_is_refused(tag: str) -> None:
    """Each compiles and then fails at render, so it has to fail lint: lint is the gate."""
    body = "{{ first_name }}\n" + tag
    issues = lint(LINKEDIN, None, body)
    assert LintRule.UNSUPPORTED in {issue.rule for issue in issues}
    assert issues[0].message.startswith("line 2: `")
    with pytest.raises(TemplateRenderError, match="is not available in a message template"):
        render(LINKEDIN, None, body, _values(first_name="A"), today=TODAY)


def test_lint_issues_round_trip_through_json() -> None:
    issue = LintIssue(LintRule.BAD_LINK, Severity.WARNING, Part.BODY, "broken", "http:/x", 3)
    assert issue.to_json() == {
        "rule": "bad_link",
        "severity": "warning",
        "part": "body",
        "message": "broken",
        "field": "http:/x",
        "line": 3,
    }
    assert LintIssue.from_json(issue.to_json()) == issue


def test_lint_stored_before_lines_reads_with_no_line() -> None:
    stored = {"rule": "syntax", "severity": "error", "part": "body", "message": "x", "field": None}
    assert LintIssue.from_json(stored).line is None


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
    "{{ first_name | attr('_' ~ '_class__') }}",
    "{{ me['_' ~ '_class__'] }}",
    "{{ [1].append(2) }}",
]


@pytest.mark.parametrize("escape", ESCAPES)
def test_escapes_fail_lint_and_the_render(escape: str) -> None:
    body = f"{{{{ first_name }}}} {escape}"
    rules = {issue.rule for issue in lint(LINKEDIN, None, body)}
    assert rules & {LintRule.UNSAFE_ATTRIBUTE, LintRule.UNSUPPORTED, LintRule.ATTRIBUTE_ACCESS}
    with pytest.raises(TemplateRenderError):
        render(LINKEDIN, None, body, _values(first_name="A"), today=TODAY)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{{ ''.__class__ }}", "unsafe"),
        ("{{ first_name.upper }}", "unsafe"),
        ("{{ first_name * 3 }}", "whole numbers only"),
        ("{{ first_name + '!' }}", "whole numbers only"),
        ("{{ 2 * big }}", "bits"),
        ("{{ first_name ~ first_name }}", "past 100000 characters"),
    ],
)
def test_the_sandbox_bounds_a_template_on_its_own(source: str, message: str) -> None:
    """The second line of defense: the environment refuses these even without lint."""
    env = render_module._environment(TODAY)
    context = {"first_name": "a" * 60_000, "big": 1 << MAX_INT_BITS}
    with pytest.raises(SecurityError, match=message):
        env.from_string(source).render(context)


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
    with pytest.raises(TemplateRenderError, match="ago needs a date"):
        render(LINKEDIN, None, "{{ first_name | ago }}", _values(first_name="A"), today=TODAY)
    with pytest.raises(TemplateRenderError, match=r"^body: line 1: "):
        render(LINKEDIN, None, "{{ first_name ", _values(first_name="A"), today=TODAY)


# --- rendering ------------------------------------------------------------------


def test_render_fills_every_kind_of_field() -> None:
    values = MergeValues(
        contact={"first_name": "Bo", "company": "Fixture Co", "connected_year": 2019},
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
        "Ada",
        values,
        today=TODAY,
    )
    assert rendered.subject == "Re: First 100"
    assert rendered.body == (
        "Hi Bo (Fixture Co, since 2019). Step 2, last week. Congrats on the move. Ada"
    )
    assert rendered.issues == ()


def test_missing_fields_render_empty_with_a_warning_not_an_exception() -> None:
    values = MergeValues(contact={"first_name": "Bo", "company": None, "title": "  "})
    rendered = render(
        EMAIL,
        "{{ first_name }}, {{ campaign.name }}",
        "Hi {{ first_name }} at {{ company }} as {{ title }}, {{ location }}. "
        "{{ previous_send_date | ago }} {{ step.number }}",
        values,
        today=TODAY,
    )
    assert rendered.body == "Hi Bo at  as , .  "
    assert rendered.subject == "Bo, "
    warnings = [(i.part, i.field) for i in rendered.issues if i.severity is Severity.WARNING]
    assert warnings == [
        (Part.SUBJECT, "campaign.name"),
        (Part.BODY, "company"),
        (Part.BODY, "title"),
        (Part.BODY, "location"),
        (Part.BODY, "previous_send_date"),
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
    values = MergeValues(contact={"first_name": "Bo", "company": "https://"})
    rendered = render(LINKEDIN, None, "{{ first_name }} {{ company }}", values, today=TODAY)
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


# --- a missing field never raises (#227 review) ----------------------------------------


FULL = {"first_name": "Bo", "company": "Acme", "connected_year": 2019, "years_since_connected": 7}


@pytest.mark.parametrize(
    ("shape", "missing", "present"),
    [
        ("{% if years_since_connected > 5 %}old{% endif %}", "", "old"),
        ("{{ years_since_connected + 1 }}", "", "8"),
        ("{{ 1 + years_since_connected }}", "", "8"),
        ("{{ connected_year * 2 }}", "", "4038"),
        ("{{ 5 < years_since_connected }}", "False", "True"),
        ("{{ years_since_connected >= 1 }}", "False", "True"),
        ("{{ years_since_connected == 7 }}", "False", "True"),
        ("{{ years_since_connected != 7 }}", "True", "False"),
        ("{{ 1 < years_since_connected < 9 }}", "False", "True"),
        # in, with the missing field on either side (#227 review, round 2)
        ("{% if first_name in 'Ann Bob' %}yes{% else %}no{% endif %}", "no", "yes"),
        ("{{ 'B' in first_name }}", "False", "True"),
        ("{{ first_name not in 'Ann' }}", "True", "True"),
        ("{{ 'x' not in company }}", "True", "True"),
        ("{{ company | upper }}", "", "ACME"),
        ("{{ first_name | trim | title }}", "", "Bo"),
        ("{{ company | truncate(3, leeway=0) }}", "", "..."),
        ("{{ company | default('your team') }}", "your team", "Acme"),
        ("{{ first_name ~ '!' }}", "!", "Bo!"),
        ("{{ 'x' if company else 'y' }}", "y", "x"),
        ("{{ not company }}", "True", "False"),
        ("{{ company and first_name }}", "", "Bo"),
        ("{{ years_since_connected is even }}", "False", "False"),
        ("{{ years_since_connected is divisibleby 7 }}", "False", "True"),
        ("{{ years_since_connected is defined }}", "False", "True"),
    ],
)
def test_a_missing_field_never_raises_whatever_the_template_does(
    shape: str, missing: str, present: str
) -> None:
    assert lint(LINKEDIN, None, shape) == []
    empty = render(LINKEDIN, None, shape, _values(), today=TODAY)
    assert empty.body == missing
    assert {i.rule for i in empty.issues} == {LintRule.MISSING_VALUE}
    assert render(LINKEDIN, None, shape, _values(**FULL), today=TODAY).body == present


# --- the allowlist (#227 review, round 2) -----------------------------------------------


# One template per node type the parser can produce that is off the allowlist. Each is
# refused where it stands or inside something refused, which the walk does not enter.
REFUSED_SAMPLES: dict[type[nodes.Node], str] = {
    nodes.For: "{% for x in first_name %}{% endfor %}",
    nodes.Macro: "{% macro m() %}{% endmacro %}",
    nodes.CallBlock: "{% call first_name() %}{% endcall %}",
    nodes.FilterBlock: "{% filter upper %}x{% endfilter %}",
    nodes.With: "{% with a = 1 %}{% endwith %}",
    nodes.Block: "{% block b %}{% endblock %}",
    nodes.Include: "{% include 'other' %}",
    nodes.Import: "{% import 'macros' as m %}",
    nodes.FromImport: "{% from 'macros' import greet %}",
    nodes.Extends: "{% extends 'base' %}",
    nodes.Assign: "{% set a = 1 %}",
    nodes.AssignBlock: "{% set a %}x{% endset %}",
    nodes.NSRef: "{% set ns.a = 1 %}",
    nodes.ScopedEvalContextModifier: "{% autoescape true %}x{% endautoescape %}",
    nodes.Getitem: "{{ first_name[0] }}",
    nodes.Slice: "{{ first_name[1:] }}",
    nodes.Call: "{{ first_name() }}",
    nodes.List: "{{ [first_name] }}",
    nodes.Tuple: "{{ (first_name, 1) }}",
    nodes.Dict: "{{ {'a': first_name} }}",
    nodes.Pair: "{{ {'a': first_name} }}",
    nodes.Sub: "{{ connected_year - 1 }}",
    nodes.Div: "{{ connected_year / 2 }}",
    nodes.FloorDiv: "{{ connected_year // 2 }}",
    nodes.Mod: "{{ connected_year % 2 }}",
    nodes.Pow: "{{ connected_year ** 2 }}",
    nodes.Neg: "{{ -connected_year }}",
    nodes.Pos: "{{ +connected_year }}",
}
# Node types only extensions or the compiler make; no template source parses into them.
NEVER_PARSED: set[type[nodes.Node]] = {
    nodes.ExprStmt,  # {% do %}, an extension
    nodes.Break,  # {% break %}, an extension
    nodes.Continue,
    nodes.EvalContextModifier,
    nodes.InternalName,
    nodes.MarkSafe,
    nodes.MarkSafeIfAutoescape,
    nodes.ContextReference,
    nodes.DerivedContextReference,
    nodes.EnvironmentAttribute,
    nodes.ExtensionAttribute,
    nodes.ImportedName,
    nodes.Scope,
    nodes.OverlayScope,
}


def _concrete_node_types() -> set[type[nodes.Node]]:
    return {
        cls
        for _, cls in inspect.getmembers(nodes, inspect.isclass)
        if issubclass(cls, nodes.Node) and not cls.abstract and cls.__module__ == nodes.__name__
    }


def test_every_node_type_is_allowed_sampled_or_never_parsed() -> None:
    """A Jinja release that adds a node type fails here until someone decides about it."""
    concrete = _concrete_node_types()
    assert concrete == ALLOWED_NODES | set(REFUSED_SAMPLES) | NEVER_PARSED
    assert not ALLOWED_NODES & (set(REFUSED_SAMPLES) | NEVER_PARSED)


@pytest.mark.parametrize(
    "node_type",
    [pytest.param(cls, id=cls.__name__) for cls in sorted(REFUSED_SAMPLES, key=str)],
)
def test_every_node_type_off_the_allowlist_is_refused(node_type: type[nodes.Node]) -> None:
    body = "{{ first_name }}" + REFUSED_SAMPLES[node_type]
    tree = render_module._environment(TODAY).parse(body)
    assert any(type(node) is node_type for node in tree.find_all(nodes.Node))
    issues = lint(LINKEDIN, None, body)
    assert LintRule.UNSUPPORTED in {issue.rule for issue in issues}
    with pytest.raises(TemplateRenderError, match="is not available in a message template"):
        render(LINKEDIN, None, body, _values(first_name="Bo"), today=TODAY)


@pytest.mark.parametrize(
    ("shape", "why"),
    [
        # Round 1 let these through as missing-field shapes; the allowlist refuses them.
        ("{{ company.upper() }}", LintRule.UNSUPPORTED),  # refused as a call
        ("{{ first_name.strip() }}", LintRule.UNSUPPORTED),
        ("{{ first_name.format }}", LintRule.ATTRIBUTE_ACCESS),
        ("{{ last_position_change.max }}", LintRule.ATTRIBUTE_ACCESS),
        ("{{ campaign.name.upper }}", LintRule.ATTRIBUTE_ACCESS),
        ("{{ first_name | int }}", LintRule.UNSUPPORTED),
        ("{{ years_since_connected | round }}", LintRule.UNSUPPORTED),
        ("{{ first_name | tojson }}", LintRule.UNSUPPORTED),
        ("{{ first_name | pprint }}", LintRule.UNSUPPORTED),
        ("{{ first_name | attr('upper') }}", LintRule.UNSUPPORTED),
        ("{{ first_name | center(9) }}", LintRule.UNSUPPORTED),
        ("{{ first_name is sameas first_name }}", LintRule.UNSUPPORTED),
        ("{{ first_name | upper(*a) }}", LintRule.UNSUPPORTED),
        ("{{ first_name * 2 }}", LintRule.UNSUPPORTED),
        ("{{ 'ab' + first_name }}", LintRule.UNSUPPORTED),
        ("{{ company | truncate(1000000000) }}", LintRule.UNSUPPORTED),
        ("{{ company | truncate(length=first_name) }}", LintRule.UNSUPPORTED),
        ("{{ 1.5 }}", LintRule.UNSUPPORTED),
        ("{{ '" + "a" * (MAX_LITERAL_CHARS + 1) + "' }}", LintRule.UNSUPPORTED),
        ("{{ self }}", LintRule.UNSUPPORTED),
    ],
)
def test_expressions_off_the_allowlist_are_refused_even_for_a_missing_field(
    shape: str, why: LintRule
) -> None:
    body = "{{ first_name }}" + shape
    assert why in {issue.rule for issue in lint(LINKEDIN, None, body)}
    for values in (_values(), _values(**FULL)):
        with pytest.raises(TemplateRenderError):  # a refused template, never a crash
            render(LINKEDIN, None, body, values, today=TODAY)


def test_the_allowlists_are_pinned() -> None:
    assert {
        "default", "upper", "lower", "title", "capitalize", "trim", "truncate", "ago",
    } == ALLOWED_FILTERS  # fmt: skip
    assert {
        "defined", "undefined", "none", "number", "string", "even", "odd", "divisibleby",
    } == ALLOWED_TESTS  # fmt: skip
    assert {cls.__name__ for cls in ALLOWED_NODES} == {
        "Template", "Output", "TemplateData", "Name", "Const", "Getattr", "Filter", "Test",
        "Keyword", "If", "CondExpr", "Compare", "Operand", "And", "Or", "Not", "Concat",
        "Mul", "Add",
    }  # fmt: skip


def test_the_limits_are_pinned() -> None:
    assert MAX_OUTPUT_CHARS == 100_000
    assert MAX_INT_BITS == 10_000
    assert MAX_LITERAL_CHARS == 1_000
    assert MAX_TRUNCATE_LENGTH == 1_000
    assert MAX_NESTING == 50


# --- bounded work (#227 review) ---------------------------------------------------------


#: A refusal takes milliseconds; a template that got through would take minutes or crash.
FAST_S = scaled(1.0)


def _fails_fast(body: str, values: MergeValues | None = None) -> TemplateRenderError:
    watch = Stopwatch()
    with pytest.raises(TemplateRenderError) as caught:
        render(LINKEDIN, None, body, values or _values(first_name="Bo"), today=TODAY)
    assert watch.elapsed < FAST_S, watch
    return caught.value


def test_the_review_probes_are_refused_fast() -> None:
    """Round 2's three bypasses, each gigabytes in the web process before the allowlist."""
    doubling = (
        "{{ first_name }}{% with a = first_name %}"
        + "{% with a = a ~ a %}" * 40
        + "{{ a }}"
        + "{% endwith %}" * 41
    )
    tojson = "{{ first_name | tojson(indent=1000000000) }}"
    nested = "{{ [[first_name] * 40000] * 40000 }}"
    for body, refused in ((doubling, "with"), (tojson, "tojson"), (nested, "list")):
        assert (LintRule.UNSUPPORTED, refused) in _rules(lint(LINKEDIN, None, body))
        assert "is not available in a message template" in str(_fails_fast(body))


@pytest.mark.parametrize(
    "body",
    [
        "{{ 'a' * 10**9 }}",
        "{{ 'a' | center(1000000000) }}",
        "{{ ('a' * 1000) | replace('a', 'a' * 1000) | replace('a', 'a' * 1000) }}",
        "{% for a in 'ab' %}{% for b in 'cd' %}x{% endfor %}{% endfor %}",
        "{% macro m() %}{{ m() }}{% endmacro %}{{ m() }}",
        "{{ 2 ** 100000000 }}",
        "{{ '%999999999d' % 1 }}",
        "{{ '{:>999999999}'.format(1) }}",
        "{{ 'a' | batch(1000000000, 'x') | list }}",
    ],
)
def test_round_one_probes_stay_refused(body: str) -> None:
    _fails_fast("{{ first_name }}" + body)


def test_text_built_with_tilde_counts_against_the_budget_as_it_grows() -> None:
    values = _values(first_name="a" * 200)
    body = "{{ first_name }}" + "{{ first_name ~ first_name ~ first_name }}" * 200
    assert "past 100000 characters" in str(_fails_fast(body, values))


def test_filter_results_count_against_the_budget() -> None:
    values = _values(first_name="a" * 900)
    body = "{{ first_name }}" + "{{ first_name | upper | lower | title }}" * 60
    assert "past 100000 characters" in str(_fails_fast(body, values))


def test_numbers_stay_small() -> None:
    big = "9" * MAX_LITERAL_CHARS  # about 3,300 bits; four of them pass 10,000
    body = "{{ first_name }}{{ " + " * ".join([big] * 4) + " }}"
    assert lint(EMAIL, SUBJECT, body) == []
    assert "bits" in str(_fails_fast(body))


@pytest.mark.parametrize(
    ("digits", "rule"),
    [(4_000, LintRule.UNSUPPORTED), (5_000, LintRule.SYNTAX)],  # 5,000 is past int()'s limit
)
def test_a_huge_number_literal_is_refused_not_a_crash(digits: int, rule: LintRule) -> None:
    body = "{{ first_name }}{{ " + "9" * digits + " }}"
    assert rule in {issue.rule for issue in lint(LINKEDIN, None, body)}
    _fails_fast(body)


@pytest.mark.parametrize(
    "deep",
    [
        " * ".join(["9"] * 600),  # Python's compiler refuses this with a SyntaxError
        "(" * 600 + "1" + ")" * 600,
        " and ".join(["first_name"] * 600),
        "{% if first_name %}" * 100 + "x" + "{% endif %}" * 100,
    ],
    ids=["product", "parentheses", "and", "if"],
)
def test_deep_nesting_is_refused_fast_not_a_crash(deep: str) -> None:
    body = "{{ first_name }}" + (deep if deep.startswith("{%") else "{{ " + deep + " }}")
    watch = Stopwatch()
    issues = lint(EMAIL, SUBJECT, body)
    assert watch.elapsed < FAST_S, watch
    assert has_errors(issues)
    assert {issue.rule for issue in issues} <= {LintRule.UNSUPPORTED, LintRule.SYNTAX}
    _fails_fast(body)


def test_the_nesting_limit_is_ours_not_the_compilers() -> None:
    """``MAX_NESTING`` refuses a tree the stock compiler would still take (#226 review note).

    The cases above nest past Python's own limit, so a walker that lost its depth
    check would still see them fail, from ``SyntaxError``. A chain of ``and``
    nests one level per term: 49 terms sit at the limit and 50 go one past it,
    both far below the depth at which Jinja or Python gives up.
    """

    def chain(terms: int) -> str:
        return "{{ " + " and ".join(["first_name"] * terms) + " }}"

    jinja2.Environment().from_string(chain(50))  # compiles fine without the walker
    assert lint(LINKEDIN, None, chain(MAX_NESTING - 1)) == []
    rendered = render(
        LINKEDIN, None, chain(MAX_NESTING - 1), _values(first_name="Ada"), today=TODAY
    )
    assert rendered.body == "Ada"
    issues = lint(LINKEDIN, None, chain(MAX_NESTING))
    assert _rules(issues) == [(LintRule.UNSUPPORTED, "nesting")]
    assert "nesting deeper than 50 levels" in str(_fails_fast(chain(MAX_NESTING)))


def test_the_output_stops_at_the_limit() -> None:
    long = _values(first_name="a" * (MAX_OUTPUT_CHARS + 1))
    assert "longer than 100000 characters" in str(_fails_fast("{{ first_name }}", long))


# --- the subject is one line (#227 review) ----------------------------------------------


def test_a_merge_value_cannot_add_an_email_header() -> None:
    values = _values(first_name="Bo", company="Acme\r\nBcc: x@example.test")
    rendered = render(
        EMAIL, "Hi {{ company }}", "{{ first_name }}\n{{ company }}", values, today=TODAY
    )
    assert rendered.subject == "Hi Acme Bcc: x@example.test"
    assert rendered.body == "Bo\nAcme\r\nBcc: x@example.test"  # the body keeps its lines


@pytest.mark.parametrize("brk", ["\r", "\n", "\r\n", "\n\n", "\x0b", "\x0c", "\x85", "\u2028"])
def test_every_line_break_in_a_subject_becomes_one_space(brk: str) -> None:
    values = _values(first_name=f"A{brk}B")
    rendered = render(EMAIL, "{{ first_name }}", "{{ first_name }}", values, today=TODAY)
    assert rendered.subject == "A B"


# --- lint: line numbers (#344) ------------------------------------------------------


def _lines(issues: list[LintIssue]) -> list[tuple[LintRule, str | None, int | None]]:
    return [(issue.rule, issue.field, issue.line) for issue in issues]


def test_each_finding_about_one_place_names_its_line() -> None:
    body = (
        "Hi {{ first_name }},\n"  # 1
        "{{ frist_name }}\n"  # 2
        "{{ me.age }} {{ me._x }}\n"  # 3
        "{{ company.upper }}\n"  # 4
        "{% for x in y %}{% endfor %}\n"  # 5
        "see http:/broken\n"  # 6
    )
    assert _lines(lint(EMAIL, SUBJECT, body)) == [
        (LintRule.UNDEFINED_VARIABLE, "frist_name", 2),
        (LintRule.REMOVED_FIELD, "me.age", 3),
        (LintRule.UNSAFE_ATTRIBUTE, "_x", 3),
        (LintRule.ATTRIBUTE_ACCESS, "company.upper", 4),
        (LintRule.UNSUPPORTED, "for", 5),
        (LintRule.BAD_LINK, "http:/broken", 6),
    ]


def test_a_syntax_error_names_its_line() -> None:
    [issue] = lint(EMAIL, SUBJECT, "{{ first_name }}\n\n{{ oops")
    assert (issue.rule, issue.line) == (LintRule.SYNTAX, 3)


def test_a_finding_about_the_whole_part_has_no_line() -> None:
    issues = lint(EMAIL, "", "no fields\nat all")
    assert _lines(issues) == [
        (LintRule.MISSING_SUBJECT, None, None),
        (LintRule.NO_CONTACT_FIELD, None, None),
    ]


def test_a_bad_link_after_tags_and_fields_that_span_lines_names_the_right_line() -> None:
    body = (
        "{{\n first_name\n}}\n"  # lines 1-3
        "{% if company\n %}x{% endif %}{# a\ncomment #}\n"  # lines 4-6
        "go to https://{{ company }}.example/ok or https://bad_host!.example\n"  # line 7
    )
    assert _lines(lint(EMAIL, SUBJECT, body)) == [
        (LintRule.BAD_LINK, "https://bad_host!.example", 7)
    ]


def test_the_same_finding_twice_is_reported_once_at_its_first_line() -> None:
    body = "{{ first_name }}\n{{ frist_name }}\n{{ frist_name }}"
    assert _lines(lint(EMAIL, SUBJECT, body)) == [(LintRule.UNDEFINED_VARIABLE, "frist_name", 2)]


def test_a_missing_value_warning_names_the_line_the_field_is_first_used_on() -> None:
    rendered = render(
        EMAIL, SUBJECT, "Hi {{ first_name }}\n\n{{ company }} {{ company }}",
        _values(first_name="Bo"), today=TODAY,
    )  # fmt: skip
    assert _lines(list(rendered.issues)) == [(LintRule.MISSING_VALUE, "company", 3)]


# --- the editor's field list (#344) -----------------------------------------------


def test_merge_fields_list_every_field_spec_11_1_names() -> None:
    fields = merge_fields()
    assert [(f.name, f.group, f.insert) for f in fields] == [
        ("first_name", FieldGroup.CONTACT, "first_name"),
        ("last_name", FieldGroup.CONTACT, "last_name"),
        ("company", FieldGroup.CONTACT, "company"),
        ("title", FieldGroup.CONTACT, "title"),
        ("location", FieldGroup.CONTACT, "location"),
        ("connected_year", FieldGroup.CONTACT, "connected_year"),
        ("years_since_connected", FieldGroup.CONTACT, "years_since_connected"),
        ("last_position_change", FieldGroup.CONTACT, "last_position_change"),
        ("personal_line", FieldGroup.PERSONAL, "personal_line"),
        ("campaign.name", FieldGroup.CAMPAIGN, "campaign.name"),
        ("step.number", FieldGroup.CAMPAIGN, "step.number"),
        ("previous_send_date", FieldGroup.CAMPAIGN, "previous_send_date | ago"),
    ]
    assert all(f.description.strip() for f in fields)
    assert not any(f.name.startswith("me") for f in fields)  # #320, #342
    assert [g.value for g in FieldGroup] == ["contact", "personal", "campaign"]


def test_every_listed_field_lints_clean_so_the_list_and_lint_agree() -> None:
    for item in merge_fields():
        body = f"{{{{ first_name }}}} {{{{ {item.insert} }}}}"
        assert lint(LINKEDIN, None, body) == [], item.name


def test_merge_fields_name_exactly_what_lint_allows() -> None:
    """The reverse of the clean-lint test: every name lint allows is listed, and no other."""
    allowed = set(render_module.SCALAR_FIELDS) | {
        f"{namespace}.{key}"
        for namespace, keys in render_module.NAMESPACE_FIELDS.items()
        for key in keys
    }
    assert {f.name for f in merge_fields()} == allowed
    for name in allowed:  # and lint does allow each one
        assert lint(LINKEDIN, None, f"{{{{ first_name }}}} {{{{ {name} }}}}") == []


def test_a_field_lint_allows_but_the_list_cannot_place_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        render_module, "NAMESPACE_FIELDS", {**render_module.NAMESPACE_FIELDS, "sender": ("email",)}
    )
    with pytest.raises(ValueError, match="'sender' has no group"):
        merge_fields()
    monkeypatch.undo()
    monkeypatch.setattr(render_module, "SCALAR_FIELDS", render_module.SCALAR_FIELDS | {"nickname"})
    with pytest.raises(ValueError, match="'nickname' has no group"):
        merge_fields()


def test_every_listed_field_has_an_invented_placeholder() -> None:
    assert {f.name for f in merge_fields()} == set(PLACEHOLDER_EXAMPLES)
    assert all(placeholder_example(f.name) for f in merge_fields())


# --- LinkedIn messages (P4-11) ---------------------------------------------------------


def _sized(chars: int) -> str:
    """A clean one-line LinkedIn body exactly ``chars`` characters long."""
    head = "Hi {{ first_name }} "
    return head + "x" * (chars - len(head))


def test_the_linkedin_limits_are_pinned() -> None:
    assert LINKEDIN_MESSAGE_MAX_CHARS == 8000
    assert TYPING_WARN_CHARS == 1000
    assert TYPING_LINT_SECONDS == 240


@pytest.fixture
def newlines_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag as it was before P4-03 (#382), and as it goes back to if a later capture
    shows Shift+Enter sending: these tests pin that refusing branch."""
    monkeypatch.setattr(render_module, "SHIFT_ENTER_NEWLINES_ALLOWED", False)


def test_a_multi_line_linkedin_body_lints_clean_with_the_real_flag() -> None:
    """P4-03 (#382): with Shift+Enter allowed, a multi-line LinkedIn body is clean."""
    assert lint(LINKEDIN, None, "Hi {{ first_name }},\n\nthanks\r\nagain") == []


def test_lint_reads_the_newline_flag_from_the_pacing_module() -> None:
    # The one flag P4-03 flips: lint holds no copy of its own.
    assert (
        vars(render_module)["SHIFT_ENTER_NEWLINES_ALLOWED"] is pacing.SHIFT_ENTER_NEWLINES_ALLOWED
    )
    # P4-03 (#382) set it, with the Shift+Enter press and its pins (ADR 0007).
    assert pacing.SHIFT_ENTER_NEWLINES_ALLOWED is True
    assert not hasattr(render_module, "LINKEDIN_ALLOW_NEWLINES")
    assert not hasattr(render_module, "LINKEDIN_MESSAGE_LONG_CHARS")


@pytest.mark.parametrize("subject", ["Hello", "", "   "])
def test_a_linkedin_template_with_a_subject_is_a_warning(subject: str) -> None:
    """#448: flagged, but ignored rather than blocking, so a saved one keeps working."""
    issues = lint(LINKEDIN, subject, GOOD_BODY)
    assert _lines(issues) == [(LintRule.LINKEDIN_SUBJECT, None, 1)]
    assert issues[0].part is Part.SUBJECT
    assert issues[0].severity is Severity.WARNING
    assert not has_errors(issues)


def test_a_linkedin_subject_is_never_analysed_or_rendered() -> None:
    issues = lint(LINKEDIN, "Hi {{ me.first_name }}", GOOD_BODY)
    assert [i.rule for i in issues] == [LintRule.LINKEDIN_SUBJECT]
    rendered = render(LINKEDIN, "Hi {{ first_name }}", GOOD_BODY, _values(), today=date(2026, 1, 1))
    assert rendered.subject is None


def test_a_linkedin_body_at_the_warning_limit_is_clean() -> None:
    body = _sized(TYPING_WARN_CHARS)
    assert len(body) == 1000
    assert lint(LINKEDIN, None, body) == []


def test_a_linkedin_body_over_the_warning_limit_is_a_warning() -> None:
    issues = lint(LINKEDIN, None, _sized(TYPING_WARN_CHARS + 1))
    assert _lines(issues) == [(LintRule.LINKEDIN_LONG, None, 1)]
    assert issues[0].severity is Severity.WARNING
    assert issues[0].part is Part.BODY
    assert "1,001 characters" in issues[0].message
    assert not has_errors(issues)


# Plain prose, with words and sentences: it crosses 240 seconds at about 1,090 characters.
PROSE = (
    "It was great to meet you at the product meetup last week, and I enjoyed hearing how "
    "your team ships so often. Would you be open to a short call next month? I would love "
    "to compare notes on hiring, tooling, and the move to smaller releases. "
) * 10


def test_a_linkedin_body_too_slow_to_type_is_a_warning_at_save() -> None:
    # 1,275 characters of this shape (one long word) are expected to take just over 240
    # seconds. The template text is never typed as it stands, so it is only a warning.
    slow = _sized(1275)
    assert pacing.typing_expected_seconds(slow) > TYPING_LINT_SECONDS
    issues = lint(LINKEDIN, None, slow)
    assert _lines(issues) == [(LintRule.LINKEDIN_TYPING_TIME, None, 1)]
    assert issues[0].severity is Severity.WARNING
    assert not has_errors(issues)
    assert "takes about 241 seconds to type, over 240" in issues[0].message
    just_under = _sized(1274)
    assert pacing.typing_expected_seconds(just_under) <= TYPING_LINT_SECONDS
    assert [i.rule for i in lint(LINKEDIN, None, just_under)] == [LintRule.LINKEDIN_LONG]


@pytest.mark.parametrize(
    ("chars", "rule"),
    [(1050, LintRule.LINKEDIN_LONG), (1200, LintRule.LINKEDIN_TYPING_TIME)],
    ids=["1050_only_long", "1200_too_slow"],
)
def test_the_typing_time_threshold_for_real_prose(chars: int, rule: LintRule) -> None:
    text = PROSE[:chars]
    at_save = lint(LINKEDIN, None, "{{ first_name }} " + text)
    assert [(i.rule, i.severity) for i in at_save] == [(rule, Severity.WARNING)]
    rendered = render(
        LINKEDIN, None, "{{ personal_line }}",
        MergeValues(contact={}, personal_line=text), today=TODAY,
    )  # fmt: skip
    expected = Severity.WARNING if rule is LintRule.LINKEDIN_LONG else Severity.ERROR
    assert [(i.rule, i.severity) for i in rendered.issues] == [(rule, expected)]


def test_the_typing_time_line_is_where_the_body_passes_the_threshold() -> None:
    body = "Hi {{ first_name }}\n" + PROSE[:600] + "\n" + PROSE[600:1200]
    issues = lint(LINKEDIN, None, body)
    assert (LintRule.LINKEDIN_TYPING_TIME, None, 3) in _lines(issues)


def test_a_short_body_can_still_be_too_slow_to_type() -> None:
    # Every sentence end adds a pause, so this is slow at well under 1,000 characters.
    body = "Hi {{ first_name }}. " + "A. " * 320
    assert len(body) < TYPING_WARN_CHARS
    issues = lint(LINKEDIN, None, body)
    assert [issue.rule for issue in issues] == [LintRule.LINKEDIN_TYPING_TIME]


def test_a_linkedin_body_at_the_hard_limit_is_only_slow_to_type() -> None:
    body = _sized(LINKEDIN_MESSAGE_MAX_CHARS)
    assert len(body) == 8000
    issues = lint(LINKEDIN, None, body)
    assert [issue.rule for issue in issues] == [LintRule.LINKEDIN_TYPING_TIME]


def test_a_linkedin_body_over_the_hard_limit_is_an_error() -> None:
    issues = lint(LINKEDIN, None, _sized(LINKEDIN_MESSAGE_MAX_CHARS + 1))
    assert _lines(issues) == [(LintRule.LINKEDIN_TOO_LONG, None, 1)]
    assert issues[0].severity is Severity.ERROR
    assert "8,001 characters" in issues[0].message


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ("Hi {{ first_name }},\nthanks", 1),
        ("Hi {{ first_name }},\r\nthanks", 1),
        ("Hi {{ first_name }},\rthanks", 1),
        ("Hi {{ first_name }}\n\n\nthanks", 1),
        ("Hi {{ first_name }}\n", 1),
        ("{{ first_name }} one two\nthree", 1),
        ("{{ first_name }}\x20one\x20\x20{{ company }}\x20\n", 1),
        ('Hi {{ first_name }}{{ "\\n" }}', 1),  # a literal that renders a line break
        ('Hi {{ first_name }}{{ company | default("a\\r\\nb") }}', 1),
    ],
)
@pytest.mark.usefixtures("newlines_refused")
def test_a_multi_line_linkedin_body_is_an_error(body: str, line: int) -> None:
    issues = lint(LINKEDIN, None, body)
    assert _lines(issues) == [(LintRule.LINKEDIN_NEWLINE, None, line)]
    assert issues[0].severity is Severity.ERROR
    assert issues[0].message == (
        "LinkedIn messages must be one paragraph: the prefill never presses Enter"
    )


@pytest.mark.usefixtures("newlines_refused")
def test_a_line_break_is_reported_on_the_line_it_ends() -> None:
    body = "Hi {{ first_name }}, one\ntwo\nthree"
    assert _lines(lint(LINKEDIN, None, body)) == [(LintRule.LINKEDIN_NEWLINE, None, 1)]
    literal = 'Hi {{ first_name }},\x20\x20\x20\x20\n\n{{ "two\\nlines" }}'
    assert _lines(lint(LINKEDIN, None, literal)) == [(LintRule.LINKEDIN_NEWLINE, None, 1)]


@pytest.mark.parametrize(
    ("char", "name"),
    [
        ("\t", "a tab"),
        ("\x00", "a NUL character"),
        ("\x0b", "U+000B"),  # VT
        ("\x0c", "U+000C"),  # FF
        ("\x1c", "U+001C"),  # FS
        ("\x1d", "U+001D"),  # GS
        ("\x1e", "U+001E"),  # RS
        ("\x85", "U+0085"),  # NEL
        ("\x7f", "U+007F"),
        ("\u2028", "a line separator (U+2028)"),
        ("\u2029", "a paragraph separator (U+2029)"),
    ],
)
def test_other_control_characters_are_always_an_error(
    char: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "Hi {{ first_name }}," + char + "thanks"
    for allowed in (False, True):  # whatever the newline flag says
        monkeypatch.setattr(render_module, "SHIFT_ENTER_NEWLINES_ALLOWED", allowed)
        issues = lint(LINKEDIN, None, body)
        assert _lines(issues) == [(LintRule.LINKEDIN_UNTYPABLE, None, 1)], allowed
        assert issues[0].severity is Severity.ERROR
        assert name in issues[0].message


def test_a_control_character_in_a_literal_is_an_error() -> None:
    issues = lint(LINKEDIN, None, '{{ first_name }}\x20{{ "a\\tb" }}')
    assert _lines(issues) == [(LintRule.LINKEDIN_UNTYPABLE, None, 1)]


def test_the_newline_flag_decides_only_about_cr_and_lf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_module, "SHIFT_ENTER_NEWLINES_ALLOWED", True)
    assert lint(LINKEDIN, None, "Hi {{ first_name }},\r\nthanks\n") == []


@pytest.mark.usefixtures("newlines_refused")
def test_the_line_of_a_long_body_is_where_it_passes_the_limit() -> None:
    first = "Hi {{ first_name }} " + "x" * 600
    issues = lint(LINKEDIN, None, first + "\n" + "y" * 500)
    assert _lines(issues) == [
        (LintRule.LINKEDIN_LONG, None, 2),
        (LintRule.LINKEDIN_NEWLINE, None, 1),
    ]


@pytest.mark.usefixtures("newlines_refused")
def test_the_line_of_a_too_long_body_is_where_it_passes_the_limit() -> None:
    lines = ["Hi {{ first_name }}", "x" * 3000, "y" * 3000, "z" * 3000]
    issues = lint(LINKEDIN, None, "\n".join(lines))
    # Lines 1 and 2 and their breaks are 3,021 characters, line 3 ends at 6,022: the
    # 8,001st character is on line 4.
    assert _lines(issues) == [
        (LintRule.LINKEDIN_TOO_LONG, None, 4),
        (LintRule.LINKEDIN_NEWLINE, None, 1),
    ]


def test_an_email_template_is_unaffected_by_the_linkedin_rules() -> None:
    long_body = "Hi {{ first_name }},\n" + "x" * (LINKEDIN_MESSAGE_MAX_CHARS + 10)
    assert lint(EMAIL, "Hello", long_body) == []
    rendered = render(EMAIL, "Hello", long_body, _values(first_name="Bo"), today=TODAY)
    assert rendered.issues == ()


@pytest.mark.usefixtures("newlines_refused")
def test_a_merge_value_that_adds_a_line_break_fails_the_rendered_linkedin_message() -> None:
    rendered = render(
        LINKEDIN,
        None,
        "Hi {{ first_name }}, {{ personal_line }}",
        MergeValues(contact={"first_name": "Bo"}, personal_line="one\ntwo"),
        today=TODAY,
    )
    assert _lines(list(rendered.issues)) == [(LintRule.LINKEDIN_NEWLINE, None, None)]
    assert has_errors(rendered.issues)


def test_a_merge_value_that_adds_length_fails_the_rendered_linkedin_message() -> None:
    body = "Hi {{ first_name }}, {{ personal_line }}"
    long = render(
        LINKEDIN, None, body,
        MergeValues(contact={"first_name": "Bo"}, personal_line="x" * 1100),
        today=TODAY,
    )  # fmt: skip
    assert _lines(list(long.issues)) == [(LintRule.LINKEDIN_LONG, None, None)]
    assert "the rendered message is" in long.issues[0].message
    too_long = render(
        LINKEDIN, None, body,
        MergeValues(contact={"first_name": "Bo"}, personal_line="x" * 9000),
        today=TODAY,
    )  # fmt: skip
    assert _lines(list(too_long.issues)) == [(LintRule.LINKEDIN_TOO_LONG, None, None)]
    assert has_errors(too_long.issues)
    slow = render(
        LINKEDIN, None, body,
        MergeValues(contact={"first_name": "Bo"}, personal_line="x" * 1500),
        today=TODAY,
    )  # fmt: skip
    assert _lines(list(slow.issues)) == [(LintRule.LINKEDIN_TYPING_TIME, None, None)]
    assert "the rendered message takes about" in slow.issues[0].message


def test_a_merge_value_with_a_control_character_fails_the_rendered_linkedin_message() -> None:
    rendered = render(
        LINKEDIN, None, "Hi {{ first_name }}, {{ company }}",
        _values(first_name="Bo", company="Acme\tInc"), today=TODAY,
    )  # fmt: skip
    assert _lines(list(rendered.issues)) == [(LintRule.LINKEDIN_UNTYPABLE, None, None)]
    assert "the rendered message contains a tab" in rendered.issues[0].message


@pytest.mark.parametrize("padding", [1100, 1500], ids=["renders_long", "renders_slow"])
def test_a_too_long_template_gets_no_second_length_finding_for_the_render(padding: int) -> None:
    body = "Hi {{ first_name }} " + "x" * padding + "{# " + "y" * 7000 + " #}"
    rendered = render(LINKEDIN, None, body, _values(first_name="Bo"), today=TODAY)
    assert TYPING_WARN_CHARS < len(rendered.body) <= LINKEDIN_MESSAGE_MAX_CHARS
    assert [issue.rule for issue in rendered.issues] == [LintRule.LINKEDIN_TOO_LONG]


def test_a_slow_template_that_renders_long_gets_no_extra_long_warning() -> None:
    # The comment makes the template text slow (a warning); the render is long, but
    # quick enough, and its long warning gives way to the template's typing-time one.
    body = "Hi {{ first_name }}, " + PROSE[:1030] + "{# " + PROSE[:600] + " #}"
    assert pacing.typing_expected_seconds(body) > TYPING_LINT_SECONDS
    rendered = render(LINKEDIN, None, body, _values(first_name="Bo"), today=TODAY)
    assert TYPING_WARN_CHARS < len(rendered.body) < 1090
    assert pacing.typing_expected_seconds(rendered.body) <= TYPING_LINT_SECONDS
    assert [(i.rule, i.severity) for i in rendered.issues] == [
        (LintRule.LINKEDIN_TYPING_TIME, Severity.WARNING)
    ]


def test_a_slow_template_with_short_branches_renders_without_an_error() -> None:
    # 1,190 characters of source, but each render types only one 550-character branch.
    body = (
        "Hi {{ first_name }}, {% if company %}" + PROSE[:550] + "{% else %}"
        + PROSE[550:1100] + "{% endif %}"
    )  # fmt: skip
    at_save = lint(LINKEDIN, None, body)
    assert [(i.rule, i.severity) for i in at_save] == [
        (LintRule.LINKEDIN_TYPING_TIME, Severity.WARNING)
    ]
    for values in (_values(first_name="Bo", company="Acme"), _values(first_name="Bo")):
        rendered = render(LINKEDIN, None, body, values, today=TODAY)
        assert len(rendered.body) < 600
        assert not has_errors(rendered.issues)


def test_a_slow_template_warning_never_hides_a_rendered_message_too_slow_to_type() -> None:
    body = "Hi {{ first_name }}, {{ personal_line }}{# " + PROSE[:1200] + " #}"
    assert [i.rule for i in lint(LINKEDIN, None, body)] == [LintRule.LINKEDIN_TYPING_TIME]
    short = render(
        LINKEDIN, None, body,
        MergeValues(contact={"first_name": "Bo"}, personal_line="Congrats."),
        today=TODAY,
    )  # fmt: skip
    assert not has_errors(short.issues)
    slow = render(
        LINKEDIN, None, body,
        MergeValues(contact={"first_name": "Bo"}, personal_line=PROSE[:1200]),
        today=TODAY,
    )  # fmt: skip
    assert [(i.rule, i.severity) for i in slow.issues] == [
        (LintRule.LINKEDIN_TYPING_TIME, Severity.WARNING),
        (LintRule.LINKEDIN_TYPING_TIME, Severity.ERROR),
    ]
    assert slow.issues[1].line is None


@pytest.mark.parametrize("newline", ["\r", "\r\n", "\n"], ids=["cr", "crlf", "lf"])
@pytest.mark.usefixtures("newlines_refused")
def test_an_untypable_character_is_reported_on_its_own_line(newline: str) -> None:
    body = "{{ first_name }} a" + newline + "b" + newline + "c\td"
    issues = lint(LINKEDIN, None, body)
    assert _lines(issues) == [
        (LintRule.LINKEDIN_NEWLINE, None, 1),
        (LintRule.LINKEDIN_UNTYPABLE, None, 3),
    ]


def test_the_lf_of_a_crlf_is_on_the_line_the_crlf_ends() -> None:
    source = "a\r\nb"
    assert [render_module._line_of(source, offset) for offset in range(5)] == [1, 1, 1, 2, 2]


@pytest.mark.usefixtures("newlines_refused")
def test_a_linkedin_finding_in_the_template_is_not_repeated_for_the_render() -> None:
    body = "Hi {{ first_name }}\n" + "x" * (LINKEDIN_MESSAGE_MAX_CHARS + 1)
    rendered = render(LINKEDIN, None, body, _values(first_name="Bo"), today=TODAY)
    assert [issue.rule for issue in rendered.issues] == [
        LintRule.LINKEDIN_TOO_LONG,
        LintRule.LINKEDIN_NEWLINE,
    ]
    assert all(issue.line is not None for issue in rendered.issues)
