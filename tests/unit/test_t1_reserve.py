"""T1 reserve + CHARGE_MORNING_AUTO — forecast-driven morning charge before T1."""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

from custom_components.smart_rce.application.battery_schedule_service import (
    BatteryScheduleService,
)
from custom_components.smart_rce.domain.battery_schedule import (
    BatterySchedule,
    BatteryScheduleEntry,
    DisengageReason,
    ReserveWindow,
    SlotBehavior,
    SlotDisengaged,
    SlotEngaged,
    SlotKind,
    T1SocReserve,
)
from custom_components.smart_rce.domain.pv_forecast import PvForecast
from custom_components.smart_rce.domain.target_soc import TargetSoc, TargetSocResult

TZ = timezone.utc
DAY = date(2026, 10, 12)  # Monday


def _at(h: int, m: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def _auto(fallback: float = 60.0, *, enabled: bool = True) -> BatteryScheduleEntry:
    return BatteryScheduleEntry(
        kind=SlotKind.CHARGE_MORNING_AUTO,
        enabled=enabled,
        start=time(4, 0),
        end=time(7, 0),
        target_soc=fallback,
        behavior=SlotBehavior.DELAYED_TO_END,
    )


def _target(
    value: int,
    day: date = DAY,
    source: str = "live",
    window: ReserveWindow = ReserveWindow.FULL,
) -> T1SocReserve:
    return T1SocReserve(
        day=day,
        value=value,
        flat=value,
        prev_without_worst=None,
        dip_kwh=None,
        source=source,
        window=window,
    )


def _schedule(*entries: BatteryScheduleEntry) -> BatterySchedule:
    today = BatteryScheduleEntry.defaults_for_all_kinds()
    for e in entries:
        today[e.kind] = e
    return BatterySchedule(today=today)


def _result(value: int) -> TargetSocResult:
    return TargetSocResult(value=value, buckets=[], dip_kwh=0.0)


def _engaged(events) -> SlotEngaged:
    return next(e for e in events if isinstance(e, SlotEngaged))


class TestPrevWithoutWorst:
    def test_drops_only_the_single_highest(self):
        assert T1SocReserve.prev_without_worst_of([24, 29, 28, 30, 23]) == 29

    def test_tie_at_the_top_keeps_the_twin(self):
        assert T1SocReserve.prev_without_worst_of([30, 30, 20]) == 30

    def test_single_value_is_the_answer(self):
        assert T1SocReserve.prev_without_worst_of([41]) == 41

    def test_empty_is_none(self):
        assert T1SocReserve.prev_without_worst_of([]) is None


class TestFromTargetSoc:
    def test_p60_reading_drives_the_value(self):
        ts = TargetSoc(
            variant=PvForecast.LIVE,
            flat=_result(25),
            prev_days=[_result(v) for v in (24, 29, 28, 30, 23)] + [None],
            p60=_result(22),
        )
        target = T1SocReserve.from_target_soc(
            DAY, ts, source="live", window=ReserveWindow.FULL
        )
        assert target is not None
        assert (
            target.value,
            target.p60,
            target.flat,
            target.prev_without_worst,
        ) == (22, 22, 25, 29)

    def test_flat_without_consumption_history(self):
        ts = TargetSoc(variant=PvForecast.LIVE, flat=_result(36), prev_days=[None])
        target = T1SocReserve.from_target_soc(
            DAY, ts, source="live", window=ReserveWindow.FULL
        )
        assert target is not None
        assert (target.value, target.p60) == (36, None)

    def test_capped_at_100(self):
        ts = TargetSoc(variant=PvForecast.LIVE, flat=_result(126), prev_days=[])
        target = T1SocReserve.from_target_soc(
            DAY, ts, source="live", window=ReserveWindow.FULL
        )
        assert target is not None and target.value == 100

    def test_nothing_computed_is_none(self):
        ts = TargetSoc(variant=PvForecast.LIVE, flat=None, prev_days=[None, None])
        assert (
            T1SocReserve.from_target_soc(
                DAY, ts, source="live", window=ReserveWindow.FULL
            )
            is None
        )


class TestEffectiveTarget:
    def test_engages_with_forecast_target_and_reports_source(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        _, events = sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        engaged = _engaged(events)
        assert engaged.slot is SlotKind.CHARGE_MORNING_AUTO
        assert (engaged.target_soc, engaged.target_source) == (40.0, "live")

    def test_without_target_falls_back_to_slot_number(self):
        sch = _schedule(_auto(fallback=60.0))
        # 20 -> 60% = 3000 s + 240 s lead -> starts by 06:06.
        _, events = sch.compute_operation(_at(6, 10), 20.0, is_workday=True)
        engaged = _engaged(events)
        assert (engaged.target_soc, engaged.target_source) == (60.0, "fallback")

    def test_target_for_another_day_is_ignored(self):
        sch = _schedule(_auto(fallback=60.0))
        sch.set_t1_reserve(_target(40, day=date(2026, 10, 9)), _at(3))
        _, events = sch.compute_operation(_at(6, 10), 20.0, is_workday=True)
        assert _engaged(events).target_source == "fallback"

    def test_slot_number_untouched_by_target(self):
        sch = _schedule(_auto(fallback=60.0))
        sch.set_t1_reserve(_target(40), _at(3))
        sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        assert sch.today_entry_for(SlotKind.CHARGE_MORNING_AUTO).target_soc == 60.0


class TestDelayedLead:
    """20 -> 40% = 20 pp x 75 s = 1500 s; lead adds 240 s -> start by 06:31."""

    def test_waits_while_more_than_time_plus_lead_remains(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        _, events = sch.compute_operation(_at(6, 30), 20.0, is_workday=True)
        assert not any(isinstance(e, SlotEngaged) for e in events)

    def test_starts_four_minutes_before_the_rate_model_would(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        # 06:32 leaves 1680 s: above the bare 1500 s, inside 1500 + 240.
        _, events = sch.compute_operation(_at(6, 32), 20.0, is_workday=True)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING_AUTO

    def test_every_charge_slot_has_the_lead(self):
        manual = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING,
            enabled=True,
            start=time(4, 0),
            end=time(7, 0),
            target_soc=40.0,
            behavior=SlotBehavior.DELAYED_TO_END,
        )
        sch = _schedule(manual)
        _, events = sch.compute_operation(_at(6, 32), 20.0, is_workday=True)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING

    def test_only_the_morning_auto_discharge_has_a_lead(self):
        assert all(
            k.profile.delayed_lead_sec == 0.0
            for k in SlotKind
            if k.direction.is_discharge and k is not SlotKind.DISCHARGE_MORNING_AUTO
        )
        assert SlotKind.DISCHARGE_MORNING_AUTO.profile.delayed_lead_sec == 240.0
        assert all(
            k.profile.delayed_lead_sec == 240.0
            for k in SlotKind
            if not k.direction.is_discharge
        )


class TestStartGap:
    def test_two_points_short_does_not_start(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        _, events = sch.compute_operation(_at(6, 59), 38.0, is_workday=True)
        assert not any(isinstance(e, SlotEngaged) for e in events)

    def test_three_points_short_starts(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        _, events = sch.compute_operation(_at(6, 59), 37.0, is_workday=True)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING_AUTO

    def test_target_dropping_below_soc_ends_the_charge(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        sch.compute_operation(_at(6, 40), 20.0, is_workday=True)
        sch.set_t1_reserve(_target(25), _at(3))
        _, events = sch.compute_operation(_at(6, 45), 26.0, is_workday=True)
        ended = next(e for e in events if isinstance(e, SlotDisengaged))
        assert ended.reason is DisengageReason.TARGET_REACHED
        assert ended.target_soc == 25.0


class TestWorkdayGate:
    def test_day_off_never_starts(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        _, events = sch.compute_operation(_at(6, 50), 20.0, is_workday=False)
        assert not any(isinstance(e, SlotEngaged) for e in events)

    def test_unknown_calendar_counts_as_workday(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(40), _at(3))
        _, events = sch.compute_operation(_at(6, 50), 20.0, is_workday=None)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING_AUTO

    def test_manual_morning_charge_ignores_the_gate(self):
        manual = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING,
            enabled=True,
            start=time(4, 0),
            end=time(7, 0),
            target_soc=40.0,
            behavior=SlotBehavior.IMMEDIATE,
        )
        sch = _schedule(manual)
        _, events = sch.compute_operation(_at(5, 0), 20.0, is_workday=False)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING


class TestManualBeatsAuto:
    def test_both_ready_manual_wins(self):
        manual = BatteryScheduleEntry(
            kind=SlotKind.CHARGE_MORNING,
            enabled=True,
            start=time(4, 0),
            end=time(7, 0),
            target_soc=50.0,
            behavior=SlotBehavior.IMMEDIATE,
        )
        sch = _schedule(_auto(), manual)
        sch.set_t1_reserve(_target(40), _at(3))
        _, events = sch.compute_operation(_at(6, 50), 20.0, is_workday=True)
        assert _engaged(events).slot is SlotKind.CHARGE_MORNING


class TestPersistence:
    def test_target_round_trips(self):
        sch = _schedule(_auto())
        sch.set_t1_reserve(_target(42, source="tomorrow_live"), _at(3))
        restored = BatterySchedule.from_dict(sch.to_dict())
        assert restored.t1_reserve == _target(42, source="tomorrow_live")

    def test_store_without_new_fields_restores_defaults(self):
        data = BatterySchedule().to_dict()
        data.pop("t1_reserve")
        for scope in ("today", "tomorrow"):
            data[scope].pop(SlotKind.CHARGE_MORNING_AUTO.name)
        restored = BatterySchedule.from_dict(data)
        assert restored.t1_reserve is None
        auto = restored.today_entry_for(SlotKind.CHARGE_MORNING_AUTO)
        assert (auto.enabled, auto.start, auto.end, auto.target_soc) == (
            False,
            time(4, 0),
            time(7, 0),
            60.0,
        )

    def test_set_same_target_reports_no_change(self):
        sch = BatterySchedule()
        assert sch.set_t1_reserve(_target(40), _at(3)) is True
        assert sch.set_t1_reserve(_target(40), _at(3)) is False


class TestResolveT1Reserve:
    """Which forecast variant and window feed the reserve, by time of day."""

    @staticmethod
    def _catalog() -> SimpleNamespace:
        def ts(v: PvForecast, flat: int) -> TargetSoc:
            return TargetSoc(variant=v, flat=_result(flat), prev_days=[])

        return SimpleNamespace(
            target_socs={
                PvForecast.AT_6: ts(PvForecast.AT_6, 90),
                PvForecast.LIVE: ts(PvForecast.LIVE, 35),
                PvForecast.LIVE_SOFT: ts(PvForecast.LIVE_SOFT, 31),
                PvForecast.TOMORROW_LIVE: ts(PvForecast.TOMORROW_LIVE, 48),
                PvForecast.TOMORROW_LIVE_SOFT: ts(PvForecast.TOMORROW_LIVE_SOFT, 44),
                PvForecast.EXTRAP_PATTERN: ts(PvForecast.EXTRAP_PATTERN, 52),
                PvForecast.EXTRAP_BAND: TargetSoc(
                    variant=PvForecast.EXTRAP_BAND, flat=None, prev_days=[]
                ),
            }
        )

    @staticmethod
    def _live(h: int, m: int = 0) -> T1SocReserve:
        reserve = BatteryScheduleService._resolve_t1_reserve(  # noqa: SLF001
            _at(h, m), TestResolveT1Reserve._catalog()
        )
        assert reserve is not None
        return reserve

    def test_night_is_todays_whole_window(self):
        r = self._live(3)
        assert (r.day, r.value, r.source, r.window) == (
            DAY,
            46,
            "live_soft",
            ReserveWindow.FULL,
        )
        assert (r.value_without_margin, r.margin_pp) == (31, 15)

    def test_margin_steps_down_through_the_morning(self):
        # Catalog: Soft 31, Live 35, no Extrap Proportional/Blend -> both capped
        # steps (07-08, 08-09) at 35 - 31 = 4.
        assert [
            self._live(h, m).margin_pp
            for h, m in ((6, 59), (7, 0), (8, 0), (9, 0), (13, 0))
        ] == [15, 4, 4, 0, 0]
        assert self._live(8, 30).value == 35
        assert self._live(8, 30).to_dict()["live_reading"] == 35

    def test_extrap_reserve_has_no_margin(self):
        r = BatteryScheduleService._resolve_extrap_reserve(  # noqa: SLF001
            _at(6), self._catalog()
        )
        assert r is not None
        assert r.margin_pp == 0

    def test_t1_hours_are_what_is_left(self):
        assert self._live(7).window is ReserveWindow.REMAINING
        assert self._live(12, 59).window is ReserveWindow.REMAINING

    def test_early_afternoon_replays_today(self):
        r = self._live(13)
        assert (r.day, r.window) == (DAY, ReserveWindow.PAST)
        assert self._live(15, 59).day == DAY

    def test_from_sixteen_targets_tomorrow(self):
        r = self._live(16)
        assert (r.day, r.value, r.source, r.window) == (
            date(2026, 10, 13),
            59,  # the night charge target: 44 + 15
            "tomorrow_live_soft",
            ReserveWindow.FULL,
        )

    def test_extrap_reserve_takes_the_highest_variant_without_at6(self):
        r = BatteryScheduleService._resolve_extrap_reserve(  # noqa: SLF001
            _at(9), self._catalog()
        )
        assert r is not None
        assert (r.value, r.source) == (52, "extrap_pattern")

    def test_extrap_reserve_has_no_tomorrow(self):
        assert (
            BatteryScheduleService._resolve_extrap_reserve(  # noqa: SLF001
                _at(16), self._catalog()
            )
            is None
        )


class TestCheckpoint:
    """The stored copy follows the reserve at most every 15 min within a window."""

    def test_same_window_change_waits_for_the_interval(self):
        sch = BatterySchedule()
        first = _target(30, window=ReserveWindow.REMAINING)
        sch.set_t1_reserve(first, _at(8, 0))
        sch.set_t1_reserve(_target(28, window=ReserveWindow.REMAINING), _at(8, 5))
        assert sch.t1_reserve is not None and sch.t1_reserve.value == 28
        assert sch.to_dict()["t1_reserve"]["value"] == 30
        sch.set_t1_reserve(_target(25, window=ReserveWindow.REMAINING), _at(8, 15))
        assert sch.to_dict()["t1_reserve"]["value"] == 25

    def test_new_day_or_window_is_stored_at_once(self):
        sch = BatterySchedule()
        sch.set_t1_reserve(_target(30, window=ReserveWindow.REMAINING), _at(12, 55))
        sch.set_t1_reserve(_target(36, window=ReserveWindow.PAST), _at(13, 0))
        assert sch.to_dict()["t1_reserve"]["window"] == "past"
        tomorrow = _target(29, day=DAY + timedelta(days=1), source="tomorrow_live")
        sch.set_t1_reserve(tomorrow, _at(16, 0))
        assert sch.to_dict()["t1_reserve"]["day"] == "2026-10-13"

    def test_first_release_key_still_restores(self):
        data = BatterySchedule().to_dict()
        data.pop("t1_reserve")
        data["morning_target"] = {
            "day": "2026-10-12",
            "value": 30,
            "flat": 29,
            "prev_without_worst": 30,
            "source": "tomorrow_live",
        }
        restored = BatterySchedule.from_dict(data).t1_reserve
        assert restored is not None
        assert (restored.value, restored.window) == (30, ReserveWindow.FULL)


class TestNotification:
    """The telegram names the target and shouts when it is the fallback."""

    @staticmethod
    def _message(source: str | None, target: float | None = 40.0) -> str:
        from custom_components.smart_rce.infrastructure.battery_schedule_notifier import (  # noqa: PLC0415
            BatteryScheduleNotifier,
        )

        rendered = BatteryScheduleNotifier._render(  # noqa: SLF001
            SlotEngaged(
                slot=SlotKind.CHARGE_MORNING_AUTO,
                soc=20.0,
                at=_at(6, 40),
                target_soc=target,
                target_source=source,
            )
        )
        assert rendered is not None
        return rendered[1]

    def test_forecast_target_names_its_source(self):
        assert self._message("live").endswith("target 40% (from live).")

    def test_fallback_is_flagged(self):
        assert "FALLBACK" in self._message("fallback", 60.0)

    def test_fixed_slot_shows_plain_target(self):
        assert self._message(None).endswith("target 40%.")


class TestMargin:
    """Falling margin on top of the reserve (decision 15.10.2026)."""

    def test_steps_on_the_reserve_day(self):
        steps = [
            T1SocReserve.margin_pp_at(DAY, _at(h, m))
            for h, m in (
                (0, 0),
                (6, 59),
                (7, 0),
                (7, 59),
                (8, 0),
                (8, 59),
                (9, 0),
                (15, 0),
            )
        ]
        assert steps == [15, 15, 10, 10, 5, 5, 0, 0]

    def test_evening_before_takes_the_first_step_and_a_past_day_none(self):
        assert (
            T1SocReserve.margin_pp_at(DAY, _at(18, day=DAY - timedelta(days=1))) == 15
        )
        assert T1SocReserve.margin_pp_at(DAY, _at(6, day=DAY + timedelta(days=1))) == 0

    def test_capped_at_100_and_round_trips(self):
        ts = TargetSoc(variant=PvForecast.LIVE, flat=_result(95), prev_days=[])
        r = T1SocReserve.from_target_soc(
            DAY, ts, source="live", window=ReserveWindow.FULL, margin_at=_at(6)
        )
        assert r is not None
        assert (r.value, r.value_without_margin, r.margin_pp) == (100, 95, 15)
        assert T1SocReserve.from_dict(r.to_dict()) == r

    def test_value_at_reads_the_step_of_the_given_moment(self):
        ts = TargetSoc(variant=PvForecast.LIVE, flat=_result(25), prev_days=[])
        r = T1SocReserve.from_target_soc(
            DAY, ts, source="live", window=ReserveWindow.REMAINING, margin_at=_at(7, 41)
        )
        assert r is not None
        assert r.value == 35  # seen at 07:41
        assert [r.value_at(_at(h)) for h in (7, 8, 9)] == [35, 30, 25]

    def test_reserve_stored_before_the_margin_reads_as_without(self):
        old = T1SocReserve.from_dict(
            {"day": DAY.isoformat(), "value": 31, "source": "live_soft"}
        )
        assert old is not None
        assert (old.margin_pp, old.value_at(_at(8))) == (0, 36)


class TestLiveCappedStep:
    """08-09: margin = max(0, min(5, Live - Soft)) (decision 15.10.2026)."""

    @staticmethod
    def _margin(live: int | None, soft: int = 23, h: int = 8, m: int = 41) -> int:
        return T1SocReserve.margin_pp_for(DAY, _at(h, m), soft, {"live": live})

    def test_sunny_live_equals_soft_no_margin(self):
        assert self._margin(23) == 0

    def test_very_cloudy_live_far_above_soft_full_step(self):
        assert self._margin(42) == 5  # 14.10: Live 42 vs Soft 23

    def test_live_in_between_takes_the_difference(self):
        assert self._margin(26) == 3

    def test_live_below_soft_never_negative(self):
        assert self._margin(20) == 0

    def test_without_live_the_step_stands(self):
        assert self._margin(None) == 5

    def test_night_and_after_nine_ignore_caps(self):
        assert self._margin(23, h=6, m=11) == 15
        assert self._margin(99, h=9, m=0) == 0

    def test_value_at_eight_uses_the_live_reading_held(self):
        soft = TargetSoc(variant=PvForecast.LIVE_SOFT, flat=_result(25), prev_days=[])
        live = TargetSoc(variant=PvForecast.LIVE, flat=_result(27), prev_days=[])
        r = T1SocReserve.from_target_soc(
            DAY,
            soft,
            source="live_soft",
            window=ReserveWindow.REMAINING,
            margin_at=_at(8, 1),
            caps={"live": live},
        )
        assert r is not None
        assert (r.value, r.to_dict()["live_reading"]) == (27, 27)
        assert [r.value_at(_at(h)) for h in (6, 8, 9)] == [40, 27, 25]


class TestSevenToEightStep:
    """07-08: margin = max(0, min(10, max(Live, Extrap Prop, Extrap Blend) - Soft))."""

    _PROP = "extrapolated_live_proportional"
    _BLEND = "extrapolated_live_blend"

    @staticmethod
    def _margin(caps: dict, soft: int = 20) -> int:
        return T1SocReserve.margin_pp_for(DAY, _at(7, 41), soft, caps)

    def test_all_equal_to_soft_no_margin(self):
        assert self._margin({"live": 20, self._PROP: 20, self._BLEND: 20}) == 0

    def test_extrap_far_above_soft_full_step(self):
        assert self._margin({"live": 21, self._PROP: 34, self._BLEND: 22}) == 10

    def test_highest_variant_sets_the_margin(self):
        assert self._margin({"live": 21, self._PROP: 26, self._BLEND: 23}) == 6

    def test_without_extraps_it_is_live(self):
        assert self._margin({"live": 23, self._PROP: None}) == 3
        assert self._margin({"live": 23}) == 3

    def test_no_cap_at_all_keeps_the_step(self):
        assert self._margin({}) == 10

    def test_resolved_reserve_holds_every_cap(self):
        def ts(v: PvForecast, flat: int | None) -> TargetSoc:
            return TargetSoc(
                variant=v,
                flat=_result(flat) if flat is not None else None,
                prev_days=[],
            )

        catalog = SimpleNamespace(
            target_socs={
                PvForecast.LIVE_SOFT: ts(PvForecast.LIVE_SOFT, 20),
                PvForecast.LIVE: ts(PvForecast.LIVE, 21),
                PvForecast.EXTRAP_PROPORTIONAL: ts(PvForecast.EXTRAP_PROPORTIONAL, 34),
                PvForecast.EXTRAP_BLEND: ts(PvForecast.EXTRAP_BLEND, None),
            }
        )
        r = BatteryScheduleService._resolve_t1_reserve(_at(7, 41), catalog)  # noqa: SLF001
        assert r is not None
        attrs = r.to_dict()
        assert (r.value, r.margin_pp) == (30, 10)
        assert (
            attrs["live_reading"],
            attrs["extrap_proportional_reading"],
            attrs["extrap_blend_reading"],
        ) == (21, 34, None)
        assert T1SocReserve.from_dict(attrs) == r


def test_margin_step_keys_are_pv_forecast_keys():
    keys = {v.key for v in PvForecast}
    for _until, _pp, caps in T1SocReserve._MARGIN_STEPS:  # noqa: SLF001
        assert set(caps) <= keys
