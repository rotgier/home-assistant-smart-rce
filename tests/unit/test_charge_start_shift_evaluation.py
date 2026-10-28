"""ChargeStartShiftService — when it evaluates, what it applies, what it reports."""

from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.smart_rce.application.battery_charge_service import (
    BatteryChargeService,
    ForecastShiftMove,
)
from custom_components.smart_rce.application.charge_start_shift_service import (
    ChargeStartShiftService,
    RewindReport,
    ShiftReport,
)
from custom_components.smart_rce.domain.battery_charge_policy import BatteryChargePolicy
from custom_components.smart_rce.domain.charge_start_shift import ShiftVariant
from custom_components.smart_rce.domain.pv_surplus import (
    SurplusProfile,
    slots_from_hourly,
)
from custom_components.smart_rce.infrastructure.charge_start_shift_notifier import (
    render,
    render_rewind,
)
import pytest

DAY = date(2026, 10, 13)
H = 60
PRICES = (
    500, 500, 500, 500, 500, 500, 450, 420, 400, 380, 300, 200,
    100, 100, 200, 400, 600, 700, 700, 650, 600, 550, 500, 500,
)  # fmt: skip


class _Repo:
    def __init__(self) -> None:
        self.policy = BatteryChargePolicy()

    async def persist(self) -> None:
        pass

    def save_if_changed(self) -> None:
        pass


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 13, 7, 36, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def at(self, hour: int, minute: int) -> None:
        self.now = datetime(2026, 10, 13, hour, minute, tzinfo=timezone.utc)


class _Surplus:
    """Same LIVE surplus for every variant: 4 kWh/h from 8 to 16, flat use."""

    def today_profile(
        self, *, history: bool, pv_scale: float, source: object = None
    ) -> SurplusProfile:
        pv = slots_from_hourly(dict.fromkeys(range(8, 16), 4.0))
        return SurplusProfile.build(DAY, pv, None)


def _setup(soc: float = 20.0, s0: time = time(12, 0)):
    clock = _Clock()
    charge = BatteryChargeService(repo=_Repo(), clock=clock, actuator=MagicMock())
    charge.refresh_start_charge(s0, clock.now)
    ems = SimpleNamespace(
        battery_charge_service=charge,
        charge_slots=SimpleNamespace(today_start=s0),
        last_input_state=SimpleNamespace(battery_soc=soc),
        rce_prices=SimpleNamespace(
            rce_prices=SimpleNamespace(
                today=SimpleNamespace(day=DAY, hour_price=PRICES)
            )
        ),
    )
    reports: list[ShiftReport] = []
    alerts: list[RewindReport] = []
    service = ChargeStartShiftService(
        ems, _Surplus(), reports.append, alerts.append, clock
    )
    service.alerts = alerts  # test handle
    return service, charge, clock, reports


def test_nothing_before_0741():
    service, _, _, _ = _setup()
    service.evaluate()
    assert service.last is None


def _run(service, clock, hour, minutes):
    for minute in minutes:
        clock.at(hour, minute)
        service.evaluate()


def test_waits_for_extrap_then_moves_and_reports_why():
    # SoC 20% -> 8.77 kWh to full. 12:00 window (12-14) gives 6.2 kWh, 11:00
    # (11-15) 12.4 kWh -> candidate 11:00. Live alone would move at 08:11; the
    # applied variant waits for 09:10 and Extrap (same forecast here) agreeing.
    service, charge, clock, reports = _setup()
    _run(service, clock, 8, (1, 6, 11))
    assert charge.start_charge_hour_override == time(12, 0)
    assert service.observed_start(ShiftVariant.FLAT) == 11 * H
    assert reports == []

    _run(service, clock, 9, (11, 16, 21))

    assert charge.start_charge_hour_override == time(11, 0)
    assert len(reports) == 1
    report = reports[0]
    assert report.move == ForecastShiftMove(
        before=12 * H, after=11 * H, manual_start=None
    )
    assert report.need_kwh == pytest.approx(8.77)
    assert report.surplus_kwh == pytest.approx(12.4)
    assert report.window_end_hour == 15
    assert service.last is not None
    assert service.last.extrap == 11 * H
    assert set(service.last.candidates) == set(ShiftVariant)


def test_moves_before_0910_when_live_is_short_even_at_the_earliest():
    service, charge, clock, reports = _setup(soc=10.0, s0=time(12, 0))
    service._pv_surplus = _ThinSurplus()  # noqa: SLF001
    _run(service, clock, 8, (1, 6, 11))
    assert charge.start_charge_hour_override == time(8, 11)
    assert len(reports) == 1


class _ThinSurplus:
    """1.5 kWh/h from 8 to 16 — 0.6 kWh/h above use, never enough for 10.64."""

    def today_profile(
        self, *, history: bool, pv_scale: float, source: object = None
    ) -> SurplusProfile:
        pv = slots_from_hourly(dict.fromkeys(range(8, 16), 1.5))
        return SurplusProfile.build(DAY, pv, None)


def test_stops_at_s0():
    service, _, clock, _ = _setup(s0=time(9, 0))
    clock.at(9, 1)
    service.evaluate()
    assert service.last is None


def test_a_full_battery_waits_past_s0_for_the_cheapest_hours():
    # 28.10.2026 shape: SoC 90%, S0 09:00 (380), 12:00-13:00 cost 100. The
    # applied variant moves the start later — past S0 — and keeps evaluating
    # until it.
    service, charge, clock, reports = _setup(soc=90.0, s0=time(9, 0))
    _run(service, clock, 7, (41, 46, 51))
    assert charge.start_charge_hour_override == time(12, 0)
    assert reports[-1].move == ForecastShiftMove(
        before=9 * H, after=12 * H, manual_start=None
    )
    assert "(później)" in render(reports[-1])
    _run(service, clock, 9, (1, 6, 11, 16))
    assert service.last is not None and service.last.at == clock.now
    assert charge.start_charge_hour_override == time(12, 0)
    # The observed variants keep the rule up to S0 and stop there.
    assert service.observed_start(ShiftVariant.FLAT) == 9 * H
    assert service.last.by_pv == {}


def test_stops_once_locked():
    service, charge, clock, _ = _setup()
    _run(service, clock, 9, (11, 16, 21))
    shift = charge.forecast_shift
    assert shift is not None
    shift._locked = True  # noqa: SLF001 — state reached through the lock
    clock.at(9, 26)
    before = service.last
    service.evaluate()
    assert service.last is before


def test_message_names_the_move_and_the_hand_set_start():
    report = ShiftReport(
        move=ForecastShiftMove(before=12 * H, after=9 * H, manual_start=time(11, 30)),
        s0=12 * H,
        soc=35.0,
        need_kwh=7.64,
        surplus_kwh=8.12,
        window_end_hour=15,
    )
    text = render(report)
    assert text.splitlines() == [
        "Start BY SIĘ zmienił: 12:00 na 09:00 (wcześniej), ale obowiązuje ręczny 11:30",
        (
            "SOC 35% — do pełna 7,6 kWh; prognoza nadwyżki 09:00-15:00: 8,1 kWh"
            " (Live Soft minus p60 zużycia)"
        ),
        "Z cen RCE start o 12:00",
    ]


def test_locked_start_raises_a_rewind_alert_instead_of_moving():
    # Charging since 09:00 (locked) at SoC 60% -> 4.81 kWh to full; even S0
    # 12:00 (window 12-14) gives 6.2 kWh by both forecasts -> pause until 12:00.
    service, charge, clock, reports = _setup(soc=60.0)
    shift = charge._repo.policy.forecast_shift_for(DAY, 12 * H)  # noqa: SLF001
    shift._start, shift._locked = 9 * H, True  # noqa: SLF001
    _run(service, clock, 9, (6, 11, 16))

    assert reports == []
    assert len(service.alerts) == 1
    alert = service.alerts[0]
    assert (alert.start, alert.suggestion, alert.alert_no) == (9 * H, 12 * H, 1)
    assert shift.start == 9 * H  # nothing changed automatically


def test_no_rewind_alert_under_a_hand_set_start():
    service, charge, clock, _ = _setup(soc=60.0)
    shift = charge._repo.policy.forecast_shift_for(DAY, 12 * H)  # noqa: SLF001
    shift._start, shift._locked = 9 * H, True  # noqa: SLF001
    charge._repo.policy.set_start_charge_hour_manual(time(9, 0), DAY)  # noqa: SLF001
    _run(service, clock, 9, (6, 11, 16))
    assert service.alerts == []


def test_rewind_message_says_how_to_pause():
    text = render_rewind(
        RewindReport(
            start=9 * H,
            soc=45.0,
            need_kwh=6.1,
            suggestion=11 * H,
            live_kwh=7.24,
            extrap_kwh=8.9,
            window_end_hour=15,
            alert_no=1,
            max_alerts=3,
        )
    )
    assert text.splitlines() == [
        (
            "Ładowanie z PV trwa od 09:00 (SOC 45%). Start o 11:00 nadal napełni"
            " baterię: do pełna 6,1 kWh, nadwyżka 11:00-15:00 wg Live Soft 7,2 kWh,"
            " wg Extrap 8,9 kWh."
        ),
        (
            "Żeby przerwać: ustaw ręcznie start 11:00 (EMS Battery Charge Start"
            " Hour Override)."
        ),
        "Alarm 1/3 dziś.",
    ]


def test_no_telegram_line_starts_with_start():
    """CallMeBot's firewall answers 403 to a line beginning with "start"."""
    texts = [
        render(
            ShiftReport(
                move=ForecastShiftMove(
                    before=11 * 60, after=8 * 60, manual_start=manual
                ),
                s0=11 * 60,
                soc=40.0,
                need_kwh=6.9,
                surplus_kwh=1.4,
                window_end_hour=16,
            )
        )
        for manual in (None, time(9, 0))
    ]
    for text in texts:
        for line in text.splitlines()[1:]:
            assert not line.lower().startswith("start"), line


class _HistorySurplus:
    """p60 use eats the surplus: only 2 kWh/h left from 8 to 16, flat 4."""

    def __init__(self, history_loaded: bool = True) -> None:
        self.history_loaded = history_loaded

    def today_profile(
        self, *, history: bool, pv_scale: float, source: object = None
    ) -> SurplusProfile | None:
        if history and not self.history_loaded:
            return None
        rate = 2.0 if history else 4.0
        pv = slots_from_hourly(dict.fromkeys(range(8, 16), rate))
        return SurplusProfile.build(DAY, pv, None)


def test_applied_variant_reads_the_p60_consumption():
    # SoC 20% -> 8.77 kWh. Flat surplus: 11:00 enough (12.4). With p60 use
    # (2 kWh/h) only an earlier start would do, so from 09:11 it is "now".
    service, charge, clock, _ = _setup()
    service._pv_surplus = _HistorySurplus()  # noqa: SLF001
    _run(service, clock, 9, (11, 16, 21))
    assert charge.start_charge_hour_override == time(9, 21)
    assert service.observed_start(ShiftVariant.FLAT) == 11 * H


def test_applied_variant_falls_back_to_flat_without_history():
    service, charge, clock, _ = _setup()
    service._pv_surplus = _HistorySurplus(history_loaded=False)  # noqa: SLF001
    _run(service, clock, 9, (11, 16, 21))
    assert charge.start_charge_hour_override == time(11, 0)


def test_every_today_pv_variant_gets_its_candidate():
    from custom_components.smart_rce.domain.pv_forecast import PvForecast

    service, _, clock, _ = _setup()
    _run(service, clock, 8, (1,))
    assert service.last is not None
    assert set(service.last.by_pv) == set(PvForecast.today())
    # Same surplus for every variant here -> same start as the flat variant.
    assert {c.start for c in service.last.by_pv.values()} == {11 * H}
