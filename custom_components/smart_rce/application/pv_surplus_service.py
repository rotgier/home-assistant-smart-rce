"""PvSurplusService — hourly PV surplus forecast, realized surplus, variant table.

Which day: today until the start of T2, then tomorrow (the evening already
shows what the next day's sun can fill). Window: actual charge start of that
day .. start of T2 — before the charge starts the battery takes nothing from
PV, so only those hours make room for sun.

Consumption: per-hour median of the same kind of earlier days (`DayType`),
plus the flat 0.9 kWh/h for comparison. PV: the LIVE forecast for the sensors;
every variant of the day for the table.

Realized surplus is today's measured counterpart over the same window — the
forecast at 08:00 against it at the end of the window calibrates the forecast.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util

from ..domain.pv_forecast import PvForecast
from ..domain.pv_surplus import (
    DayType,
    SurplusProfile,
    median_by_hour,
    surplus_window_end,
)

if TYPE_CHECKING:
    from ..domain.pv_forecast import PvForecastResult
    from ..infrastructure.pv_forecast.hourly_energy_loader import HourlyEnergyLoader
    from .energy_balance_service import EnergyBalanceService

_LOGGER = logging.getLogger(__name__)

# Energy that goes in per 1 pp of SoC when charging — battery charge counter
# against SoC on 2026-10-11 (6.6 kWh for 58 pp).
CHARGE_KWH_PER_PP: Final[float] = 0.114


@dataclass(frozen=True)
class SurplusForecast:
    """Forecast surplus of one day: LIVE with median and flat consumption."""

    day: date
    source: str
    day_type: DayType
    start_hour: int | None
    end_hour: int
    prev: SurplusProfile
    flat: SurplusProfile
    prev_days: tuple[date, ...]
    matrix: dict[str, Any] = field(default_factory=dict)

    def total(self, profile: SurplusProfile) -> float | None:
        if self.start_hour is None:
            return None
        return round(profile.total(self.start_hour, self.end_hour), 3)

    def remaining(self, profile: SurplusProfile, now: datetime) -> float | None:
        """Surplus from the current hour on (today), whole window otherwise."""
        if self.start_hour is None:
            return None
        start = self.start_hour
        if now.date() == self.day:
            start = max(start, now.hour)
        return round(profile.total(start, self.end_hour), 3)


@dataclass(frozen=True)
class RealizedSurplus:
    """Measured surplus of today over the same window, so far."""

    day: date
    start_hour: int | None
    end_hour: int
    profile: SurplusProfile

    @property
    def total(self) -> float | None:
        if self.start_hour is None:
            return None
        return round(self.profile.total(self.start_hour, self.end_hour), 3)


class PvSurplusService:
    """Owns the surplus figures; sensors read them, listeners get notified."""

    # Table rows: matrix key (shared with the Target SOC card labels) per variant.
    _TODAY_ROWS: Final = (
        ("live", PvForecast.LIVE),
        ("at_6", PvForecast.AT_6),
        ("extrap_pattern", PvForecast.EXTRAP_PATTERN),
        ("extrap_propor", PvForecast.EXTRAP_PROPORTIONAL),
        ("extrap_band", PvForecast.EXTRAP_BAND),
        ("extrap_band_recent", PvForecast.EXTRAP_BAND_RECENT),
    )
    _TOMORROW_ROWS: Final = (
        ("live", PvForecast.TOMORROW_LIVE),
        ("at_6", PvForecast.TOMORROW_AT_6),
    )
    _WEEKDAY_ABBR: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def __init__(
        self,
        energy_balance: EnergyBalanceService,
        loader: HourlyEnergyLoader,
        clock: Callable[[], datetime] = dt_util.now,
    ) -> None:
        self._energy_balance = energy_balance
        self._loader = loader
        self._clock = clock
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._profiles_day: date | None = None
        self._prev_profiles: dict[date, dict[int, float]] = {}
        self.forecast: SurplusForecast | None = None
        self.realized: RealizedSurplus | None = None

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    async def async_refresh(self) -> None:
        """Reload consumption history (on a new target day) and today's data."""
        now = self._clock()
        day = self._target_day(now)
        if day != self._profiles_day:
            prev_days = DayType.of(day).previous_days(day)
            try:
                self._prev_profiles = await self._loader.consumption_by_day(prev_days)
                self._profiles_day = day
            except Exception:  # noqa: BLE001 — keep the old profiles, retry next tick
                _LOGGER.exception("PvSurplusService: consumption history load failed")
        await self._refresh_realized(now)
        self.recompute()

    async def _refresh_realized(self, now: datetime) -> None:
        today = now.date()
        try:
            pv, cons = await self._loader.today_by_hour(today)
        except Exception:  # noqa: BLE001 — keep the previous figure
            _LOGGER.exception("PvSurplusService: realized energy load failed")
            return
        # Only measured hours count; hours to come stay out of the sum.
        measured = {h: pv.get(h, 0.0) for h in range(now.hour + 1)}
        self.realized = RealizedSurplus(
            day=today,
            start_hour=self._energy_balance.target_socs.inputs.start_charge_hour_today,
            end_hour=min(surplus_window_end(today), now.hour + 1),
            profile=SurplusProfile.build(today, measured, cons),
        )

    @callback
    def recompute(self) -> None:
        """Rebuild the forecast from the latest PV forecast and cached history."""
        now = self._clock()
        day = self._target_day(now)
        is_today = day == now.date()
        rows = (
            PvSurplusService._TODAY_ROWS
            if is_today
            else PvSurplusService._TOMORROW_ROWS
        )
        inputs = self._energy_balance.target_socs.inputs
        start = (
            inputs.start_charge_hour_today
            if is_today
            else inputs.start_charge_hour_tomorrow
        )
        prev_days = tuple(sorted(self._prev_profiles, reverse=True))
        median = (
            median_by_hour(self._prev_profiles.values())
            if self._prev_profiles
            else None
        )
        live_pv = self._pv_by_hour(rows[0][1], day)
        self.forecast = SurplusForecast(
            day=day,
            source="live" if is_today else "tomorrow_live",
            day_type=DayType.of(day),
            start_hour=start,
            end_hour=surplus_window_end(day),
            prev=SurplusProfile.build(day, live_pv, median),
            flat=SurplusProfile.build(day, live_pv, None),
            prev_days=prev_days,
            matrix=self._matrix(day, rows, start, median, prev_days),
        )
        self._notify()

    def _target_day(self, now: datetime) -> date:
        today = now.date()
        if now.hour < surplus_window_end(today):
            return today
        return today + timedelta(days=1)

    def _matrix(
        self,
        day: date,
        rows: tuple[tuple[str, PvForecast], ...],
        start: int | None,
        median: dict[int, float] | None,
        prev_days: tuple[date, ...],
    ) -> dict[str, Any]:
        """Variant x consumption table in the Target SOC card's payload shape."""
        end = surplus_window_end(day)
        if start is None:
            return {}
        columns: list[tuple[str, dict[int, float] | None, dict[str, Any]]] = [
            ("flat", None, {"key": "flat", "label": "Flat", "sub": "0,9 kWh/h"}),
        ]
        if median is not None:
            columns.append(
                ("median", median, {"key": "median", "label": "Mediana", "sub": "prev"})
            )
        for idx, prev_day in enumerate(prev_days, start=1):
            columns.append(
                (
                    f"prev_{idx}",
                    self._prev_profiles[prev_day],
                    {
                        "key": f"prev_{idx}",
                        "weekday": PvSurplusService._WEEKDAY_ABBR[prev_day.weekday()],
                    },
                )
            )
        cells: dict[str, float] = {}
        pv_sums: dict[str, float] = {}
        cons_sums: dict[str, float] = {}
        pv_strategies: list[str] = []
        for key, variant in rows:
            pv = self._pv_by_hour(variant, day)
            if not pv:
                continue
            pv_strategies.append(key)
            pv_sums[key] = round(sum(v for h, v in pv.items() if start <= h < end), 2)
            for col_key, cons, _meta in columns:
                profile = SurplusProfile.build(day, pv, cons)
                cells[f"{key}|{col_key}"] = round(profile.total(start, end), 2)
                cons_sums[col_key] = round(profile.consumption(start, end), 2)
        return {
            "pv_strategies": pv_strategies,
            "cons_strategies": [meta for _k, _c, meta in columns],
            "cells_surplus_kwh": cells,
            "pv_sums_kwh": pv_sums,
            "cons_sums_kwh": cons_sums,
            "source_day_pv_sums_kwh": {},
            "prev_dates": [d.isoformat() for d in prev_days],
        }

    def _pv_by_hour(self, variant: PvForecast, day: date) -> dict[int, float]:
        result: PvForecastResult | None = self._energy_balance.forecasts.get(variant)
        out: dict[int, float] = {}
        if result is None:
            return out
        for period in result.forecast:
            moment = datetime.fromisoformat(str(period.period_start).replace(" ", "T"))
            if moment.date() == day:
                # pv_estimate_adjusted is a kWh/h rate over a 30-min period.
                out[moment.hour] = out.get(moment.hour, 0.0) + (
                    period.pv_estimate_adjusted / 2
                )
        return out

    def _notify(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
