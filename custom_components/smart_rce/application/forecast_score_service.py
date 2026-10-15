"""ForecastScoreService — scores each day's forecast decisions and keeps the results.

Schedule (decision 16.10.2026):
- 16:06 PRELIMINARY — today from the recorder; all of D1 is final by then;
- 21:06 FINAL — today from the recorder; nothing that matters moves later;
- 00:20 CHECK — yesterday again, from the energy archive (00:15): the same
  result is expected; a different one (a gap in the statistics) replaces the
  final and the difference is logged. The check reuses the start and the
  afternoon spans 21:06 stored — by midnight the live state has moved on.
- at startup: catch up every day since the archive began without a final
  (and today's runs already due), so a restart at 21:06 loses nothing.

Rankings read final scores only, over the 30 days ending at the asked day.
Scoring is CPU work on pure domain code — it runs in the executor.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import CALLBACK_TYPE

from ..domain.forecast_score.day_actuals import DayActuals
from ..domain.forecast_score.day_score import (
    DayInputs,
    DayScore,
    ScoreStatus,
    score_day,
)
from ..domain.forecast_score.production_outcome import (
    ChargeStartFact,
    start_from_soc_rise,
    struck_start,
)
from ..domain.forecast_score.ranking import rank
from ..domain.forecast_score.variant_results import from_archive
from ..domain.pv_surplus import DayType

if TYPE_CHECKING:
    from datetime import tzinfo

    from homeassistant.core import HomeAssistant

    from ..domain.energy_day import EnergyDay
    from ..infrastructure.forecast_score_archive import ForecastScoreArchive
    from ..infrastructure.forecast_score_sources import ForecastScoreSources
    from .battery_charge_service import BatteryChargeService

_LOGGER = logging.getLogger(__name__)


class ForecastScoreService:
    """Application service: when to score, from what, and where it goes."""

    PRELIMINARY_AT: Final = time(16, 6)
    FINAL_AT: Final = time(21, 6)
    CHECK_AT: Final = time(0, 20)
    # First day with both archives (snapshots from 03.10 04:15, energy from 03.10).
    FIRST_DAY: Final = date(2026, 10, 4)
    RANKING_DAYS: Final = 30

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        sources: ForecastScoreSources,
        archive: ForecastScoreArchive,
        charge: BatteryChargeService,
        clock: Callable[[], datetime],
        tz: Callable[[], tzinfo],
        code_version: str,
    ) -> None:
        self._hass = hass
        self._sources = sources
        self._archive = archive
        self._charge = charge
        self._clock = clock
        self._tz = tz
        self._code_version = code_version
        self._cache: dict[date, DayScore | None] = {}
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    # --- scheduled runs --- #

    async def async_preliminary(self, _now: datetime | None = None) -> None:
        await self._score_today(ScoreStatus.PRELIMINARY)

    async def async_final(self, _now: datetime | None = None) -> None:
        await self._score_today(ScoreStatus.FINAL)

    async def async_check(self, _now: datetime | None = None) -> None:
        """Rescore yesterday from the energy archive; replace the final if it moved."""
        today = self._clock().date()
        yesterday = today - timedelta(days=1)
        energy = await self._sources.archived_energy(yesterday)
        if energy is None:
            _LOGGER.warning("Forecast score: %s not in the energy archive", yesterday)
            return
        stored = await self.async_day(yesterday)
        if stored is None or stored.status is not ScoreStatus.FINAL:
            await self._score(yesterday, energy, ScoreStatus.FINAL)
        else:
            checked = await self._score(
                yesterday,
                energy,
                ScoreStatus.FINAL,
                start=stored.outcome.start,
                spans=self._spans_of(stored),
                write=False,
            )
            if checked is not None:
                moved = checked.differences(stored)
                if moved:
                    _LOGGER.warning(
                        "Forecast score: %s rescored from the archive differs in %s",
                        yesterday,
                        moved,
                    )
                    replacing = DayScore.from_dict(
                        {**checked.to_dict(), "replaced": list(moved)}
                    )
                    await self._store(replacing)
        await self._hass.async_add_executor_job(self._archive.close_days_before, today)

    async def async_catch_up(self, _now: datetime | None = None) -> None:
        """Score every past day without a final, and today's runs already due."""
        now = self._clock()
        today = now.date()
        day = self.FIRST_DAY
        while day < today:
            stored = await self.async_day(day)
            if stored is None or stored.status is not ScoreStatus.FINAL:
                energy = await self._sources.archived_energy(day)
                if energy is None and (today - day).days <= 1:
                    energy = await self._sources.recorder_energy(day)
                if energy is not None:
                    await self._score(day, energy, ScoreStatus.FINAL)
            day += timedelta(days=1)
        stored = await self.async_day(today)
        if now.time() >= self.FINAL_AT:
            if stored is None or stored.status is not ScoreStatus.FINAL:
                await self.async_final()
        elif now.time() >= self.PRELIMINARY_AT and stored is None:
            await self.async_preliminary()

    # --- reads (sensors) --- #

    async def async_day(self, day: date) -> DayScore | None:
        """Return the day's latest score (the last final, else the preliminary)."""
        if day not in self._cache:
            try:
                records = await self._hass.async_add_executor_job(
                    self._archive.records, day
                )
                self._cache[day] = DayScore.latest_of(records)
            except Exception:  # noqa: BLE001 — an unreadable day is a missing day
                _LOGGER.exception("Forecast score: %s unreadable", day)
                self._cache[day] = None
        return self._cache[day]

    async def async_ranking(self, end: date) -> dict[str, Any]:
        """Ranking tables of the final days among the RANKING_DAYS ending at `end`."""
        finals: list[DayScore] = []
        for back in range(self.RANKING_DAYS):
            score = await self.async_day(end - timedelta(days=back))
            if score is not None and score.status is ScoreStatus.FINAL:
                finals.append(score)
        return await self._hass.async_add_executor_job(rank, finals)

    @property
    def latest(self) -> DayScore | None:
        """The most recent scored day in memory (headline sensors)."""
        scored = [s for s in self._cache.values() if s is not None]
        return max(scored, key=lambda s: (s.day, s.computed_at)) if scored else None

    # --- scoring --- #

    async def _score_today(self, status: ScoreStatus) -> None:
        today = self._clock().date()
        energy = await self._sources.recorder_energy(today)
        await self._score(today, energy, status)

    async def _score(
        self,
        day: date,
        energy: EnergyDay,
        status: ScoreStatus,
        *,
        start: ChargeStartFact | None = None,
        spans: list[tuple[int, int]] | None = None,
        write: bool = True,
    ) -> DayScore | None:
        try:
            inputs = await self._inputs(day, energy, start, spans)
            score = await self._hass.async_add_executor_job(
                lambda: score_day(
                    inputs,
                    status=status,
                    computed_at=self._clock(),
                    code_version=self._code_version,
                )
            )
        except Exception:  # noqa: BLE001 — a failed day must not stop the others
            _LOGGER.exception("Forecast score: scoring %s failed", day)
            return None
        if write:
            await self._store(score)
        return score

    async def _inputs(
        self,
        day: date,
        energy: EnergyDay,
        start: ChargeStartFact | None,
        spans: list[tuple[int, int]] | None,
    ) -> DayInputs:
        snapshots = await self._sources.snapshots(day)
        scored = await self._hass.async_add_executor_job(from_archive, snapshots)
        actuals = DayActuals(energy, self._tz())
        prices = await self._sources.prices(day)
        s0 = self._sources.s0(day, prices)
        if start is None:
            start = await self._start_fact(day, actuals, s0)
        if spans is None:
            spans = await self._sources.afternoon_spans(day)
        workdays = (
            await self._sources.earlier_workdays(day)
            if DayType.of(day) is DayType.WORKDAY
            else {}
        )
        return DayInputs(
            actuals=actuals,
            snapshots=scored,
            earlier_workdays=workdays,
            earlier_same_type=await self._sources.earlier_same_type(day),
            prices=prices,
            s0=s0,
            start=start,
            afternoon_charge=spans,
        )

    async def _start_fact(
        self, day: date, actuals: DayActuals, s0: int | None
    ) -> ChargeStartFact:
        """Find the start that struck: start entity history, else the SoC rise."""
        shift = self._charge.forecast_shift
        today = shift is not None and shift.day == day
        locked = shift.locked if shift is not None and today else None
        manual = self._charge.start_charge_is_manual_today if today else None
        changes = await self._sources.start_changes(day)
        struck = struck_start(changes) if changes else None
        if struck is not None:
            return ChargeStartFact(struck, "override", s0, locked, manual)
        rise = start_from_soc_rise(actuals)
        return ChargeStartFact(
            rise, "soc_rise" if rise is not None else "none", s0, locked, manual
        )

    @staticmethod
    def _spans_of(score: DayScore) -> list[tuple[int, int]] | None:
        if score.outcome.topups is None:
            return None
        return [(t.first, t.end) for t in score.outcome.topups]

    async def _store(self, score: DayScore) -> None:
        try:
            await self._hass.async_add_executor_job(
                self._archive.add, score.day, score.to_dict()
            )
        except OSError:
            _LOGGER.exception("Forecast score: writing %s failed", score.day)
            return
        self._cache[score.day] = score
        _LOGGER.info(
            "Forecast score %s %s: reserve %s pp (PV %s, consumption %s), start %s h",
            score.day,
            score.status,
            score.reserve_error_pp,
            score.reserve_error_pv_pp,
            score.reserve_error_consumption_pp,
            score.start_delta_h,
        )
        for update_callback in list(self._listeners.values()):
            update_callback()
