"""TargetSocMatrixService — application service for the target-SOC matrix.

DDD application layer: reads pre-computed cells from `TargetSocCatalog`
aggregate (every `TargetSoc` persistuje `pv_profile` + `cons_view_flat` +
`cons_views_prev` + `flat.dip_kwh`/`prev_days[N].dip_kwh` after each
`recalculate_target_soc`). Matrix service is now a **pure view** —
no `_calculate_target_soc` calls, no profile building, just reads +
serialization for the dashboard payload.

Source-day realized PV is fetched on demand via `RealizedPvLoader`
(orthogonal concern, prev-workday data not owned by catalog). Apples-to-apples
comparison with today's Σ PV per strategy is enforced by routing realized
PV through `PvProfile.from_realized_buckets(...).with_now_override(now, pv_w)`
— same time-shift formula as today's `PvForecastResult.to_profile(now, pv_w)`.

Returns a dict shaped for the smart_rce service response and the
bridging sensor attribute:

```python
{
    "date": "2026-05-13",
    "kind": "today" | "tomorrow" | "past_unsupported" | "out_of_cache" | "error",
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

Past dates surface as `kind: "past_unsupported"` (Iter 2 will add Historical
matrix support via sensor history reads). D+2+ dates surface as
`kind: "out_of_cache"` — catalog only caches today + tomorrow, no on-demand
fetch path for further-future dates.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any, assert_never

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..application.energy_balance_service import EnergyBalanceService
from ..domain.pv_forecast import PvForecast
from ..domain.target_soc import PvProfile, TargetSoc
from ..domain.target_soc_catalog import TargetSocCatalog
from ..domain.target_soc_matrix import ConsLabel
from ..infrastructure.pv_forecast.realized_pv_loader import RealizedPvLoader

_LOGGER = logging.getLogger(__name__)


def _matrix_key(v: PvForecast) -> str:
    """Cross-repo dashboard contract — short stable key per variant.

    Today/tomorrow live in separate resolver dicts (no key collision), so
    AT_6 and TOMORROW_AT_6 deliberately share key 'at_6'. EXTRAP_PROPORTIONAL
    abbreviated to 'extrap_propor' to match `PV_LABELS` lookup in
    `target-soc-matrix-card.js` (cross-repo coupling).

    Exhaustive match — `assert_never` rzuca `AssertionError` przy starcie
    smart_rce dla nowego wariantu w PvForecast bez case tutaj (resolver
    dict comprehensions na module-level wykonują się przy imporcie). Bez
    mypy/CI to wciąż fail-loud at startup; dashboard nie wystartuje aż
    wariant zostanie dodany do tej funkcji.
    """
    match v:
        case PvForecast.AT_6 | PvForecast.TOMORROW_AT_6:
            return "at_6"
        case PvForecast.LIVE | PvForecast.TOMORROW_LIVE:
            return "live"
        case PvForecast.EXTRAP_PATTERN:
            return "extrap_pattern"
        case PvForecast.EXTRAP_PROPORTIONAL:
            return "extrap_propor"
        case PvForecast.EXTRAP_BAND:
            return "extrap_band"
        case PvForecast.EXTRAP_BAND_RECENT:
            return "extrap_band_recent"
        case _:
            assert_never(v)


# PV-strategy resolver tables (matrix key → catalog `PvForecast`). Generated
# from the enum at import-time. Order follows enum declaration (preserved
# by Python 3.7+ dict + classmethod partitions); dashboards iterate this
# order to choose default-on series.
_TODAY_PV_RESOLVERS: dict[str, PvForecast] = {
    _matrix_key(v): v for v in PvForecast.today()
}
_TOMORROW_PV_RESOLVERS: dict[str, PvForecast] = {
    _matrix_key(v): v for v in PvForecast.tomorrow()
}

_LIVE_CONS_KEY: str = "live"

# 12-bucket window (7:00..12:30) — strict PvProfile/ConsumptionProfile contract.
_BUCKET_TIMES: tuple[tuple[int, int], ...] = tuple(
    (7 + idx // 2, (idx % 2) * 30) for idx in range(12)
)

_WEEKDAY_ABBR: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class TargetSocMatrixService:
    """Assemble the PV × Cons strategy matrix for a target date."""

    def __init__(
        self,
        hass: HomeAssistant,
        energy_balance_service: EnergyBalanceService,
        realized_pv_loader: RealizedPvLoader,
    ) -> None:
        self._hass = hass
        self._energy_balance_service = energy_balance_service
        self._realized_pv_loader = realized_pv_loader

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
        if target_date > today + timedelta(days=1):
            return {
                "date": target_date.isoformat(),
                "kind": "out_of_cache",
                "matrix": None,
            }
        is_today = target_date == today
        target_socs_aggregate = self._energy_balance_service.target_socs
        resolvers = _TODAY_PV_RESOLVERS if is_today else _TOMORROW_PV_RESOLVERS
        profiles = (
            target_socs_aggregate.consumption_profiles.today_profiles
            if is_today
            else target_socs_aggregate.consumption_profiles.tomorrow_profiles
        )
        if all(p is None for p in profiles):
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

        cells_pct, cells_kwh, pv_sums_kwh, pv_buckets = self._read_cells(
            resolvers, target_socs_aggregate
        )
        if not cells_pct:
            # No variant has both result + recalc-computed flat — startup race
            # (PV forecast not yet bound, or fail-hard branch tripped on live
            # signals missing). Surface same error as no-cons case.
            _LOGGER.warning(
                "TargetSocMatrixService: no target_soc cells for %s — "
                "returning error payload (catalog not yet populated?)",
                target_date,
            )
            return {
                "date": target_date.isoformat(),
                "kind": "error",
                "error": "catalog_unavailable",
                "matrix": None,
            }

        cons_labels, cons_source_dates, cons_sums_kwh, cons_buckets = self._read_cons(
            resolvers, target_socs_aggregate
        )

        # Apples-to-apples Σ PV source via PvProfile.with_now_override —
        # same time-shift formula as today's PvForecastResult.to_profile.
        # `now` is None outside the 7-13 window (no in-progress override).
        matrix_now = now if is_today and 7 <= now.hour < 13 else None
        live_pv_w = (
            self._energy_balance_service.forecasts.signals.pv_power_w
            if matrix_now
            else None
        )
        source_day_pv_sums = await self._source_day_pv_sums(
            cons_source_dates, now=matrix_now, live_pv_w=live_pv_w
        )

        _LOGGER.debug(
            "TargetSocMatrixService: %s for %s — %d PV × %d Cons, %d cells",
            "today" if is_today else "tomorrow",
            target_date,
            len(pv_sums_kwh),
            len(cons_labels),
            len(cells_pct),
        )
        return {
            "date": target_date.isoformat(),
            "kind": "today" if is_today else "tomorrow",
            "matrix": _serialize(
                pv_strategies=tuple(pv_sums_kwh.keys()),
                cons_strategies=tuple(cons_labels.values()),
                cells_pct=cells_pct,
                cells_kwh=cells_kwh,
                pv_sums_kwh=pv_sums_kwh,
                cons_sums_kwh=cons_sums_kwh,
                source_day_pv_sums_kwh=source_day_pv_sums,
                pv_buckets=pv_buckets,
                cons_buckets=cons_buckets,
                cons_source_dates=cons_source_dates,
            ),
        }

    # --- Catalog reads --- #

    def _read_cells(
        self,
        resolvers: dict[str, PvForecast],
        target_socs_aggregate: TargetSocCatalog,
    ) -> tuple[
        dict[tuple[str, str], int],
        dict[tuple[str, str], float],
        dict[str, float],
        dict[str, list[float]],
    ]:
        """Read every (PV strategy, Cons strategy) cell from catalog.

        Catalog already computed all cells (per-variant TargetSoc holds
        flat + 8 prev_days TargetSocResults plus the pv_profile / cons_views
        that produced them). We just project into matrix dicts.

        Returns:
            cells_pct: {(pv_key, cons_key): target_soc_percent}
            cells_kwh: {(pv_key, cons_key): dip_kwh}
            pv_sums_kwh: {pv_key: sum(pv_profile.buckets) kWh}
            pv_buckets: {pv_key: list[12 floats]} for chart serialization

        """
        cells_pct: dict[tuple[str, str], int] = {}
        cells_kwh: dict[tuple[str, str], float] = {}
        pv_sums_kwh: dict[str, float] = {}
        pv_buckets: dict[str, list[float]] = {}
        for pv_key, variant in resolvers.items():
            entity: TargetSoc | None = target_socs_aggregate.target_socs.get(variant)
            if entity is None or entity.flat is None or entity.pv_profile is None:
                continue  # variant doesn't cover this target_date / not ready
            pv_sums_kwh[pv_key] = round(sum(entity.pv_profile.buckets.values()), 3)
            pv_buckets[pv_key] = [
                round(entity.pv_profile.get(h, m), 4) for h, m in _BUCKET_TIMES
            ]
            # Live cons (flat baseline with optional live override)
            cells_pct[(pv_key, _LIVE_CONS_KEY)] = entity.flat.value
            cells_kwh[(pv_key, _LIVE_CONS_KEY)] = entity.flat.dip_kwh
            # Prev cons N
            for idx, result in enumerate(entity.prev_days):
                if result is None:
                    continue
                key = f"prev_{idx + 1}"
                cells_pct[(pv_key, key)] = result.value
                cells_kwh[(pv_key, key)] = result.dip_kwh
        return cells_pct, cells_kwh, pv_sums_kwh, pv_buckets

    def _read_cons(
        self,
        resolvers: dict[str, PvForecast],
        target_socs_aggregate: TargetSocCatalog,
    ) -> tuple[
        dict[str, ConsLabel],
        dict[str, date],
        dict[str, float],
        dict[str, list[float]],
    ]:
        """Read Cons labels + source dates + cons_views (same for every variant).

        All variants share the same cons profile bundle (today_profiles or
        tomorrow_profiles), so reading from the first ready variant gives
        cons_view_flat + cons_views_prev that drove all cells_pct entries.
        """
        cons_labels: dict[str, ConsLabel] = {
            _LIVE_CONS_KEY: ConsLabel(key=_LIVE_CONS_KEY, weekday=None)
        }
        cons_source_dates: dict[str, date] = {}
        cons_sums_kwh: dict[str, float] = {}
        cons_buckets: dict[str, list[float]] = {}

        # Find the first variant whose recalc populated cons_view_flat.
        first_entity: TargetSoc | None = None
        for variant in resolvers.values():
            entity = target_socs_aggregate.target_socs.get(variant)
            if entity is not None and entity.cons_view_flat is not None:
                first_entity = entity
                break
        if first_entity is None:
            return cons_labels, cons_source_dates, cons_sums_kwh, cons_buckets

        assert first_entity.cons_view_flat is not None
        cons_sums_kwh[_LIVE_CONS_KEY] = round(
            sum(first_entity.cons_view_flat.buckets.values()), 3
        )
        cons_buckets[_LIVE_CONS_KEY] = [
            round(first_entity.cons_view_flat.get(h, m), 4) for h, m in _BUCKET_TIMES
        ]

        for idx, cv in enumerate(first_entity.cons_views_prev):
            if cv is None:
                continue
            key = f"prev_{idx + 1}"
            cons_sums_kwh[key] = round(sum(cv.buckets.values()), 3)
            cons_buckets[key] = [round(cv.get(h, m), 4) for h, m in _BUCKET_TIMES]
            weekday = (
                _WEEKDAY_ABBR[cv.source_date.weekday()]
                if cv.source_date is not None
                else None
            )
            cons_labels[key] = ConsLabel(key=key, weekday=weekday)
            if cv.source_date is not None:
                cons_source_dates[key] = cv.source_date

        return cons_labels, cons_source_dates, cons_sums_kwh, cons_buckets

    # --- Source-day realized PV --- #

    async def _source_day_pv_sums(
        self,
        cons_source_dates: dict[str, date],
        *,
        now: datetime | None,
        live_pv_w: float | None,
    ) -> dict[str, float | None]:
        """Realized PV sum per Cons-prev source day, time-shifted symmetric to today.

        Routes realized 30-min bucket totals through
        `PvProfile.from_realized_buckets(realized).with_now_override(now, pv_w)`
        → past=0, in-progress=pv_w×remaining, future=raw bucket value. Same
        formula as today's `PvForecastResult.to_profile(now, pv_w)` for cells
        and `Σ PV per strategy` — automatic apples-to-apples comparison with
        today's column when `now_in_window`.
        """
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
            realized = per_date.get(src, {})
            if not realized:
                out[key] = None
                continue
            profile = PvProfile.from_realized_buckets(realized).with_now_override(
                now=now, pv_power_w_5min=live_pv_w
            )
            out[key] = round(sum(profile.buckets.values()), 3)
        return out


# --- Helpers --- #


def _serialize(
    *,
    pv_strategies: tuple[str, ...],
    cons_strategies: tuple[ConsLabel, ...],
    cells_pct: dict[tuple[str, str], int],
    cells_kwh: dict[tuple[str, str], float],
    pv_sums_kwh: dict[str, float],
    cons_sums_kwh: dict[str, float],
    source_day_pv_sums_kwh: dict[str, float | None],
    pv_buckets: dict[str, list[float]],
    cons_buckets: dict[str, list[float]],
    cons_source_dates: dict[str, date],
) -> dict[str, Any]:
    """Convert dataclass + tuple-keyed dicts → JSON-friendly attribute shape.

    HA attributes must be JSON-serializable; `dict[tuple, ...]` isn't.
    Stringify cell keys as `"<pv_key>|<cons_key>"` so Jinja in markdown
    cards can split on `|` and look up entries directly.
    """
    return {
        "pv_strategies": list(pv_strategies),
        "cons_strategies": [
            {"key": c.key, "weekday": c.weekday} for c in cons_strategies
        ],
        "cells_pct": {f"{pv}|{cons}": v for (pv, cons), v in cells_pct.items()},
        "cells_kwh": {f"{pv}|{cons}": v for (pv, cons), v in cells_kwh.items()},
        "pv_sums_kwh": dict(pv_sums_kwh),
        "cons_sums_kwh": dict(cons_sums_kwh),
        "source_day_pv_sums_kwh": dict(source_day_pv_sums_kwh),
        "pv_buckets_by_strategy": dict(pv_buckets),
        "cons_buckets_by_strategy": dict(cons_buckets),
        "cons_source_dates_by_strategy": {
            k: d.isoformat() for k, d in cons_source_dates.items()
        },
    }
