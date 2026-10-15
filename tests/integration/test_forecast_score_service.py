"""ForecastScoreService on a real event loop: final, check, catch-up, archive."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.smart_rce.application.forecast_score_service import (
    ForecastScoreService,
)
from custom_components.smart_rce.domain.energy_day import EnergyDay
from custom_components.smart_rce.domain.forecast_score.day_score import (
    DayScore,
    ScoreStatus,
)
from custom_components.smart_rce.domain.pv_forecast.forecast_snapshot import (
    ForecastSnapshot,
)
from custom_components.smart_rce.infrastructure.daily_jsonl_archive import (
    DailyJsonlArchive,
)
from custom_components.smart_rce.infrastructure.forecast_score_archive import (
    ForecastScoreArchive,
)

from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Warsaw")
FIXTURE = Path(__file__).parents[1] / "fixtures" / "forecast_score"
DAY = date(2026, 10, 14)
CONSUMPTION = "consumption_minus_water_kwh"


class _Sources:
    """Fixture archive instead of the recorder, the PSE API and live state."""

    def __init__(self) -> None:
        self._forecast = DailyJsonlArchive(FIXTURE / "forecast")
        self._energy = DailyJsonlArchive(FIXTURE / "energy")
        self._prices = json.loads((FIXTURE / "rce_prices.json").read_text())
        self.recorder_pv_scale = 1.0
        self.prices_available = True

    async def snapshots(self, day: date) -> list[ForecastSnapshot]:
        return [ForecastSnapshot.from_dict(r) for r in self._forecast.read_day(day)]

    async def archived_energy(self, day: date) -> EnergyDay | None:
        return EnergyDay.from_records(self._energy.read_day(day))

    async def recorder_energy(self, day: date) -> EnergyDay:
        energy = EnergyDay.from_records(self._energy.read_day(day))
        assert energy is not None
        series = {
            **energy.series,
            "pv_kwh": [
                None if v is None else v * self.recorder_pv_scale
                for v in energy.series["pv_kwh"]
            ],
        }
        return EnergyDay(energy.day, energy.start, series, energy.source)

    async def earlier_workdays(self, day: date) -> dict[date, dict[int, float]]:
        return self._earlier(day)

    async def earlier_same_type(self, day: date) -> dict[date, dict[int, float]]:
        return self._earlier(day)

    def _earlier(self, day: date) -> dict[date, dict[int, float]]:
        out = {}
        for back in range(1, 15):
            d = day - timedelta(days=back)
            energy = EnergyDay.from_records(self._energy.read_day(d))
            if d.weekday() < 5 and energy is not None:
                out[d] = energy.energy_by_half_hour(CONSUMPTION, TZ)
        return out

    async def prices(self, day: date) -> tuple[float, ...] | None:
        if not self.prices_available:
            return None
        found = self._prices.get(day.isoformat())
        return tuple(found) if found else None

    def s0(self, _day: date, prices: tuple[float, ...] | None) -> int | None:
        return 660 if prices else None

    async def start_changes(self, _day: date) -> list[tuple[int, int]] | None:
        return [(0, 540), (1, 660), (480, 495)]

    async def afternoon_spans(self, _day: date) -> list[tuple[int, int]] | None:
        return [(14 * 60, 15 * 60)]


class _Charge:
    forecast_shift = None
    start_charge_is_manual_today = False


PUBLISHED: list[DayScore] = []


def _service(
    hass: HomeAssistant, tmp_path: Path, now: datetime, sources: Any
) -> tuple[ForecastScoreService, ForecastScoreArchive]:
    PUBLISHED.clear()
    archive = ForecastScoreArchive(DailyJsonlArchive(tmp_path))
    service = ForecastScoreService(
        hass,
        sources=sources,
        archive=archive,
        charge=_Charge(),  # type: ignore[arg-type]
        clock=lambda: now,
        tz=lambda: TZ,
        code_version="test",
        publish=PUBLISHED.extend,
    )
    service.FIRST_DAY = DAY  # type: ignore[misc]
    return service, archive


async def test_final_then_check_keeps_an_unchanged_day(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    sources = _Sources()
    service, archive = _service(
        hass, tmp_path, datetime(2026, 10, 14, 21, 6, tzinfo=TZ), sources
    )
    await service.async_final()
    score = await service.async_day(DAY)
    assert score is not None
    assert score.status is ScoreStatus.FINAL
    assert score.outcome.start.start == 495
    assert score.outcome.start.source == "override"
    assert service.latest == score
    assert [score] == PUBLISHED  # the final went out as a daily statistic

    service._clock = lambda: datetime(2026, 10, 15, 0, 20, tzinfo=TZ)  # noqa: SLF001
    await service.async_check()
    records = archive.records(DAY)
    assert len(records) == 1  # the check found nothing to replace
    assert (tmp_path / "2026" / "2026-10-14.jsonl.gz").exists()


async def test_check_replaces_a_final_that_moved(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    sources = _Sources()
    sources.recorder_pv_scale = 0.5  # the 21:06 statistics were short
    service, archive = _service(
        hass, tmp_path, datetime(2026, 10, 14, 21, 6, tzinfo=TZ), sources
    )
    await service.async_final()
    service._clock = lambda: datetime(2026, 10, 15, 0, 20, tzinfo=TZ)  # noqa: SLF001
    service._cache.clear()  # noqa: SLF001
    await service.async_check()
    records = archive.records(DAY)
    assert len(records) == 2
    latest = await service.async_day(DAY)
    assert latest is not None
    assert latest.replaced
    # The check reused the start 21:06 stored.
    assert latest.outcome.start.start == 495


async def test_catch_up_scores_missed_days_and_rankings_read_finals(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    service, _archive = _service(
        hass, tmp_path, datetime(2026, 10, 15, 10, 0, tzinfo=TZ), _Sources()
    )
    await service.async_catch_up()
    score = await service.async_day(DAY)
    assert score is not None
    assert score.status is ScoreStatus.FINAL
    ranking = await service.async_ranking(date(2026, 10, 15))
    assert ranking["days"] == [DAY.isoformat()]


async def test_check_without_prices_keeps_the_final(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    sources = _Sources()
    sources.recorder_pv_scale = 0.5  # would differ — but the check is degraded
    service, archive = _service(
        hass, tmp_path, datetime(2026, 10, 14, 21, 6, tzinfo=TZ), sources
    )
    await service.async_final()
    stored = await service.async_day(DAY)
    assert stored is not None
    assert stored.start
    sources.prices_available = False  # the PSE API fails at 00:20
    service._clock = lambda: datetime(2026, 10, 15, 0, 20, tzinfo=TZ)  # noqa: SLF001
    await service.async_check()
    assert len(archive.records(DAY)) == 1
    assert await service.async_day(DAY) == stored


async def test_preliminary_is_neither_latest_nor_published(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    service, _archive = _service(
        hass, tmp_path, datetime(2026, 10, 14, 16, 6, tzinfo=TZ), _Sources()
    )
    await service.async_preliminary()
    score = await service.async_day(DAY)
    assert score is not None
    assert score.status is ScoreStatus.PRELIMINARY
    assert service.latest is None
    assert PUBLISHED == []
