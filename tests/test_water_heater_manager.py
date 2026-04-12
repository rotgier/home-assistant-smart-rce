from custom_components.smart_rce.domain.ems import InputState, WaterHeaterManager


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
