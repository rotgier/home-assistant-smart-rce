"""TargetSocMatrixService — application service for the target-SOC matrix.

DDD application layer: pulls PV strategy buckets from the TargetSocCatalog
aggregate (today: 6 strategies, tomorrow: 2 — fewer because extrapolated
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
from ..domain.consumption_profiles import PREV_DAYS_COUNT, ConsumptionProfile
from ..domain.pv_forecast import PvForecast, PvForecasts
from ..domain.target_soc import PvProfile
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
        target_socs = self._pv_forecast_service.target_socs
        updater = self._pv_forecast_service.updater

        # Now-aware only for today's matrix. Toggle defaults to ON when
        # the input_boolean is missing or in an unknown state — explicit
        # "off" needed to revert to full-window simulation. Both live
        # signals must be available too (fail-hard contract on to_view /
        # to_profile when `now` is given).
        live_cons_w_raw = target_socs.inputs.live_consumption_w
        live_pv_w_raw = updater.signals.pv_power_w
        now_aware = (
            is_today
            and self._now_aware_enabled()
            and live_cons_w_raw is not None
            and live_pv_w_raw is not None
        )
        matrix_now: datetime | None = now if now_aware else None
        live_cons_w = live_cons_w_raw if now_aware else None
        live_pv_w = live_pv_w_raw if now_aware else None

        pv_profiles = self._pv_profiles(
            updater,
            is_today=is_today,
            target_date=target_date,
            now=matrix_now,
            live_pv_power_w=live_pv_w,
        )
        # Reuse the TargetSocCatalog aggregate's cached profiles for today /
        # tomorrow (refreshed by `PvForecastService.refresh_profiles_*`
        # — every minute / bucket-boundary refetches were a waste, the
        # data only changes daily plus today-prev_1-of-tomorrow grows
        # within the PV window). For target_date == D+2 or further, fall
        # back to an on-demand fetch — rare path, no cache.
        if is_today:
            loaded_profiles = target_socs.consumption_profiles.today_profiles
        elif is_tomorrow:
            loaded_profiles = target_socs.consumption_profiles.tomorrow_profiles
        else:
            loaded_profiles = await self._consumption_loader.fetch_for_anchor(
                target_date, PREV_DAYS_COUNT
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
            loaded_profiles, now=matrix_now, live_consumption_w=live_cons_w
        )
        source_day_pv_sums = await self._source_day_pv_sums(cons_source_dates)
        sch = (
            target_socs.inputs.start_charge_hour_today
            if is_today
            else target_socs.inputs.start_charge_hour_tomorrow
        )

        matrix = compute_matrix(
            pv_profiles_by_strategy=pv_profiles,
            cons_profiles_by_strategy=cons_profiles,
            cons_labels=cons_labels,
            source_day_pv_sums=source_day_pv_sums,
            start_charge_hour=sch,
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
        self,
        updater: PvForecasts,
        *,
        is_today: bool,
        target_date: date,
        now: datetime | None,
        live_pv_power_w: float | None,
    ) -> dict[str, PvProfile]:
        """Map each PV strategy key to its 12-bucket `PvProfile`.

        For today + now-aware mode, profiles are built with `now` and
        `live_pv_power_w` so the in-progress bucket carries live remaining
        kWh (matrix cells then match the bridging sensors). For tomorrow
        or full-window today, plain forecast snapshots — `now=None`.

        Strategies whose `PvForecastResult` is missing or doesn't cover
        `target_date` are skipped — `to_profile()` raises `ValueError`
        and the strategy simply doesn't appear in the matrix.
        """
        keys = _TODAY_PV_KEYS if is_today else _TOMORROW_PV_KEYS
        out: dict[str, PvProfile] = {}
        resolver_map = _TODAY_PV_RESOLVERS if is_today else _TOMORROW_PV_RESOLVERS
        for key in keys:
            adjusted = updater.get(resolver_map[key])
            if adjusted is None or not adjusted.forecast:
                continue
            try:
                out[key] = adjusted.to_profile(
                    target_date, now=now, pv_power_w_5min=live_pv_power_w
                )
            except ValueError:
                # Strategy has no periods for target_date (date-picker out of range).
                continue
        return out

    # --- Cons inputs --- #

    def _cons_inputs(
        self,
        profiles: list[ConsumptionProfile | None],
        *,
        now: datetime | None,
        live_consumption_w: float | None,
    ) -> tuple[dict[str, ConsumptionProfile], dict[str, ConsLabel], dict[str, date]]:
        """Build Cons profile map + labels + source-date map (for realized PV lookup).

        Each profile is passed through `to_view(now, live_consumption_w)`
        — for today+now-aware that bakes the live in-progress integration
        into the bucket values; for tomorrow / non-today (`now=None`) it
        returns the profile unchanged (back-compat).
        """
        flat = ConsumptionProfile.flat()
        cons_profiles: dict[str, ConsumptionProfile] = {
            _LIVE_CONS_KEY: flat.to_view(now=now, live_consumption_w=live_consumption_w)
        }
        cons_labels: dict[str, ConsLabel] = {
            _LIVE_CONS_KEY: ConsLabel(key=_LIVE_CONS_KEY, weekday=None)
        }
        cons_source_dates: dict[str, date] = {}
        for idx, profile in enumerate(profiles):
            if profile is None:
                continue
            key = f"prev_{idx + 1}"
            cons_profiles[key] = profile.to_view(
                now=now, live_consumption_w=live_consumption_w
            )
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


# --- PV-strategy resolver tables (matrix key → catalog PvForecast) --- #


_TODAY_PV_RESOLVERS: dict[str, PvForecast] = {
    "at_6": PvForecast.AT_6,
    "live": PvForecast.LIVE,
    "extrap_pattern": PvForecast.EXTRAP_PATTERN,
    "extrap_propor": PvForecast.EXTRAP_PROPORTIONAL,
    "extrap_band": PvForecast.EXTRAP_BAND,
    "extrap_band_recent": PvForecast.EXTRAP_BAND_RECENT,
}

_TOMORROW_PV_RESOLVERS: dict[str, PvForecast] = {
    "at_6": PvForecast.TOMORROW_AT_6,
    "live": PvForecast.TOMORROW_LIVE,
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
