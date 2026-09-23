"""netkeeper.services.pacing: ``[linkedin.pacing]`` as the extractor's profiles (P2-11).

The module exists because ``plan_enrichment`` defaults every knob to the pacing
module's constants, and those constants equal ``PacingSettings``' defaults field
for field. A caller that forgets to pass the owner's settings therefore looks
correct in every test written against the defaults and diverges silently the
moment somebody edits ``config.toml`` -- which is exactly what `netkeeper
rehearse` did. So the first test here is the one that would have caught it: the
two sets of defaults agreeing is asserted rather than relied on, and every
field is checked against a value that is *not* the default.
"""

from __future__ import annotations

from dataclasses import replace

from netkeeper.config import PacingSettings
from netkeeper.linkedin.pacing import (
    DEFAULT_BURST_PROFILE,
    DEFAULT_DELAY_PROFILE,
    BurstProfile,
    DelayProfile,
)
from netkeeper.services.pacing import profiles


def test_the_defaults_of_both_sides_agree() -> None:
    """Appendix C is written down twice, and the two copies have to say the same thing.

    ``config.example.toml``'s ``[linkedin.pacing]`` and the constants in
    ``netkeeper.linkedin.pacing`` are independent transcriptions of Appendix C.
    If they ever disagree, a caller that passes the config and one that does
    not would pace differently, and only one of them would be what the owner
    configured.
    """
    result = profiles(PacingSettings())

    assert result.delay == DEFAULT_DELAY_PROFILE
    assert result.burst == DEFAULT_BURST_PROFILE


def test_every_field_is_carried_across() -> None:
    """Each value differs from its default, so a field the adapter drops shows up here."""
    settings = PacingSettings(
        profile_delay_median_s=7,
        profile_delay_sigma=0.9,
        distraction_p=0.25,
        distraction_range_s=(30, 90),
        burst_size=(3, 4),
        burst_break_s=(60, 120),
    )

    result = profiles(settings)

    assert result.delay == DelayProfile(median=7.0, sigma=0.9, tail_p=0.25, tail_range=(30.0, 90.0))
    assert result.burst == BurstProfile(size_range=(3, 4), break_range_s=(60.0, 120.0))


def test_the_ranges_come_back_as_floats() -> None:
    """TOML writes ``[300, 1200]`` as integers; the pacing module draws real waits."""
    result = profiles(PacingSettings())

    assert all(isinstance(value, float) for value in result.delay.tail_range)
    assert all(isinstance(value, float) for value in result.burst.break_range_s)
    assert isinstance(result.delay.median, float)


def test_a_value_outside_its_safe_range_is_carried_across_unchanged() -> None:
    """The adapter does not correct anything, and must not.

    A median of zero is a real config somebody can write. Reporting it is
    ``services.posture``'s job and refusing it is ``pacing.human_delay``'s; an
    adapter that quietly substituted a sane value would defeat both, and the
    owner would keep the broken config and never hear about it.
    """
    result = profiles(replace(PacingSettings(), profile_delay_median_s=0))

    assert result.delay.median == 0.0
