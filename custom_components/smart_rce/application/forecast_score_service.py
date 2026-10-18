"""ForecastScoreService — scores each day's forecast decisions and keeps the results.

Schedule (decision 16.10.2026; runs during the day from 18.10.2026):
- during the day, PRELIMINARY with the data up to then (`DayInputs.cutoff`;
  parts it cannot settle yet wait, `DayScore.pending`), once per trigger:
  the charge start (the clock reaches today's start, by the cheapest hours or
  shifted by the forecast — the morning forecasts against what came so far),
  S0, the battery at 100% (the full-charge estimate), 13:06 (D1 complete).
  A minute tick checks them; triggers due together make one run;
- the button: the same, now (FINAL after 21:06);
- 16:06 PRELIMINARY — today from the recorder; D1 and D2 complete;
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
    has_morning_reserve,
    score_day,
)
from ..domain.forecast_score.production_outcome import (
    ChargeStartFact,
    start_from_soc_rise,
    struck_start,
)
from ..domain.forecast_score.ranking import rank
from ..domain.forecast_score.variant_results import from_archive

if TYPE_CHECKING:
    from collections.abc import Sequence
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
    INTERIM_AT: Final = time(13, 6)
    # The triggers during the day: from the start of the charge block (06:00)
    # until the 16:06 preliminary takes over.
    TRIGGERS_FROM: Final = time(6, 0)
    # The battery is full (the D3 trigger).
    FULL_SOC: Final = 100.0
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
        publish: Callable[[Sequence[DayScore]], None],
    ) -> None:
        self._hass = hass
        self._sources = sources
        self._archive = archive
        self._charge = charge
        self._clock = clock
        self._tz = tz
        self._code_version = code_version
        # Final days out as daily long-term statistics (rewrites a day's row).
        self._publish = publish
        self._cache: dict[date, DayScore | None] = {}
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        # Triggers already run today (read back from today's records after a
        # restart, so a restart does not run them again).
        self._fired: tuple[date, set[str]] | None = None

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    # --- scheduled runs --- #

    async def async_tick(self, _now: datetime | None = None) -> None:
        """Score today once for the triggers that came due since the last run."""
        now = self._clock()
        if not self.TRIGGERS_FROM <= now.time() < self.PRELIMINARY_AT:
            return
        if self._fired is None or self._fired[0] != now.date():
            self._fired = (now.date(), await self._fired_on(now.date()))
        fired = self._fired[1]
        due = [t for t in self._due(now) if t not in fired]
        if not due:
            return
        fired.update(due)
        await self._score_today(
            ScoreStatus.PRELIMINARY, trigger="+".join(due), cutoff=now
        )

    async def _fired_on(self, day: date) -> set[str]:
        """Triggers today's stored runs already answered."""
        try:
            records = await self._hass.async_add_executor_job(
                self._archive.records, day
            )
        except Exception:  # noqa: BLE001 — unreadable: run the triggers again
            _LOGGER.exception("Forecast score: %s unreadable", day)
            return set()
        return {
            trigger
            for record in records
            for trigger in str(record.get("trigger", "")).split("+")
            if trigger
        }

    def _due(self, now: datetime) -> list[str]:
        """Triggers whose moment has come today (in the order of the day)."""
        minute = now.hour * 60 + now.minute
        due: list[str] = []
        start = self._charge.start_charge_hour_override
        if start is not None and minute >= start.hour * 60 + start.minute:
            due.append("charge_start")
        s0 = self._sources.live_s0()
        if s0 is not None and minute >= s0:
            due.append("s0")
        soc = self._sources.battery_soc()
        if soc is not None and soc >= self.FULL_SOC:
            due.append("full_charge")
        if now.time() >= self.INTERIM_AT:
            due.append(self.INTERIM_AT.strftime("%H:%M"))
        return due

    async def async_manual(self) -> None:
        """Score today now (the button): with the data so far, FINAL after 21:06."""
        now = self._clock()
        if now.time() >= self.FINAL_AT:
            await self._score_today(ScoreStatus.FINAL, trigger="manual")
        else:
            await self._score_today(
                ScoreStatus.PRELIMINARY, trigger="manual", cutoff=now
            )

    async def async_preliminary(self, _now: datetime | None = None) -> None:
        await self._score_today(
            ScoreStatus.PRELIMINARY,
            trigger=self.PRELIMINARY_AT.strftime("%H:%M"),
            cutoff=self._clock(),
        )

    async def async_final(self, _now: datetime | None = None) -> None:
        await self._score_today(
            ScoreStatus.FINAL, trigger=self.FINAL_AT.strftime("%H:%M")
        )

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
            await self._score(yesterday, energy, ScoreStatus.FINAL, trigger="check")
        else:
            checked = await self._score(
                yesterday,
                energy,
                ScoreStatus.FINAL,
                trigger="check",
                start=stored.outcome.start,
                spans=self._spans_of(stored),
                write=False,
            )
            if checked is not None and self._degraded(checked, stored):
                _LOGGER.warning(
                    "Forecast score: %s rescored from the archive lost a decision"
                    " (prices or snapshots unavailable?) — the 21:06 final stays",
                    yesterday,
                )
            elif checked is not None:
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
                    await self._score(
                        day, energy, ScoreStatus.FINAL, trigger="catch_up"
                    )
            day += timedelta(days=1)
        stored = await self.async_day(today)
        if now.time() >= self.FINAL_AT:
            if stored is None or stored.status is not ScoreStatus.FINAL:
                await self.async_final()
        elif now.time() >= self.PRELIMINARY_AT and stored is None:
            await self.async_preliminary()
        await self._publish_all(today)
        # Days read back from the archive change what the sensors show even
        # when nothing was rescored.
        self._notify()

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
        """The most recent FINAL day in memory (the "last final" sensors).

        A preliminary result never shows here: it would make the sensors jump
        at 16:06 and back at 21:06.
        """
        finals = [
            s
            for s in self._cache.values()
            if s is not None and s.status is ScoreStatus.FINAL
        ]
        return max(finals, key=lambda s: (s.day, s.computed_at)) if finals else None

    # --- scoring --- #

    async def _score_today(
        self, status: ScoreStatus, *, trigger: str, cutoff: datetime | None = None
    ) -> None:
        today = self._clock().date()
        energy = await self._sources.recorder_energy(today)
        await self._score(today, energy, status, trigger=trigger, cutoff=cutoff)

    async def _score(  # noqa: PLR0913 — the runs differ in these knobs only
        self,
        day: date,
        energy: EnergyDay,
        status: ScoreStatus,
        *,
        trigger: str,
        cutoff: datetime | None = None,
        start: ChargeStartFact | None = None,
        spans: list[tuple[int, int]] | None = None,
        write: bool = True,
    ) -> DayScore | None:
        try:
            inputs = await self._inputs(day, energy, start, spans, cutoff)
            score = await self._hass.async_add_executor_job(
                lambda: score_day(
                    inputs,
                    status=status,
                    computed_at=self._clock(),
                    code_version=self._code_version,
                    trigger=trigger,
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
        cutoff: datetime | None,
    ) -> DayInputs:
        snapshots = await self._sources.snapshots(day)
        scored = await self._hass.async_add_executor_job(from_archive, snapshots)
        actuals = DayActuals(energy, self._tz())
        prices = await self._sources.prices(day)
        if start is not None and start.s0 is not None:
            s0: int | None = start.s0  # the check: S0 as 21:06 saw it
        else:
            s0 = self._sources.s0(day, prices)
        if start is None:
            start = await self._start_fact(day, actuals, s0)
        if spans is None:
            spans = await self._sources.afternoon_spans(day)
        workdays = (
            await self._sources.earlier_workdays(day)
            if has_morning_reserve(day)
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
            morning_plan=await self._sources.morning_plan(day),
            cutoff=cutoff,
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

    async def _publish_all(self, today: date) -> None:
        """Rewrite the daily statistics of every final day (startup)."""
        finals: list[DayScore] = []
        day = self.FIRST_DAY
        while day <= today:
            score = await self.async_day(day)
            if score is not None and score.status is ScoreStatus.FINAL:
                finals.append(score)
            day += timedelta(days=1)
        if finals:
            self._publish(finals)

    @staticmethod
    def _degraded(checked: DayScore, stored: DayScore) -> bool:
        """Tell a rescore that lost a decision the final has — not a correction."""
        return (not checked.start and bool(stored.start)) or (
            not checked.reserve and bool(stored.reserve)
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
        if score.status is ScoreStatus.FINAL:
            self._publish([score])
        _LOGGER.info(
            "Forecast score %s %s (%s): reserve %s pp (PV %s, consumption %s),"
            " start %s h",
            score.day,
            score.status,
            score.trigger,
            score.reserve_error_pp,
            score.reserve_error_pv_pp,
            score.reserve_error_consumption_pp,
            score.start_delta_h,
        )
        self._notify()

    def _notify(self) -> None:
        for update_callback in list(self._listeners.values()):
            update_callback()
