"""PV weather-impact multiplier — pure function.

Computes a 0..1 PV-output multiplier from rainfall forecast inputs. Mirrors
the formula previously inlined as Jinja in the dashboard weather table card
(`dashboards/views/rce.py::weather_table_card`). Centralizing it here lets
the smart_rce PV adjustment (`PvForecast._adjust_*_period`) reuse the same
math against live weather data without dragging Jinja-style approximations
into the production path.

Inputs and output ranges:
- `probability` — precipitation probability, 0..100 (percent)
- `amount_max_mm` — upper bound of forecast rainfall in mm (the
  `precipitation_amount_mm_max` field from wo-cloud hourly forecast)
- `duration_max_min` — upper bound of forecast rainfall duration in
  minutes within the hour (0..60)
- returns `(coverage, heaviness, penalty, multiplier)`, each 0..1 floats
  except `multiplier` which is clamped to [0.1, 1.0]

The formula:
    coverage  = duration / 60                            ∈ [0, 1]
    heaviness = min(amount / 5, 1)                       ∈ [0, 1]
    confidence = min(probability / 50, 1)                ∈ [0, 1]
    penalty   = coverage × (0.4 + 0.5 × heaviness) × confidence
    multiplier = max(0.1, 1 - penalty)

Special case: if probability < 30 or duration == 0, no rain expected →
`multiplier = 1.0`, `penalty = 0.0`. This matches the dashboard
short-circuit and prevents the formula from misfiring on noisy low-prob
data with non-zero amount estimates.
"""

from __future__ import annotations

from typing import NamedTuple

PROB_THRESHOLD = 30  # below this, treat as "no rain" — multiplier=1.0
AMOUNT_HEAVY_CAP_MM = 5.0  # rainfall >= this counts as fully heavy
PROB_CONFIDENCE_CAP = 50.0  # probability >= this counts as fully confident
HEAVINESS_BASE = 0.4  # baseline penalty weight even for light rain
HEAVINESS_SCALE = 0.5  # additional weight that scales with heaviness
MULTIPLIER_FLOOR = 0.1  # never zero out PV completely


class MultiplierBreakdown(NamedTuple):
    """Per-input intermediate values + final multiplier."""

    coverage: float
    heaviness: float
    penalty: float
    multiplier: float


def compute_multiplier(
    probability: float | None,
    amount_max_mm: float | None,
    duration_max_min: float | None,
) -> MultiplierBreakdown:
    """Compute PV weather multiplier breakdown.

    `None` inputs are treated as zero — common when wo-cloud omits
    `precipitation.details.*` for low-probability hours.
    """
    prob = float(probability or 0)
    amount = float(amount_max_mm or 0)
    duration = float(duration_max_min or 0)

    if prob < PROB_THRESHOLD or duration == 0:
        return MultiplierBreakdown(
            coverage=duration / 60.0 if duration else 0.0,
            heaviness=min(amount / AMOUNT_HEAVY_CAP_MM, 1.0) if amount else 0.0,
            penalty=0.0,
            multiplier=1.0,
        )

    coverage = duration / 60.0
    heaviness = min(amount / AMOUNT_HEAVY_CAP_MM, 1.0)
    confidence = min(prob / PROB_CONFIDENCE_CAP, 1.0)
    penalty = coverage * (HEAVINESS_BASE + HEAVINESS_SCALE * heaviness) * confidence
    multiplier = max(MULTIPLIER_FLOOR, 1.0 - penalty)

    return MultiplierBreakdown(
        coverage=coverage,
        heaviness=heaviness,
        penalty=penalty,
        multiplier=multiplier,
    )
