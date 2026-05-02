from datetime import datetime

from custom_components.smart_rce.domain.ems import Ems
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE
import pytest

# TODO: upgrade tests + DoD-guard diagnostic test są strukturalnie out-of-date
# - skip_upgrade=True dla battery_charge_limit>7 (commit a799e97) — testy z cl=18
#   nie mogą obserwować upgrade activation, trzeba przepisać z cl≤7 + większy
#   exported_energy żeby budget+bonus przekroczył próg SMALL/BIG/BOTH
# - DoD=0% guard usunięty z water_heater w Etap 2 (logika przeniesiona do
#   GridExportManager) — test_diagnostics_none_when_guard_active testuje
#   nieistniejący guard
_OUT_OF_DATE = pytest.mark.skip(
    reason="TODO: rewrite for current logic (skip_upgrade if cl>7, no DoD guard)"
)

NOON = datetime(2026, 4, 16, 12, 0, tzinfo=TIMEZONE)
NOON_50 = datetime(2026, 4, 16, 12, 50, tzinfo=TIMEZONE)  # 10 min do końca godziny
NOON_55 = datetime(2026, 4, 16, 12, 55, tzinfo=TIMEZONE)  # 5 min do końca godziny
NOON_59_30 = datetime(2026, 4, 16, 12, 59, 30, tzinfo=TIMEZONE)  # 30s — cutoff aktywny


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
    water_heater_strategy=None,
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
        water_heater_strategy=water_heater_strategy,
        now=now,
    )


class TestBalancedBaseline:
    """Piętro 1 — baseline z rezerwacją dla baterii."""

    def test_18a_low_soc_small(self):
        """pv=5500, charge_limit=18A, soc=30% → reserved=3500, budget=2000 → SMALL."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_baseline == "small_is_on"
        # balanced_heater_budget = -heater_budget (sign flip dla wykresu spójnego
        # z battery_power — charging = negative)
        assert mgr.water_heater.balanced_heater_budget == -2000.0

    def test_18a_low_soc_big(self):
        """pv=7000, charge_limit=18A, soc=30% → reserved=3000, budget=4000 → BIG."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-7000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.balanced_baseline == "big_is_on"

    def test_18a_low_soc_both(self):
        """pv=8000, charge_limit=18A, soc=30% → reserved=3000, budget=5000 → BOTH."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-8000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_baseline == "both_are_on"

    def test_18a_high_soc_big(self):
        """pv=5500, charge_limit=18A, soc=70% → reserved=2000, budget=3500 → BIG."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=70.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.balanced_baseline == "big_is_on"

    def test_7a_small(self):
        """pv=3000, charge_limit=7A, soc=95% → reserved=1000, budget=2000 → SMALL."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=95.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.balanced_baseline == "small_is_on"
        assert mgr.water_heater.balanced_heater_budget == -2000.0

    def test_2a_reserved_300(self):
        """pv=2000, charge_limit=2A → reserved=300, budget=1700 → SMALL."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-2000.0,
                battery_charge_limit=2.0,
                battery_soc=98.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_heater_budget == -1700.0

    def test_0a_no_reservation(self):
        """pv=2000, charge_limit=0A → reserved=0, budget=2000 → SMALL."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-2000.0,
                battery_charge_limit=0.0,
                battery_soc=99.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_heater_budget == -2000.0

    def test_low_pv_off(self):
        """pv=1200, charge_limit=18A, soc=30% → reserved=3500, budget=-2300 → OFF."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1200.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        # heater_budget=-2300 → balanced_heater_budget = -(-2300) = +2300
        assert mgr.water_heater.balanced_heater_budget == 2300.0


class TestBalancedBatteryFirstStrategy:
    """BATTERY_FIRST: reserved=4500 gdy charge_limit>7 (bateria może mocno ładować)."""

    def test_battery_first_18a_reserves_4500_heaters_off(self):
        """pv=5500, charge_limit=18A, BATTERY_FIRST → reserved=4500, budget=1000 < SMALL → OFF."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="BATTERY_FIRST",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_heater_budget == -1000.0

    def test_battery_first_18a_big_pv_allows_small(self):
        """pv=6500, charge_limit=18A, BATTERY_FIRST → reserved=4500, budget=2000 ≥ SMALL."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="BATTERY_FIRST",
                consumption_minus_pv=-6500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.balanced_baseline == "small_is_on"

    def test_battery_first_fallback_when_charge_limit_drops(self):
        """BATTERY_FIRST + charge_limit=2A → fallback do normalnej logiki (reserved=300)."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="BATTERY_FIRST",
                consumption_minus_pv=-2000.0,
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.0,
            )
        )
        # charge_limit=2 nie jest >7 → BATTERY_FIRST się nie aktywuje →
        # reserved=300 → budget=1700 → SMALL
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_heater_budget == -1700.0

    def test_battery_first_fallback_at_charge_limit_7(self):
        """BATTERY_FIRST + charge_limit=7A → fallback (7>7 is False)."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="BATTERY_FIRST",
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=85.0,
                exported_energy_hourly=0.0,
            )
        )
        # charge_limit=7 nie jest >7 → normalna logika: reserved=1000 → budget=2000 → SMALL
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_heater_budget == -2000.0

    def test_normal_strategy_uses_existing_algorithm(self):
        """NORMAL (lub None) + charge_limit=18A, soc=30% → reserved=3500 (post-bffaf23)."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="NORMAL",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        # reserved=3500, budget=2000 → SMALL (jak w test_18a_low_soc_small)
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_baseline == "small_is_on"
        assert mgr.water_heater.balanced_heater_budget == -2000.0

    def test_none_strategy_uses_existing_algorithm(self):
        """strategy=None (stan po restarcie przed loadem) → istniejąca logika."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy=None,
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_baseline == "small_is_on"

    def test_battery_first_18a_skip_upgrade_despite_export(self):
        """BATTERY_FIRST + 18A + exported=150Wh → nie aktywuj upgrade (SMALL).

        Real scenario z 2026-04-20 13:54: PV akurat niewielki nadmiar,
        cumulative export narósł do 100 Wh w godzinie, a bateria wciąż
        ładuje się @ 18A. Upgrade wcześniej włączał SMALL ignorując strategy.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="BATTERY_FIRST",
                consumption_minus_pv=-5500.0,  # pv_available=5500
                battery_charge_limit=18.0,
                battery_soc=74.0,
                exported_energy_hourly=0.150,  # 150 Wh > próg 100
            )
        )
        # reserved=4500, budget=1000 < SMALL → baseline=BOTH_ARE_OFF
        # upgrade SKIP bo strategy=BATTERY_FIRST AND charge_limit>7
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_active is False

    def test_battery_first_2a_upgrade_works_when_battery_slowing(self):
        """BATTERY_FIRST + charge_limit=2A → fallback: upgrade działa normalnie.

        Gdy bateria zbliża się do pełna (charge_limit spadł z 18→2), strategy
        BATTERY_FIRST przestaje się aktywować, upgrade znów chroni przed
        marnowaniem eksportu.

        now=NOON_50 (10 min do końca): bonus = 150 Wh / (10/60 h) = 900 W
        → effective = 700 + 900 = 1600 W ≥ SMALL_POWER → upgrade SMALL.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="BATTERY_FIRST",
                consumption_minus_pv=-1000.0,  # pv_available=1000
                battery_charge_limit=2.0,
                battery_soc=98.0,
                exported_energy_hourly=0.150,
                now=NOON_50,
            )
        )
        # reserved=300 (charge_limit=2), budget=700 < SMALL → baseline=BOTH_ARE_OFF
        # skip_upgrade=False (charge_limit=2). bonus=900W → effective=1600 → SMALL
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_upgrade_target == "off -> small"

    def test_normal_strategy_18a_also_skips_upgrade(self):
        """NORMAL + 18A + exported>100 → skip upgrade universally.

        PV max 9.1 kW, bateria max 5.2 kW, grzałki max 4.5 kW = 9.7 kW > PV.
        Przy charge_limit>7 bateria chce max mocy — każdy włączony watt
        grzałki jest zabrany baterii (Goodwe rebalansuje). Historyczny
        exported_energy>100 Wh to wcześniejsze burst, nie aktualny surplus.
        Skip applies do NORMAL i BATTERY_FIRST jednakowo.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="NORMAL",
                consumption_minus_pv=-2000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.150,
            )
        )
        # reserved=3000, budget=-1000 → baseline=BOTH_ARE_OFF
        # skip_upgrade bo charge_limit>7 → target zostaje BOTH_ARE_OFF
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_active is False

    def test_normal_2a_upgrade_works_when_battery_slowing(self):
        """NORMAL + charge_limit=2A + exported>100 → upgrade działa.

        Gdy bateria zwalnia (charge_limit spadło do 2A, max ~580W), dokładanie
        grzałki nie kanibalizuje baterii — reszta PV może iść gdzie indziej.

        now=NOON_50: bonus = 150/(10/60) = 900 W → effective = 1600 → SMALL.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                water_heater_strategy="NORMAL",
                consumption_minus_pv=-1000.0,  # pv_available=1000
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.150,
                now=NOON_50,
            )
        )
        # reserved=300, budget=700 < SMALL → baseline=BOTH_ARE_OFF
        # bonus=900W → effective=1600 → upgrade SMALL
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_upgrade_target == "off -> small"

    def test_hysteresis_holds_current_state(self):
        """Histereza trzyma obecny stan na granicy progu."""
        mgr = Ems()
        # SMALL jest włączona
        mgr.update_state(
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
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.balanced_baseline == "small_is_on"

    def test_hysteresis_does_not_hold_higher_state(self):
        """Histereza NIE trzyma wyższego stanu."""
        mgr = Ems()
        # BIG jest włączona, ale budget na SMALL
        mgr.update_state(
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
        assert mgr.water_heater.balanced_baseline == "both_are_off"


class TestBalancedUpgrade:
    """Piętro 2 — upgrade z budżetu eksportu godzinowego."""

    def test_upgrade_off_to_small(self):
        """cl=7A (no skip_upgrade), pv=1200, exp=1.4kWh → bonus=1400 → SMALL upgrade.

        cl=7 → reserved=1000 (cl>2). budget=1200-1000=200, baseline=OFF.
        time_left=1h → bonus=1400W. effective=1600 ≥ SMALL_POWER=1500 → upgrade SMALL.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1200.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.4,
            )
        )
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_active is True
        assert mgr.water_heater.should_turn_on_small is True

    def test_upgrade_small_to_big(self):
        """cl=7A, pv=3000, exp=1.1kWh → bonus=1100 → BIG upgrade.

        cl=7 → reserved=1000. budget=3000-1000=2000, baseline=SMALL.
        bonus=1100W. effective=3100 ≥ BIG_POWER=3000 → upgrade BIG.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.1,
            )
        )
        assert mgr.water_heater.balanced_baseline == "small_is_on"
        assert mgr.water_heater.balanced_upgrade_active is True
        assert mgr.water_heater.should_turn_on is True  # BIG

    def test_upgrade_big_to_both(self):
        """cl=7A, pv=4000, exp=1.6kWh → bonus=1600 → BOTH upgrade.

        cl=7 → reserved=1000. budget=4000-1000=3000, baseline=BIG.
        bonus=1600W. effective=4600 ≥ BOTH_POWER=4500 → upgrade BOTH.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-4000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.6,
            )
        )
        assert mgr.water_heater.balanced_baseline == "big_is_on"
        assert mgr.water_heater.balanced_upgrade_active is True
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True  # BOTH

    def test_upgrade_both_stays_both(self):
        """baseline=BOTH, exported=120Wh → BOTH (max, no upgrade)."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-8000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,
            )
        )
        assert mgr.water_heater.balanced_baseline == "both_are_on"
        assert mgr.water_heater.balanced_upgrade_active is False

    def test_no_upgrade_below_threshold(self):
        """baseline=SMALL, exported=80Wh → za mało na upgrade."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.08,
            )
        )
        assert mgr.water_heater.balanced_baseline == "small_is_on"
        assert mgr.water_heater.balanced_upgrade_active is False

    def test_upgrade_hysteresis_holds(self):
        """Upgrade BIG aktywny, exp drop ale w hysteresis → trzymaj BIG.

        cl=7, pv=3000 → budget=2000, baseline=SMALL. Tick 1: exp=1.1 → bonus=1100,
        effective=3100 ≥ BIG=3000 → upgrade BIG. Tick 2 (big_on=True): exp=0.6 →
        bonus=600, effective=2600 ≥ BIG-HYSTERESIS=2500 AND current=BIG → trzyma BIG.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.1,
            )
        )
        assert mgr.water_heater.balanced_baseline == "small_is_on"
        assert mgr.water_heater.balanced_upgrade_active is True
        # Drop exp ale w hysteresis (bonus=600 → effective=2600 ≥ 2500=BIG-500)
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                big_on=True,
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=0.6,
            )
        )
        assert mgr.water_heater.balanced_baseline == "small_is_on"
        assert mgr.water_heater.balanced_upgrade_active is True

    def test_upgrade_hysteresis_releases(self):
        """Upgrade BIG aktywny, exp drop poniżej hysteresis → release do SMALL.

        Tick 1: jak holds. Tick 2 (big_on=True): exp=0.4 → bonus=400, effective=2400
        < BIG-HYSTERESIS=2500 → release. effective ≥ SMALL=1500 → upgrade=SMALL.
        baseline=SMALL, target=SMALL → upgrade_active=False.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.1,
            )
        )
        assert mgr.water_heater.balanced_upgrade_active is True
        # Drop poniżej hysteresis
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                big_on=True,
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=0.4,
            )
        )
        assert mgr.water_heater.balanced_upgrade_active is False
        assert mgr.water_heater.balanced_baseline == "small_is_on"


class TestBalancedOverrideAndDiagnostics:
    """Override SOC≥90 nie odpala dla BALANCED + diagnostyka."""

    def test_no_soc90_override(self):
        """mode=BALANCED, soc=95, exp=1.4kWh → override SOC≥90 NIE odpala dla BALANCED.

        cl=7 → reserved=1000. budget=1200-1000=200, baseline=OFF.
        bonus=1400W. effective=1600 ≥ SMALL=1500 → upgrade SMALL.
        Override SOC≥90 (BIG forsowany) odpala TYLKO dla ASAP/WASTED, NIE BALANCED.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1200.0,
                battery_charge_limit=7.0,
                battery_soc=95.0,
                exported_energy_hourly=1.4,
            )
        )
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_active is True
        # Override SOC≥90 NIE forsuje BIG dla BALANCED
        assert mgr.water_heater.should_turn_on is False  # nie BIG
        assert mgr.water_heater.should_turn_on_small is True  # SMALL z upgrade

    def test_diagnostics_none_in_wasted_mode(self):
        """Diagnostyka BALANCED = None/False gdy tryb WASTED."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="WASTED",
                consumption_minus_pv=-6000.0,
                battery_charge_limit=18.0,
                exported_energy_hourly=0.5,
            )
        )
        assert mgr.water_heater.balanced_heater_budget is None
        assert mgr.water_heater.balanced_baseline is None
        assert mgr.water_heater.balanced_upgrade_active is False

    @_OUT_OF_DATE
    def test_diagnostics_none_when_guard_active(self):
        """Diagnostyka = None gdy guard DoD=0% aktywny (guard usunięty w Etap 2)."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
            )
        )
        assert mgr.water_heater.balanced_heater_budget is None
        assert mgr.water_heater.balanced_baseline is None
        assert mgr.water_heater.balanced_upgrade_active is False

    def test_guard_works_with_balanced(self):
        """Guard DoD=0% działa z BALANCED."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                depth_of_discharge=0,
                exported_energy_hourly=-0.05,
                consumption_minus_pv=-5000.0,
            )
        )
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_off is True


class TestBalancedExportBonus:
    """Piętro 2 — adaptacyjny upgrade pod budżet eksportu w resztę godziny."""

    def test_incident_scenario_off_to_both_at_end_of_hour(self):
        """Scenariusz incydentu 2026-04-26.

        0.5 kWh wyeksportowane, 5 min do końca, niski pv →
        adaptacyjny upgrade z OFF bezpośrednio do BOTH (skok N+3).
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1500.0,  # pv=1500
                battery_charge_limit=2.0,  # bateria nie chce już dużo (skip=False)
                battery_soc=95.0,
                exported_energy_hourly=0.5,  # 500 Wh już oddane
                now=NOON_55,  # 5 min do końca
            )
        )
        # reserved=300, heater_budget=1200 < SMALL → baseline=OFF
        # bonus = 500/(5/60) = 6000 W → cap do BOTH_POWER=4500
        # effective = 1200 + 4500 = 5700 → BOTH (≥4500)
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_target == "off -> both"
        assert mgr.water_heater.balanced_export_bonus_w == 4500.0
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True

    def test_skip_n_plus_2_off_to_big(self):
        """Skok N→N+2 (OFF→BIG) gdy bonus wystarcza tylko na BIG, nie na BOTH."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1500.0,  # pv=1500
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.3,  # 300 Wh
                now=NOON_50,  # 10 min do końca, t_left_h=1/6
            )
        )
        # reserved=300, heater_budget=1200, baseline=OFF
        # bonus = 300/(10/60) = 1800 W (no cap)
        # effective = 1200 + 1800 = 3000 → BIG (>=3000, <4500)
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_target == "off -> big"
        assert mgr.water_heater.balanced_export_bonus_w == 1800.0
        assert mgr.water_heater.should_turn_on is True

    def test_no_upgrade_at_start_of_hour_low_exported(self):
        """Wczesna godzina + małe exported → bonus znikomy, target=baseline.

        Gdyby aktywować upgrade na SMALL przy 50 Wh exported i pełnej godzinie
        do końca, SMALL przez 60 min zjadłby 1500 Wh — 30× więcej niż mamy
        do "zjedzenia". Adaptacyjny algorytm tego nie robi.
        """
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1000.0,  # pv=1000
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.05,  # 50 Wh
                now=NOON,  # pełna godzina do końca
            )
        )
        # reserved=300, heater_budget=700, baseline=OFF
        # bonus = 50/1 = 50 W → effective = 750 → OFF
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_target == "off (baseline)"
        assert mgr.water_heater.balanced_export_bonus_w == 50.0

    def test_cutoff_last_minute_disables_bonus(self):
        """W ostatnich 60s godziny bonus=0 — nie aktywuj upgrade'u tuż przed resetem."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1500.0,  # pv=1500
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.5,  # duży exported
                now=NOON_59_30,  # 30s do końca, < EXPORT_BONUS_CUTOFF_SEC
            )
        )
        # reserved=300, budget=1200, baseline=OFF
        # cutoff aktywny → bonus=0 → effective=1200 → OFF
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_target == "off (baseline)"
        assert mgr.water_heater.balanced_export_bonus_w == 0.0

    def test_skip_upgrade_charge_limit_18_blocks_bonus(self):
        """charge_limit>7 → skip_upgrade, bonus=0 nawet z dużym exported."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-2000.0,  # pv=2000
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.5,
                now=NOON_50,
            )
        )
        # reserved=3500 (charge>7, soc<50), budget=-1500, baseline=OFF
        # skip_upgrade=True → bonus=0 → effective=-1500 → OFF
        assert mgr.water_heater.balanced_baseline == "both_are_off"
        assert mgr.water_heater.balanced_upgrade_target == "off (baseline)"
        assert mgr.water_heater.balanced_export_bonus_w == 0.0

    def test_adaptive_downgrade_when_pv_drops(self):
        """Symetryczny downgrade gdy pv nagle spadnie.

        Gdy bonus + pv spadną tak, że effective < heater_W(N) - hysteresis,
        target schodzi do niższego stanu.
        """
        mgr = Ems()
        # Ostatnio mieliśmy BOTH (current_state=BOTH_ARE_ON), ale pv nagle spadło
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                big_on=True,
                small_on=True,
                consumption_minus_pv=-500.0,  # pv=500 (drop)
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.1,  # 100 Wh
                now=NOON_55,  # 5 min do końca
            )
        )
        # reserved=300, heater_budget=200, baseline=OFF
        # bonus = 100/(5/60) = 1200 W → effective = 1400
        # Drabinka: 1400 < 1500 ale current==BOTH (nie SMALL) → histereza
        # SMALL nie trzyma. → OFF (1400 < 1500-500=1000? nie, 1400>=1000)
        # dokładnie: dla SMALL warunek hysteresy wymaga current==SMALL — nie BOTH
        # → upgrade_candidate = OFF. baseline=OFF. target=OFF.
        assert mgr.water_heater.balanced_upgrade_target == "off (baseline)"
        # Tu BOTH→OFF to symetria: kiedy adaptacyjny budżet spadł, schodzimy
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False

    def test_export_bonus_capped_at_both_power(self):
        """Bonus jest cappowany do BOTH_POWER żeby nie udawać niemożliwego budżetu."""
        mgr = Ems()
        mgr.update_state(
            _state(
                heater_mode="BALANCED",
                consumption_minus_pv=-1500.0,
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=2.0,  # 2 kWh — nierealistycznie dużo
                now=NOON_55,  # bonus by skoczył do 24kW bez cap
            )
        )
        # bonus = min(4500, 2000/0.0833) = min(4500, 24000) = 4500
        assert mgr.water_heater.balanced_export_bonus_w == 4500.0
