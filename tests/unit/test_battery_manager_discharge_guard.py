"""Tests for BatteryManager discharge guard (pre-charge window) + toggle-guard."""

from datetime import datetime, time

from custom_components.smart_rce.domain.battery import BatteryManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE


def _state(
    *,
    now: datetime,
    exported_energy_hourly: float = 0.0,
    start_charge_hour_override: time | None = None,
    battery_charge_toggle_on: bool | None = None,
    depth_of_discharge: float | None = None,
    battery_charge_limit: float = 18.0,
    consumption_minus_pv_5_minutes: float | None = None,
    rce_should_hold_for_peak: bool | None = True,
    is_workday: bool | None = True,
) -> InputState:
    return InputState(
        water_heater_big_is_on=False,
        water_heater_small_is_on=False,
        battery_soc=50.0,
        battery_charge_limit=battery_charge_limit,
        battery_power_2_minutes=0.0,
        consumption_minus_pv_2_minutes=0.0,
        consumption_minus_pv_5_minutes=consumption_minus_pv_5_minutes,
        exported_energy_hourly=exported_energy_hourly,
        heater_mode="BALANCED",
        depth_of_discharge=depth_of_discharge,
        battery_charge_toggle_on=battery_charge_toggle_on,
        start_charge_hour_override=start_charge_hour_override,
        rce_should_hold_for_peak=rce_should_hold_for_peak,
        is_workday=is_workday,
        now=now,
    )


def _at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 4, 20, h, m, tzinfo=TIMEZONE)  # 2026-04-20 = Monday


class TestPreChargeWindowDetection:
    """Pre-charge window: 7:00 ≤ now < start_charge_hour_override."""

    def test_inside_window_hour_only(self):
        mgr = BatteryManager()
        mgr.update(_state(now=_at(7, 30), start_charge_hour_override=time(10, 0)))
        # block_discharge set only via hysteresis; first tick sets _last_hour_seen
        assert mgr._last_hour_seen == 7

    def test_before_7_am(self):
        mgr = BatteryManager()
        mgr.update(_state(now=_at(6, 59), start_charge_hour_override=time(10, 0)))
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen is None

    def test_exact_start_charge_hour(self):
        mgr = BatteryManager()
        mgr.update(_state(now=_at(10, 0), start_charge_hour_override=time(10, 0)))
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen is None

    def test_override_with_minutes_inside(self):
        """Override 10:30, now 09:45 → still in pre-charge."""
        mgr = BatteryManager()
        mgr.update(_state(now=_at(9, 45), start_charge_hour_override=time(10, 30)))
        assert mgr._last_hour_seen == 9  # inside window

    def test_override_with_minutes_after(self):
        """Override 10:30, now 10:31 → out of pre-charge."""
        mgr = BatteryManager()
        mgr.update(_state(now=_at(10, 31), start_charge_hour_override=time(10, 30)))
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen is None

    def test_override_none(self):
        mgr = BatteryManager()
        mgr.update(_state(now=_at(9, 0), start_charge_hour_override=None))
        assert mgr.should_block_battery_discharge is False


class TestHourStartReset:
    """Każda nowa godzina w pre-charge startuje z block_discharge=False."""

    def test_hour_transition_resets_flag(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr._last_hour_seen = 7
        mgr.update(
            _state(
                now=_at(8, 0),
                exported_energy_hourly=0.2,  # 200 Wh — byłoby set normalnie
                start_charge_hour_override=time(10, 0),
            )
        )
        # Hour transition 7→8: reset PRZED hysteresis
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen == 8

    def test_first_update_in_window(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(7, 1),
                exported_energy_hourly=0.5,  # 500 Wh
                start_charge_hour_override=time(10, 0),
            )
        )
        # Pierwsza iteracja w nowej godzinie: reset, flag stays False
        assert mgr.should_block_battery_discharge is False


class TestHysteresisMine100_50:
    """Hysteresis MINE: set>=100 Wh, reset<50 Wh. Dead zone 50..100 keeps state."""

    def _set_hour_seen(self, mgr: BatteryManager, hour: int) -> None:
        """Pre-seed _last_hour_seen so following update skips reset branch."""
        mgr._last_hour_seen = hour

    def test_below_set_threshold_stays_false(self):
        mgr = BatteryManager()
        self._set_hour_seen(mgr, 9)
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=0.060,  # 60 Wh — dead zone
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_above_set_threshold(self):
        mgr = BatteryManager()
        self._set_hour_seen(mgr, 9)
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=0.110,  # 110 Wh — set
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_true_in_dead_zone_stays_true(self):
        mgr = BatteryManager()
        self._set_hour_seen(mgr, 9)
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=0.070,  # 70 Wh — dead zone
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_true_below_reset_becomes_false(self):
        mgr = BatteryManager()
        self._set_hour_seen(mgr, 9)
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=0.049,  # 49 Wh — reset
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_true_negative_becomes_false(self):
        mgr = BatteryManager()
        self._set_hour_seen(mgr, 9)
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=-0.005,  # -5 Wh
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_false_just_below_set(self):
        mgr = BatteryManager()
        self._set_hour_seen(mgr, 9)
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=0.099,  # 99 Wh — just below 100
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_true_at_reset_boundary(self):
        """exported=50 — not <50 → stays True."""
        mgr = BatteryManager()
        self._set_hour_seen(mgr, 9)
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=0.050,  # 50 Wh (exact)
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.should_block_battery_discharge is True


class TestEdgeCases:
    def test_none_exported_noop(self):
        mgr = BatteryManager()
        state = _state(now=_at(9, 0), start_charge_hour_override=time(10, 0))
        state.exported_energy_hourly = None
        mgr.update(state)
        # _none_present → early return, nothing changes
        assert mgr.should_block_battery_discharge is False

    def test_override_none_no_tracking(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True  # pretend previously set
        mgr.update(
            _state(
                now=_at(9, 0),
                exported_energy_hourly=0.200,
                start_charge_hour_override=None,  # brak danych
            )
        )
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen is None


class TestPostChargeWindowDetection:
    """Post-charge: start_charge_hour_override ≤ now < 13:00."""

    def test_inside_post_charge(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(11, 30),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=0.0,  # dead zone default
            )
        )
        # In post-charge, _last_hour_seen reset to None
        assert mgr._last_hour_seen is None
        assert mgr.should_block_battery_discharge is False  # default / dead zone

    def test_exactly_13_is_out(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(13, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-1000.0,  # would trigger set
            )
        )
        # 13:00 poza post-charge window
        assert mgr.should_block_battery_discharge is False

    def test_before_start_not_post_charge(self):
        """Now=09:00, override=10:00 → pre-charge, nie post-charge."""
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(9, 30),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-1000.0,
                exported_energy_hourly=0.0,  # hour balance zero — no pre-charge set
            )
        )
        # In pre-charge, _last_hour_seen set, block via exported hysteresis (not avg_5min)
        assert mgr._last_hour_seen == 9
        # exported=0 → dead zone for pre-charge hysteresis, False
        assert mgr.should_block_battery_discharge is False


class TestPostChargeHysteresis5MinAvg:
    """Post-charge: hysteresis na avg_5min (sustained surplus/deficit).

    Set: avg_5min < -500W (sustained surplus)
    Reset: avg_5min > 0W (sustained deficit)
    Dead zone -500..0 — zachowuje poprzedni stan.
    """

    def test_surplus_sustained_sets(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(11, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-600.0,  # -600W surplus
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_at_surplus_boundary(self):
        """avg_5min = -500 not < -500 → stays."""
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(11, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-500.0,
            )
        )
        # -500 is NOT < -500 → no set → stays False (initial)
        assert mgr.should_block_battery_discharge is False

    def test_deficit_sustained_resets(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True  # previous state
        mgr.update(
            _state(
                now=_at(11, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=100.0,  # deficit
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_at_deficit_boundary(self):
        """avg_5min = 0 not > 0 → stays."""
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(11, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=0.0,
            )
        )
        # 0 is NOT > 0 → dead zone → stays True
        assert mgr.should_block_battery_discharge is True

    def test_dead_zone_keeps_true(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(11, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-300.0,  # -300W in dead zone
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_dead_zone_keeps_false(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = False
        mgr.update(
            _state(
                now=_at(11, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-200.0,  # dead zone
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_none_avg_5min_safe_default(self):
        """Brak danych avg_5min → False (safe default)."""
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True  # previous
        mgr.update(
            _state(
                now=_at(11, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=None,
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_continuous_across_hour_boundary(self):
        """Post-charge nie resetuje per-hour — flow przez granicę."""
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(11, 55),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-700.0,
            )
        )
        assert mgr.should_block_battery_discharge is True

        # Granica godziny 11:59 → 12:00. block_discharge zostaje True.
        mgr.update(
            _state(
                now=_at(12, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-600.0,
            )
        )
        assert mgr.should_block_battery_discharge is True  # nie reset
        # _last_hour_seen nadal None (post-charge)
        assert mgr._last_hour_seen is None

    def test_pre_to_post_transition(self):
        """Przejście pre-charge → post-charge (crossing start_charge_hour)."""
        mgr = BatteryManager()
        # Pre-charge: 09:30, override=10:00
        mgr.update(
            _state(
                now=_at(9, 30),
                start_charge_hour_override=time(10, 0),
                exported_energy_hourly=0.0,
                consumption_minus_pv_5_minutes=-1000.0,
            )
        )
        assert mgr._last_hour_seen == 9
        # Przejście na 10:00 — post-charge now. _last_hour_seen resetuje się do None.
        mgr.update(
            _state(
                now=_at(10, 0),
                start_charge_hour_override=time(10, 0),
                consumption_minus_pv_5_minutes=-1000.0,  # surplus
            )
        )
        assert mgr._last_hour_seen is None
        assert mgr.should_block_battery_discharge is True  # avg_5min < -500 set


class TestAfternoonWindowDetection:
    """Afternoon: 13:00 ≤ now < 19:00."""

    def test_just_before_afternoon(self):
        mgr = BatteryManager()
        mgr.update(_state(now=_at(12, 59)))
        # 12:59 jest poza afternoon (a także poza pre/post bo brak override)
        # → out-of-window, block_discharge=False
        assert mgr.should_block_battery_discharge is False

    def test_at_13_00_boundary_inclusive(self):
        # 13:00 exact → afternoon window. With hold=True, status quo.
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(13, 0),
                rce_should_hold_for_peak=True,
            )
        )
        # afternoon-static — block_discharge=False
        assert mgr.should_block_battery_discharge is False

    def test_at_18_59_still_afternoon(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(18, 59),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-1000.0,  # surplus
            )
        )
        # afternoon-dynamic — instant_surplus → block_discharge=True
        assert mgr.should_block_battery_discharge is True

    def test_at_19_00_boundary_exclusive(self):
        # 19:00 exact → out of afternoon window
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(19, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-1000.0,
            )
        )
        # poza afternoon — block_discharge reset to False
        assert mgr.should_block_battery_discharge is False


class TestAfternoonStaticMode:
    """High-price (hold=True) — BatteryManager nie steruje, status quo."""

    def test_surplus_does_not_set(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(15, 0),
                rce_should_hold_for_peak=True,
                consumption_minus_pv_5_minutes=-1000.0,
                exported_energy_hourly=0.500,  # 500 Wh exported
            )
        )
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen is None

    def test_deficit_does_not_set(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(15, 0),
                rce_should_hold_for_peak=True,
                consumption_minus_pv_5_minutes=+500.0,  # deficit
            )
        )
        assert mgr.should_block_battery_discharge is False


class TestAfternoonDynamicMode:
    """Low-price (hold=False) — BatteryManager dynamic na avg_5min OR exported_wh."""

    def test_instant_surplus_sets_regardless_of_export(self):
        # avg_5min < -500W (surplus) → SET, niezależnie od exported_wh
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-600.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_hourly_net_export_sets_in_dead_zone(self):
        # avg_5min w dead zone (-200W), ale exported>0 → SET
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-200.0,
                exported_energy_hourly=0.200,
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_dead_zone_no_export_keeps_state(self):
        # avg_5min dead zone + exported<0 + prev=False → keep False
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = False
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-200.0,
                exported_energy_hourly=-0.050,
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_dead_zone_no_export_keeps_true(self):
        # avg_5min dead zone + exported<0 + prev=True → keep True
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-200.0,
                exported_energy_hourly=-0.050,
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_deficit_with_export_keeps_state(self):
        # avg_5min deficit ALE exported_wh>0 → keep state (nie reset)
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=+50.0,
                exported_energy_hourly=0.200,
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_deficit_no_export_resets(self):
        # avg_5min deficit AND exported<=0 → RESET (allow discharge)
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=+50.0,
                exported_energy_hourly=-0.050,
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_deficit_zero_export_resets_at_boundary(self):
        # exported=0 jest <=0, też RESET przy deficit
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=+50.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_avg_5min_none_safe_default(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=None,
                exported_energy_hourly=0.200,
            )
        )
        # None → safe default False, ignore exported
        assert mgr.should_block_battery_discharge is False


class TestAfternoonHoldFlagTransitions:
    """Hold flag może się zmienić mid-window gdy świeże RCE prices przyszły."""

    def test_hold_true_to_false_enables_dynamic(self):
        mgr = BatteryManager()
        # Najpierw hold=True (afternoon-static)
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=True,
                consumption_minus_pv_5_minutes=-600.0,
                exported_energy_hourly=0.300,
            )
        )
        assert mgr.should_block_battery_discharge is False

        # Hold flip True → False, surplus + export → SET
        mgr.update(
            _state(
                now=_at(14, 5),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-600.0,
                exported_energy_hourly=0.300,
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_hold_false_to_true_disables_dynamic(self):
        mgr = BatteryManager()
        # Najpierw hold=False, surplus → block_discharge=True
        mgr.update(
            _state(
                now=_at(14, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-600.0,
            )
        )
        assert mgr.should_block_battery_discharge is True

        # Hold flip False → True, BatteryManager wycofuje dynamic → False
        mgr.update(
            _state(
                now=_at(14, 5),
                rce_should_hold_for_peak=True,
                consumption_minus_pv_5_minutes=-600.0,
            )
        )
        assert mgr.should_block_battery_discharge is False


class TestPreChargePassthroughWeekend:
    """W weekend (is_workday=False) BatteryManager nie steruje block_discharge."""

    def test_weekend_pre_charge_export_does_not_set(self):
        mgr = BatteryManager()
        mgr._last_hour_seen = 8  # already in hour, post-reset state
        mgr.update(
            _state(
                now=_at(8, 30),
                start_charge_hour_override=time(11, 0),
                exported_energy_hourly=0.150,
                is_workday=False,
            )
        )
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen is None  # weekend nie trackuje hour

    def test_weekend_clears_leftover_state(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr._last_hour_seen = 7
        mgr.update(
            _state(
                now=_at(8, 0),
                start_charge_hour_override=time(11, 0),
                is_workday=False,
            )
        )
        assert mgr.should_block_battery_discharge is False
        assert mgr._last_hour_seen is None

    def test_workday_logic_unchanged_export_sets(self):
        mgr = BatteryManager()
        mgr._last_hour_seen = 8
        mgr.update(
            _state(
                now=_at(8, 30),
                start_charge_hour_override=time(11, 0),
                exported_energy_hourly=0.150,
                is_workday=True,
            )
        )
        # workday + same hour + exported>=100 → SET
        assert mgr.should_block_battery_discharge is True

    def test_is_workday_none_keeps_state(self):
        # Defensive None handling — sensor jeszcze niezaładowany, keep state.
        # Patrz TestDefensiveNoneHandling dla pełnego pokrycia.
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True  # restored prev state
        mgr._last_hour_seen = 8
        mgr.update(
            _state(
                now=_at(8, 30),
                start_charge_hour_override=time(11, 0),
                exported_energy_hourly=0.150,
                is_workday=None,
            )
        )
        assert mgr.should_block_battery_discharge is True
        assert mgr._last_hour_seen == 8


class TestPostChargePassthroughWeekend:
    def test_weekend_post_charge_surplus_no_set(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(11, 30),
                start_charge_hour_override=time(11, 0),
                consumption_minus_pv_5_minutes=-1000.0,  # surplus
                is_workday=False,
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_weekend_post_charge_clears_leftover(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(11, 30),
                start_charge_hour_override=time(11, 0),
                is_workday=False,
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_workday_post_charge_logic_unchanged(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(11, 30),
                start_charge_hour_override=time(11, 0),
                consumption_minus_pv_5_minutes=-1000.0,
                is_workday=True,
            )
        )
        assert mgr.should_block_battery_discharge is True


class TestAfternoonNotAffectedByWorkday:
    """Afternoon używa rce_should_hold_for_peak, niezależnie od is_workday."""

    def test_weekend_afternoon_dynamic_still_works(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(15, 0),
                rce_should_hold_for_peak=False,
                consumption_minus_pv_5_minutes=-1000.0,
                is_workday=False,
            )
        )
        # hold=False → afternoon-dynamic, surplus → SET
        assert mgr.should_block_battery_discharge is True

    def test_weekend_afternoon_static_still_works(self):
        mgr = BatteryManager()
        mgr.update(
            _state(
                now=_at(15, 0),
                rce_should_hold_for_peak=True,
                consumption_minus_pv_5_minutes=-1000.0,
                is_workday=False,
            )
        )
        # hold=True → afternoon-static, BatteryManager nie steruje
        assert mgr.should_block_battery_discharge is False


class TestDefensiveNoneHandling:
    """Defensive None handling — bug 14:33 reprodukcja.

    Gdy critical sensor=None (typowo race po HA restart), BatteryManager
    nie zmienia stanu (keep state).
    """

    def test_afternoon_hold_none_keeps_state_true(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True  # restored prev state
        mgr.update(
            _state(
                now=_at(14, 33),
                rce_should_hold_for_peak=None,
                consumption_minus_pv_5_minutes=-1000.0,
                exported_energy_hourly=0.5,
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_afternoon_hold_none_keeps_state_false(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = False
        mgr.update(
            _state(
                now=_at(14, 33),
                rce_should_hold_for_peak=None,
                consumption_minus_pv_5_minutes=-1000.0,
                exported_energy_hourly=0.5,
            )
        )
        assert mgr.should_block_battery_discharge is False

    def test_pre_charge_workday_none_keeps_state(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr._last_hour_seen = 7
        mgr.update(
            _state(
                now=_at(8, 30),
                start_charge_hour_override=time(10, 0),
                is_workday=None,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_block_battery_discharge is True
        assert mgr._last_hour_seen == 7

    def test_post_charge_workday_none_keeps_state(self):
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(11, 30),
                start_charge_hour_override=time(10, 0),
                is_workday=None,
                consumption_minus_pv_5_minutes=-1000.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_block_battery_discharge is True

    def test_afternoon_hold_explicit_true_static_works(self):
        """Sanity: explicit True wciąż triggers static branch."""
        mgr = BatteryManager()
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(14, 33),
                rce_should_hold_for_peak=True,
                consumption_minus_pv_5_minutes=-1000.0,
                exported_energy_hourly=0.5,
            )
        )
        # afternoon-static → BatteryManager nie steruje, ustawia False
        assert mgr.should_block_battery_discharge is False
