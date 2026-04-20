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


class TestBlockChargeToggleGuard:
    """block_charge guard: hourly_balance_negative only set if toggle_on."""

    def test_toggle_off_blocks_set(self):
        """In pre-charge toggle=False → hourly_balance_negative never sets."""
        mgr = BatteryManager()
        mgr._last_hour_seen = 9
        mgr.should_block_battery_discharge = True  # in_guard_window via block_discharge
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=-0.100,  # -100 Wh
                battery_charge_toggle_on=False,
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False

    def test_toggle_on_allows_set(self):
        mgr = BatteryManager()
        mgr._last_hour_seen = 9
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=-0.100,
                battery_charge_toggle_on=True,
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.hourly_balance_negative is True
        assert mgr.should_block_battery_charge is True

    def test_hysteresis_hold_when_toggle_turns_off_mid_flag(self):
        """Once hourly_balance_negative=True, toggle going False doesn't reset."""
        mgr = BatteryManager()
        mgr._last_hour_seen = 9
        mgr.should_block_battery_discharge = True
        mgr.hourly_balance_negative = True  # already set from prev tick
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=-0.010,  # -10 Wh, hysteresis hold (<HYSTERESIS_WH=50)
                battery_charge_toggle_on=False,
                start_charge_hour_override=time(10, 0),
            )
        )
        # Flag stays True via hysteresis (exported<50 keeps it)
        assert mgr.hourly_balance_negative is True

    def test_hysteresis_exit_high_export(self):
        mgr = BatteryManager()
        mgr._last_hour_seen = 9
        mgr.should_block_battery_discharge = True
        mgr.hourly_balance_negative = True
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=0.080,  # 80 Wh >= HYSTERESIS_WH=50
                battery_charge_toggle_on=True,
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False

    def test_dod_zero_or_branch_post_pre_charge(self):
        """After pre-charge window, DoD=0 OR branch activates guard."""
        mgr = BatteryManager()
        # 14:00 — post pre-charge. block_discharge=False. DoD=0 (from morning automation).
        mgr.update(
            _state(
                now=_at(14, 0),
                exported_energy_hourly=-0.100,
                battery_charge_toggle_on=True,
                depth_of_discharge=0,
                start_charge_hour_override=time(10, 0),
            )
        )
        # block_discharge out of pre-charge = False, but DoD=0 triggers OR branch
        assert mgr.should_block_battery_discharge is False
        assert mgr.hourly_balance_negative is True
        assert mgr.should_block_battery_charge is True


class TestEdgeCases:
    def test_low_charge_limit_no_block(self):
        mgr = BatteryManager()
        mgr._last_hour_seen = 9
        mgr.should_block_battery_discharge = True
        mgr.update(
            _state(
                now=_at(9, 30),
                exported_energy_hourly=-0.100,
                battery_charge_toggle_on=True,
                battery_charge_limit=0,  # <2
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.hourly_balance_negative is True  # set
        assert (
            mgr.should_block_battery_charge is False
        )  # but not blocked — limit too low

    def test_none_exported_noop(self):
        mgr = BatteryManager()
        state = _state(now=_at(9, 0), start_charge_hour_override=time(10, 0))
        state.exported_energy_hourly = None
        mgr.update(state)
        # _none_present → early return, nothing changes
        assert mgr.should_block_battery_discharge is False
        assert mgr.hourly_balance_negative is False

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
