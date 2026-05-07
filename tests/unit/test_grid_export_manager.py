"""Tests for GridExportManager — POSITIVE and NEGATIVE balance handling.

POSITIVE: STANDBY (PV<200W) or CHARGE_BATTERY adaptive (PV≥200W).
NEGATIVE: adaptive charge/discharge buckets (target meter +1500W export).
Active window: post_charge → next day 7:00 (skip pre_charge).
"""

from datetime import datetime, time

from custom_components.smart_rce.domain.grid_export import (
    GridExportManager,
    InterventionDirection,
)
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE

# Reference timestamps — workday 2026-04-16 (Thursday)
NOON = datetime(2026, 4, 16, 12, 0, 0, tzinfo=TIMEZONE)
LATE = datetime(2026, 4, 16, 12, 59, 45, tzinfo=TIMEZONE)
END_OF_HOUR = datetime(2026, 4, 16, 12, 59, 55, tzinfo=TIMEZONE)
NEXT_HOUR = datetime(2026, 4, 16, 13, 5, 0, tzinfo=TIMEZONE)
PRE_CHARGE = datetime(
    2026, 4, 16, 8, 0, 0, tzinfo=TIMEZONE
)  # before start_charge=10:00
POST_CHARGE = datetime(2026, 4, 16, 11, 0, 0, tzinfo=TIMEZONE)
EVENING = datetime(2026, 4, 16, 21, 0, 0, tzinfo=TIMEZONE)


def _state(
    *,
    now: datetime = POST_CHARGE,
    exported_energy_hourly: float | None = 0.0,
    battery_soc: float | None = 80.0,
    battery_charge_toggle_on: bool | None = True,
    pv_power: float | None = 3000.0,
    pv_power_avg_2_minutes: float | None = None,  # None → fallback to pv_power
    consumption_minus_pv_2_minutes: float | None = -3000.0,  # surplus PV 3kW
    battery_charge_limit: float | None = 18.0,  # high BMS
    depth_of_discharge: float | None = 78.0,  # min_soc = 100-78 = 22% (NEGATIVE gate)
    start_charge_hour_override: time | None = time(10, 0),
    other_ems_automation_active_this_hour: bool | None = False,
    grid_export_strategy_mode: str | None = "charge_adaptive",
    ems_allow_discharge_override: bool | None = False,
) -> InputState:
    return InputState(
        now=now,
        exported_energy_hourly=exported_energy_hourly,
        battery_soc=battery_soc,
        battery_charge_toggle_on=battery_charge_toggle_on,
        pv_power=pv_power,
        pv_power_avg_2_minutes=pv_power_avg_2_minutes,
        consumption_minus_pv_2_minutes=consumption_minus_pv_2_minutes,
        battery_charge_limit=battery_charge_limit,
        depth_of_discharge=depth_of_discharge,
        start_charge_hour_override=start_charge_hour_override,
        other_ems_automation_active_this_hour=other_ems_automation_active_this_hour,
        grid_export_strategy_mode=grid_export_strategy_mode,
        ems_allow_discharge_override=ems_allow_discharge_override,
    )


class TestStandby:
    """STANDBY entry/avg fallback (PV<200W → discharge_battery xset=0)."""

    def test_standby_when_pv_low(self):
        """PV<200W → STANDBY (pv_power_avg_2_minutes=None → fallback to pv_power)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=EVENING,
                exported_energy_hourly=0.10,
                pv_power=50,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0
        assert mgr.last_decision_reason == "low_pv_standby"

    def test_standby_uses_avg_not_instantaneous(self):
        """Transient spike-down — instant pv<200 but avg_2min>200 → CHARGE."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                pv_power=50,  # transient spike-down (inverter "stalled")
                pv_power_avg_2_minutes=2500,  # real PV stable
            )
        )
        # Manager uses avg_2min (2500W >= 200W) → CHARGE_BATTERY (NOT STANDBY)
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"

    def test_standby_avg_below_threshold(self):
        """Sustained low PV — avg_2min<200, instant may be higher → STANDBY."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=EVENING,
                exported_energy_hourly=0.10,
                pv_power=300,  # transient spike-up
                pv_power_avg_2_minutes=80,  # mean steadily low
            )
        )
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0
        assert mgr.last_decision_reason == "low_pv_standby"


class TestStrategyOverride:
    """STANDBY takes precedence over charge_adaptive."""

    def test_pv_drops_during_charge_switches_to_standby(self):
        """PV drops below 200W during CHARGE → STANDBY."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3000,  # surplus 3kW → entry CHARGE
                pv_power=3000,
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        # PV gone
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3000,
                pv_power=50,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0
        assert mgr.last_decision_reason == "low_pv_standby"


class TestEntryGates:
    """Gates blocking entry."""

    def test_pre_charge_skip(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=PRE_CHARGE,  # 08:00 < start_charge_override=10:00
                exported_energy_hourly=0.10,
                start_charge_hour_override=time(10, 0),
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert "in_pre_charge_window" in mgr.last_decision_reason

    def test_balance_below_threshold(self):
        """Hourly ≤ 0.06 and ≥ -0.05 → deadzone (no entry, manager routes by range)."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.05))
        assert mgr.intervention_active is False
        assert "balance_in_deadzone" in mgr.last_decision_reason

    def test_balance_just_above_threshold(self):
        """Hourly = 0.061 → entry."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.061,
            )
        )
        assert mgr.intervention_active is True

    def test_soc_at_entry_ceiling(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_soc=100))
        assert mgr.intervention_active is False
        assert "soc_at_entry_ceiling" in mgr.last_decision_reason

    def test_soc_at_99_blocked_to_avoid_flap(self):
        # SOC_ENTRY_CEILING=99 prevents flapping when SoC oscillates 99↔100.
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_soc=99))
        assert mgr.intervention_active is False
        assert "soc_at_entry_ceiling" in mgr.last_decision_reason

    def test_toggle_off(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_charge_toggle_on=False))
        assert mgr.intervention_active is False
        assert "toggle_off" in mgr.last_decision_reason

    def test_late_hour_blocks(self):
        mgr = GridExportManager()
        mgr.update(_state(now=LATE, exported_energy_hourly=0.10))
        assert mgr.intervention_active is False
        assert "too_late_in_hour" in mgr.last_decision_reason

    def test_late_hour_just_before_threshold_passes(self):
        """minute=59 AND second<40 → entry allowed."""
        mgr = GridExportManager()
        just_before = datetime(2026, 4, 16, 12, 59, 35, tzinfo=TIMEZONE)
        mgr.update(
            _state(
                now=just_before,
                exported_energy_hourly=0.10,
            )
        )
        assert mgr.intervention_active is True

    def test_other_automation_active_blocks(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                other_ems_automation_active_this_hour=True,
            )
        )
        assert mgr.intervention_active is False
        assert "other_automation_active" in mgr.last_decision_reason


class TestExitGates:
    """Exit from intervention when exit conditions are met."""

    def test_exit_balance_recovered(self):
        """Hourly < 0.05 → exit."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
            )
        )
        assert mgr.intervention_active is True
        mgr.update(
            _state(
                exported_energy_hourly=0.04,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.recommended_xset is None
        assert mgr.last_decision_reason == "balance_recovered"

    def test_exit_soc_ceiling(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_soc=98,
            )
        )
        assert mgr.intervention_active is True
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_soc=100,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "soc_ceiling_exit"

    def test_exit_toggle_off(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
            )
        )
        assert mgr.intervention_active is True
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_charge_toggle_on=False,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "toggle_off_exit"

    def test_exit_end_of_hour(self):
        """End-of-hour cleanup mimo dalej positive balance.

        Entry must be in the same hour as END_OF_HOUR (hour=12),
        otherwise first-of-hour rollover would fire earlier.
        """
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=NOON,
                exported_energy_hourly=0.10,
            )
        )
        assert mgr.intervention_active is True
        mgr.update(
            _state(
                now=END_OF_HOUR,
                exported_energy_hourly=0.10,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "end_of_hour_cleanup"

    def test_exit_hour_rollover(self):
        """Active in hour=11, next update hour=13 → exit hour_rollover."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=POST_CHARGE,
                exported_energy_hourly=0.10,
            )
        )  # hour=11
        assert mgr.intervention_active is True
        assert mgr._intervention_started_hour == 11
        mgr.update(
            _state(
                now=NEXT_HOUR,
                exported_energy_hourly=0.10,
            )
        )  # hour=13
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.last_decision_reason == "hour_rollover"


class TestNonePresent:
    """No-op when required data missing."""

    def test_none_now(self):
        mgr = GridExportManager()
        mgr.update(
            InputState(
                exported_energy_hourly=0.10,
                battery_soc=80,
                pv_power=3000,
            )
        )  # now=None
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "none_present"

    def test_none_exported_energy(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=None))
        assert mgr.last_decision_reason == "none_present"

    def test_none_battery_soc(self):
        mgr = GridExportManager()
        mgr.update(_state(battery_soc=None))
        assert mgr.last_decision_reason == "none_present"

    def test_none_pv_power(self):
        mgr = GridExportManager()
        mgr.update(_state(pv_power=None))
        assert mgr.last_decision_reason == "none_present"

    def test_none_battery_charge_limit_uses_lookup(self):
        """battery_charge_limit=None → bypasses low_bms shortcut, uses lookup."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_charge_limit=None,
                consumption_minus_pv_2_minutes=-3000,  # surplus 3kW
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"
        assert "charge_adaptive_" in mgr.last_decision_reason

    def test_none_consumption_minus_pv_2_minutes(self):
        """charge_adaptive wymaga consumption_minus_pv → None → neutral."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=None,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "none_pv_available"


class TestStrategyMode:
    """input_select.smart_rce_grid_export_strategy_mode kontroluje manager."""

    def test_disabled_intervention_off_diagnostic_in_reason(self):
        """Disabled → recommended=auto, intervention_active=False, reason ma would-be info."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,  # would-be: charge_adaptive
                consumption_minus_pv_2_minutes=-3000,  # surplus 3kW → would-be CHARGE
                grid_export_strategy_mode="disabled",
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.recommended_xset is None
        assert "disabled" in mgr.last_decision_reason
        assert "charge_battery" in mgr.last_decision_reason

    def test_disabled_when_no_intervention_pure_diagnostic(self):
        """Disabled + balance in deadzone → reason = 'disabled (balance_in_deadzone_X.XXX)'."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.03,  # in deadzone (-0.05 < 0.03 ≤ 0.06)
                grid_export_strategy_mode="disabled",
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert "disabled" in mgr.last_decision_reason
        assert "balance_in_deadzone" in mgr.last_decision_reason

    def test_none_strategy_mode_defaults_to_disabled(self):
        """grid_export_strategy_mode=None (helper niegotowy) → traktuj jak disabled."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                grid_export_strategy_mode=None,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert "no_strategy_mode" in mgr.last_decision_reason


class TestStrategyModeChargeAdaptive:
    """charge_adaptive mode: lookup table na pv_available (-consumption_minus_pv_2_minutes).

    pv_avail = -consumption_minus_pv_2_minutes, so:
    - consumption_minus_pv = -5000 → pv_avail = 5000
    - consumption_minus_pv = +500  → pv_avail = -500
    """

    def test_pv_above_4000_xset_6000(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-5000,  # pv_avail = 5000 > 4000
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 6000

    def test_pv_3500_xset_5000(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3500,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 5000

    def test_pv_2500_xset_4000(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-2500,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 4000

    def test_pv_500_xset_2000(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-500,  # pv_avail = 500 > 0
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 2000

    def test_pv_minus_500_xset_1000(self):
        """pv_avail between -1000 and 0 → Xset 1000."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=500,  # pv_avail = -500 > -1000
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 1000
        assert mgr.recommended_ems_mode == "charge_battery"

    def test_pv_minus_1500_auto_intervention_active(self):
        """pv_avail ≤ -1000 → mode=AUTO but intervention STILL active.

        Manager does NOT call _set_neutral — to avoid entry/exit flap when
        hourly is steadily > 0.06. Listener restores Goodwe to AUTO. When
        pv_avail rises above -1000, manager will again issue charge_battery.
        """
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=1500,  # pv_avail = -1500
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.intervention_active is True  # NADAL active
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.recommended_xset is None
        assert "charge_adaptive_auto" in mgr.last_decision_reason

    def test_pv_boundary_4000_exactly_xset_5000(self):
        """Strict threshold `> 4000`. Value exactly 4000 → Xset 5000 (second bucket)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-4000,  # pv_avail = 4000 (nie >4000)
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 5000  # drugi bucket: > 3000

    def test_none_consumption_minus_pv_defensive(self):
        """consumption_minus_pv_2_minutes=None → defensive no-op."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=None,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.last_decision_reason == "none_pv_available"

    def test_low_bms_shortcut_xset_3500(self):
        """charge_adaptive + battery_charge_limit ≤ 7A → Xset 3500 (BMS clamp)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-5000,  # pv_avail 5000 (lookup → 6000)
                battery_charge_limit=2,  # low BMS
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        # Low BMS shortcut wins — Xset 3500 zamiast lookup 6000
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 3500
        assert "low_bms" in mgr.last_decision_reason

    def test_low_bms_boundary_7_uses_shortcut(self):
        """battery_charge_limit = 7 (threshold ≤) → low BMS shortcut."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-5000,
                battery_charge_limit=7,  # boundary
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 3500

    def test_low_bms_above_threshold_uses_lookup(self):
        """battery_charge_limit > 7 → normalna lookup."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-5000,
                battery_charge_limit=8,  # above threshold
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 6000  # lookup bucket pv_avail > 4000

    def test_low_bms_with_none_charge_limit_uses_lookup(self):
        """battery_charge_limit=None → defensive, use lookup (do not block manager)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-5000,
                battery_charge_limit=None,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 6000  # lookup, bez low_bms shortcut

    def test_hysteresis_stay_within_extended_range(self):
        """Stay at current Xset when pv_avail in extended range.

        Current Xset 5000 (range 3000-4000), pv_avail 2950 → extended
        (2700, 4300] → stay 5000 (zamiast lookup → 4000).
        """
        mgr = GridExportManager()
        # First update — selects 5000 from lookup (pv_avail=3500)
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3500,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 5000
        # Second update — pv_avail drops to 3050 (below 3000 threshold? No, 3050>3000).
        # Without hysteresis: lookup → 5000 too. But I want the test to check stay.
        # Edge case: pv_avail 2950 (below threshold, lookup → 4000),
        # but in extended range (2700, 4300] for current=5000.
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-2950,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 5000  # stay (extended range)
        assert "stay" in mgr.last_decision_reason

    def test_hysteresis_drop_when_outside_extended_range(self):
        """Drop to lookup when pv_avail leaves extended range.

        Current Xset 5000, pv_avail 2600 → poza extended (2700, 4300]
        → lookup wybierze 4000.
        """
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3500,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 5000
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-2600,  # pv_avail = 2600 < 2700
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 4000  # drop (lookup, pv_avail > 2000)

    def test_hysteresis_upgrade_when_above_extended_range(self):
        """Hysteresis: current Xset 5000, pv_avail 4400 → above extended → upgrade to 6000."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3500,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 5000
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-4400,  # pv_avail = 4400 > 4300
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 6000  # upgrade (lookup)

    def test_hysteresis_first_tick_no_hysteresis(self):
        """First tick (current_xset=None — transition from auto) → plain lookup."""
        mgr = GridExportManager()
        # Manager in auto, recommended_xset=None
        assert mgr.recommended_xset is None
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3500,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        # Plain lookup (bez hysteresis na pierwszym ticku)
        assert mgr.recommended_xset == 5000
        assert "stay" not in mgr.last_decision_reason

    def test_pv_low_standby_overrides_charge_adaptive(self):
        """pv_power_avg_2_minutes < 200 → STANDBY priority nad charge_adaptive."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=EVENING,
                exported_energy_hourly=0.10,
                pv_power=50,
                consumption_minus_pv_2_minutes=-3000,  # pv_avail 3000 BUT pv<200
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        # PV<200 → STANDBY (krok 1 ma priority nad charge_adaptive)
        assert mgr.recommended_ems_mode == "discharge_battery"


class TestIdempotency:
    """Output stability on repeated update.

    Two updates with the same state — recommended_* stable, reason may change
    from 'entry_*' to 'stay_*' (that is OK semantically).
    """

    def test_two_updates_same_state_keeps_outputs(self):
        mgr = GridExportManager()
        s = _state(
            exported_energy_hourly=0.10,
        )
        mgr.update(s)
        snapshot = (
            mgr.intervention_active,
            mgr.recommended_ems_mode,
            mgr.recommended_xset,
        )
        mgr.update(s)
        assert (
            mgr.intervention_active,
            mgr.recommended_ems_mode,
            mgr.recommended_xset,
        ) == snapshot
        # last_decision_reason changes from "charge_adaptive_*" to
        # "charge_adaptive_stay_*" (hysteresis) — OK.
        assert "charge_adaptive_stay_" in mgr.last_decision_reason


# ============ NEGATIVE balance tests ============


class TestNegativeEntry:
    """Entry NEGATIVE — time-dependent threshold + feasibility gates."""

    def test_entry_pre45_below_005(self):
        """Pre-45min: hourly < -0.05 → entry."""
        mgr = GridExportManager()
        early = datetime(2026, 4, 16, 12, 30, 0, tzinfo=TIMEZONE)
        mgr.update(_state(now=early, exported_energy_hourly=-0.06))
        assert mgr.intervention_active is True
        assert mgr.intervention_direction is InterventionDirection.NEGATIVE

    def test_no_entry_pre45_above_005(self):
        """Pre-45min: hourly > -0.05 → no entry."""
        mgr = GridExportManager()
        early = datetime(2026, 4, 16, 12, 30, 0, tzinfo=TIMEZONE)
        mgr.update(_state(now=early, exported_energy_hourly=-0.04))
        assert mgr.intervention_active is False

    def test_entry_post45_below_zero(self):
        """Post-45min: hourly < 0 → entry (any negative)."""
        mgr = GridExportManager()
        late = datetime(2026, 4, 16, 12, 50, 0, tzinfo=TIMEZONE)
        mgr.update(_state(now=late, exported_energy_hourly=-0.02))
        assert mgr.intervention_active is True
        assert mgr.intervention_direction is InterventionDirection.NEGATIVE

    def test_no_entry_post45_zero_or_positive(self):
        """Post-45min: hourly ≥ 0 → no entry."""
        mgr = GridExportManager()
        late = datetime(2026, 4, 16, 12, 50, 0, tzinfo=TIMEZONE)
        mgr.update(_state(now=late, exported_energy_hourly=0.0))
        assert mgr.intervention_active is False

    def test_no_entry_soc_below_hard_floor(self):
        """SoC ≤ 10 → no entry."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10, battery_soc=10))
        assert mgr.intervention_active is False
        assert "soc_below_hard_floor" in mgr.last_decision_reason

    def test_no_entry_dod_none(self):
        """depth_of_discharge=None → no entry."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10, depth_of_discharge=None))
        assert mgr.intervention_active is False

    def test_no_entry_consumption_minus_pv_none(self):
        """consumption_minus_pv_2_minutes=None → no entry."""
        mgr = GridExportManager()
        mgr.update(
            _state(exported_energy_hourly=-0.10, consumption_minus_pv_2_minutes=None)
        )
        assert mgr.intervention_active is False

    def test_no_entry_other_automation_active(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                other_ems_automation_active_this_hour=True,
            )
        )
        assert mgr.intervention_active is False

    def test_no_entry_late_hour(self):
        mgr = GridExportManager()
        late = datetime(2026, 4, 16, 12, 59, 50, tzinfo=TIMEZONE)
        mgr.update(_state(now=late, exported_energy_hourly=-0.10))
        assert mgr.intervention_active is False

    def test_no_entry_discharge_bucket_at_dod_floor_no_surplus(self):
        """SoC=floor + pv_available < 0 (deficit) → entry blocked.

        No PV surplus to redirect; AUTO/load-following more efficient than
        STOP intervention. Strict pv_available < 0 entry block.
        """
        mgr = GridExportManager()
        # consumption_minus_pv = +500 (deficit) → pv_avail = -500 → bucket discharge
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=500,
                battery_soc=22,  # = 100 - DoD (78)
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is False
        assert "soc_at_dod_floor_no_pv_surplus" in mgr.last_decision_reason

    def test_entry_at_dod_floor_with_pv_surplus_clamps_to_stop(self):
        """SoC=floor + pv_available >= 0 → entry allowed, discharge clamps to STOP.

        PV surplus redirects to grid as export, helping NEGATIVE balance
        recovery even though battery cannot discharge further.
        """
        mgr = GridExportManager()
        # consumption_minus_pv = -500 (surplus) → pv_avail = 500 → bucket discharge -1000
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-500,
                battery_soc=22,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0  # clamped to STOP
        assert "negative_stop_xset_0" in mgr.last_decision_reason

    def test_entry_at_dod_floor_with_mild_deficit_blocks(self):
        """SoC=floor + pv_available = -100 (mild deficit) → entry STILL blocked.

        Entry uses strict pv_available < 0 threshold (asymmetric with
        continue's -200W hysteresis).
        """
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=100,  # pv_avail = -100
                battery_soc=22,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is False
        assert "soc_at_dod_floor_no_pv_surplus" in mgr.last_decision_reason

    def test_continue_at_dod_floor_mild_deficit_keeps_stop(self):
        """In intervention, SoC drops to floor + pv_available in (-200, 0] → keep STOP.

        Hysteresis prevents flap when pv_available oscillates near zero with
        deficit balance.
        """
        mgr = GridExportManager()
        # Enter at SoC=50 with PV surplus
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-500,
                battery_soc=50,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is True
        # SoC drops to floor, PV mild deficit (within -200W hysteresis)
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=100,  # pv_avail = -100
                battery_soc=22,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_xset == 0
        assert "negative_stop_xset_0" in mgr.last_decision_reason

    def test_continue_at_dod_floor_deep_deficit_exits(self):
        """In intervention, SoC=floor + pv_available < -200 → exit (deep deficit)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-500,
                battery_soc=50,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is True
        # SoC drops to floor, PV deep deficit (beyond -200W hysteresis)
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=300,  # pv_avail = -300
                battery_soc=22,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "soc_at_dod_floor_exit"

    def test_entry_charge_bucket_at_soc_ceiling(self):
        """Bucket charge (pv_avail > 1000) + SoC = 100 → entry pozwolony, clamp do STOP."""
        mgr = GridExportManager()
        # pv_avail = 3000 → bucket charge xset 2000, but SoC=100 → clamp to 0
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-3000,  # pv_avail = 3000
                battery_soc=100,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.intervention_direction is InterventionDirection.NEGATIVE
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0  # clamped from charge to stop


class TestNegativeExit:
    """Exit NEGATIVE — feasibility loss / recovery / end_of_hour."""

    def test_exit_balance_recovered(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10))
        assert mgr.intervention_active is True
        # Recovery to positive balance
        mgr.update(_state(exported_energy_hourly=0.01))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "negative_balance_recovered"

    def test_exit_soc_at_dod_floor_during_discharge(self):
        """Bucket discharge + SoC drops to floor → exit."""
        mgr = GridExportManager()
        # Entry NEGATIVE with bucket discharge
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=500,  # pv_avail = -500 → discharge
                battery_soc=30,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is True
        # SoC drops to floor (22)
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=500,
                battery_soc=22,
                depth_of_discharge=78,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "soc_at_dod_floor_exit"

    def test_no_exit_soc_floor_during_charge_bucket(self):
        """Bucket charge + SoC=100 → clamp do STOP, NIE exit."""
        mgr = GridExportManager()
        # Entry charge bucket
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-3000,  # pv_avail = 3000 → charge
                battery_soc=80,
            )
        )
        assert mgr.intervention_active is True
        # SoC dochodzi do 100 — bucket charge + SoC=100 → clamp do STOP
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-3000,
                battery_soc=100,
            )
        )
        # Continue intervention (clamp), NIE exit
        assert mgr.intervention_active is True
        assert mgr.recommended_xset == 0  # clamped to stop bucket
        assert mgr.recommended_ems_mode == "discharge_battery"

    def test_exit_end_of_hour(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10))
        assert mgr.intervention_active is True
        # End of hour
        eoh = datetime(2026, 4, 16, 11, 59, 55, tzinfo=TIMEZONE)
        mgr.update(_state(now=eoh, exported_energy_hourly=-0.10))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "end_of_hour_cleanup"

    def test_exit_hour_rollover(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10))
        assert mgr.intervention_active is True
        # Hour rollover
        next_hour = datetime(2026, 4, 16, 12, 5, 0, tzinfo=TIMEZONE)
        mgr.update(_state(now=next_hour, exported_energy_hourly=-0.10))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "hour_rollover"


class TestNegativeAdaptiveBuckets:
    """Adaptive buckets — pv_avail → mode/xset (target +1500W eksport)."""

    def test_pv_above_5000_charge_4000(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-6000,  # pv_avail = 6000
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 4000

    def test_pv_4000_to_5000_charge_3000(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-4500,  # pv_avail = 4500
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 3000

    def test_pv_1000_to_2000_charge_zero_stop(self):
        """Bucket STOP — bateria stoi, eksport = pv_avail."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-1500,  # pv_avail = 1500
            )
        )
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0

    def test_pv_zero_to_1000_discharge_1000(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-500,  # pv_avail = 500
            )
        )
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 1000

    def test_pv_negative_discharge(self):
        """Deficit — bucket discharge."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=1500,  # pv_avail = -1500
            )
        )
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 3000  # bucket -2000..-1000 → discharge 3000

    def test_pv_below_minus_4000_cap(self):
        """Deepest bucket — discharge cap 6000W (BMS max)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=5000,  # pv_avail = -5000
            )
        )
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 6000

    def test_hysteresis_stay_in_extended_range(self):
        """Bucket stable when pv_avail is within ±300W of bucket boundary."""
        mgr = GridExportManager()
        # Entry: pv_avail = 4500 → charge 3000
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-4500,
            )
        )
        assert mgr.recommended_xset == 3000
        # pv_avail jumps to 5100 — within extended range (5000-300 < pv_avail <= +inf)
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-5100,
            )
        )
        # Hysteresis: range bucketu (4000, 5000), extended (3700, 5300]
        # pv=5100 < 5300 → stay
        assert mgr.recommended_xset == 3000
        assert "stay" in mgr.last_decision_reason


class TestInterventionDirection:
    """Public API get_active_intervention()."""

    def test_idle_returns_none(self):
        mgr = GridExportManager()
        assert mgr.get_active_intervention() is None

    def test_positive_returns_positive(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10))
        assert mgr.intervention_active is True
        assert mgr.get_active_intervention() is InterventionDirection.POSITIVE

    def test_negative_returns_negative(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10))
        assert mgr.intervention_active is True
        assert mgr.get_active_intervention() is InterventionDirection.NEGATIVE

    def test_after_exit_returns_none(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10))
        mgr.update(_state(exported_energy_hourly=0.01))
        assert mgr.intervention_active is False
        assert mgr.get_active_intervention() is None


class TestNegativeInPreCharge:
    """NEGATIVE also works in pre_charge window (POSITIVE skips)."""

    def test_negative_entry_in_pre_charge_with_soc(self):
        """Pre_charge + hourly < -0.05 + SoC > min_soc → NEGATIVE entry pozwolony."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=PRE_CHARGE,  # 08:00 < start_charge=10:00
                exported_energy_hourly=-0.10,
                battery_soc=80,  # > min_soc (22)
                consumption_minus_pv_2_minutes=500,  # pv_avail=-500 → discharge bucket
            )
        )
        assert mgr.intervention_active is True
        assert mgr.intervention_direction is InterventionDirection.NEGATIVE

    def test_positive_blocked_in_pre_charge(self):
        """Pre_charge + hourly > 0.06 → POSITIVE blocked (BatteryManager rules)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=PRE_CHARGE,
                exported_energy_hourly=0.10,
            )
        )
        assert mgr.intervention_active is False
        assert "in_pre_charge_window" in mgr.last_decision_reason


class TestChargeToggleClamp:
    """Toggle off → bucket charge clamp do STOP."""

    def test_charge_toggle_off_clamps_to_stop(self):
        """Bucket charge (xset>0) + toggle=False → clamp xset=0."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=-3000,  # pv_avail=3000 → bucket charge xset 2000
                battery_charge_toggle_on=False,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0  # clamped

    def test_discharge_bucket_unaffected_by_toggle(self):
        """Bucket discharge is not clamped by toggle (toggle applies to charge)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                consumption_minus_pv_2_minutes=500,  # pv_avail=-500 → discharge 2000
                battery_charge_toggle_on=False,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 2000  # not clamped


class TestEmsOverride:
    """ems_allow_discharge_override → blokuje TYLKO NEGATIVE (POSITIVE OK).

    User wymusza discharge → manager nie ingeruje w NEGATIVE intervention
    (which conflicts with discharge intent). POSITIVE force charge is still OK
    (increases SoC, unrelated to user discharge intent).
    """

    def test_override_blocks_negative_entry(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                ems_allow_discharge_override=True,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert "ems_allow_discharge_override" in mgr.last_decision_reason

    def test_override_blocks_positive_entry(self):
        """POSITIVE entry blocked by override (parity with battery + negative).

        User forces discharge via ems_allow_discharge_override → smart_rce
        does not interfere, even when hourly export positive (DISCHARGE_BATTERY in
        in progress → naturally export rises, but that is intentional).
        """
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                ems_allow_discharge_override=True,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert "ems_allow_discharge_override" in mgr.last_decision_reason

    def test_override_during_active_positive_exits(self):
        """Override activates while POSITIVE intervention active → exit."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10))
        assert mgr.intervention_active is True
        assert mgr.intervention_direction is InterventionDirection.POSITIVE
        # Override on
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                ems_allow_discharge_override=True,
            )
        )
        assert mgr.intervention_active is False
        assert "ems_allow_discharge_override" in mgr.last_decision_reason

    def test_override_during_active_negative_exits(self):
        """Override activates while NEGATIVE intervention active → exit."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=-0.10))
        assert mgr.intervention_active is True
        assert mgr.intervention_direction is InterventionDirection.NEGATIVE
        # Override on
        mgr.update(
            _state(
                exported_energy_hourly=-0.10,
                ems_allow_discharge_override=True,
            )
        )
        assert mgr.intervention_active is False
        assert "ems_allow_discharge_override" in mgr.last_decision_reason
