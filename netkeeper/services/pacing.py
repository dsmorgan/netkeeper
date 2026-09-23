"""Config's ``[linkedin.pacing]`` as the profiles :mod:`netkeeper.linkedin.pacing` takes.

The core-side adapter for a pure extractor module, the same seam
:mod:`netkeeper.services.heat` and :mod:`netkeeper.services.budgets` already
draw: the pacing math is pure and lives under ``linkedin/``, the config it is
parameterized by belongs to the core, and the translation between them happens
here rather than in either.

Why it needs to exist at all: ``pacing.plan_enrichment`` defaults every knob to
the module constants, which today happen to equal ``PacingSettings``' defaults
field for field. That means a caller that forgets to pass the user's settings
looks correct in every test and diverges the moment somebody edits
``config.toml`` -- ``netkeeper rehearse`` did exactly that, rehearsing at a
25-second median while a config asking for 5 sat unread. One conversion, in one
place, used by every caller, is the fix.

``ScrollProfile`` has no counterpart here: Appendix B gives scrolling no config
keys, so :func:`profiles` returns the delay and burst profiles only and a
caller takes the scroll defaults. When a ``[linkedin.pacing]`` scroll key is
added, it is added here and every caller inherits it.
"""

from __future__ import annotations

from dataclasses import dataclass

from netkeeper.config import PacingSettings
from netkeeper.linkedin.pacing import BurstProfile, DelayProfile


@dataclass(frozen=True, slots=True)
class PacingProfiles:
    """``[linkedin.pacing]``, in the shape ``plan_enrichment`` takes."""

    delay: DelayProfile
    burst: BurstProfile


def profiles(settings: PacingSettings) -> PacingProfiles:
    """``settings`` as the profiles :func:`netkeeper.linkedin.pacing.plan_enrichment` takes.

    A straight field mapping, with the tuples widened from ``int`` to ``float``
    because TOML writes ``[300, 1200]`` as integers and the pacing module draws
    real-valued waits from them. Nothing is validated here: a value outside its
    safe range is :mod:`netkeeper.services.posture`'s to report and the pacing
    module's to refuse, and an adapter that silently corrected one would hide
    both.
    """
    return PacingProfiles(
        delay=DelayProfile(
            median=float(settings.profile_delay_median_s),
            sigma=settings.profile_delay_sigma,
            tail_p=settings.distraction_p,
            tail_range=(
                float(settings.distraction_range_s[0]),
                float(settings.distraction_range_s[1]),
            ),
        ),
        burst=BurstProfile(
            size_range=settings.burst_size,
            break_range_s=(
                float(settings.burst_break_s[0]),
                float(settings.burst_break_s[1]),
            ),
        ),
    )
