from datetime import datetime

from custom_components.smart_rce.domain.ems import InputState, WaterHeaterManager
from custom_components.smart_rce.domain.rce import TIMEZONE

NOON = datetime(2026, 4, 16, 12, 0, tzinfo=TIMEZONE)


def _state(
    *,
    big_on=False,
    small_on=False,
    battery_soc=50.0,
    battery_charge_limit=18.0,
    battery_power_2_minutes=0.0,
    consumption_minus_pv=-3000.0,
    exported_energy_hourly=0.5,
    heater_mode="WASTED",
    depth_of_discharge=None,
    now=NOON,
) -> InputState:
    return InputState(
        water_heater_big_is_on=big_on,
        water_heater_small_is_on=small_on,
        battery_soc=battery_soc,
        battery_charge_limit=battery_charge_limit,
        battery_power_2_minutes=battery_power_2_minutes,
        consumption_minus_pv_2_minutes=consumption_minus_pv,
        exported_energy_hourly=exported_energy_hourly,
        heater_mode=heater_mode,
        depth_of_discharge=depth_of_discharge,
        now=now,
    )


class TestHourlyBalanceGuard:
    """Guard: ochrona bilansu godzinowego gdy DoD=0%."""

    def test_dod_zero_negative_balance_forces_both_off(self):
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
                heater_mode="ASAP",
            )
        )
        assert mgr.should_turn_on is False
        assert mgr.should_turn_off is True
        assert mgr.should_turn_on_small is False
        assert mgr.should_turn_off_small is True
        assert mgr.should_block_battery_charge is True

    def test_dod_zero_hysteresis_keeps_heaters_off(self):
        mgr = WaterHeaterManager()
        # Najpierw ujemny bilans
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.01,
            )
        )
        assert mgr.should_block_battery_charge is True

        # Bilans lekko dodatni (+40 Wh) ale poniżej progu 50 Wh
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=0.04,
            )
        )
        assert mgr.should_turn_on is False
        assert mgr.should_turn_off is True
        assert mgr.should_block_battery_charge is True

    def test_dod_zero_release_after_threshold(self):
        mgr = WaterHeaterManager()
        # Ujemny bilans
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.03,
            )
        )
        assert mgr.should_block_battery_charge is True

        # Recovery powyżej 50 Wh (0.06 kWh = 60 Wh)
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=0.06,
                consumption_minus_pv=-3000.0,
            )
        )
        assert mgr.should_block_battery_charge is False
        # Normalna logika WASTED wraca — z 3kW PV i 18A charge limit
        # pv_surplus = 3000 - 18*290 = 3000 - 5220 = -2220 → BOTH_ARE_OFF
        assert mgr.should_turn_on is False

    def test_dod_greater_than_zero_guard_inactive(self):
        """Gdy DoD > 0%, guard nie działa — istniejąca automatyzacja HA obsługuje."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=80,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
                heater_mode="ASAP",
            )
        )
        # Guard nie aktywny, normalna logika ASAP z 5kW PV
        assert mgr.should_block_battery_charge is False
        assert mgr.should_turn_on is True  # ASAP: 5000 > 3300

    def test_dod_none_guard_inactive(self):
        """Gdy sensor unavailable (depth_of_discharge=None), guard nie działa."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=None,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
                heater_mode="ASAP",
            )
        )
        assert mgr.should_block_battery_charge is False
        assert mgr.should_turn_on is True

    def test_dod_change_from_zero_resets_flag(self):
        """Zmiana DoD z 0% na >0% resetuje _hourly_balance_negative."""
        mgr = WaterHeaterManager()
        # Guard aktywny, flaga ustawiona
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
            )
        )
        assert mgr._hourly_balance_negative is True
        assert mgr.should_block_battery_charge is True

        # DoD zmienia się na 90% — flaga musi się zresetować
        mgr.update(
            _state(
                depth_of_discharge=90,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
                heater_mode="ASAP",
            )
        )
        assert mgr._hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False
        assert mgr.should_turn_on is True

    def test_guard_overrides_soc_90_export_logic(self):
        """Nawet przy SOC≥90 i dużym PV, ujemny bilans + DoD=0% wygrywa."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                big_on=True,
                small_on=True,
                depth_of_discharge=0,
                battery_soc=95.0,
                battery_charge_limit=7.0,
                exported_energy_hourly=-0.01,
                consumption_minus_pv=-5000.0,
                heater_mode="ASAP",
            )
        )
        assert mgr.should_turn_on is False
        assert mgr.should_turn_off is True
        assert mgr.should_block_battery_charge is True

    def test_low_charge_limit_no_battery_block(self):
        """Gdy charge_limit < 2A (~500W), nie blokuj ładowania — za mało mocy."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                battery_charge_limit=1.5,
                heater_mode="ASAP",
            )
        )
        # Grzałki off (guard działa), ale battery charge NIE blokowany
        assert mgr.should_turn_on is False
        assert mgr.should_turn_off is True
        assert mgr.should_block_battery_charge is False

    def test_initial_state(self):
        mgr = WaterHeaterManager()
        assert mgr.should_block_battery_charge is False
        assert mgr._hourly_balance_negative is False

    def test_positive_balance_no_interference(self):
        """Przy dodatnim bilansie guard nie przeszkadza normalnej logice."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=0.5,
                consumption_minus_pv=-5000.0,
                battery_charge_limit=0.0,
                heater_mode="ASAP",
            )
        )
        assert mgr.should_block_battery_charge is False
        # ASAP z battery_full=True: 5000 > 4500 → BOTH_ARE_ON
        assert mgr.should_turn_on is True
        assert mgr.should_turn_on_small is True


class TestBalancedBaseline:
    """Piętro 1 — baseline z rezerwacją dla baterii."""

    def test_18a_low_soc_small(self):
        """pv=5500, charge_limit=18A, soc=30% → reserved=3000, budget=2500 → SMALL."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on is False
        assert mgr.should_turn_on_small is True
        assert mgr.balanced_baseline == "small_is_on"
        assert mgr.balanced_heater_budget == 2500.0

    def test_18a_low_soc_big(self):
        """pv=7000, charge_limit=18A, soc=30% → reserved=3000, budget=4000 → BIG."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-7000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on is True
        assert mgr.should_turn_on_small is False
        assert mgr.balanced_baseline == "big_is_on"

    def test_18a_low_soc_both(self):
        """pv=8000, charge_limit=18A, soc=30% → reserved=3000, budget=5000 → BOTH."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-8000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on is True
        assert mgr.should_turn_on_small is True
        assert mgr.balanced_baseline == "both_are_on"

    def test_18a_high_soc_big(self):
        """pv=5500, charge_limit=18A, soc=70% → reserved=2000, budget=3500 → BIG."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=70.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on is True
        assert mgr.balanced_baseline == "big_is_on"

    def test_7a_small(self):
        """pv=3000, charge_limit=7A, soc=95% → reserved=1000, budget=2000 → SMALL."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=95.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on_small is True
        assert mgr.should_turn_on is False
        assert mgr.balanced_baseline == "small_is_on"
        assert mgr.balanced_heater_budget == 2000.0

    def test_2a_reserved_300(self):
        """pv=2000, charge_limit=2A → reserved=300, budget=1700 → SMALL."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-2000.0,
                battery_charge_limit=2.0,
                battery_soc=98.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on_small is True
        assert mgr.balanced_heater_budget == 1700.0

    def test_1a_no_reservation(self):
        """pv=2000, charge_limit=1A → reserved=0, budget=2000 → SMALL."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-2000.0,
                battery_charge_limit=1.0,
                battery_soc=99.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on_small is True
        assert mgr.balanced_heater_budget == 2000.0

    def test_low_pv_off(self):
        """pv=1200, charge_limit=18A, soc=30% → reserved=3000, budget=-1800 → OFF."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1200.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.should_turn_on is False
        assert mgr.should_turn_on_small is False
        assert mgr.balanced_baseline == "both_are_off"
        assert mgr.balanced_heater_budget == -1800.0

    def test_hysteresis_holds_current_state(self):
        """Histereza trzyma obecny stan na granicy progu."""
        mgr = WaterHeaterManager()
        # SMALL jest włączona
        mgr.update(
            _state(
                heater_mode="BALANCED",
                small_on=True,
                consumption_minus_pv=-2400.0,  # budget=2400-1000=1400 < 1500
                battery_charge_limit=7.0,
                battery_soc=95.0,
                exported_energy_hourly=0.0,
            )
        )
        # 1400 < 1500 ale >= 1500-500=1000, current==SMALL → trzymaj SMALL
        assert mgr.should_turn_on_small is True
        assert mgr.balanced_baseline == "small_is_on"

    def test_hysteresis_does_not_hold_higher_state(self):
        """Histereza NIE trzyma wyższego stanu."""
        mgr = WaterHeaterManager()
        # BIG jest włączona, ale budget na SMALL
        mgr.update(
            _state(
                heater_mode="BALANCED",
                big_on=True,
                consumption_minus_pv=-2400.0,  # budget=1400
                battery_charge_limit=7.0,
                battery_soc=95.0,
                exported_energy_hourly=0.0,
            )
        )
        # 1400 < 1500, current==BIG ale histereza BIG wymaga current==BIG i budget>=2500
        # → OFF (bo 1400 < 1500-500=1000? NIE, 1400 >= 1000)
        # Wait: 1400 >= 1000 i current==SMALL? NIE current==BIG
        # → OFF
        assert mgr.balanced_baseline == "both_are_off"


class TestBalancedUpgrade:
    """Piętro 2 — upgrade z budżetu eksportu godzinowego."""

    def test_upgrade_off_to_small(self):
        """baseline=OFF, exported=120Wh → upgrade SMALL."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1200.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,  # 120 Wh
            )
        )
        assert mgr.balanced_baseline == "both_are_off"
        assert mgr.balanced_upgrade_active is True
        assert mgr.should_turn_on_small is True

    def test_upgrade_small_to_big(self):
        """baseline=SMALL, exported=120Wh → upgrade BIG."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,
            )
        )
        assert mgr.balanced_baseline == "small_is_on"
        assert mgr.balanced_upgrade_active is True
        assert mgr.should_turn_on is True  # BIG

    def test_upgrade_big_to_both(self):
        """baseline=BIG, exported=120Wh → upgrade BOTH."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-7000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,
            )
        )
        assert mgr.balanced_baseline == "big_is_on"
        assert mgr.balanced_upgrade_active is True
        assert mgr.should_turn_on is True
        assert mgr.should_turn_on_small is True  # BOTH

    def test_upgrade_both_stays_both(self):
        """baseline=BOTH, exported=120Wh → BOTH (max, no upgrade)."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-8000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,
            )
        )
        assert mgr.balanced_baseline == "both_are_on"
        assert mgr.balanced_upgrade_active is False

    def test_no_upgrade_below_threshold(self):
        """baseline=SMALL, exported=80Wh → za mało na upgrade."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.08,
            )
        )
        assert mgr.balanced_baseline == "small_is_on"
        assert mgr.balanced_upgrade_active is False

    def test_upgrade_hysteresis_holds(self):
        """Upgrade aktywny, exported=50Wh → trzymaj (>30)."""
        mgr = WaterHeaterManager()
        # Najpierw aktywuj upgrade (baseline=SMALL → BIG)
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5300.0,  # budget=2300 → baseline SMALL
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,
            )
        )
        assert mgr.balanced_baseline == "small_is_on"
        assert mgr.balanced_upgrade_active is True
        # Teraz exported spada do 50Wh, BIG jest włączony
        mgr.update(
            _state(
                heater_mode="BALANCED",
                big_on=True,
                consumption_minus_pv=-5300.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.05,  # 50 Wh > 30
            )
        )
        assert mgr.balanced_baseline == "small_is_on"
        assert mgr.balanced_upgrade_active is True

    def test_upgrade_hysteresis_releases(self):
        """Exported=20Wh → powrót do baseline (<30)."""
        mgr = WaterHeaterManager()
        # Aktywuj upgrade
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5300.0,  # budget=2300 → baseline SMALL
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,
            )
        )
        assert mgr.balanced_upgrade_active is True
        # Exported spada poniżej 30Wh
        mgr.update(
            _state(
                heater_mode="BALANCED",
                big_on=True,
                consumption_minus_pv=-5300.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.02,  # 20 Wh < 30
            )
        )
        assert mgr.balanced_upgrade_active is False
        assert mgr.balanced_baseline == "small_is_on"


class TestBalancedOverrideAndDiagnostics:
    """Override SOC≥90 nie odpala dla BALANCED + diagnostyka."""

    def test_no_soc90_override(self):
        """mode=BALANCED, soc=95, exported=400Wh → override NIE odpala."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1200.0,
                battery_charge_limit=18.0,
                battery_soc=95.0,
                exported_energy_hourly=0.4,  # 400 Wh
            )
        )
        # budget = 1200 - 2000 = -800 → baseline OFF
        # upgrade: OFF → SMALL (exported 400 > 100)
        assert mgr.balanced_baseline == "both_are_off"
        assert mgr.balanced_upgrade_active is True
        # Override SOC≥90 would force BIG, but in BALANCED it doesn't
        assert mgr.should_turn_on is False  # nie BIG
        assert mgr.should_turn_on_small is True  # SMALL z upgrade

    def test_diagnostics_none_in_wasted_mode(self):
        """Diagnostyka BALANCED = None/False gdy tryb WASTED."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="WASTED",
                consumption_minus_pv=-6000.0,
                battery_charge_limit=18.0,
                exported_energy_hourly=0.5,
            )
        )
        assert mgr.balanced_heater_budget is None
        assert mgr.balanced_baseline is None
        assert mgr.balanced_upgrade_active is False

    def test_diagnostics_none_when_guard_active(self):
        """Diagnostyka = None gdy guard DoD=0% aktywny."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
            )
        )
        assert mgr.balanced_heater_budget is None
        assert mgr.balanced_baseline is None
        assert mgr.balanced_upgrade_active is False

    def test_guard_works_with_balanced(self):
        """Guard DoD=0% działa z BALANCED."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                heater_mode="BALANCED",
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
            )
        )
        assert mgr.should_turn_on is False
        assert mgr.should_turn_off is True


class TestGuardTimeWindow:
    """Guard aktywny tylko w godzinach PV (< GUARD_END_HOUR=17)."""

    def _at(self, hour: int) -> datetime:
        return datetime(2026, 4, 16, hour, 0, tzinfo=TIMEZONE)

    def test_guard_active_at_16(self):
        """16:00 — guard aktywny, blokada ładowania."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                heater_mode="ASAP",
                now=self._at(16),
            )
        )
        assert mgr.should_block_battery_charge is True
        assert mgr._hourly_balance_negative is True

    def test_guard_disabled_at_17(self):
        """17:00 — guard wyłączony, brak blokady nawet przy ujemnym eksporcie."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                heater_mode="ASAP",
                now=self._at(17),
            )
        )
        assert mgr.should_block_battery_charge is False
        assert mgr._hourly_balance_negative is False

    def test_guard_disabled_at_23(self):
        """23:00 — tanie godziny RCE, ładowanie z sieci nie powinno być blokowane."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.5,  # znaczny pobór z sieci
                consumption_minus_pv=-3000.0,
                heater_mode="ASAP",
                now=self._at(23),
            )
        )
        assert mgr.should_block_battery_charge is False

    def test_guard_active_at_midnight(self):
        """0:00 — brak dolnej granicy, guard nadal aktywny (user wybrał only-end)."""
        mgr = WaterHeaterManager()
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                heater_mode="ASAP",
                now=self._at(0),
            )
        )
        assert mgr.should_block_battery_charge is True

    def test_flag_resets_when_window_closes(self):
        """Flaga _hourly_balance_negative zeruje się po przekroczeniu GUARD_END_HOUR."""
        mgr = WaterHeaterManager()
        # Najpierw guard aktywny o 16:00, flaga ustawiona
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.03,
                heater_mode="ASAP",
                now=self._at(16),
            )
        )
        assert mgr._hourly_balance_negative is True
        assert mgr.should_block_battery_charge is True

        # Godzina 17 — okno zamknięte, flaga musi się zresetować
        mgr.update(
            _state(
                depth_of_discharge=0,
                exported_energy_hourly=-0.03,
                heater_mode="ASAP",
                now=self._at(17),
            )
        )
        assert mgr._hourly_balance_negative is False
        assert mgr.should_block_battery_charge is False
