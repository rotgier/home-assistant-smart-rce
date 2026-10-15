"""Consumption percentile — how earlier days are folded into one expected profile.

The T1 reserve (D1) and the charge-start shift (D2) both read consumption as
the 60th percentile of earlier days of the same kind, per 30-min slot (decision
15.10.2026; home-assistant-ops `research/2026-10-15-percentyle-zuzycia/`).
Over 126 workdays the median left the reserve ~0.9 pp short on average, p60
~0.2 pp, p70 overshot by ~0.7 pp; one odd day still moves the profile little.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable, Mapping

CONSUMPTION_PERCENTILE: Final[float] = 0.6


def percentile(values: Iterable[float], fraction: float) -> float:
    """Linear interpolation between sorted values, rank k = (n - 1) * fraction.

    The numpy default ("linear"), so 0.5 is the median. Raises ValueError when
    there are no values.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of no values")
    k = (len(ordered) - 1) * fraction
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def percentile_by_key[K: Hashable](
    profiles: Iterable[Mapping[K, float]],
    fraction: float = CONSUMPTION_PERCENTILE,
) -> dict[K, float]:
    """Per-key percentile over profiles; a key counts the profiles that have it."""
    by_key: dict[K, list[float]] = {}
    for profile in profiles:
        for key, value in profile.items():
            by_key.setdefault(key, []).append(value)
    return {key: percentile(values, fraction) for key, values in by_key.items()}
