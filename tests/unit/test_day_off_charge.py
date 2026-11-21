"""Day off: store the PV in the battery or export it at once (rule of 01.11.2026)."""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.smart_rce.application.battery_charge_service import (
    BatteryChargeService,
)
from custom_components.smart_rce.application.day_off_charge_service import (
    DayOffChargeService,
)
from custom_components.smart_rce.domain.battery_charge_policy import (
    BatteryChargePolicy,
    ChargeStartSource,
    DayOffStart,
)
from custom_components.smart_rce.domain.day_off_charge import (
    MIN_ADVANTAGE_ZL,
    NONE,
    S0,
    DayOffChargeDecision,
    DayOffHour,
)
from custom_components.smart_rce.domain.pv_surplus import (
    SurplusProfile,
    slots_from_hourly,
)
from custom_components.smart_rce.infrastructure.day_off_charge_notifier import (
    NIGHT,
    render,
    render_unavailable,
)

SATURDAY = date(2026, 10, 31)
T3 = (0.43, 0.10)  # energy, distribution — gross zl/kWh


def _hours(rce_day: float, rce_evening: float, surplus: dict[int, float]):
    """Return a day off: flat 0.5 kWh/h consumption, surplus on top as PV."""
    out = []
    for h in range(24):
        sur = surplus.get(h, 0.0)
        rce = rce_evening if 16 <= h < 23 else rce_day
        out.append(DayOffHour(sur + 0.5, 0.5, sur, rce, *T3))
    return out


def _decide(hours, *, in_force=None, s0_hour=10, soc=10.0):
    return DayOffChargeDecision.decide(
        SATURDAY,
        hours,
        soc=soc,
        s0_hour=s0_hour,
        t2_hour=16,
        discharge_hours=(16, 17),
        in_force=in_force,
    )


class TestDecide:
    def test_high_rce_all_day_keeps_the_pv_out_of_the_battery(self):
        # Day 1.20, evening 1.25: a stored kWh comes back as 0.9 kWh worth ~1.13,
        # less than the 1.20 it would have fetched at noon. (31.10.2026 lost the
        # same way: most of the evening discharge only covered the house.)
        d = _decide(_hours(1.20, 1.25, dict.fromkeys(range(9, 15), 1.0)))
        assert d.chosen == NONE
        assert d.variant(S0).value_zl < d.variant(NONE).value_zl - MIN_ADVANTAGE_ZL
        assert d.start_minute(16) == 16 * 60  # the gate stays shut until T2

    def test_cheap_noon_and_dear_evening_keep_s0(self):
        d = _decide(_hours(0.20, 1.50, dict.fromkeys(range(9, 15), 1.0)))
        assert d.chosen == S0
        assert d.start_minute(16) is None

    def test_a_small_edge_does_not_flip_the_start(self):
        # Little surplus: whatever wins, it wins by less than the bar.
        d = _decide(_hours(1.20, 1.25, {11: 0.3}))
        assert d.chosen == S0

    def test_a_later_cheaper_hour_can_win(self):
        # 10-12 at 1.30 (more than 0.9 x 1.40 back in the evening), 13-14 at
        # 0.10: charge only from 13:00.
        hours = _hours(1.30, 1.40, dict.fromkeys(range(10, 15), 2.0))
        cheap = [
            DayOffHour(h.pv_kwh, h.cons_kwh, h.surplus_kwh, 0.10, *T3)
            if i in {13, 14}
            else h
            for i, h in enumerate(hours)
        ]
        d = _decide(cheap)
        assert d.chosen == "from_13"
        assert d.start_minute(16) == 13 * 60

    def test_the_bar_holds_for_the_decision_in_force(self):
        hours = _hours(1.20, 1.25, dict.fromkeys(range(9, 15), 1.0))
        assert _decide(hours, in_force=NONE).chosen == NONE


class TestPolicy:
    def test_round_trip_and_source(self):
        policy = BatteryChargePolicy(
            day_off_start=DayOffStart(day=SATURDAY, value=time(16), variant=NONE)
        )
        restored = BatteryChargePolicy.from_dict(policy.to_dict())
        assert restored.day_off_start == policy.day_off_start
        assert restored.start_charge_source(SATURDAY) is ChargeStartSource.DAY_OFF
        assert restored.day_off_start_for(date(2026, 11, 1)) is None

    def test_a_hand_set_start_outranks_it(self):
        policy = BatteryChargePolicy(
            start_charge_manual_day=SATURDAY,
            day_off_start=DayOffStart(day=SATURDAY, value=time(16), variant=NONE),
        )
        assert policy.start_charge_source(SATURDAY) is ChargeStartSource.MANUAL


class _Repo:
    def __init__(self, policy: BatteryChargePolicy) -> None:
        self.policy = policy

    async def persist(self) -> None:
        pass

    def save_if_changed(self) -> None:
        pass


def _at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, 31, h, m, tzinfo=timezone.utc)


class TestChargeService:
    def test_the_day_off_start_wins_over_the_forecast_shift(self):
        clock = SimpleNamespace(now=_at(6, 41))
        service = BatteryChargeService(
            repo=_Repo(BatteryChargePolicy()),
            clock=lambda: clock.now,
            actuator=MagicMock(),
        )
        start = DayOffStart(day=SATURDAY, value=time(16), variant=NONE)
        service.apply_day_off_start(clock.now, time(10), start)
        assert service.start_charge_hour_override == time(16)
        # The forecast shift evaluating later must not put its start back.
        for minute in (41, 46, 51):
            clock.now = _at(7, minute)
            service.evaluate_forecast_shift(clock.now, time(10), 9 * 60, None)
        assert service.start_charge_hour_override == time(16)
        assert service.start_charge_details()["summary"] == (
            "dzień wolny — bez ładowania z PV"
        )
        service.apply_day_off_start(clock.now, time(10), None)
        assert service.start_charge_hour_override != time(16)


class _Charge:
    def __init__(self) -> None:
        self.applied: list = []
        self.day_off_start_today = None
        self.start_charge_is_manual_today = False
        self.start_charge_hour_override = time(10)

    def apply_day_off_start(self, now, s0, start) -> None:
        self.applied.append(start)
        self.day_off_start_today = start


def _service(
    *, workday=False, rce_day=1.20, now=_at(6, 41), soc=10.0, evening=((16, 18),)
):
    charge = _Charge()
    prices = tuple((1.25 if 16 <= h < 23 else rce_day) / 1.23 * 1000 for h in range(24))
    ems = SimpleNamespace(
        is_workday_today=workday,
        last_input_state=SimpleNamespace(battery_soc=soc),
        charge_slots=SimpleNamespace(today_start=time(10), tomorrow_start=time(10)),
        rce_prices=SimpleNamespace(
            rce_prices=SimpleNamespace(
                today=SimpleNamespace(day=SATURDAY, hour_price=prices),
                tomorrow=SimpleNamespace(day=SATURDAY, hour_price=prices),
            )
        ),
        battery_charge_service=charge,
        battery_schedule_service=SimpleNamespace(
            evening_windows_on=lambda day: [
                SimpleNamespace(start=time(a), end=time(b)) for a, b in evening
            ]
        ),
    )

    class _Surplus:
        def live_soft_profile(self, day, *, tomorrow):
            pv = slots_from_hourly(dict.fromkeys(range(9, 15), 1.5))
            cons = dict.fromkeys(range(48), 0.25)
            return SurplusProfile.build(day, pv, cons)

    reports: list = []
    clock = SimpleNamespace(now=now)
    service = DayOffChargeService(ems, _Surplus(), reports.append, lambda: clock.now)
    return service, charge, reports, clock


class TestService:
    def test_nothing_on_a_workday(self):
        service, charge, reports, _ = _service(workday=True)
        service.evaluate(validation=False)
        assert charge.applied == [] and reports == [] and service.last is None

    def test_decides_at_0641_and_reports(self):
        service, charge, reports, _ = _service()
        service.evaluate(validation=False)
        assert service.last is not None and service.last.decision.chosen == NONE
        assert charge.applied[-1].value == time(16)
        assert len(reports) == 1
        assert service.last.discharge_hours == (16, 17)

    def test_the_0741_check_reports_only_a_change(self):
        service, charge, reports, clock = _service()
        service.evaluate(validation=False)
        clock.now = _at(7, 41)
        service.evaluate(validation=True)
        assert len(reports) == 1  # same decision — silent

    def test_message_lines_never_begin_with_start(self):
        service, _charge, reports, _ = _service()
        service.evaluate(validation=False)
        text = render(reports[0])
        assert "NIE ładujemy baterii z PV" in text
        assert not any(line.lower().startswith("start") for line in text.splitlines())


FRIDAY_2106 = datetime(2026, 10, 30, 21, 6, tzinfo=timezone.utc)


class TestPreview:
    """21:06 before a day off: tomorrow's decision, nothing applied (21.11.2026)."""

    def test_previews_tomorrow_without_applying_anything(self):
        service, charge, reports, _ = _service(now=FRIDAY_2106, soc=60.0)
        preview = service.preview(SATURDAY)
        assert preview is not None and preview.preview
        assert preview.decision.day == SATURDAY
        assert preview.decision.chosen == NONE
        assert charge.applied == [] and reports == [] and service.last is None
        assert preview.manual_start is None

    def test_projects_the_battery_through_the_night(self):
        # 0.5 kWh/h of house from 21:06 to 06:41 — about 4.8 kWh off 60%.
        service, *_ = _service(now=FRIDAY_2106, soc=60.0)
        preview = service.preview(SATURDAY)
        assert preview is not None
        assert 10.0 <= preview.soc < 30.0
        service, *_ = _service(now=FRIDAY_2106, soc=12.0)
        preview = service.preview(SATURDAY)
        assert preview is not None and preview.soc == 10.0

    def test_no_tomorrow_prices_no_preview(self):
        service, *_ = _service(now=FRIDAY_2106)
        assert service.preview(SATURDAY + timedelta(days=1)) is None
        text = render_unavailable(SATURDAY)
        assert "podgląd niedostępny" in text
        assert NIGHT in text

    def test_preview_text(self):
        service, *_ = _service(now=FRIDAY_2106, soc=60.0)
        preview = service.preview(SATURDAY)
        assert preview is not None
        text = render(preview)
        assert "Jutro 31.10 dzień wolny (zapowiedź, decyzja o 06:41)" in text
        assert NIGHT in text
        assert "Wieczorem sprzedaż: 16:00, 17:00." in text
        assert "SOC o 06:41 ok." in text
        assert not any(line.lower().startswith("start") for line in text.splitlines())


class TestNoEveningPlan:
    """Without an evening sale the valuation's fallback hours are no sale (21.11)."""

    def test_the_0641_message_says_no_sale(self):
        service, _charge, reports, _ = _service(evening=())
        service.evaluate(validation=False)
        assert service.last is not None and not service.last.evening_planned
        assert len(service.last.discharge_hours) == 10  # valuation fallback
        text = render(reports[0])
        assert "Wieczorem sprzedaż: brak, bateria wieczorem dla domu. SOC 10%." in text
