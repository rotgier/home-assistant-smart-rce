"""PV charge on the price chart — milestones, the run as it happened, the bands."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from custom_components.smart_rce.domain.battery_charge_policy import (
    BatteryChargePolicy,
    OverrideMode,
)
from custom_components.smart_rce.domain.charge_start_shift import (
    ChargeMilestones,
    charge_milestones,
    full_charge_minute,
)
from custom_components.smart_rce.domain.pv_charge import (
    BandKind,
    PvChargeForecast,
    PvChargeOutlook,
    PvChargeRun,
)
from custom_components.smart_rce.domain.pv_surplus import (
    SurplusProfile,
    slots_from_hourly,
)
import pytest

DAY = date(2026, 10, 17)
TZ = ZoneInfo("Europe/Warsaw")
H = 60
T2 = 21 * H


def _profile(rate_by_hour: dict[int, float]) -> SurplusProfile:
    return SurplusProfile.build(
        DAY, slots_from_hourly(rate_by_hour), dict.fromkeys(range(48), 0.0)
    )


class TestChargeMilestones:
    def test_plateau_comes_before_full_and_full_matches_full_charge_minute(self):
        profile = _profile(dict.fromkeys(range(11, 16), 50.0))
        got = charge_milestones(profile, 11 * H, 22.0, 16 * H)
        assert got.plateau_at is not None and got.full_at is not None
        # 22 -> 89 at 75 s/pp, then half the 390 s/pp plateau zone to 90.
        assert got.plateau_at == pytest.approx(11 * H + (67 * 75 + 390) / 60, abs=3)
        assert got.plateau_at < got.full_at
        assert got.full_at == full_charge_minute(profile, 11 * H, 22.0, 16 * H)
        assert got.end_soc == 100.0

    def test_short_of_the_plateau_reports_the_level_at_the_end(self):
        got = charge_milestones(
            _profile(dict.fromkeys(range(11, 16), 0.5)), 11 * H, 22.0, 16 * H
        )
        assert got.plateau_at is None
        assert got.full_at is None
        assert 22.0 < got.end_soc < 90.0

    def test_already_above_the_plateau_marks_it_at_the_start(self):
        got = charge_milestones(
            _profile(dict.fromkeys(range(11, 16), 5.0)), 12 * H, 93.0, 16 * H
        )
        assert got.plateau_at == 12 * H


class TestPvChargeRun:
    """17.10.2026: gate opened 11:00, 90% at 12:56, 100% at 13:31."""

    def _observe(self, run, minute, soc, *, opened=True, start=11 * H):
        return run.observe(
            minute, soc, opened=opened, start_in_force=start, window_end=T2
        )

    def test_records_open_plateau_full(self):
        run = PvChargeRun(day=DAY)
        assert not self._observe(run, 10 * H, 22.0, opened=False)
        assert self._observe(run, 11 * H, 22.0)
        assert run.start == 11 * H
        assert not self._observe(run, 12 * H, 60.0)
        assert self._observe(run, 12 * H + 56, 90.0)
        assert not self._observe(run, 13 * H, 90.0)
        assert self._observe(run, 13 * H + 31, 100.0)
        assert (run.plateau_at, run.full_at) == (12 * H + 56, 13 * H + 31)

    def test_gate_found_open_after_a_restart_dates_back_to_the_start_in_force(self):
        run = PvChargeRun(day=DAY)
        self._observe(run, 11 * H + 20, 30.0)
        assert run.start == 11 * H

    def test_forced_open_before_the_start_takes_the_minute_it_opened(self):
        run = PvChargeRun(day=DAY)
        self._observe(run, 8 * H, 30.0, start=11 * H)
        assert run.start == 8 * H

    def test_full_battery_at_the_start_reaches_both_at_once(self):
        run = PvChargeRun(day=DAY)
        self._observe(run, 11 * H, 100.0)
        assert run.plateau_at == run.full_at == 11 * H

    def test_end_of_the_window_keeps_the_level_of_an_unfinished_charge(self):
        run = PvChargeRun(day=DAY)
        self._observe(run, 11 * H, 30.0)
        assert self._observe(run, T2, 84.0)
        assert run.end_soc == 84.0
        assert not self._observe(run, T2 + 5, 80.0)

    def test_round_trip(self):
        run = PvChargeRun(day=DAY, start=660, plateau_at=776, full_at=811, end_soc=None)
        back = PvChargeRun.from_dict(run.to_dict())
        assert back is not None
        assert back.to_dict() == run.to_dict()
        assert PvChargeRun.from_dict({"day": "x"}) is None


class TestPolicyObserve:
    def _policy(self, **kw) -> BatteryChargePolicy:
        return BatteryChargePolicy(start_charge_hour_override=time(11, 0), **kw)

    def _at(self, h: int, m: int = 0) -> datetime:
        return datetime(2026, 10, 17, h, m, tzinfo=TZ)

    def test_night_and_block_window_do_not_start_the_run(self):
        policy = self._policy()
        policy.observe_pv_charge(self._at(3), 80.0, T2)
        policy.observe_pv_charge(self._at(9), 22.0, T2)
        assert policy.pv_charge_run is not None
        assert policy.pv_charge_run.start is None
        policy.observe_pv_charge(self._at(11), 22.0, T2)
        assert policy.pv_charge_run.start == 11 * H

    def test_disallowed_override_never_starts(self):
        policy = self._policy(charge_allowed_override=OverrideMode.DISALLOWED)
        policy.observe_pv_charge(self._at(12), 22.0, T2)
        assert policy.pv_charge_run is not None
        assert policy.pv_charge_run.start is None

    def test_a_new_day_starts_a_new_run_and_it_persists(self):
        policy = self._policy()
        policy.observe_pv_charge(self._at(11), 22.0, T2)
        restored = BatteryChargePolicy.from_dict(policy.to_dict())
        assert restored.pv_charge_run is not None
        assert restored.pv_charge_run.start == 11 * H
        restored.observe_pv_charge(datetime(2026, 10, 18, 7, tzinfo=TZ), 40.0, T2)
        assert restored.pv_charge_run.day == date(2026, 10, 18)
        assert restored.pv_charge_run.start is None


class TestOutlook:
    def _forecast(self, start, plateau, full, end_soc=100.0, *, from_now=False):
        return PvChargeForecast(
            start, 30.0, ChargeMilestones(plateau, full, end_soc), from_now=from_now
        )

    def test_before_the_start_both_bands_are_forecast(self):
        out = PvChargeOutlook.build(
            DAY,
            run=None,
            forecast=self._forecast(11 * H, 13 * H, 13 * H + 40),
            now=8 * H,
            soc=30.0,
            window_end=T2,
        )
        assert [(b.kind, b.start, b.end, b.done) for b in out.bands] == [
            (BandKind.TO_PLATEAU, 11 * H, 13 * H, False),
            (BandKind.TO_FULL, 13 * H, 13 * H + 40, False),
        ]
        assert (out.full_at, out.full_forecast) == (13 * H + 40, True)

    def test_under_way_the_run_is_solid_up_to_now_and_the_forecast_goes_on(self):
        run = PvChargeRun(day=DAY, start=11 * H)
        out = PvChargeOutlook.build(
            DAY,
            run=run,
            forecast=self._forecast(12 * H, 13 * H, 13 * H + 40, from_now=True),
            now=12 * H,
            soc=60.0,
            window_end=T2,
        )
        done, ahead, top = out.bands
        assert (done.done, done.ongoing, done.start, done.end) == (
            True,
            True,
            11 * H,
            12 * H,
        )
        assert (ahead.done, ahead.start, ahead.end) == (False, 12 * H, 13 * H)
        assert top.kind is BandKind.TO_FULL and not top.done
        attrs = out.to_dict(TZ)["bands"]
        assert attrs[0]["end"] is None and attrs[0]["soc"] is None
        assert attrs[1]["start"] is None
        assert attrs[2]["start"] == "2026-10-17T13:00:00+02:00"

    def test_after_the_fact_two_solid_bands_and_no_forecast(self):
        run = PvChargeRun(day=DAY, start=11 * H, plateau_at=776, full_at=811)
        out = PvChargeOutlook.build(
            DAY, run=run, forecast=None, now=15 * H, soc=100.0, window_end=T2
        )
        assert [(b.kind, b.start, b.end, b.done, b.reached) for b in out.bands] == [
            (BandKind.TO_PLATEAU, 660, 776, True, True),
            (BandKind.TO_FULL, 776, 811, True, True),
        ]
        assert (out.full_at, out.full_forecast) == (811, False)

    def test_short_of_full_by_t2_ends_at_t2_with_the_level(self):
        out = PvChargeOutlook.build(
            DAY,
            run=None,
            forecast=self._forecast(11 * H, None, None, end_soc=87.04),
            now=8 * H,
            soc=30.0,
            window_end=T2,
        )
        (band,) = out.bands
        assert (band.end, band.reached, band.soc) == (T2, False, 87.0)
        assert out.full_at is None

    def test_reached_plateau_leaves_only_the_top_to_the_forecast(self):
        run = PvChargeRun(day=DAY, start=11 * H, plateau_at=776)
        out = PvChargeOutlook.build(
            DAY,
            run=run,
            forecast=self._forecast(13 * H, 13 * H, 13 * H + 30, from_now=True),
            now=13 * H,
            soc=92.0,
            window_end=T2,
        )
        kinds = [(b.kind, b.done) for b in out.bands]
        assert kinds == [
            (BandKind.TO_PLATEAU, True),
            (BandKind.TO_FULL, True),
            (BandKind.TO_FULL, False),
        ]
        assert out.bands[2].starts_now


class TestService:
    """PvChargeService on fakes: start in force, SoC the slots leave, plan day."""

    @staticmethod
    def _service(now, *, plan_day, runs=(), sessions=(), soc=40.0, run=None):
        from types import SimpleNamespace  # noqa: PLC0415

        from custom_components.smart_rce.application.pv_charge_service import (  # noqa: PLC0415
            PvChargeService,
        )

        observed = []
        charge = SimpleNamespace(
            start_charge_hour_override=time(11, 0),
            tomorrow_plan=None,
            pv_charge_run=run,
            observe_pv_charge=lambda *a: observed.append(a),
        )
        ems = SimpleNamespace(
            last_input_state=SimpleNamespace(battery_soc=soc),
            battery_charge_service=charge,
            battery_schedule_service=SimpleNamespace(
                plan_day=plan_day,
                upcoming_runs=list(runs),
                sessions_today=list(sessions),
            ),
            charge_slots=SimpleNamespace(tomorrow_start=time(10, 30)),
        )
        asked = []

        class _Surplus:
            def live_soft_profile(self, day, *, tomorrow):
                asked.append((day, tomorrow))
                return SurplusProfile.build(
                    day,
                    slots_from_hourly(dict.fromkeys(range(10, 16), 3.0)),
                    dict.fromkeys(range(48), 0.0),
                )

        service = PvChargeService(ems, _Surplus(), lambda: now)
        service.refresh()
        return service, observed, asked

    def test_today_before_the_start_takes_the_morning_discharge_target(self):
        now = datetime(2026, 10, 17, 6, 0, tzinfo=TZ)
        run = SimpleRun(end=datetime(2026, 10, 17, 9, 0, tzinfo=TZ), target_soc=22.0)
        service, observed, asked = self._service(now, plan_day=DAY, runs=[run])
        forecast_start = service.outlook.bands[0]
        assert forecast_start.start == 11 * H and not forecast_start.done
        assert observed and asked == [(DAY, False)]
        assert service.outlook.full_at is not None
        # From 22% (after the discharge) the plateau comes later than from 40% now.
        without, _o, _a = self._service(now, plan_day=DAY)
        assert forecast_start.end > without.outlook.bands[0].end

    def test_tomorrow_preview_starts_at_s0_and_records_nothing_of_tomorrow(self):
        now = datetime(2026, 10, 17, 21, 30, tzinfo=TZ)
        tomorrow = date(2026, 10, 18)
        service, _observed, asked = self._service(now, plan_day=tomorrow)
        assert service.outlook.day == tomorrow
        assert service.outlook.start == 10 * H + 30
        assert asked == [(tomorrow, True)]


class SimpleRun:
    def __init__(self, end: datetime, target_soc: float) -> None:
        self.end = end
        self.target_soc = target_soc
