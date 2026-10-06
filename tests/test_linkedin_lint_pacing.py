"""LinkedIn lint (P4-11, #377) agrees with the typing plan (P4-10, #376).

The prefill types a rendered LinkedIn message with
:func:`netkeeper.linkedin.pacing.typing_plan`. Whatever the plan refuses, lint must
already have flagged as an error, so no step whose messages lint passes can fail at
send time, and lint must not flag a character the plan types.

The character rules agree exactly, for every seed: a newline or an untypable cluster
is a lint error if and only if the plan refuses the text for it. The length rule
can't agree exactly, because the plan's duration is random: lint compares the
*expected* duration with :data:`~netkeeper.linkedin.pacing.TYPING_LINT_SECONDS`, a
margin under the plan's :data:`~netkeeper.linkedin.pacing.MAX_TYPING_SECONDS`. So a
text lint passes never draws a plan over the ceiling, and a text far over always
does; between the two, lint is the stricter.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from netkeeper.campaigns import render as render_module
from netkeeper.campaigns.render import (
    LintIssue,
    LintRule,
    MergeValues,
    Severity,
    lint,
    render,
)
from netkeeper.linkedin.pacing import (
    LINE_BREAK_CHARS,
    MAX_TYPING_SECONDS,
    NEWLINE_CHARS,
    SHIFT_ENTER_NEWLINES_ALLOWED,
    SUBDIVISION_FLAGS,
    TYPING_LINT_SECONDS,
    MultilineRefused,
    TypingPlanError,
    TypingTooLong,
    UnsupportedCharacter,
    is_untypable_cluster,
    typing_expected_seconds,
    typing_length_warning,
    typing_plan,
)
from netkeeper.models import TemplateChannel

LINKEDIN = TemplateChannel.LINKEDIN
TODAY = date(2026, 10, 4)
SEEDS = range(12)

CHARACTER_RULES = frozenset({LintRule.LINKEDIN_NEWLINE, LintRule.LINKEDIN_UNTYPABLE})
LENGTH_ERRORS = frozenset({LintRule.LINKEDIN_TOO_LONG, LintRule.LINKEDIN_TYPING_TIME})


def _tag_sequence(code: str) -> str:
    return "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in code) + "\U000e007f"


ENGLAND = _tag_sequence("gbeng")
assert ENGLAND in SUBDIVISION_FLAGS

# Each text is what the prefill would be handed: a rendered message.
TEXTS: dict[str, str] = {
    "plain": "Hi Bo, thanks for the chat at the meetup.",
    "accents": "Olá José, um café? Ça va, Zoë.",
    "combining_marks": "Cafe\u0301 and nai\u0308ve",
    "cjk": "\u4f60\u597d\uff0cBo\u3002",
    "zwj_family": "Hi Bo \U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466 see you",
    "zwj_profession": "\U0001f469\U0001f3fd\u200d\U0001f4bb at work",
    "skin_tone": "\U0001f44b\U0001f3fd Bo",
    "regional_flag": "\U0001f1f5\U0001f1f9 Lisbon",
    "england_flag": f"{ENGLAND} London",
    "emoji_presentation": "\u2764\ufe0f thanks",
    "text_presentation": "\u2764\ufe0e thanks",
    "keycap": "1\ufe0f\u20e3 first",
    "zwnj": "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
    # A Unicode 17 linker between consonants: a cluster whose end depends on the text
    # before it, from regex 2026.9.29 on (#411).
    "vedic_linker": "Hi \u0915\u1cf5\u0915 ok",
    "vedic_linker_two": "Hi \u0915\u1cf6\u0915 ok",
    "zanabazar_linker": "Hi \U00011a0b\U00011a3a\U00011a0b ok",
    "linker_run": "\u0915\u094d\u1cf5\u1cf5\u0915\u200b",
    "lf": "Hi Bo,\nthanks",
    "cr": "Hi Bo,\rthanks",
    "crlf": "Hi Bo,\r\nthanks",
    "lf_cr": "Hi Bo,\n\rthanks",
    "trailing_lf": "Hi Bo\n",
    "blank_lines": "Hi Bo\n\n\nthanks",
    "vt": "Hi\x0bBo",
    "ff": "Hi\x0cBo",
    "fs": "Hi\x1cBo",
    "gs": "Hi\x1dBo",
    "rs": "Hi\x1eBo",
    "nel": "Hi\x85Bo",
    "line_separator": "Hi\u2028Bo",
    "paragraph_separator": "Hi\u2029Bo",
    "tab": "Hi\tBo",
    "nul": "Hi\x00Bo",
    "del": "Hi\x7fBo",
    "zero_width_space": "Hi\u200bBo",
    "bidi_override": "Hi \u202eoB",
    "byte_order_mark": "\ufeffHi Bo",
    "soft_hyphen": "co\u00adoperate",
    "private_use": "Hi \ue000 Bo",
    "unassigned": "Hi \u0378 Bo",
    "combining_grapheme_joiner": "Hi\u034fBo",
    "hangul_filler": "Hi \u3164 Bo",
    "mongolian_selector": "Hi\u180bBo",
    "variation_selector_1": "\u2764\ufe00 thanks",
    "two_variation_selectors": "\u2764\ufe0f\ufe0e thanks",
    "other_tag_sequence": f"{_tag_sequence('usca')} hi",
    "bare_tag_characters": "Hi " + "".join(chr(0xE0000 + ord(c)) for c in "secret") + " Bo",
    "lf_then_tab": "Hi Bo,\n\tthanks",
    "tab_then_lf": "Hi\tBo,\nthanks",
    "at_warning_limit": "x" * 1000,
    "over_warning_limit": "x" * 1001,
    "just_under_lint_seconds": "x" * 1270,
    "just_over_lint_seconds": "x" * 1300,
    "short_but_slow": "A. " * 320,
    "far_over_ceiling": "x" * 2000,
    "over_linkedin_limit": "x" * 8001,
    "long_and_multiline": "x" * 1100 + "\nthanks",
}


def _rendered(text: str) -> tuple[LintIssue, ...]:
    """Render ``text`` as the whole of a LinkedIn message, as the prefill would get it."""
    rendered = render(
        LINKEDIN,
        None,
        "{{ personal_line }}",
        MergeValues(contact={}, personal_line=text),
        today=TODAY,
    )
    assert rendered.body == text
    return rendered.issues


def _errors(issues: tuple[LintIssue, ...] | list[LintIssue]) -> set[LintRule]:
    return {issue.rule for issue in issues if issue.severity is Severity.ERROR}


def _refusal(text: str, seed: int) -> type[TypingPlanError] | None:
    try:
        typing_plan(text, random.Random(seed))
    except TypingPlanError as exc:
        return type(exc)
    return None


def test_the_flag_lint_reads_is_the_one_the_plan_defaults_to() -> None:
    assert vars(render_module)["SHIFT_ENTER_NEWLINES_ALLOWED"] is SHIFT_ENTER_NEWLINES_ALLOWED
    assert SHIFT_ENTER_NEWLINES_ALLOWED is True  # set by P4-03 (#382), ADR 0007


@pytest.mark.parametrize("text", TEXTS.values(), ids=TEXTS.keys())
def test_lint_flags_exactly_the_characters_the_plan_refuses(text: str) -> None:
    char_errors = _errors(_rendered(text)) & CHARACTER_RULES
    for seed in SEEDS:
        refusal = _refusal(text, seed)
        refused_for_a_character = refusal in {MultilineRefused, UnsupportedCharacter}
        assert bool(char_errors) == refused_for_a_character, (seed, refusal, char_errors)
    # And rule by rule: the newline rule is the plan's newline refusal, the character
    # rule the plan's character refusal, whichever the plan happens to meet first.
    has_newline = any(char in NEWLINE_CHARS for char in text)
    newline_refused = has_newline and not SHIFT_ENTER_NEWLINES_ALLOWED
    assert (LintRule.LINKEDIN_NEWLINE in char_errors) == newline_refused
    plan_without_newlines = text.replace("\r", " ").replace("\n", " ")
    assert (LintRule.LINKEDIN_UNTYPABLE in char_errors) == (
        _refusal(plan_without_newlines, 0) is UnsupportedCharacter
    )


@pytest.mark.parametrize("text", TEXTS.values(), ids=TEXTS.keys())
def test_whatever_the_plan_refuses_is_a_lint_error(text: str) -> None:
    errors = _errors(_rendered(text))
    for seed in SEEDS:
        refusal = _refusal(text, seed)
        if refusal is TypingTooLong:
            assert errors & LENGTH_ERRORS, seed
        elif refusal is not None:
            assert errors & CHARACTER_RULES, seed


def test_a_text_lint_passes_is_always_typed() -> None:
    passed = {name: text for name, text in TEXTS.items() if not _errors(_rendered(text))}
    assert {"zwj_family", "england_flag", "over_warning_limit", "just_under_lint_seconds"} <= (
        passed.keys()
    )
    for name, text in passed.items():
        for seed in SEEDS:
            assert _refusal(text, seed) is None, (name, seed)


@pytest.mark.parametrize("text", TEXTS.values(), ids=TEXTS.keys())
def test_the_typing_time_rule_is_the_expected_duration_against_the_lint_seconds(
    text: str,
) -> None:
    errors = _errors(_rendered(text))
    if LintRule.LINKEDIN_TOO_LONG in errors:
        return  # one length finding at most, and LinkedIn's own limit comes first
    over = typing_expected_seconds(text) > TYPING_LINT_SECONDS
    assert (LintRule.LINKEDIN_TYPING_TIME in errors) == over


@pytest.mark.parametrize("text", TEXTS.values(), ids=TEXTS.keys())
def test_lint_warns_exactly_when_the_pacing_module_warns(text: str) -> None:
    rules = {issue.rule for issue in _rendered(text)}
    length = rules & (LENGTH_ERRORS | {LintRule.LINKEDIN_LONG})
    # The warning stands alone, or gives way to a length error about the same text.
    assert bool(length) == (typing_length_warning(text) or bool(length & LENGTH_ERRORS))
    if not length & LENGTH_ERRORS:
        assert (LintRule.LINKEDIN_LONG in rules) == typing_length_warning(text)


def test_the_length_margin_is_real() -> None:
    # Lint is the stricter between its threshold and the ceiling: this text is a lint
    # error, yet the plan types it with every seed.
    text = TEXTS["just_over_lint_seconds"]
    assert LintRule.LINKEDIN_TYPING_TIME in _errors(_rendered(text))
    assert all(_refusal(text, seed) is None for seed in SEEDS)
    # Far over, the plan refuses it with every seed, and lint says so too.
    far = TEXTS["far_over_ceiling"]
    assert typing_expected_seconds(far) > MAX_TYPING_SECONDS
    assert all(_refusal(far, seed) is TypingTooLong for seed in SEEDS)
    assert LintRule.LINKEDIN_TYPING_TIME in _errors(_rendered(far))


@pytest.mark.parametrize("char", sorted(LINE_BREAK_CHARS), ids=lambda c: f"U+{ord(c):04X}")
def test_every_line_break_but_cr_and_lf_is_a_lint_error(char: str) -> None:
    text = f"Hi{char}Bo"
    if char in NEWLINE_CHARS:
        # CR and LF follow the newline flag, which P4-03 (#382) set: a Shift+Enter step.
        expected = set() if SHIFT_ENTER_NEWLINES_ALLOWED else {LintRule.LINKEDIN_NEWLINE}
    else:
        expected = {LintRule.LINKEDIN_UNTYPABLE}
    assert _errors(_rendered(text)) == expected
    assert render_module._LINE_BREAKS.fullmatch(char)  # a header splits on it too


SWEEP = [
    *range(0x0000, 0x0300),
    *range(0x2000, 0x2070),
    *range(0xFE00, 0xFE10),
    *range(0xFFF0, 0x10000),
    0x034F, 0x0378, 0x115F, 0x1160, 0x180B, 0x180E, 0x180F, 0x3164, 0xE000, 0xFEFF,
    0x1F3F4, 0x1F600, 0xE0001, 0xE0020, 0xE0067, 0xE007F, 0xE0100, 0xF0000, 0x10FFFF,
]  # fmt: skip


def test_lint_and_the_plan_agree_on_each_code_point_of_a_sweep() -> None:
    disagree = []
    for code in SWEEP:
        char = chr(code)
        text = f"a{char}b"
        template_errors = _errors(lint(LINKEDIN, None, "{{ first_name }} " + text))
        rendered_errors = _errors(_rendered(text))
        refused = _refusal(text, 0) is not None
        flagged = bool(rendered_errors & CHARACTER_RULES)
        at_save = bool(template_errors & CHARACTER_RULES)
        if not (flagged == refused == at_save):
            disagree.append(f"U+{code:04X}")
    assert disagree == []


@pytest.mark.parametrize(
    "cluster",
    [ENGLAND, _tag_sequence("usca"), "\u2764\ufe0f", "\u2764\ufe0f\ufe0e", "e\u0301"],
    ids=["england", "other_tag", "vs16", "two_vs", "combining"],
)
def test_a_cluster_is_judged_whole_as_the_plan_judges_it(cluster: str) -> None:
    flagged = LintRule.LINKEDIN_UNTYPABLE in _errors(_rendered(f"Hi {cluster} Bo"))
    assert flagged == is_untypable_cluster(cluster)


# Consonants, linkers old and new, marks, joiners, regional indicators, and a few
# characters lint and the plan refuse: texts whose clusters lean on what comes before.
_CONTEXT_ALPHABET = [
    *("\u0915", "\u0916", "\u1cf5", "\u1cf6", "\u094d", "\U00011a0b", "\U00011a3a"),
    *("\U00011a47", "\u17d2", "\u1780", "a", " ", "\u0301", "\u200d", "\ufe0f"),
    *("\U0001f1f5", "\U0001f1f9", "\U0001f600", "\n", "\u200b", "\ufe00", "\U000e0067"),
]


def test_lint_and_the_plan_agree_on_texts_whose_clusters_depend_on_context() -> None:
    rng = random.Random(411)
    disagree = []
    for _ in range(300):
        body = "".join(rng.choice(_CONTEXT_ALPHABET) for _ in range(rng.randint(1, 10)))
        text = f"\u0915{body}x"  # rendering would trim whitespace at either end
        char_errors = _errors(_rendered(text)) & CHARACTER_RULES
        refusal = _refusal(text, 0)  # anything but a TypingPlanError fails the test here
        if bool(char_errors) != (refusal in {MultilineRefused, UnsupportedCharacter}):
            disagree.append(ascii(text))
    assert disagree == []
