"""PV surplus — day types, per-hour median, window sums, service day/window."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.smart_rce.application.pv_surplus_service import PvSurplusService
from custom_components.smart_rce.domain.pv_forecast import PvForecast
from custom_components.smart_rce.domain.pv_surplus import (
    DayType,
    SurplusProfile,
    median_by_hour,
    surplus_window_end,
)
import pytest

TZ = timezone.utc


class TestDayType:
    def test_kinds(self):
        assert DayType.of(date(2026, 10, 12)) is DayType.WORKDAY  # Monday
        assert DayType.of(date(2026, 10, 10)) is DayType.SATURDAY
        assert DayType.of(date(2026, 10, 11)) is DayType.SUNDAY

    def test_weekday_holiday_counts_as_sunday(self):
        assert DayType.of(date(2026, 11, 11)) is DayType.SUNDAY  # Wednesday

    def test_workday_history_skips_weekends_and_holidays(self):
        days = DayType.WORKDAY.previous_days(date(2026, 11, 12))
        assert len(days) == 10
        assert date(2026, 11, 11) not in days  # holiday
        assert all(d.weekday() < 5 for d in days)

    def test_weekend_history_is_five_of_the_same(self):
        sats = DayType.SATURDAY.previous_days(date(2026, 10, 17))
        assert sats == [date(2026, 10, 10) - timedelta(days=7 * i) for i in range(5)]

    def test_sunday_pool_includes_weekday_holidays(self):
        days = DayType.SUNDAY.previous_days(date(2026, 11, 15))
        assert date(2026, 11, 11) in days
        assert len(days) == 5


def test_median_is_per_hour():
    profiles = [{9: 1.0, 10: 0.2}, {9: 3.0, 10: 0.4}, {9: 2.0, 10: 9.0}]
    assert median_by_hour(profiles) == {9: 2.0, 10: 0.4}


def test_window_end_is_t2_start_by_season():
    assert surplus_window_end(date(2026, 10, 12)) == 16
    assert surplus_window_end(date(2026, 7, 12)) == 19


class TestSurplusProfile:
    def test_surplus_never_negative_and_sums_window(self):
        p = SurplusProfile.build(
            date(2026, 10, 12), {10: 0.5, 11: 2.4, 12: 3.0}, {10: 0.9, 11: 0.6, 12: 1.0}
        )
        assert p.total(10, 13) == pytest.approx(1.8 + 2.0)
        assert p.total(12, 13) == pytest.approx(2.0)

    def test_none_consumption_is_flat(self):
        p = SurplusProfile.build(date(2026, 10, 12), {11: 2.4}, None)
        assert p.total(11, 12) == pytest.approx(1.5)


class _FakeForecasts:
    def __init__(self, by_variant):
        self._by = by_variant

    def get(self, variant):
        return self._by.get(variant)


def _result(day: str, rates_by_hour: dict[int, float]):
    periods = [
        SimpleNamespace(
            period_start=f"{day} {h:02d}:{m:02d}:00+02:00", pv_estimate_adjusted=rate
        )
        for h, rate in rates_by_hour.items()
        for m in (0, 30)
    ]
    return SimpleNamespace(forecast=periods)


class _Loader:
    def __init__(self, profiles):
        self.profiles = profiles

    async def consumption_by_day(self, days):
        return dict.fromkeys(days, self.profiles)

    async def today_by_hour(self, today):
        return {11: 3.0}, {11: 1.0}


def _service(now: datetime, start_today=11, start_tomorrow=10):
    eb = SimpleNamespace(
        forecasts=_FakeForecasts(
            {
                PvForecast.LIVE: _result("2026-10-12", {11: 4.0, 12: 2.0, 15: 1.0}),
                PvForecast.TOMORROW_LIVE: _result("2026-10-13", {10: 3.0}),
            }
        ),
        target_socs=SimpleNamespace(
            inputs=SimpleNamespace(
                start_charge_hour_today=start_today,
                start_charge_hour_tomorrow=start_tomorrow,
            )
        ),
    )
    return PvSurplusService(
        eb, _Loader(dict.fromkeys(range(24), 0.5)), clock=lambda: now
    )


class TestService:
    async def test_today_until_t2(self):
        svc = _service(datetime(2026, 10, 12, 9, 0, tzinfo=TZ))
        await svc.async_refresh()
        f = svc.forecast
        assert f is not None
        assert (f.day, f.source, f.start_hour, f.end_hour) == (
            date(2026, 10, 12),
            "live",
            11,
            16,
        )
        # Hours 11/12/15 carry 4.0/2.0/1.0 kWh; median use 0.5 kWh/h.
        assert f.total(f.prev) == pytest.approx(3.5 + 1.5 + 0.5)
        assert f.total(f.flat) == pytest.approx(3.1 + 1.1 + 0.1)
        assert f.matrix["cells_surplus_kwh"]["live|median"] == pytest.approx(5.5)
        assert len(f.prev_days) == 10

    async def test_tomorrow_from_t2(self):
        svc = _service(datetime(2026, 10, 12, 16, 0, tzinfo=TZ))
        await svc.async_refresh()
        f = svc.forecast
        assert f is not None
        assert (f.day, f.source, f.start_hour) == (
            date(2026, 10, 13),
            "tomorrow_live",
            10,
        )

    async def test_no_start_hour_means_no_total(self):
        svc = _service(datetime(2026, 10, 12, 9, 0, tzinfo=TZ), start_today=None)
        await svc.async_refresh()
        assert (
            svc.forecast is not None and svc.forecast.total(svc.forecast.prev) is None
        )

    async def test_realized_counts_measured_hours_only(self):
        svc = _service(datetime(2026, 10, 12, 11, 40, tzinfo=TZ))
        await svc.async_refresh()
        assert svc.realized is not None and svc.realized.total == pytest.approx(2.0)
