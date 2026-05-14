"""TargetSocMatrixService — application service for the target-SOC matrix.

DDD application layer: pulls PV strategy buckets from the PvForecast
aggregate (today: 8 strategies, tomorrow: 2 — fewer because extrapolated
variants are dziś-only), Cons baselines from consumption_profiles + a
synthetic live baseline, and source-day realized PV sums via
`RealizedPvLoader.fetch_for_dates`. Delegates the cross-product to the
pure domain `target_soc_matrix.compute_matrix`.

Returns a dict shaped for the smart_rce service response and the
bridging sensor attribute:

```python
{
    "date": "2026-05-13",
    "kind": "today" | "tomorrow" | "past_unsupported",
    "matrix": {
        "pv_strategies": [...],
        "cons_strategies": [{"key": "...", "weekday": "Mon"}, ...],
        "cells_pct": {"pv_key|cons_key": int, ...},
        "cells_kwh": {"pv_key|cons_key": float, ...},
        "pv_sums_kwh": {"pv_key": float, ...},
        "cons_sums_kwh": {"cons_key": float, ...},
        "source_day_pv_sums_kwh": {"cons_key": float | None, ...},
    },
}
```

Past dates surface as `kind: "past_unsupported"` (no matrix) — the
dashboard renders an "N/A" message. v2 will add recorder-based
reconstruction; out of scope here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..application.pv_forecast_service import PvForecastService
from ..domain import pv_forecast as pv_forecast_module
from ..domain.pv_forecast import (
    AdjustedPvForecast,
    ConsumptionProfile,
    ExtrapolatedLive,
    PvForecast,
    PvProfile,
)
from ..domain.target_soc_matrix import ConsLabel, TargetSocMatrix, compute_matrix
from ..infrastructure.pv_forecast.consumption_profile_loader import (
    ConsumptionProfileLoader,
)
from ..infrastructure.pv_forecast.realized_pv_loader import RealizedPvLoader

_LOGGER = logging.getLogger(__name__)

# Strategy key conventions — stable identifiers for matrix tuples and
# dashboard column/row layout. Order matters: dashboards iterate the
# tuple to choose default-on series.
_TODAY_PV_KEYS: tuple[str, ...] = (
    "at_6",
    "live",
    "extrap",
    "extrap_5min",
    "extrap_pattern",
    "extrap_propor",
    "extrap_band",
    "extrap_band_recent",
)
_TOMORROW_PV_KEYS: tuple[str, ...] = ("at_6", "live")

_LIVE_CONS_KEY: str = "live"

# 12-bucket window (7:00..12:30) — strict PvProfile/ConsumptionProfile contract.
_BUCKET_TIMES: tuple[tuple[int, int], ...] = tuple(
    (7 + idx // 2, (idx % 2) * 30) for idx in range(12)
)

_WEEKDAY_ABBR: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Toggle that controls whether the *today* matrix is computed in
# "now-aware" mode (in-progress bucket time-prorated, past buckets
# skipped — cells match the bridging sensors) or full-window mode
# (iterate 7:00..12:30 unconditionally — porównywalnie z prev workday).
# Default: now-aware (state missing / unknown → True).
_NOW_AWARE_TOGGLE = "input_boolean.rce_target_soc_matrix_now_aware"


class TargetSocMatrixService:
    """Assemble the PV × Cons strategy matrix for a target date."""

    def __init__(
        self,
        hass: HomeAssistant,
        pv_forecast_service: PvForecastService,
        realized_pv_loader: RealizedPvLoader,
        consumption_loader: ConsumptionProfileLoader,
    ) -> None:
        self._hass = hass
        self._pv_forecast_service = pv_forecast_service
        self._realized_pv_loader = realized_pv_loader
        self._consumption_loader = consumption_loader

    async def async_get_matrix(self, target_date: date) -> dict[str, Any]:
        """Build and return the matrix payload for `target_date`."""
        now = dt_util.now()
        today = now.date()
        if target_date < today:
            return {
                "date": target_date.isoformat(),
                "kind": "past_unsupported",
                "matrix": None,
            }
        is_today = target_date == today
        is_tomorrow = target_date == today + timedelta(days=1)
        forecast = self._pv_forecast_service.forecast

        pv_profiles = self._pv_profiles(forecast, is_today, target_date)
        # Reuse the PvForecast aggregate's cached profiles for today /
        # tomorrow (refreshed by `PvForecastService.refresh_profiles_*`
        # — every minute / bucket-boundary refetches were a waste, the
        # data only changes daily plus today-prev_1-of-tomorrow grows
        # within the PV window). For target_date == D+2 or further, fall
        # back to an on-demand fetch — rare path, no cache.
        if is_today:
            loaded_profiles = forecast.consumption_profiles.today_profiles
        elif is_tomorrow:
            loaded_profiles = forecast.consumption_profiles.tomorrow_profiles
        else:
            loaded_profiles = await self._consumption_loader.fetch_for_anchor(
                target_date, pv_forecast_module.PREV_DAYS_COUNT
            )
        # Surface a loud error when ALL slots are None — likely the
        # workday calendar / recorder hasn't loaded yet (startup race)
        # or has been broken for a while. Custom card renders the error
        # panel instead of a misleading partial matrix.
        if all(p is None for p in loaded_profiles):
            _LOGGER.warning(
                "TargetSocMatrixService: no consumption profiles available "
                "for %s — returning error payload",
                target_date,
            )
            return {
                "date": target_date.isoformat(),
                "kind": "error",
                "error": "consumption_profiles_unavailable",
                "matrix": None,
            }
        cons_profiles, cons_labels, cons_source_dates = self._cons_inputs(
            loaded_profiles
        )
        source_day_pv_sums = await self._source_day_pv_sums(cons_source_dates)
        sch = (
            forecast.start_charge_hour_today
            if is_today
            else forecast.start_charge_hour_tomorrow
        )
        # Now-aware only for today's matrix. Toggle defaults to ON when
        # the input_boolean is missing or in an unknown state — explicit
        # "off" needed to revert to full-window simulation.
        matrix_now: datetime | None = None
        matrix_live_cons_w: float | None = None
        if is_today and self._now_aware_enabled():
            matrix_now = now
            matrix_live_cons_w = forecast.live_consumption_w

        matrix = compute_matrix(
            pv_profiles_by_strategy=pv_profiles,
            cons_profiles_by_strategy=cons_profiles,
            cons_labels=cons_labels,
            source_day_pv_sums=source_day_pv_sums,
            start_charge_hour=sch,
            now=matrix_now,
            live_consumption_w=matrix_live_cons_w,
        )
        _LOGGER.debug(
            "TargetSocMatrixService: %s for %s — %d PV × %d Cons, %d cells",
            "today" if is_today else "tomorrow",
            target_date,
            len(matrix.pv_strategies),
            len(matrix.cons_strategies),
            len(matrix.cells_pct),
        )
        return {
            "date": target_date.isoformat(),
            "kind": "today" if is_today else "tomorrow",
            "matrix": _serialize(matrix, pv_profiles, cons_profiles, cons_source_dates),
        }

    def _now_aware_enabled(self) -> bool:
        """Read the now-aware toggle; default ON when missing/unknown."""
        state = self._hass.states.get(_NOW_AWARE_TOGGLE)
        if state is None or state.state in ("unknown", "unavailable"):
            return True
        return state.state == "on"

    # --- PV inputs --- #

    def _pv_profiles(
        self, forecast: PvForecast, is_today: bool, target_date: date
    ) -> dict[str, PvProfile]:
        """Map each PV strategy key to its 12-bucket `PvProfile`.

        Strategies whose `AdjustedPvForecast` is missing or doesn't cover
        `target_date` are skipped — `to_profile()` raises `ValueError`
        and the strategy simply doesn't appear in the matrix.
        """
        keys = _TODAY_PV_KEYS if is_today else _TOMORROW_PV_KEYS
        out: dict[str, PvProfile] = {}
        for key in keys:
            adjusted = self._pv_source(forecast, key, is_today)
            if adjusted is None or not adjusted.forecast:
                continue
            try:
                out[key] = adjusted.to_profile(target_date)
            except ValueError:
                # Strategy has no periods for target_date (date-picker out of range).
                continue
        return out

    @staticmethod
    def _pv_source(
        forecast: PvForecast, key: str, is_today: bool
    ) -> AdjustedPvForecast | None:
        """Resolve a strategy key → AdjustedPvForecast on the aggregate."""
        if is_today:
            return _TODAY_PV_RESOLVERS[key](forecast)
        return _TOMORROW_PV_RESOLVERS[key](forecast)

    # --- Cons inputs --- #

    def _cons_inputs(
        self, profiles: list[ConsumptionProfile | None]
    ) -> tuple[dict[str, ConsumptionProfile], dict[str, ConsLabel], dict[str, date]]:
        """Build Cons profile map + labels + source-date map (for realized PV lookup)."""
        cons_profiles: dict[str, ConsumptionProfile] = {
            _LIVE_CONS_KEY: ConsumptionProfile.flat()
        }
        cons_labels: dict[str, ConsLabel] = {
            _LIVE_CONS_KEY: ConsLabel(key=_LIVE_CONS_KEY, weekday=None)
        }
        cons_source_dates: dict[str, date] = {}
        for idx, profile in enumerate(profiles):
            if profile is None:
                continue
            key = f"prev_{idx + 1}"
            cons_profiles[key] = profile
            weekday = (
                _WEEKDAY_ABBR[profile.source_date.weekday()]
                if profile.source_date is not None
                else None
            )
            cons_labels[key] = ConsLabel(key=key, weekday=weekday)
            if profile.source_date is not None:
                cons_source_dates[key] = profile.source_date
        return cons_profiles, cons_labels, cons_source_dates

    # --- Source-day realized PV --- #

    async def _source_day_pv_sums(
        self, cons_source_dates: dict[str, date]
    ) -> dict[str, float | None]:
        """For each Cons strategy, fetch the realized PV sum (7-13) that day."""
        out: dict[str, float | None] = {_LIVE_CONS_KEY: None}
        if not cons_source_dates:
            return out
        dates = list(set(cons_source_dates.values()))
        try:
            per_date = await self._realized_pv_loader.fetch_for_dates(dates)
        except Exception:  # noqa: BLE001 — defensive, surface None instead of crashing
            _LOGGER.exception("Failed to fetch realized PV for matrix source days")
            for key in cons_source_dates:
                out[key] = None
            return out
        for key, src in cons_source_dates.items():
            buckets = per_date.get(src, {})
            total = sum(v for (h, _m), v in buckets.items() if 7 <= h < 13)
            out[key] = round(total, 3) if buckets else None
        return out


# --- PV-strategy resolver tables (closed-over forecast → AdjustedPvForecast) --- #


def _live_extrap_adjusted(
    extrap_fn,
) -> callable:
    """Bind a getter from ExtrapolatedLive bundle to its `.adjusted` field."""

    def _resolver(forecast: PvForecast) -> AdjustedPvForecast | None:
        bundle: ExtrapolatedLive = extrap_fn(forecast)
        return bundle.adjusted if bundle is not None else None

    return _resolver


_TODAY_PV_RESOLVERS: dict[str, callable] = {
    "at_6": lambda f: f.adjusted_at_6,
    "live": lambda f: f.adjusted_live,
    "extrap": _live_extrap_adjusted(lambda f: f.extrapolated_live),
    "extrap_5min": _live_extrap_adjusted(lambda f: f.extrapolated_live_5min),
    "extrap_pattern": _live_extrap_adjusted(lambda f: f.extrapolated_live_pattern),
    "extrap_propor": _live_extrap_adjusted(lambda f: f.extrapolated_live_proportional),
    "extrap_band": _live_extrap_adjusted(lambda f: f.extrapolated_live_band),
    "extrap_band_recent": _live_extrap_adjusted(
        lambda f: f.extrapolated_live_band_recent
    ),
}

_TOMORROW_PV_RESOLVERS: dict[str, callable] = {
    "at_6": lambda f: f.adjusted_tomorrow,
    "live": lambda f: f.adjusted_tomorrow_live,
}


# --- Helpers --- #


def _profile_to_buckets_list(
    profile: PvProfile | ConsumptionProfile,
) -> list[float]:
    """Project strict 12-bucket VO → ordered list for serialization."""
    return [profile.get(h, m) for h, m in _BUCKET_TIMES]


def _serialize(
    matrix: TargetSocMatrix,
    pv_profiles: dict[str, PvProfile],
    cons_profiles: dict[str, ConsumptionProfile],
    cons_source_dates: dict[str, date],
) -> dict[str, Any]:
    """Convert dataclass + tuple-keyed dicts → JSON-friendly attribute shape.

    HA attributes must be JSON-serializable; `dict[tuple, ...]` isn't.
    Stringify cell keys as `"<pv_key>|<cons_key>"` so Jinja in markdown
    cards can split on `|` and look up entries directly. Also surfaces
    the raw 30-min bucket lists per strategy so the dashboard chart can
    plot each PV/Cons strategy as a time-series, and the source date
    per Cons-prev strategy (ISO string) so the chart can shift history
    onto the date-picker target day.
    """
    return {
        "pv_strategies": list(matrix.pv_strategies),
        "cons_strategies": [
            {"key": c.key, "weekday": c.weekday} for c in matrix.cons_strategies
        ],
        "cells_pct": {f"{pv}|{cons}": v for (pv, cons), v in matrix.cells_pct.items()},
        "cells_kwh": {f"{pv}|{cons}": v for (pv, cons), v in matrix.cells_kwh.items()},
        "pv_sums_kwh": dict(matrix.pv_sums_kwh),
        "cons_sums_kwh": dict(matrix.cons_sums_kwh),
        "source_day_pv_sums_kwh": dict(matrix.source_day_pv_sums_kwh),
        "pv_buckets_by_strategy": {
            k: [round(v, 4) for v in _profile_to_buckets_list(p)]
            for k, p in pv_profiles.items()
        },
        "cons_buckets_by_strategy": {
            k: [round(v, 4) for v in _profile_to_buckets_list(p)]
            for k, p in cons_profiles.items()
        },
        "cons_source_dates_by_strategy": {
            k: d.isoformat() for k, d in cons_source_dates.items()
        },
    }
