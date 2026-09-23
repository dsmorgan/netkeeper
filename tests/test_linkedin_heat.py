"""netkeeper.linkedin.heat: pure decay math for the heat score (spec 9.7).

No database, no session, no models -- this module lives under ``linkedin/``
and stays on the pure side of the extractor boundary (ADR 0005, spec 9.10).
See tests/test_services_heat.py for the persisted version.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netkeeper.linkedin.heat import (
    COLD_EPSILON,
    COOLDOWN_FLOOR,
    HeatState,
    clear,
    cooldown_multiplier,
    decayed_score,
    is_skipping,
    raise_heat,
    shrink,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
HALF_LIFE_HOURS = 6.0


def _state(score: float, updated_at: datetime = NOW) -> HeatState:
    return HeatState(score=score, updated_at=updated_at)


# --- decay on read -----------------------------------------------------------


def test_decayed_score_at_zero_elapsed_is_the_stored_score() -> None:
    assert decayed_score(_state(4.0), NOW, half_life_hours=HALF_LIFE_HOURS) == 4.0


def test_decayed_score_halves_after_one_half_life() -> None:
    later = NOW + timedelta(hours=HALF_LIFE_HOURS)
    assert decayed_score(_state(8.0), later, half_life_hours=HALF_LIFE_HOURS) == pytest.approx(4.0)


def test_decayed_score_quarters_after_two_half_lives() -> None:
    later = NOW + timedelta(hours=2 * HALF_LIFE_HOURS)
    assert decayed_score(_state(8.0), later, half_life_hours=HALF_LIFE_HOURS) == pytest.approx(2.0)


def test_reading_twice_with_no_new_event_gives_a_lower_number_the_second_time() -> None:
    """The acceptance criterion, verbatim: nothing ticks this down but the passage of time.

    Removing the decay (returning ``state.score`` unconditionally) makes the
    two reads equal, which fails this test -- that is the point of it.
    """
    state = _state(5.0)
    first = decayed_score(state, NOW + timedelta(hours=1), half_life_hours=HALF_LIFE_HOURS)
    second = decayed_score(state, NOW + timedelta(hours=2), half_life_hours=HALF_LIFE_HOURS)
    assert second < first


def test_decayed_score_never_grows_from_a_now_before_updated_at() -> None:
    earlier = NOW - timedelta(hours=1)
    assert decayed_score(_state(3.0), earlier, half_life_hours=HALF_LIFE_HOURS) == 3.0


def test_decayed_score_needs_an_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decayed_score(_state(1.0), datetime(2026, 9, 20, 12, 0), half_life_hours=HALF_LIFE_HOURS)


def test_half_life_hours_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        decayed_score(_state(1.0), NOW, half_life_hours=0)


def test_heat_state_needs_an_aware_updated_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        HeatState(score=1.0, updated_at=datetime(2026, 9, 20, 12, 0))


def test_heat_state_rejects_a_negative_score() -> None:
    with pytest.raises(ValueError, match="negative"):
        HeatState(score=-1.0, updated_at=NOW)


# --- raising and clearing -----------------------------------------------------


def test_raise_heat_adds_to_the_decayed_score_not_the_raw_one() -> None:
    """A mutation that adds ``per_block`` to the raw stored score (skipping decay first)
    would give 9.0 here instead of the correct, smaller number."""
    state = _state(8.0, updated_at=NOW)
    later = NOW + timedelta(hours=HALF_LIFE_HOURS)  # decays 8.0 -> 4.0 first
    updated = raise_heat(state, later, per_block=1.0, half_life_hours=HALF_LIFE_HOURS)
    assert updated.score == pytest.approx(5.0)
    assert updated.updated_at == later


def test_raise_heat_rejects_a_negative_block() -> None:
    with pytest.raises(ValueError, match="negative"):
        raise_heat(_state(0.0), NOW, per_block=-1.0, half_life_hours=HALF_LIFE_HOURS)


def test_clear_resets_to_cold_as_of_now() -> None:
    cold = clear(NOW)
    assert cold.score == 0.0
    assert cold.updated_at == NOW


def test_clear_is_the_manual_override_a_later_raise_still_rebuilds_from() -> None:
    cold = clear(NOW)
    warmed = raise_heat(
        cold, NOW + timedelta(hours=1), per_block=1.0, half_life_hours=HALF_LIFE_HOURS
    )
    assert warmed.score == pytest.approx(1.0)


# --- skip threshold and multiplier --------------------------------------------


def test_is_skipping_below_threshold_is_false() -> None:
    assert not is_skipping(_state(1.0), NOW, half_life_hours=HALF_LIFE_HOURS, skip_threshold=2.5)


def test_is_skipping_at_or_above_threshold_is_true() -> None:
    assert is_skipping(_state(2.5), NOW, half_life_hours=HALF_LIFE_HOURS, skip_threshold=2.5)
    assert is_skipping(_state(3.0), NOW, half_life_hours=HALF_LIFE_HOURS, skip_threshold=2.5)


def test_is_skipping_uses_the_decayed_score_not_the_raw_one() -> None:
    """Raw score is over threshold, but it decayed below it by ``now``."""
    state = _state(10.0, updated_at=NOW)
    much_later = NOW + timedelta(hours=20 * HALF_LIFE_HOURS)
    assert not is_skipping(state, much_later, half_life_hours=HALF_LIFE_HOURS, skip_threshold=2.5)


def test_cooldown_multiplier_is_exactly_the_floor_when_cold() -> None:
    assert cooldown_multiplier(_state(0.0), NOW, half_life_hours=HALF_LIFE_HOURS) == COOLDOWN_FLOOR


def test_cooldown_multiplier_grows_with_heat() -> None:
    cool = cooldown_multiplier(_state(1.0), NOW, half_life_hours=HALF_LIFE_HOURS)
    hot = cooldown_multiplier(_state(5.0), NOW, half_life_hours=HALF_LIFE_HOURS)
    assert hot > cool > COOLDOWN_FLOOR


def test_cooldown_multiplier_decays_toward_the_floor_over_time() -> None:
    state = _state(5.0, updated_at=NOW)
    soon = cooldown_multiplier(state, NOW + timedelta(hours=1), half_life_hours=HALF_LIFE_HOURS)
    later = cooldown_multiplier(state, NOW + timedelta(hours=10), half_life_hours=HALF_LIFE_HOURS)
    assert COOLDOWN_FLOOR <= later < soon


# --- shrink --------------------------------------------------------------


def test_shrink_never_reaches_zero() -> None:
    assert shrink(1, 100.0) == 1


def test_shrink_divides_and_floors() -> None:
    assert shrink(100, 4.0) == 25
    assert shrink(101, 4.0) == 25


def test_shrink_at_the_floor_multiplier_is_unchanged() -> None:
    assert shrink(60, COOLDOWN_FLOOR) == 60


def test_shrink_refuses_a_multiplier_below_one() -> None:
    with pytest.raises(ValueError, match=r"at least 1\.0"):
        shrink(60, 0.5)


def test_shrink_refuses_a_non_positive_base_limit() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        shrink(0, 2.0)


# --- the cold cutoff (#160) ------------------------------------------------------


def test_the_cold_epsilon_is_one_hundredth() -> None:
    """Pinned by value (CLAUDE.md): a drift up would forget real heat, down would
    bring back the days-long residue #160 removed."""
    assert COLD_EPSILON == 0.01


def test_one_throttle_is_forgotten_by_the_budget_two_days_later() -> None:
    """#160: before the cutoff, the residue (score 0.0039, multiplier 1.0039) took
    one unit off a 60 budget -- and kept taking it for about 13 days."""
    throttled = raise_heat(clear(NOW), NOW, per_block=1.0, half_life_hours=HALF_LIFE_HOURS)
    later = NOW + timedelta(hours=48)

    assert decayed_score(throttled, later, half_life_hours=HALF_LIFE_HOURS) == 0.0
    assert cooldown_multiplier(throttled, later, half_life_hours=HALF_LIFE_HOURS) == 1.0
    assert shrink(60, cooldown_multiplier(throttled, later, half_life_hours=HALF_LIFE_HOURS)) == 60


def test_a_throttle_still_shrinks_the_budget_while_heat_is_meaningful() -> None:
    """One half-life after one block: score 0.5, multiplier 1.5, 60 -> 40."""
    throttled = raise_heat(clear(NOW), NOW, per_block=1.0, half_life_hours=HALF_LIFE_HOURS)
    later = NOW + timedelta(hours=HALF_LIFE_HOURS)

    multiplier = cooldown_multiplier(throttled, later, half_life_hours=HALF_LIFE_HOURS)
    assert multiplier == pytest.approx(1.5)
    assert shrink(60, multiplier) == 40


def test_heat_just_above_the_cutoff_still_counts() -> None:
    state = _state(0.0101)

    assert decayed_score(state, NOW, half_life_hours=HALF_LIFE_HOURS) == 0.0101
    assert cooldown_multiplier(state, NOW, half_life_hours=HALF_LIFE_HOURS) > COOLDOWN_FLOOR
    assert shrink(60, cooldown_multiplier(state, NOW, half_life_hours=HALF_LIFE_HOURS)) == 59


def test_heat_just_below_the_cutoff_reads_cold() -> None:
    """Including at zero elapsed: a stored score under the cutoff is cold on every read."""
    state = _state(0.0099)

    assert decayed_score(state, NOW, half_life_hours=HALF_LIFE_HOURS) == 0.0
    assert cooldown_multiplier(state, NOW, half_life_hours=HALF_LIFE_HOURS) == 1.0


def test_one_throttle_goes_cold_after_about_forty_hours() -> None:
    """``6 * log2(1 / 0.01)`` = 39.9 hours with the Appendix C defaults."""
    throttled = raise_heat(clear(NOW), NOW, per_block=1.0, half_life_hours=HALF_LIFE_HOURS)

    at_39h = decayed_score(throttled, NOW + timedelta(hours=39), half_life_hours=HALF_LIFE_HOURS)
    at_40h = decayed_score(throttled, NOW + timedelta(hours=40), half_life_hours=HALF_LIFE_HOURS)
    assert at_39h > 0.0
    assert at_40h == 0.0


def test_blocks_at_the_cutoff_never_accumulate() -> None:
    """Why posture treats ``per_block == COLD_EPSILON`` as off: a minute later the
    first block has decayed under the cutoff and reads 0.0, so the next block
    starts from nothing again."""
    state = raise_heat(clear(NOW), NOW, per_block=0.01, half_life_hours=HALF_LIFE_HOURS)
    for minute in range(1, 11):
        state = raise_heat(
            state, NOW + timedelta(minutes=minute), per_block=0.01, half_life_hours=HALF_LIFE_HOURS
        )
    assert state.score == 0.01
    assert decayed_score(state, NOW + timedelta(minutes=11), half_life_hours=HALF_LIFE_HOURS) == 0.0
