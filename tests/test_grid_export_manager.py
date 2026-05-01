"""Tests for GridExportManager — POSITIVE balance handling.

Strategie: STANDBY (PV<200W), CHARGE_BATTERY adaptive (PV≥200W).
Active window: post_charge → next day 7:00 (skip pre_charge).
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
    consumption_minus_pv_2_minutes: float | None = -3000.0,  # surplus PV 3kW
    battery_charge_limit: float | None = 18.0,  # high BMS
    start_charge_hour_override: time | None = time(10, 0),
    other_ems_automation_active_this_hour: bool | None = False,
    grid_export_strategy_mode: str | None = "charge_adaptive",
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
        start_charge_hour_override=start_charge_hour_override,
        other_ems_automation_active_this_hour=other_ems_automation_active_this_hour,
        grid_export_strategy_mode=grid_export_strategy_mode,
    )


class TestStandby:
    """STANDBY entry/avg fallback (PV<200W → discharge_battery xset=0)."""

    def test_standby_when_pv_low(self):
        """PV<200W → STANDBY (pv_power_avg_2_minutes=None → fallback do pv_power)."""
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
        """Transient spike-down — chwilowy pv<200, ale avg_2min>200 → CHARGE."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                pv_power=50,  # chwilowy spike-down (inwerter "przymulił")
                pv_power_avg_2_minutes=2500,  # rzeczywisty PV stable
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
                pv_power=300,  # chwilowy spike-up
                pv_power_avg_2_minutes=80,  # mean stabilnie niskie
            )
        )
        assert mgr.recommended_ems_mode == "discharge_battery"
        assert mgr.recommended_xset == 0
        assert mgr.last_decision_reason == "low_pv_standby"


class TestStrategyOverride:
    """STANDBY ma priorytet nad charge_adaptive."""

    def test_pv_drops_during_charge_switches_to_standby(self):
        """PV padnie poniżej 200W w trakcie CHARGE → STANDBY."""
        mgr = GridExportManager()
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3000,  # surplus 3kW → entry CHARGE
                pv_power=3000,
            )
        )
        assert mgr.recommended_ems_mode == "charge_battery"
        # PV zniknęło
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
        mgr.update(
            _state(
                exported_energy_hourly=0.061,
            )
        )
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
                battery_soc=99,
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

        Entry musi być w tej samej godzinie co END_OF_HOUR (hour=12),
        inaczej first-of-hour rollover odpali wcześniej.
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
        """Aktywne w hour=11, kolejny update hour=13 → exit hour_rollover."""
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
        assert mgr.last_decision_reason == "none_consumption_minus_pv_2_minutes"


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

    pv_avail = -consumption_minus_pv_2_minutes, więc:
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
        """pv_avail między -1000 a 0 → Xset 1000."""
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
        """pv_avail ≤ -1000 → mode=AUTO ale intervention NADAL active.

        Manager NIE robi _set_neutral — żeby uniknąć flap entry/exit gdy
        hourly stale > 0.06. Listener cofa Goodwe do AUTO. Gdy pv_avail
        wzrośnie ponad -1000, manager znów wystawi charge_battery.
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
        """Próg ścisły `> 4000`. Wartość dokładnie 4000 → Xset 5000 (drugi bucket)."""
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
        assert mgr.last_decision_reason == "none_consumption_minus_pv_2_minutes"

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
        """battery_charge_limit = 7 (próg ≤) → low BMS shortcut."""
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
        """battery_charge_limit=None → defensive, użyj lookup (nie blokuj manager)."""
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
        """Stay przy current Xset gdy pv_avail w extended range.

        Current Xset 5000 (range 3000-4000), pv_avail 2950 → extended
        (2700, 4300] → stay 5000 (zamiast lookup → 4000).
        """
        mgr = GridExportManager()
        # Pierwszy update — wybierze 5000 z lookup (pv_avail=3500)
        mgr.update(
            _state(
                exported_energy_hourly=0.10,
                consumption_minus_pv_2_minutes=-3500,
                grid_export_strategy_mode="charge_adaptive",
            )
        )
        assert mgr.recommended_xset == 5000
        # Drugi update — pv_avail spada do 3050 (poniżej progu 3000? Nie, 3050>3000).
        # Bez hysteresis: lookup → 5000 też. Ale chcę żeby test sprawdzał stay.
        # Zrobię edge case: pv_avail 2950 (poniżej progu, lookup → 4000),
        # ale w extended range (2700, 4300] dla current=5000.
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
        """Drop do lookup gdy pv_avail wyjdzie poza extended range.

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
        """Hysteresis: current Xset 5000, pv_avail 4400 → powyżej extended → upgrade do 6000."""
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
        """Pierwszy tick (current_xset=None — przejście z auto) → plain lookup."""
        mgr = GridExportManager()
        # Manager w auto, recommended_xset=None
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
    """Stabilność outputs przy powtórnym update.

    Dwa update'y z tym samym state — recommended_* stabilne, reason może się
    zmienić z 'entry_*' na 'stay_*' (to jest OK semantycznie).
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
        # last_decision_reason zmienia się z "charge_adaptive_*" na
        # "charge_adaptive_stay_*" (hysteresis) — OK.
        assert "charge_adaptive_stay_" in mgr.last_decision_reason
