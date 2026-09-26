"""netkeeper.campaigns.render (spec 11.1; item P3-03): the sandbox, merge fields, ``ago``, lint."""

from __future__ import annotations

import inspect
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import jinja2
import pytest
from jinja2 import nodes
from jinja2.exceptions import SecurityError

from netkeeper.campaigns import render as render_module
from netkeeper.campaigns.render import (
    ALLOWED_FILTERS,
    ALLOWED_NODES,
    ALLOWED_TESTS,
    CONTACT_FIELDS,
    MAX_INT_BITS,
    MAX_LITERAL_CHARS,
    MAX_NESTING,
    MAX_OUTPUT_CHARS,
    MAX_TRUNCATE_LENGTH,
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
        "{{ previous_send_date | ago }} {{ personal_line }}"
        "{% if company %}at {{ company | upper }}{% elif title %}!{% else %}?{% endif %}"
        "{{ years_since_connected + 1 }} {{ step.number * 2 }} {{ first_name ~ '!' }}"
        "{{ 'x' if company else 'y' }} {{ first_name in 'Ann Bob' }} {{ not company }}"
        "{{ company and title or location }} {{ connected_year is even }}"
        "{{ first_name | default('there') | trim | title | lower | capitalize }}"
        "{{ company | truncate(10, true, end='..', leeway=0) }} {{ none }} {{ true }}"
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
    body = "{{ first_name }} {{ me }} {{ me | upper }} {{ me[first_name] }}"
    issues = lint(LINKEDIN, None, body, ME.keys())
    assert [(issue.rule, issue.field, issue.message) for issue in issues] == [
        (
            LintRule.UNDEFINED_VARIABLE,
            "me",
            "`me` is a group of fields; name one, like `me.name`",
        ),
        (
            LintRule.UNSUPPORTED,
            "subscript",
            "line 1: `subscript` is not available in a message template",
        ),
    ]


def test_the_globals_jinja_ships_are_not_merge_fields() -> None:
    body = "{{ first_name }}{{ range }}{{ cycler }}{{ lipsum }}"
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


def test_a_template_that_does_not_parse_is_a_syntax_error() -> None:
    for body in ["{{ first_name ", "{% if first_name %}", "{{ first_name | }}"]:
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
def test_reaching_for_another_template_is_refused(tag: str) -> None:
    """Each compiles and then fails at render, so it has to fail lint: lint is the gate."""
    body = "{{ first_name }}\n" + tag
    issues = lint(LINKEDIN, None, body, ME.keys())
    assert LintRule.UNSUPPORTED in {issue.rule for issue in issues}
    assert issues[0].message.startswith("line 2: `")
    with pytest.raises(TemplateRenderError, match="is not available in a message template"):
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
    "{{ first_name | attr('_' ~ '_class__') }}",
    "{{ me['_' ~ '_class__'] }}",
    "{{ [1].append(2) }}",
]


@pytest.mark.parametrize("escape", ESCAPES)
def test_escapes_fail_lint_and_the_render(escape: str) -> None:
    body = f"{{{{ first_name }}}} {escape}"
    rules = {issue.rule for issue in lint(LINKEDIN, None, body, ME.keys())}
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
    assert lint(LINKEDIN, None, shape, ME.keys()) == []
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
    issues = lint(LINKEDIN, None, body, ME.keys())
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
        ("{{ me.name.upper }}", LintRule.ATTRIBUTE_ACCESS),
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
    assert why in {issue.rule for issue in lint(LINKEDIN, None, body, ME.keys())}
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


def _fails_fast(body: str, values: MergeValues | None = None) -> TemplateRenderError:
    start = time.perf_counter()
    with pytest.raises(TemplateRenderError) as caught:
        render(LINKEDIN, None, body, values or _values(first_name="Bo"), today=TODAY)
    assert time.perf_counter() - start < 1.0
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
        assert (LintRule.UNSUPPORTED, refused) in _rules(lint(LINKEDIN, None, body, ME.keys()))
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
    assert lint(LINKEDIN, None, body, ME.keys()) == []
    assert "bits" in str(_fails_fast(body))


@pytest.mark.parametrize(
    ("digits", "rule"),
    [(4_000, LintRule.UNSUPPORTED), (5_000, LintRule.SYNTAX)],  # 5,000 is past int()'s limit
)
def test_a_huge_number_literal_is_refused_not_a_crash(digits: int, rule: LintRule) -> None:
    body = "{{ first_name }}{{ " + "9" * digits + " }}"
    assert rule in {issue.rule for issue in lint(LINKEDIN, None, body, ME.keys())}
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
    start = time.perf_counter()
    issues = lint(LINKEDIN, None, body, ME.keys())
    assert time.perf_counter() - start < 1.0
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
    assert lint(LINKEDIN, None, chain(MAX_NESTING - 1), ME.keys()) == []
    rendered = render(
        LINKEDIN, None, chain(MAX_NESTING - 1), _values(first_name="Ada"), today=TODAY
    )
    assert rendered.body == "Ada"
    issues = lint(LINKEDIN, None, chain(MAX_NESTING), ME.keys())
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
