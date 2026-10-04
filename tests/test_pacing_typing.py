"""netkeeper.linkedin.pacing's typing plan: P4-10 (#376).

The plan decides how P4-03's prefill types a LinkedIn message, one keystroke at
a time. These tests are property-style: each one draws many seeds and many
invented bodies, and asserts the property holds for every one of them.

Every statistical bound states, in a comment, why it is wide enough not to
flake and narrow enough that a real change to the distribution fails it. The
bounds were checked by hand against 300 seeds while writing this file.
"""

from __future__ import annotations

import math
import random
import statistics

import pytest

from netkeeper.linkedin import pacing
from netkeeper.linkedin.pacing import (
    InvalidTypingProfile,
    MultilineRefused,
    TypeStep,
    TypingPlanError,
    TypingTooLong,
    UnsupportedCharacter,
    is_untypable,
    is_untypable_cluster,
    plan_duration,
    typing_expected_seconds,
    typing_length_warning,
    typing_plan,
)

_SEEDS = range(200)
# A sentence-dense body: a sentence-end pause every few characters.
_DENSE_999 = ("I am so ok. We go. Hi! " * 50)[:999]
# Emoji and combining sequences, written as escapes so no invisible character hides
# in the source.
_FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # man, ZWJ, woman, ZWJ, girl
_THUMB_TONE = "\U0001f44d\U0001f3fd"  # thumbs up, medium skin tone
_FLAG_US = "\U0001f1fa\U0001f1f8"  # regional indicators U, S
_FLAG_ENGLAND = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
_FLAG_SCOTLAND = "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
_E_ACUTE = "e\u0301"  # e, combining acute accent
_WORDS = (
    "hi",
    "there",
    "it",
    "has",
    "been",
    "a",
    "while.",
    "hope",
    "the",
    "new",
    "role",
    "is",
    "going",
    "well!",
    "are",
    "you",
    "free",
    "for",
    "coffee",
    "next",
    "week?",
    "I",
    "would",
    "love",
    "to",
    "catch",
    "up",
    "and",
    "hear",
    "what",
    "you",
    "are",
    "working",
    "on,",
    "and",
    "share",
    "a",
    "few",
    "things",
    "from",
    "my",
    "side.",
)


def _unclamped(
    text: str, seed: int, *, max_seconds: float, allow_newlines: bool = False
) -> pacing.TypingPlan:
    """A plan past the 300 s ceiling, for statistics only, through the private path."""
    return pacing._typing_plan_unclamped(
        text,
        random.Random(seed),
        pacing.DEFAULT_TYPING,
        allow_newlines=allow_newlines,
        max_seconds=max_seconds,
    )


def _body(length: int, seed: int) -> str:
    """An invented single-line body of exactly ``length`` characters."""
    rng = random.Random(seed)
    out = ""
    while len(out) < length:
        out += rng.choice(_WORDS) + " "
    return out[:length]


def _random_text(rng: random.Random, length: int) -> str:
    """Arbitrary typable text with line breaks in every form, emoji, and odd spaces."""
    alphabet = [
        *"abcdefgh .,!?'-",
        *("\n", "\r\n", "\r", "\u00a0", "\u00e9", "\U0001f600", "\u65e5"),
        *(_FAMILY, _THUMB_TONE, _FLAG_US, _FLAG_SCOTLAND, _E_ACUTE),
    ]
    return "".join(rng.choice(alphabet) for _ in range(length))


# --- pinned constants ----------------------------------------------------------


def test_typing_constants_are_pinned_to_the_decision() -> None:
    """Maintainer decision 2026-10-03, written out (CLAUDE.md, safety-relevant constants)."""
    assert (
        pacing.TypingProfile(
            char_median_s=0.14,
            char_sigma=0.45,
            word_extra_median_s=0.12,
            sentence_extra_median_s=0.6,
            extra_sigma=0.45,
            thinking_p=0.02,
            thinking_range_s=(0.8, 2.5),
            floor_s=0.04,
        )
        == pacing.DEFAULT_TYPING
    )
    # Each field by literal too, so a failure names the field that drifted.
    profile = pacing.DEFAULT_TYPING
    assert (profile.char_median_s, profile.char_sigma) == (0.14, 0.45)
    assert (profile.word_extra_median_s, profile.sentence_extra_median_s) == (0.12, 0.6)
    assert profile.extra_sigma == 0.45
    assert (profile.thinking_p, profile.thinking_range_s) == (0.02, (0.8, 2.5))
    assert profile.floor_s == 0.04
    assert pacing.MAX_TYPING_SECONDS == 300
    assert pacing.TYPING_WARN_CHARS == 1000
    assert pacing.TYPING_LINT_SECONDS == 240
    # The same line breaks netkeeper.campaigns.render splits a header on.
    assert frozenset("\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029") == pacing.LINE_BREAK_CHARS
    # The only line breaks the newline flag governs.
    assert frozenset({"\r", "\n"}) == pacing.NEWLINE_CHARS
    # Multi-line stays refused until P4-06 (#374) shows Shift+Enter never sends.
    assert pacing.SHIFT_ENTER_NEWLINES_ALLOWED is False


# --- determinism -----------------------------------------------------------------


def test_the_same_seed_gives_the_same_plan() -> None:
    for seed in _SEEDS:
        text = _random_text(random.Random(seed), 120)
        first = typing_plan(text, random.Random(seed), allow_newlines=True)
        second = typing_plan(text, random.Random(seed), allow_newlines=True)
        assert first == second


def test_different_seeds_give_different_plans() -> None:
    text = _body(200, 1)
    plans = {typing_plan(text, random.Random(seed)) for seed in range(50)}
    assert len(plans) == 50


def test_the_plan_never_reads_the_global_random() -> None:
    text = _body(200, 2)
    random.seed(1)
    first = typing_plan(text, random.Random(7))
    random.seed(99)
    second = typing_plan(text, random.Random(7))
    assert first == second


# --- the keystrokes themselves -----------------------------------------------------


def test_no_step_ever_carries_an_enter() -> None:
    """No plain Enter, ever: no chunk holds a line break or any control character."""
    for seed in _SEEDS:
        rng = random.Random(seed)
        text = _random_text(rng, rng.randint(0, 300))
        plan = typing_plan(text, random.Random(seed), allow_newlines=True)
        for step in plan:
            assert not any(c in pacing.LINE_BREAK_CHARS for c in step.chunk)
            assert not any(is_untypable(c) for c in step.chunk)
            assert all(ord(c) >= 0x20 and not 0x7F <= ord(c) <= 0x9F for c in step.chunk)
            if step.newline:
                assert step.chunk == ""
                assert not step.needs_insert_text
            else:
                assert step.chunk


def test_line_breaks_become_their_own_newline_steps() -> None:
    """``\\n``, ``\\r``, and ``\\r\\n`` are one newline step each; the text round-trips."""
    for seed in _SEEDS:
        rng = random.Random(seed)
        text = _random_text(rng, rng.randint(0, 300))
        plan = typing_plan(text, random.Random(seed), allow_newlines=True)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        rebuilt = "".join("\n" if step.newline else step.chunk for step in plan)
        assert rebuilt == normalized
        assert sum(step.newline for step in plan) == normalized.count("\n")


@pytest.mark.parametrize(
    "text", ["zebra\nquilt", "zebra\r\nquilt", "zebra\rquilt", "zebra\n", "\n"]
)
def test_a_line_break_is_refused_by_default(text: str) -> None:
    with pytest.raises(MultilineRefused) as raised:
        typing_plan(text, random.Random(0))
    assert "zebra" not in str(raised.value)  # the body is never quoted


_CONTROLS = [chr(cp) for cp in [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]]


@pytest.mark.parametrize("char", _CONTROLS, ids=[f"U+{ord(c):04X}" for c in _CONTROLS])
def test_every_control_character_is_refused_or_a_newline_step(char: str) -> None:
    """Every C0 and C1 control and DEL: CR or LF by default raises MultilineRefused,
    and with newlines allowed becomes a newline step. Anything else, including the
    other line breaks (VT, FF, FS, GS, RS, NEL), raises UnsupportedCharacter with the
    flag on or off."""
    text = f"secret{char}body"
    with pytest.raises(TypingPlanError) as raised:
        typing_plan(text, random.Random(0))
    assert "secret" not in str(raised.value)
    assert is_untypable(char)
    if char in {"\r", "\n"}:
        assert isinstance(raised.value, MultilineRefused)
        plan = typing_plan(text, random.Random(0), allow_newlines=True)
        assert [step.newline for step in plan].count(True) == 1
    else:
        assert isinstance(raised.value, UnsupportedCharacter)
        with pytest.raises(UnsupportedCharacter):
            typing_plan(text, random.Random(0), allow_newlines=True)


@pytest.mark.parametrize(
    "char",
    [
        "\u2028",  # line separator
        "\u2029",  # paragraph separator
    ],
)
@pytest.mark.parametrize("allow_newlines", [False, True])
def test_unicode_separators_are_refused_whatever_the_flag(char: str, allow_newlines: bool) -> None:
    assert char in pacing.LINE_BREAK_CHARS
    assert char not in pacing.NEWLINE_CHARS
    with pytest.raises(UnsupportedCharacter):
        typing_plan(f"a{char}b", random.Random(0), allow_newlines=allow_newlines)


@pytest.mark.parametrize(
    "char",
    [
        "\u202e",  # right-to-left override (Cf)
        "\u200b",  # zero-width space (Cf)
        "\ufeff",  # byte-order mark (Cf)
        "\u2066",  # left-to-right isolate (Cf)
        "\ue000",  # private use (Co)
        "\ud800",  # lone surrogate (Cs)
        "\U000e0001",  # language tag (Cf), outside the allowed tag range
        "\u0378",  # unassigned (Cn)
    ],
)
def test_format_private_surrogate_and_unassigned_are_refused(char: str) -> None:
    assert is_untypable(char)
    with pytest.raises(UnsupportedCharacter) as raised:
        typing_plan(f"secret{char}body", random.Random(0), allow_newlines=True)
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "text",
    [
        "\U000e0068\U000e0069",  # a lone tag run: "hi" in tags
        "a\U000e0068\U000e0069",  # tags after a letter
        " \U000e0068\U000e0069",  # tags after a space
        "\U0001f600\U000e0068\U000e0069\U000e007f",  # tags on an emoji that isn't a flag
        "\U0001f3f4\U000e0068\U000e0069",  # a black flag without the cancel tag
        "\U0001f3f4\U000e007f",  # a black flag with no tag letters
        "\U0001f3f4\U000e0048\U000e0049\U000e007f",  # uppercase tag letters
        "\U000e007f",  # a lone cancel tag
    ],
)
def test_tag_characters_outside_a_subdivision_flag_are_refused(text: str) -> None:
    for allow_newlines in (False, True):
        with pytest.raises(UnsupportedCharacter):
            typing_plan(f"ok {text} ok", random.Random(0), allow_newlines=allow_newlines)
    with pytest.raises(ValueError):
        TypeStep(chunk=text, delay_before_s=0.1, newline=False)


@pytest.mark.parametrize("flag", [_FLAG_ENGLAND, _FLAG_SCOTLAND])
def test_subdivision_flags_are_typable(flag: str) -> None:
    assert not is_untypable_cluster(flag)
    plan = typing_plan(f"go {flag}", random.Random(0))
    assert plan[-1].chunk == flag
    assert plan[-1].needs_insert_text


@pytest.mark.parametrize(
    "text",
    [
        "\u2764\ufe0f\ufe0f",  # two variation selectors on one heart
        "a\ufe0e\ufe0f",
        "\u8fbb\U000e0100\U000e0101",  # two ideographic variation selectors
        "a\u034fb",  # combining grapheme joiner
        "\u115f",  # Hangul choseong filler
        "\u1160",  # Hangul jungseong filler
        "\u3164",  # Hangul filler
    ],
)
def test_invisible_fillers_and_stacked_variation_selectors_are_refused(text: str) -> None:
    with pytest.raises(UnsupportedCharacter):
        typing_plan(text, random.Random(0), allow_newlines=True)


def test_one_variation_selector_is_typable() -> None:
    heart = "\u2764\ufe0f"
    assert not is_untypable_cluster(heart)
    assert typing_plan(heart, random.Random(0))[0].chunk == heart


def test_newline_pairs_other_than_crlf_are_two_steps() -> None:
    for text in ["a\n\rb", "a\r\rb", "a\n\nb"]:
        plan = typing_plan(text, random.Random(0), allow_newlines=True)
        assert [step.newline for step in plan] == [False, True, True, False]
    plan = typing_plan("a\r\nb", random.Random(0), allow_newlines=True)
    assert [step.newline for step in plan] == [False, True, False]


def test_the_joiners_and_tag_characters_are_typable() -> None:
    for char in ["\u200d", "\u200c", *(chr(cp) for cp in range(0xE0020, 0xE0080))]:
        assert not is_untypable(char)


def test_a_step_cannot_be_built_around_a_line_break() -> None:
    """The plan's shape is enforced by TypeStep itself, not only by typing_plan."""
    for chunk in ["\n", "\r", "\r\n", "a\n", "ab", "", "\t", "a\u200b", "\u202e"]:
        with pytest.raises(ValueError):
            TypeStep(chunk=chunk, delay_before_s=0.1, newline=False)
    with pytest.raises(ValueError):
        TypeStep(chunk="\n", delay_before_s=0.1, newline=True)
    for delay in [-0.1, math.nan, math.inf, -math.inf]:
        with pytest.raises(ValueError):
            TypeStep(chunk="a", delay_before_s=delay, newline=False)


def test_each_grapheme_cluster_is_one_step() -> None:
    """An emoji sequence or a letter with a combining mark is one step, inserted as text."""
    clusters = [
        "\U0001f600",
        _FAMILY,
        _THUMB_TONE,
        _FLAG_US,
        _FLAG_SCOTLAND,
        _E_ACUTE,
        "\u65e5",
    ]
    plan = typing_plan("ok " + " ".join(clusters), random.Random(3))
    chunks = [step.chunk for step in plan if step.chunk != " "]
    assert chunks == ["o", "k", *clusters]
    for step in plan:
        printable_ascii = len(step.chunk) == 1 and 0x20 <= ord(step.chunk) <= 0x7E
        assert step.needs_insert_text is not printable_ascii


def test_no_typos_or_backspaces() -> None:
    """Every step types the next character of the body, in order, and nothing else."""
    for seed in _SEEDS:
        text = _body(150, seed)
        plan = typing_plan(text, random.Random(seed))
        assert "".join(step.chunk for step in plan) == text
        assert all(step.chunk not in {"\b", "\x7f"} for step in plan)


# --- timing ---------------------------------------------------------------------------


def test_every_delay_is_at_or_above_the_floor() -> None:
    for seed in _SEEDS:
        rng = random.Random(seed)
        text = _random_text(rng, 200)
        for step in typing_plan(text, random.Random(seed), allow_newlines=True):
            assert step.delay_before_s >= 0.04


def test_the_floor_applies_when_the_distribution_would_go_under_it() -> None:
    profile = pacing.TypingProfile(char_median_s=0.001, thinking_p=0.0)
    plan = typing_plan("x" * 200, random.Random(0), profile)
    assert all(step.delay_before_s == 0.04 for step in plan)


def test_the_median_per_character_delay_is_near_140_ms() -> None:
    # A run with no word or sentence boundary, so only the per-character delay and
    # the 2% thinking pause apply. Over 5,000 characters the sample median of a
    # lognormal(log 0.14, 0.45) lands within a few ms of 0.14 s (the thinking pause
    # moves it by about 2% at most); 0.125 to 0.155 never flakes across 200 seeds,
    # and a median of 0.12 or 0.16 fails it.
    for seed in range(20):
        plan = _unclamped("x" * 5000, seed, max_seconds=10_000)
        median = statistics.median(step.delay_before_s for step in plan)
        assert 0.125 <= median <= 0.155


def test_the_delay_distribution_has_a_lognormal_spread() -> None:
    # A lognormal with sigma 0.45 puts its quartiles at 0.14 * exp(+-0.674 * 0.45),
    # about 0.103 and 0.190 s. The bounds allow sampling noise; a uniform delay (zero
    # spread) or a sigma of 0.2 or 0.8 fails them.
    plan = _unclamped("x" * 5000, 11, max_seconds=10_000)
    q1, _, q3 = statistics.quantiles((step.delay_before_s for step in plan), n=4)
    assert 0.093 <= q1 <= 0.113
    assert 0.175 <= q3 <= 0.210


def test_thinking_pauses_happen_about_two_percent_of_the_time() -> None:
    # Only a thinking pause pushes a no-boundary delay past 0.8 s in practice: the
    # per-character lognormal goes that high with probability about 1e-4. Over
    # 20,000 characters the expected count is 400 (sd about 20); 320 to 480 is four
    # standard deviations each way, and a probability of 0.01 or 0.03 fails it.
    plan = _unclamped("x" * 20_000, 5, max_seconds=100_000)
    long = sum(step.delay_before_s >= 0.8 for step in plan)
    assert 320 <= long <= 480
    assert max(step.delay_before_s for step in plan) < 2.5 + 2.0


def test_word_and_sentence_ends_get_their_extra_pause() -> None:
    # Median delay of the first character of a word is about 0.14 + 0.12 s, and of a
    # sentence about 0.14 + 0.6 s; the median of a mid-word character stays 0.14 s.
    mid_word: list[float] = []
    word_start: list[float] = []
    sentence_start: list[float] = []
    for seed in range(100):
        plan = typing_plan("abcd efgh. ijkl " * 10, random.Random(seed))
        for index, step in enumerate(plan):
            previous = plan[index - 1].chunk if index else ""
            before = plan[index - 2].chunk if index > 1 else ""
            if step.chunk == " ":
                continue
            if previous == " " and before == ".":
                sentence_start.append(step.delay_before_s)
            elif previous == " ":
                word_start.append(step.delay_before_s)
            elif previous not in {"", " "}:
                mid_word.append(step.delay_before_s)
    assert 0.125 <= statistics.median(mid_word) <= 0.155
    assert 0.22 <= statistics.median(word_start) <= 0.30
    assert 0.65 <= statistics.median(sentence_start) <= 0.85


def test_a_500_character_body_takes_an_expected_time() -> None:
    # Over 300 seeds while writing this, a 500-character body took 104 to 141 s
    # (mean 117 s, about 0.23 s a character with pauses). 85 to 170 s never flakes,
    # and doubling or halving the median per-character delay fails it.
    for seed in _SEEDS:
        duration = plan_duration(typing_plan(_body(500, seed), random.Random(seed)))
        assert 85 <= duration <= 170


# --- the ceiling and the warning -----------------------------------------------------


def test_a_body_over_the_ceiling_raises_typing_too_long() -> None:
    for seed in range(20):
        with pytest.raises(TypingTooLong) as raised:
            typing_plan(_body(3000, seed), random.Random(seed))
        assert raised.value.ceiling_s == 300
        assert raised.value.duration_s > 300
        assert isinstance(raised.value, TypingPlanError)
        assert "hope" not in str(raised.value) and "coffee" not in str(raised.value)


def test_every_returned_plan_is_under_the_ceiling() -> None:
    """The property the ceiling guarantees: no plan the function returns exceeds it."""
    for seed in _SEEDS:
        length = random.Random(seed).randint(800, 1600)
        try:
            plan = typing_plan(_body(length, seed), random.Random(seed))
        except TypingTooLong:
            continue
        assert plan_duration(plan) <= 300


def test_the_ceiling_is_honored_at_a_custom_value() -> None:
    text = _body(100, 0)
    plan = typing_plan(text, random.Random(0))
    duration = plan_duration(plan)
    with pytest.raises(TypingTooLong):
        typing_plan(text, random.Random(0), max_seconds=duration - 0.001)
    assert typing_plan(text, random.Random(0), max_seconds=duration) == plan


def test_the_warning_starts_above_1000_characters() -> None:
    assert typing_length_warning("") is False
    assert typing_length_warning("x" * 999) is False
    assert typing_length_warning("x" * 1000) is False  # exactly 1,000 is clean
    assert typing_length_warning("x" * 1001) is True
    # Every length on either side of the line, by property rather than example.
    for length in range(0, 2000, 37):
        assert typing_length_warning(_body(length, length)) is (length > 1000)


def test_a_dense_body_under_the_warning_can_still_exceed_the_ceiling() -> None:
    # The character-count warning alone does not keep a body under the ceiling: a
    # sentence-dense 999-character body expects about 297 s, so roughly a third of
    # seeds go over 300 s. typing_expected_seconds is what flags it.
    assert typing_length_warning(_DENSE_999) is False
    assert typing_expected_seconds(_DENSE_999) > pacing.TYPING_LINT_SECONDS
    outcomes = []
    for seed in range(100):
        try:
            typing_plan(_DENSE_999, random.Random(seed))
            outcomes.append(True)
        except TypingTooLong:
            outcomes.append(False)
    assert True in outcomes and False in outcomes


def test_the_estimate_matches_the_sampled_mean() -> None:
    # Over ten batches of 60 seeds, the sampled mean landed within 1.5% of the
    # estimate for every body here. Within 3% never flakes; dropping the lognormal's
    # mean correction (about 10%) or the thinking pause (about 15%) fails it.
    for text in [_DENSE_999, _body(500, 4), "x" * 1500, _body(800, 9).replace(" ", "\n")]:
        expected = typing_expected_seconds(text)
        sampled = statistics.mean(
            plan_duration(_unclamped(text, seed, allow_newlines=True, max_seconds=1e9))
            for seed in range(60)
        )
        assert abs(sampled - expected) <= 0.03 * expected


def test_the_estimate_is_deterministic_and_never_raises() -> None:
    awkward = "a\tb\u202ec\n" + _FAMILY + "\x00"
    assert typing_expected_seconds(awkward) == typing_expected_seconds(awkward)
    assert typing_expected_seconds("") == 0


def test_a_body_under_the_lint_threshold_fits_the_ceiling() -> None:
    """The property P4-11's lint relies on: an expected time at or under 240 s leaves
    enough margin that no seed goes over 300 s."""
    # Each body is trimmed to the longest prefix whose estimate is under the lint
    # threshold. The sampled spread is about 7 s, so 300 s is about eight standard
    # deviations above 240 s.
    bodies = [("I am so ok. We go. Hi! " * 60), _body(2000, 1), "x" * 2000]
    for body in bodies:
        length = len(body)
        while typing_expected_seconds(body[:length]) > pacing.TYPING_LINT_SECONDS:
            length -= 10
        text = body[:length]
        for seed in range(100):
            assert plan_duration(typing_plan(text, random.Random(seed))) <= 300


def test_a_total_that_is_not_finite_never_passes_the_ceiling() -> None:
    # Two finite delays of about 1e308 overflow to an infinite total. Even with an
    # infinite ceiling, which ``inf <= inf`` would pass, the plan is refused.
    huge = pacing.TypingProfile(char_median_s=1e308, char_sigma=0.0, thinking_p=0.0)
    with pytest.raises(TypingTooLong):
        pacing._typing_plan_unclamped(
            "ab", random.Random(0), huge, allow_newlines=False, max_seconds=math.inf
        )


@pytest.mark.parametrize("max_seconds", [300.001, 1e9, math.inf])
def test_max_seconds_never_raises_the_ceiling(max_seconds: float) -> None:
    with pytest.raises(TypingTooLong) as raised:
        typing_plan(_body(3000, 0), random.Random(0), max_seconds=max_seconds)
    assert raised.value.ceiling_s == 300


@pytest.mark.parametrize(
    "field",
    [
        {"char_median_s": math.nan},
        {"char_median_s": 0.0},
        {"char_median_s": -0.1},
        {"char_median_s": math.inf},
        {"word_extra_median_s": 0.0},
        {"sentence_extra_median_s": math.nan},
        {"floor_s": 0.0},
        {"floor_s": math.inf},
        {"char_sigma": -0.1},
        {"char_sigma": 2.5},
        {"extra_sigma": math.nan},
        {"thinking_p": -0.01},
        {"thinking_p": 1.01},
        {"thinking_p": math.nan},
        {"thinking_range_s": (2.5, 0.8)},
        {"thinking_range_s": (-1.0, 1.0)},
        {"thinking_range_s": (0.8, math.inf)},
        {"thinking_range_s": (math.nan, 1.0)},
    ],
    ids=lambda field: next(iter(field)),
)
def test_a_profile_out_of_range_is_refused(field: dict[str, object]) -> None:
    with pytest.raises(InvalidTypingProfile) as raised:
        pacing.TypingProfile(**field)  # type: ignore[arg-type]
    assert isinstance(raised.value, TypingPlanError)


def test_profile_edges_that_are_allowed() -> None:
    pacing.TypingProfile(char_sigma=0.0, extra_sigma=2.0, thinking_p=0.0)
    pacing.TypingProfile(thinking_p=1.0, thinking_range_s=(1.0, 1.0))


def test_an_empty_body_is_an_empty_plan() -> None:
    assert typing_plan("", random.Random(0)) == ()
    assert plan_duration(()) == 0
