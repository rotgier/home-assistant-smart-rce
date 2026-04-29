"""Tests for GridExportManager — POSITIVE balance state machine.

Strategie: BATTERY_STANDBY (PV<200W), CHARGE_BATTERY 6000W, BUY_POWER 1500W.
Active window: post_charge → next day 7:00 (skip pre_charge).

Konwencja sensorów (mletenay/Goodwe):
- battery_power: ujemne = charging
- meter_active_power_total: ujemne = import
- *_avg_27s = max wartość w 18s (dla wartości ujemnych = NAJMNIEJ INTENSYWNE)

W _apply_strategy konwertujemy na dodatnie:
- battery_charging_avg_27s = -battery_power_avg_27s
- meter_import_avg_27s = -meter_active_power_total_avg_27s
"""

from datetime import datetime, time

from custom_components.smart_rce.domain.grid_export import GridExportManager
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
    pv_power_avg_2_minutes: float | None = None,  # None → fallback do pv_power
    battery_charge_limit: float | None = 18.0,  # high BMS
    battery_power_avg_27s: float | None = -3000.0,  # charging 3kW
    meter_active_power_total_avg_27s: float | None = -1000.0,  # import 1kW
    start_charge_hour_override: time | None = time(10, 0),
    other_ems_automation_active_this_hour: bool | None = False,
    grid_export_strategy_mode: str
    | None = "all",  # default "all" w testach (full state machine)
) -> InputState:
    return InputState(
        now=now,
        exported_energy_hourly=exported_energy_hourly,
        battery_soc=battery_soc,
        battery_charge_toggle_on=battery_charge_toggle_on,
        pv_power=pv_power,
        pv_power_avg_2_minutes=pv_power_avg_2_minutes,
        battery_charge_limit=battery_charge_limit,
        battery_power_avg_27s=battery_power_avg_27s,
        meter_active_power_total_avg_27s=meter_active_power_total_avg_27s,
        start_charge_hour_override=start_charge_hour_override,
        other_ems_automation_active_this_hour=other_ems_automation_active_this_hour,
        grid_export_strategy_mode=grid_export_strategy_mode,
    )


class TestEntryStrategy:
    """Wybór strategii przy wejściu w intervention (high BMS branch)."""

    def test_entry_charge_battery_after_intense_charging(self):
        """battery_charging_avg_27s > 2.5 kW → CHARGE_BATTERY 6000."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,  # 3 kW sustained charging > 2.5 kW
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 6000
        assert mgr.last_decision_reason == "entry_charge_intense_charging"

    def test_entry_buy_power_default(self):
        """battery_charging_avg_27s ≤ 2.5 kW → BUY_POWER 1500."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-1000,  # 1 kW charging, below threshold
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "buy_power"
        assert mgr.recommended_xset == 1500
        assert mgr.last_decision_reason == "entry_buy_power_default"

    def test_standby_when_pv_low(self):
        """PV<200W → STANDBY niezależnie od BMS branch.

        pv_power_avg_2_minutes=None → fallback do chwilowego pv_power.
        """
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=EVENING,
                exported_energy_hourly=0.10,
                pv_power=50,
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "battery_standby"
        assert mgr.recommended_xset is None
        assert mgr.last_decision_reason == "low_pv_standby"

    def test_standby_uses_avg_not_instantaneous(self):
        """Transient spike-down — chwilowy pv<200, ale avg_2min>200 → CHARGE."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                pv_power=50,  # chwilowy spike-down (inwerter "przymulił")
                pv_power_avg_2_minutes=2500,  # rzeczywisty PV stable
                battery_power_avg_27s=-3000,  # entry CHARGE
            )
        )
        # Manager używa avg_2min (2500W >= 200W) → CHARGE_BATTERY (NIE STANDBY)
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"

    def test_standby_avg_below_threshold(self):
        """Sustained low PV — avg_2min<200, chwilowy może być wyżej → STANDBY."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=EVENING,
                exported_energy_hourly=0.10,
                pv_power=300,  # chwilowy spike-up (zachód słońca, ale stabilnie niskie)
                pv_power_avg_2_minutes=80,  # mean stabilnie niskie
            )
        )
        assert mgr.recommended_ems_mode == "battery_standby"
        assert mgr.last_decision_reason == "low_pv_standby"

    def test_low_bms_charge_branch(self):
        """battery_charge_limit ≤ 7 → CHARGE 6000 (BMS sam ograniczy)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_charge_limit=2,
                battery_power_avg_27s=-500,  # nawet niskie charging
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 6000
        assert mgr.last_decision_reason == "low_bms_charge"


class TestStateMachineSwitches:
    """Switching CHARGE↔BUY w trakcie intervention."""

    def test_charge_to_buy_when_meter_aggressive_import(self):
        """CHARGE → BUY gdy meter_import_avg_27s > 3.9 kW."""
        mgr = GridExportManager()
        # Entry CHARGE
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,  # entry CHARGE
                meter_active_power_total_avg_27s=-1000,
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        # Meter import wzrasta — > 3.9 kW
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,
                meter_active_power_total_avg_27s=-4500,  # 4.5 kW import sustained
            )
        )
        assert mgr.recommended_ems_mode == "buy_power"
        assert mgr.recommended_xset == 1500
        assert mgr.last_decision_reason == "switch_charge_to_buy_meter_aggressive"

    def test_buy_to_charge_when_battery_near_bms_cap(self):
        """BUY → CHARGE gdy battery_charging_avg_27s > 4.9 kW."""
        mgr = GridExportManager()
        # Entry BUY
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-1000,  # entry BUY
                meter_active_power_total_avg_27s=-1500,
            )
        )
        assert mgr.recommended_ems_mode == "buy_power"
        # Bateria charging zbliża się do BMS cap
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-5100,  # 5.1 kW sustained charging
                meter_active_power_total_avg_27s=-1500,
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 6000
        assert mgr.last_decision_reason == "switch_buy_to_charge_near_bms_cap"

    def test_stay_charge_when_meter_below_threshold(self):
        """W CHARGE: meter_import_avg_27s ≤ 3.9 kW → stay CHARGE."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_power_avg_27s=-3000))
        assert mgr.recommended_ems_mode == "charge_battery"
        # Meter import 3 kW (poniżej progu 3.9)
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,
                meter_active_power_total_avg_27s=-3000,
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.last_decision_reason == "stay_charge_battery"

    def test_stay_buy_when_battery_below_cap(self):
        """W BUY: battery_charging_avg_27s ≤ 4.9 kW → stay BUY."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_power_avg_27s=-1000))
        assert mgr.recommended_ems_mode == "buy_power"
        # Bateria 4 kW (poniżej progu 4.9)
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-4000,
            )
        )
        assert mgr.recommended_ems_mode == "buy_power"
        assert mgr.last_decision_reason == "stay_buy_power"


class TestStrategyOverride:
    """STANDBY ma priorytet nad state machine, niezależnie od trybu."""

    def test_pv_drops_during_charge_switches_to_standby(self):
        """PV padnie poniżej 200W w trakcie CHARGE → STANDBY."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10, battery_power_avg_27s=-3000, pv_power=3000
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        # PV zniknęło
        mgr.update(
            _state(
                exported_energy_hourly=0.10, battery_power_avg_27s=-3000, pv_power=50
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "battery_standby"
        assert mgr.recommended_xset is None
        assert mgr.last_decision_reason == "low_pv_standby"


class TestEntryGates:
    """Gates blokujące entry."""

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
        assert mgr.last_decision_reason == "in_pre_charge_window"

    def test_balance_below_threshold(self):
        """Hourly ≤ 0.06 → no entry (entry threshold > 0.06)."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.05))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "balance_below_threshold"

    def test_balance_just_above_threshold(self):
        """Hourly = 0.061 → entry."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.061, battery_power_avg_27s=-3000))
        assert mgr.intervention_active is True

    def test_soc_at_ceiling(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_soc=100))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "soc_at_ceiling"

    def test_toggle_off(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_charge_toggle_on=False))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "toggle_off"

    def test_late_hour_blocks(self):
        mgr = GridExportManager()
        mgr.update(_state(now=LATE, exported_energy_hourly=0.10))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "too_late_in_hour"

    def test_late_hour_just_before_threshold_passes(self):
        """minute=59 AND second<40 → entry allowed."""
        mgr = GridExportManager()
        just_before = datetime(2026, 4, 16, 12, 59, 35, tzinfo=TIMEZONE)
        mgr.update(
            _state(
                now=just_before,
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,
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
        assert mgr.last_decision_reason == "other_automation_active"


class TestExitGates:
    """Wyjście z intervention przy spełnieniu warunków exit."""

    def test_exit_balance_recovered(self):
        """Hourly < 0.05 → exit."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_power_avg_27s=-3000))
        assert mgr.intervention_active is True
        mgr.update(_state(exported_energy_hourly=0.04, battery_power_avg_27s=-3000))
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.recommended_xset is None
        assert mgr.last_decision_reason == "balance_recovered"

    def test_exit_soc_ceiling(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10, battery_soc=99, battery_power_avg_27s=-3000
            )
        )
        assert mgr.intervention_active is True
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_soc=100,
                battery_power_avg_27s=-3000,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "soc_ceiling_exit"

    def test_exit_toggle_off(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_power_avg_27s=-3000))
        assert mgr.intervention_active is True
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_charge_toggle_on=False,
                battery_power_avg_27s=-3000,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "toggle_off_exit"

    def test_exit_end_of_hour(self):
        """End-of-hour cleanup mimo dalej positive balance.

        Entry musi być w tej samej godzinie co END_OF_HOUR (hour=12),
        inaczej first-of-hour rollover odpali wcześniej.
        """
        mgr = GridExportManager()
        mgr.update(
            _state(now=NOON, exported_energy_hourly=0.10, battery_power_avg_27s=-3000)
        )
        assert mgr.intervention_active is True
        mgr.update(
            _state(
                now=END_OF_HOUR,
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "end_of_hour_cleanup"

    def test_exit_hour_rollover(self):
        """Aktywne w hour=11, kolejny update hour=13 → exit hour_rollover."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=POST_CHARGE,
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,
            )
        )  # hour=11
        assert mgr.intervention_active is True
        assert mgr._intervention_started_hour == 11
        mgr.update(
            _state(
                now=NEXT_HOUR, exported_energy_hourly=0.10, battery_power_avg_27s=-3000
            )
        )  # hour=13
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.last_decision_reason == "hour_rollover"


class TestNonePresent:
    """No-op gdy brak wymaganych danych."""

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

    def test_none_battery_charge_limit(self):
        """battery_charge_limit=None → defensive no-op (waiting for sensor)."""
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_charge_limit=None))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "none_battery_charge_limit"

    def test_none_battery_power_avg_27s_in_high_bms_machine(self):
        mgr = GridExportManager()
        mgr.update(_state(exported_energy_hourly=0.10, battery_power_avg_27s=None))
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "none_present_high_bms_machine"

    def test_none_meter_avg_27s_in_high_bms_machine(self):
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                meter_active_power_total_avg_27s=None,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.last_decision_reason == "none_present_high_bms_machine"

    def test_low_bms_branch_does_not_need_avg_27s(self):
        """battery_charge_limit ≤ 7 → CHARGE 6000 nawet bez avg_27s sensors."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_charge_limit=2,
                battery_power_avg_27s=None,
                meter_active_power_total_avg_27s=None,
            )
        )
        # Low BMS branch nie wymaga avg_27s (CHARGE 6000 jako bezwarunkowy fallback)
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 6000
        assert mgr.last_decision_reason == "low_bms_charge"


class TestStrategyMode:
    """input_select.smart_rce_grid_export_strategy_mode kontroluje manager."""

    def test_disabled_intervention_off_diagnostic_in_reason(self):
        """Disabled → recommended=auto, intervention_active=False, reason ma would-be info."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,  # would-be: entry_charge_intense_charging
                grid_export_strategy_mode="disabled",
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.recommended_xset is None
        assert "disabled" in mgr.last_decision_reason
        assert "charge_battery" in mgr.last_decision_reason
        assert "6000W" in mgr.last_decision_reason

    def test_disabled_when_no_intervention_pure_diagnostic(self):
        """Disabled + balance below threshold → reason = 'disabled (balance_below_threshold)'."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.03,  # below entry threshold
                grid_export_strategy_mode="disabled",
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert mgr.last_decision_reason == "disabled (balance_below_threshold)"

    def test_charge_or_standby_high_pv_force_charge(self):
        """charge_or_standby + PV>=200 → CHARGE 6000 force (bez state machine)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-100,  # weak charging — w 'all' byłby BUY
                grid_export_strategy_mode="charge_or_standby",
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "charge_battery"
        assert mgr.recommended_xset == 6000
        assert mgr.last_decision_reason == "charge_or_standby_force_charge"

    def test_charge_or_standby_low_pv_standby(self):
        """charge_or_standby + PV<200 → STANDBY (priority)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                now=EVENING,
                exported_energy_hourly=0.10,
                pv_power=50,
                grid_export_strategy_mode="charge_or_standby",
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "battery_standby"
        assert mgr.recommended_xset is None
        assert mgr.last_decision_reason == "low_pv_standby"

    def test_all_full_state_machine(self):
        """All → pełny state machine (CHARGE↔BUY↔STANDBY)."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-100,  # weak charging → entry BUY
                grid_export_strategy_mode="all",
            )
        )
        assert mgr.intervention_active is True
        assert mgr.recommended_ems_mode == "buy_power"
        assert mgr.recommended_xset == 1500

    def test_none_strategy_mode_defaults_to_disabled(self):
        """grid_export_strategy_mode=None (helper niegotowy) → traktuj jak disabled."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                battery_power_avg_27s=-3000,
                grid_export_strategy_mode=None,
            )
        )
        assert mgr.intervention_active is False
        assert mgr.recommended_ems_mode == "auto"
        assert "no_strategy_mode" in mgr.last_decision_reason


class TestIdempotency:
    """Stabilność outputs przy powtórnym update.

    Dwa update'y z tym samym state — recommended_* stabilne, reason może się
    zmienić z 'entry_*' na 'stay_*' (to jest OK semantycznie).
    """

    def test_two_updates_same_state_keeps_outputs(self):
        mgr = GridExportManager()
        s = _state(exported_energy_hourly=0.10, battery_power_avg_27s=-3000)
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
        # last_decision_reason zmienia się z "entry_*" na "stay_*" — OK.
        assert mgr.last_decision_reason == "stay_charge_battery"
