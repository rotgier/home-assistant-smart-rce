"""Production setup over time — which forecast, consumption model and buffer steered a day.

The "what production did" panel shows facts, so a day is scored with the setup
it really ran on, not today's. A day belongs to the setup in force at 06:11
(the main reserve moment). The variant ranking keeps today's setup — it asks
"what would the rule choose with this forecast".

Add a row here whenever production changes (deploy time, not commit time).
Sources: home-assistant-ops `plans/live-soft.md`, smart_rce commits 2321beb,
1db8b50, 20ada58, 1f87ed6 (margin), a5b711a (08-09 step capped by Live),
43d266b (07-08 step capped by max(Live, Extrap Proportional, Extrap Blend)).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, tzinfo
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo

from ..pv_forecast import PvForecast
from .consumption_models import ConsumptionModel

_WARSAW: Final = ZoneInfo("Europe/Warsaw")
# The moment a day is assigned to a setup.
ASSIGNED_AT: Final = time(6, 11)


class MarginRule(Enum):
    """How a setup's time-of-day margin was computed (`T1SocReserve`)."""

    NONE = "none"
    STEPS = "steps"  # 15/10/5, uncapped (1f87ed6): `margin_pp_at`
    LIVE_CAPS_0809 = "live_caps_0809"  # 08-09 capped by Live only (a5b711a)
    CAPPED = "capped"  # production's own caps per step (43d266b): `margin_pp_for`


@dataclass(frozen=True)
class ProductionSetup:
    """One production configuration and when it took over."""

    since: datetime
    label: str
    pv_variant: str  # a key of `variant_results.SCORED_VARIANTS`
    reserve_model: ConsumptionModel  # T1 reserve (P60, P50 or OLD_RULE)
    start_model: ConsumptionModel  # charge start (P60, P50 or FLAT)
    buffer_percent: int
    pv_label: str  # dashboard wording
    model_label: str
    # The time-of-day margin on top of the reserve and how it was capped.
    margin: MarginRule = field(default_factory=lambda: MarginRule.NONE)

    @property
    def has_margin(self) -> bool:
        return self.margin is not MarginRule.NONE


SETUPS: Final[tuple[ProductionSetup, ...]] = (
    ProductionSetup(
        datetime(2026, 1, 1, tzinfo=_WARSAW),
        "Live + max(flat, bez najgorszego), bufor 12",
        PvForecast.LIVE.key,
        ConsumptionModel.OLD_RULE,
        ConsumptionModel.FLAT,
        12,
        "Live",
        "max(flat, poprz. dni bez najgorszego)",
    ),
    ProductionSetup(
        datetime(2026, 10, 14, 0, 22, tzinfo=_WARSAW),
        "Live Soft 0,5 + max(flat, bez najgorszego), bufor 12",
        "live_soft_0.5",
        ConsumptionModel.OLD_RULE,
        ConsumptionModel.FLAT,
        12,
        "Live Soft 0,5",
        "max(flat, poprz. dni bez najgorszego)",
    ),
    ProductionSetup(
        datetime(2026, 10, 15, 0, 3, tzinfo=_WARSAW),
        "Live Soft 0,4 + mediana, bufor 10",
        PvForecast.LIVE_SOFT.key,
        ConsumptionModel.P50,
        ConsumptionModel.P50,
        10,
        "Live Soft 0,4",
        "mediana",
    ),
    ProductionSetup(
        datetime(2026, 10, 15, 19, 31, tzinfo=_WARSAW),
        "Live Soft 0,4 + p60, bufor 10",
        PvForecast.LIVE_SOFT.key,
        ConsumptionModel.P60,
        ConsumptionModel.P60,
        10,
        "Live Soft 0,4",
        "p60",
    ),
    ProductionSetup(
        datetime(2026, 10, 15, 22, 53, tzinfo=_WARSAW),
        "Live Soft 0,4 + p60, bufor 10 + margines 15/10/5",
        PvForecast.LIVE_SOFT.key,
        ConsumptionModel.P60,
        ConsumptionModel.P60,
        10,
        "Live Soft 0,4",
        "p60",
        margin=MarginRule.STEPS,
    ),
    ProductionSetup(
        datetime(2026, 10, 15, 23, 40, tzinfo=_WARSAW),
        "Live Soft 0,4 + p60, bufor 10 + margines 15/10/min(5, Live − Soft)",
        PvForecast.LIVE_SOFT.key,
        ConsumptionModel.P60,
        ConsumptionModel.P60,
        10,
        "Live Soft 0,4",
        "p60",
        margin=MarginRule.LIVE_CAPS_0809,
    ),
    ProductionSetup(
        datetime(2026, 10, 16, 8, 30, tzinfo=_WARSAW),
        "Live Soft 0,4 + p60, bufor 10 + margines 15 / min(10, max(Live, Extrap) − Soft)"
        " / min(5, Live − Soft)",
        PvForecast.LIVE_SOFT.key,
        ConsumptionModel.P60,
        ConsumptionModel.P60,
        10,
        "Live Soft 0,4",
        "p60",
        margin=MarginRule.CAPPED,
    ),
)
# Today's setup — the one the variant ranking scores against.
CURRENT: Final = SETUPS[-1]


def setup_on(day: date, tz: tzinfo) -> ProductionSetup:
    """Return the setup in force at 06:11 of `day`."""
    moment = datetime.combine(day, ASSIGNED_AT, tz)
    return [s for s in SETUPS if s.since <= moment][-1]
