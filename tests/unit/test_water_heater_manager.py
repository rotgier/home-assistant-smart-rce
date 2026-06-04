from datetime import datetime
from unittest.mock import MagicMock

from custom_components.smart_rce.application.ems import Ems
from custom_components.smart_rce.domain.grid_export import InterventionDirection
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import TIMEZONE
from custom_components.smart_rce.domain.water_heater import WaterHeaterManager


def _ems(*, charge_allowed: bool = True) -> Ems:
    """Test fixture — Ems with real WaterHeaterManager + stubbed everything else.

    `charge_allowed`: simulates BatteryChargeService output (Etap B — replaces
    legacy `state.battery_charge_toggle_on`). Default True = battery actively
    charging. Tests needing 'disabled' semantic pass `_ems(charge_allowed=False)`.
    """
    from custom_components.smart_rce.application.battery_charge_service import (
        BatteryChargeUpdateResult,
    )
    from custom_components.smart_rce.application.battery_schedule_service import (
        BatteryScheduleUpdateResult,
    )
    from custom_components.smart_rce.domain.battery_schedule import BatteryOperation
    from custom_components.smart_rce.domain.dod_policy import DodPolicy
    from custom_components.smart_rce.domain.grid_export import GridExportManager

    service = MagicMock()
    service.update = MagicMock(
        return_value=BatteryScheduleUpdateResult(
            operation=BatteryOperation.idle(),
            ems_interventions_blocked=False,
            ems_interventions_blocked_override=False,
            schedule_active_this_hour=False,
        )
    )
    charge_service = MagicMock()
    charge_service.update = MagicMock(
        return_value=BatteryChargeUpdateResult(
            charge_allowed=charge_allowed,
            start_charge_hour_override=None,
        )
    )
    charge_service.charge_allowed = charge_allowed
    # Reserved service mock returns historic default 5500 W — preserves test
    # expectations from before the configurable reserved_balanced_full landed.
    reserved_service = MagicMock()
    reserved_service.compute_current_value = MagicMock(return_value=5500)
    reserved_service.prefer_battery_first = False
    return Ems(
        dod_policy=DodPolicy(),
        grid_export=GridExportManager(),
        water_heater=WaterHeaterManager(),
        battery_schedule_service=service,
        battery_charge_service=charge_service,
        water_heater_reserved_service=reserved_service,
        dod_repository=MagicMock(),
        dod_logger=MagicMock(),
        dod_actuator=MagicMock(),
        goodwe_ems_actuator=MagicMock(),
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
        depth_of_discharge=depth_of_discharge,
        now=now,
    )


class TestBalancedBaseline:
    """Piętro 1 — baseline z rezerwacją dla baterii."""

    def test_18a_low_soc_small(self):
        """pv=7500, charge_limit=18A, soc=30% → reserved=5500, budget=2000 → SMALL."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-7500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_baseline == "small_is_on"
        # heater_budget = -heater_budget (sign flip dla wykresu spójnego
        # z battery_power — charging = negative)
        assert mgr.water_heater.heater_budget == -2000.0

    def test_18a_low_soc_big(self):
        """pv=8500, charge_limit=18A, soc=30% → reserved=5500, budget=3000 → BIG."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-8500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.heater_baseline == "big_is_on"

    def test_18a_low_soc_both(self):
        """pv=10000, charge_limit=18A, soc=30% → reserved=5500, budget=4500 → BOTH."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-10000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_baseline == "both_are_on"

    def test_18a_high_soc_small(self):
        """pv=7500, charge_limit=18A, soc=70% → reserved=5500, budget=2000 → SMALL."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-7500.0,
                battery_charge_limit=18.0,
                battery_soc=70.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.heater_baseline == "small_is_on"

    def test_7a_small(self):
        """pv=3000, charge_limit=7A, soc=95% → reserved=1000, budget=2000 → SMALL."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=95.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.heater_baseline == "small_is_on"
        assert mgr.water_heater.heater_budget == -2000.0

    def test_2a_reserved_300(self):
        """pv=2000, charge_limit=2A → reserved=300, budget=1700 → SMALL."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-2000.0,
                battery_charge_limit=2.0,
                battery_soc=98.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_budget == -1700.0

    def test_0a_no_reservation(self):
        """pv=2000, charge_limit=0A → reserved=0, budget=2000 → SMALL."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-2000.0,
                battery_charge_limit=0.0,
                battery_soc=99.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_budget == -2000.0

    def test_low_pv_off(self):
        """pv=1200, charge_limit=18A, soc=30% → reserved=5500, budget=-4300 → OFF."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-1200.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.heater_baseline == "both_are_off"
        # heater_budget=-4300 → heater_budget = -(-4300) = +4300
        assert mgr.water_heater.heater_budget == 4300.0


class TestBalancedUpgrade:
    """Piętro 2 — upgrade z budżetu eksportu godzinowego."""

    def test_upgrade_off_to_small(self):
        """cl=7A (no skip_upgrade), pv=1200, exp=1.4kWh → bonus=1400 → SMALL upgrade.

        cl=7 → reserved=1000 (cl>2). budget=1200-1000=200, baseline=OFF.
        time_left=1h → bonus=1400W. effective=1600 ≥ SMALL_POWER=1500 → upgrade SMALL.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-1200.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.4,
            )
        )
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_active is True
        assert mgr.water_heater.should_turn_on_small is True

    def test_upgrade_small_to_big(self):
        """cl=7A, pv=3000, exp=1.1kWh → bonus=1100 → BIG upgrade.

        cl=7 → reserved=1000. budget=3000-1000=2000, baseline=SMALL.
        bonus=1100W. effective=3100 ≥ BIG_POWER=3000 → upgrade BIG.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.1,
            )
        )
        assert mgr.water_heater.heater_baseline == "small_is_on"
        assert mgr.water_heater.heater_upgrade_active is True
        assert mgr.water_heater.should_turn_on is True  # BIG

    def test_upgrade_big_to_both(self):
        """cl=7A, pv=4000, exp=1.6kWh → bonus=1600 → BOTH upgrade.

        cl=7 → reserved=1000. budget=4000-1000=3000, baseline=BIG.
        bonus=1600W. effective=4600 ≥ BOTH_POWER=4500 → upgrade BOTH.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-4000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.6,
            )
        )
        assert mgr.water_heater.heater_baseline == "big_is_on"
        assert mgr.water_heater.heater_upgrade_active is True
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True  # BOTH

    def test_upgrade_both_stays_both(self):
        """baseline=BOTH, exported=120Wh → BOTH (max, no upgrade)."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-10000.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.12,
            )
        )
        assert mgr.water_heater.heater_baseline == "both_are_on"
        assert mgr.water_heater.heater_upgrade_active is False

    def test_no_upgrade_below_threshold(self):
        """baseline=SMALL, exported=80Wh → za mało na upgrade."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-7500.0,
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.08,
            )
        )
        assert mgr.water_heater.heater_baseline == "small_is_on"
        assert mgr.water_heater.heater_upgrade_active is False

    def test_upgrade_hysteresis_holds(self):
        """Upgrade BIG aktywny, exp drop ale w hysteresis → trzymaj BIG.

        cl=7, pv=3000 → budget=2000, baseline=SMALL. Tick 1: exp=1.1 → bonus=1100,
        effective=3100 ≥ BIG=3000 → upgrade BIG. Tick 2 (big_on=True): exp=0.6 →
        bonus=600, effective=2600 ≥ BIG-HYSTERESIS=2500 AND current=BIG → trzyma BIG.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.1,
            )
        )
        assert mgr.water_heater.heater_baseline == "small_is_on"
        assert mgr.water_heater.heater_upgrade_active is True
        # Drop exp ale w hysteresis (bonus=600 → effective=2600 ≥ 2500=BIG-500)
        mgr.update_state(
            _state(
                big_on=True,
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=0.6,
            )
        )
        assert mgr.water_heater.heater_baseline == "small_is_on"
        assert mgr.water_heater.heater_upgrade_active is True

    def test_upgrade_hysteresis_releases(self):
        """Upgrade BIG aktywny, exp drop poniżej hysteresis → release do SMALL.

        Tick 1: jak holds. Tick 2 (big_on=True): exp=0.4 → bonus=400, effective=2400
        < BIG-HYSTERESIS=2500 → release. effective ≥ SMALL=1500 → upgrade=SMALL.
        baseline=SMALL, target=SMALL → upgrade_active=False.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=1.1,
            )
        )
        assert mgr.water_heater.heater_upgrade_active is True
        # Drop poniżej hysteresis
        mgr.update_state(
            _state(
                big_on=True,
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=30.0,
                exported_energy_hourly=0.4,
            )
        )
        assert mgr.water_heater.heater_upgrade_active is False
        assert mgr.water_heater.heater_baseline == "small_is_on"


class TestBalancedOverrideAndDiagnostics:
    """Override SOC≥90 nie odpala dla BALANCED + diagnostyka."""

    def test_no_soc90_override(self):
        """mode=BALANCED, soc=95, exp=1.4kWh → override SOC≥90 NIE odpala dla BALANCED.

        cl=7 → reserved=1000. budget=1200-1000=200, baseline=OFF.
        bonus=1400W. effective=1600 ≥ SMALL=1500 → upgrade SMALL.
        Override SOC≥90 (BIG forsowany) odpala TYLKO dla ASAP/WASTED, NIE BALANCED.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-1200.0,
                battery_charge_limit=7.0,
                battery_soc=95.0,
                exported_energy_hourly=1.4,
            )
        )
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_active is True
        # Override SOC≥90 NIE forsuje BIG dla BALANCED
        assert mgr.water_heater.should_turn_on is False  # nie BIG
        assert mgr.water_heater.should_turn_on_small is True  # SMALL z upgrade

    def test_positive_intervention_lowers_reserved_for_high_soc(self):
        """POSITIVE + cl>7 + soc>=50 → reserved 5500→3500 → grzałki dostają więcej.

        Default reserved dla cl>7 = 5500W (placeholder na configurable z UI).
        POSITIVE intervention obniża reserved do 3500W → większy heater_budget,
        grzałki mogą się włączyć tam gdzie default je blokuje.
        """
        wh = WaterHeaterManager()
        # pv=5500, cl=18, soc=70 → bez intervention reserved=5500, budget=0 → OFF
        state = _state(
            consumption_minus_pv=-5500.0,
            battery_charge_limit=18.0,
            battery_soc=70.0,
            exported_energy_hourly=0.0,
        )
        wh.update(state, grid_export_intervention=None, battery_charge_allowed=True)
        assert wh.heater_baseline == "both_are_off"
        assert wh.heater_budget == 0.0  # -(5500-5500)

        # Z POSITIVE: reserved=3500 → budget=2000 → SMALL
        wh.update(
            state,
            grid_export_intervention=InterventionDirection.POSITIVE,
            battery_charge_allowed=True,
        )
        assert wh.heater_baseline == "small_is_on"
        assert wh.heater_budget == -2000.0  # -(5500-3500)

    def test_positive_intervention_lowers_reserved_for_low_soc(self):
        """POSITIVE + cl>7 + soc<50 → reserved 5500→3500 (zachowanie niezależne od soc)."""
        wh = WaterHeaterManager()
        state = _state(
            consumption_minus_pv=-5500.0,
            battery_charge_limit=18.0,
            battery_soc=30.0,  # soc<50
            exported_energy_hourly=0.0,
        )
        wh.update(state, grid_export_intervention=None, battery_charge_allowed=True)
        assert wh.heater_budget == 0.0

        wh.update(
            state,
            grid_export_intervention=InterventionDirection.POSITIVE,
            battery_charge_allowed=True,
        )
        assert wh.heater_budget == -2000.0  # -(5500-3500)

    def test_negative_intervention_no_bump_for_cl_gt_7(self):
        """NEGATIVE + cl>7 → reserved=5500 (same jak default, no bump).

        Default cl>7 = 5500W już zapewnia grzałki off w deficycie. NEGATIVE
        nie potrzebuje bumpować, bo default już jest wystarczająco restrykcyjny.
        Bump NEGATIVE działa dla cl<=7 — patrz test_negative_intervention_bumps_reserved_for_low_cl.
        """
        wh = WaterHeaterManager()
        state = _state(
            consumption_minus_pv=-5000.0,
            battery_charge_limit=18.0,
            battery_soc=70.0,
            exported_energy_hourly=0.0,
        )
        wh.update(state, grid_export_intervention=None, battery_charge_allowed=True)
        budget_no_intervention = wh.heater_budget

        wh.update(
            state,
            grid_export_intervention=InterventionDirection.NEGATIVE,
            battery_charge_allowed=True,
        )
        # Budget identyczny — reserved=5500 w obu przypadkach
        assert wh.heater_budget == budget_no_intervention

    def test_negative_intervention_bumps_reserved_for_low_cl(self):
        """NEGATIVE + cl=2 → reserved 300→600 → naturalnie blokuje SMALL gdy budget mały.

        cl=2 ma w aktualnym kodzie reserved=300 default, NEGATIVE bumps do 600.
        """
        wh = WaterHeaterManager()
        # pv=1700, cl=2 → bez intervention reserved=300, budget=1400 → OFF (1400<1500)
        # Hysteresis może zatrzymać SMALL gdy current=SMALL — używamy current=OFF.
        state = _state(
            consumption_minus_pv=-1800.0,  # pv_avail=1800
            battery_charge_limit=2.0,
            battery_soc=50.0,
            exported_energy_hourly=0.0,
        )
        wh.update(state, grid_export_intervention=None, battery_charge_allowed=True)
        # reserved=300, budget=1500 → SMALL (1500≥1500)
        assert wh.heater_baseline == "small_is_on"

        # Z NEGATIVE: reserved=600, budget=1200 → OFF (1200<1500)
        wh.update(
            state,
            grid_export_intervention=InterventionDirection.NEGATIVE,
            battery_charge_allowed=True,
        )
        assert wh.heater_baseline == "both_are_off"

    def test_guard_works_with_balanced(self):
        """Guard DoD=0% działa z BALANCED."""
        mgr = _ems()
        mgr.update_state(
            _state(
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

        Cap na bonus DROPPED — bonus może przekraczać BOTH_POWER, drabinka
        i tak limit na BOTH_ARE_ON.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-1500.0,  # pv=1500
                battery_charge_limit=2.0,  # bateria nie chce już dużo (skip=False)
                battery_soc=95.0,
                exported_energy_hourly=0.5,  # 500 Wh już oddane
                now=NOON_55,  # 5 min do końca
            )
        )
        # reserved=300, heater_budget=1200 < SMALL → baseline=OFF
        # bonus = 500/(5/60) = 6000 W (no cap)
        # effective = 1200 + 6000 = 7200 → BOTH (≥4500)
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_target == "off -> both"
        assert mgr.water_heater.heater_export_bonus == 6000.0
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True

    def test_skip_n_plus_2_off_to_big(self):
        """Skok N→N+2 (OFF→BIG) gdy bonus wystarcza tylko na BIG, nie na BOTH."""
        mgr = _ems()
        mgr.update_state(
            _state(
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
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_target == "off -> big"
        assert mgr.water_heater.heater_export_bonus == 1800.0
        assert mgr.water_heater.should_turn_on is True

    def test_no_upgrade_at_start_of_hour_low_exported(self):
        """Wczesna godzina + małe exported → bonus znikomy, target=baseline.

        Gdyby aktywować upgrade na SMALL przy 50 Wh exported i pełnej godzinie
        do końca, SMALL przez 60 min zjadłby 1500 Wh — 30× więcej niż mamy
        do "zjedzenia". Adaptacyjny algorytm tego nie robi.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-1000.0,  # pv=1000
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.05,  # 50 Wh
                now=NOON,  # pełna godzina do końca
            )
        )
        # reserved=300, heater_budget=700, baseline=OFF
        # bonus = 50/1 = 50 W → effective = 750 → OFF
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_target == "off (baseline)"
        assert mgr.water_heater.heater_export_bonus == 50.0

    def test_cutoff_last_minute_disables_bonus(self):
        """W ostatnich 60s godziny bonus=0 — nie aktywuj upgrade'u tuż przed resetem."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-1500.0,  # pv=1500
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=0.5,  # duży exported
                now=NOON_59_30,  # 30s do końca, < EXPORT_BONUS_CUTOFF_SEC
            )
        )
        # reserved=300, budget=1200, baseline=OFF
        # cutoff aktywny → bonus=0 → effective=1200 → OFF
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_target == "off (baseline)"
        assert mgr.water_heater.heater_export_bonus == 0.0

    def test_skip_upgrade_charge_limit_18_blocks_bonus(self):
        """charge_limit>7 → skip_upgrade, bonus=0 nawet z dużym exported."""
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-2000.0,  # pv=2000
                battery_charge_limit=18.0,
                battery_soc=30.0,
                exported_energy_hourly=0.5,
                now=NOON_50,
            )
        )
        # reserved=3500 (charge>7, soc<50), budget=-1500, baseline=OFF
        # skip_upgrade=True → bonus=0 → effective=-1500 → OFF
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_target == "off (baseline)"
        assert mgr.water_heater.heater_export_bonus == 0.0

    def test_adaptive_downgrade_when_pv_drops(self):
        """Symetryczny downgrade gdy pv nagle spadnie.

        Gdy bonus + pv spadną tak, że effective < heater_W(N) - hysteresis,
        target schodzi do niższego stanu.
        """
        mgr = _ems()
        # Ostatnio mieliśmy BOTH (current_state=BOTH_ARE_ON), ale pv nagle spadło
        mgr.update_state(
            _state(
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
        assert mgr.water_heater.heater_upgrade_target == "off (baseline)"
        # Tu BOTH→OFF to symetria: kiedy adaptacyjny budżet spadł, schodzimy
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False

    def test_export_bonus_uncapped(self):
        """Bonus NIE jest cappowany (cap dropped 2026-06-04).

        Drabinka upgrade i tak nigdy nie wybierze stanu >BOTH_ARE_ON, więc
        cap był redundantny i blokował aktywację BOTH_ARE_ON gdy heater_budget
        ujemny + duży eksport w końcówce godziny.
        """
        mgr = _ems()
        mgr.update_state(
            _state(
                consumption_minus_pv=-1500.0,
                battery_charge_limit=2.0,
                battery_soc=95.0,
                exported_energy_hourly=2.0,  # 2 kWh — nierealistycznie dużo
                now=NOON_55,  # 5 min do końca
            )
        )
        # bonus = 2000/(5/60) = 24000 W (uncapped)
        assert mgr.water_heater.heater_export_bonus == 24000.0


class TestEffectiveChargeLimitFromKwarg:
    """Pre-charge scenario: charging disabled via BatteryChargePolicy, but BMS cap stays 18A.

    Without effective_charge_limit logic, water_heater.py would treat BMS cap as
    "battery actively charging" → reserved=2500-3500 → heaters off / no upgrade
    even though battery is idle. With battery_charge_allowed=False → effective_limit=0,
    behavior matches "battery full" case (reserved=0, upgrade allowed).

    Etap B refactor: `battery_charge_toggle_on` field removed from InputState;
    value now sourced from `BatteryChargeService.charge_allowed` (passed via
    `_ems(charge_allowed=...)` test fixture).
    """

    def test_charge_disallowed_with_bms_18_treats_as_idle(self):
        """Disallowed + BMS=18 + PV=2000W → reserved=0 → SMALL turns on (1500W fits)."""
        mgr = _ems(charge_allowed=False)
        mgr.update_state(
            _state(
                consumption_minus_pv=-2000.0,
                battery_charge_limit=18.0,  # BMS cap (would suggest "charging")
                battery_soc=50.0,
                exported_energy_hourly=0.0,
            )
        )
        # Effective=0 → reserved=0 → budget=2000 → SMALL (≥1500W)
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.should_turn_on is False

    def test_charge_disallowed_allows_upgrade(self):
        """Disallowed + exported energy → upgrade ladder activates (skip_upgrade=False)."""
        mgr = _ems(charge_allowed=False)
        mgr.update_state(
            _state(
                consumption_minus_pv=-1500.0,
                battery_charge_limit=18.0,
                battery_soc=50.0,
                exported_energy_hourly=0.5,  # 500 Wh exported, want to burn
                now=NOON_50,  # 10 min left
            )
        )
        # Effective=0 → reserved=0, skip_upgrade=False
        # baseline: budget=1500 → SMALL
        # bonus: 500 / (10/60) = 3000 W → effective=4500 → BOTH upgrade
        assert mgr.water_heater.heater_upgrade_active is True
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True

    def test_charge_allowed_keeps_battery_priority(self):
        """Allowed + BMS=18 → reserved=2500-3500 (battery priority preserved)."""
        mgr = _ems(charge_allowed=True)
        mgr.update_state(
            _state(
                consumption_minus_pv=-2000.0,
                battery_charge_limit=18.0,
                battery_soc=50.0,
                exported_energy_hourly=0.0,
            )
        )
        # Effective=18 → reserved=2500 (soc>=50) → budget=-500 → OFF (battery priority)
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False


def _ems_with_prefer_battery_first(
    prefer_battery_first: bool, *, charge_allowed: bool = True
) -> Ems:
    """Test fixture — Ems with prefer_battery_first override on reserved_service mock."""
    from custom_components.smart_rce.application.battery_charge_service import (
        BatteryChargeUpdateResult,
    )
    from custom_components.smart_rce.application.battery_schedule_service import (
        BatteryScheduleUpdateResult,
    )
    from custom_components.smart_rce.domain.battery_schedule import BatteryOperation
    from custom_components.smart_rce.domain.dod_policy import DodPolicy
    from custom_components.smart_rce.domain.grid_export import GridExportManager

    service = MagicMock()
    service.update = MagicMock(
        return_value=BatteryScheduleUpdateResult(
            operation=BatteryOperation.idle(),
            ems_interventions_blocked=False,
            ems_interventions_blocked_override=False,
            schedule_active_this_hour=False,
        )
    )
    charge_service = MagicMock()
    charge_service.update = MagicMock(
        return_value=BatteryChargeUpdateResult(
            charge_allowed=charge_allowed,
            start_charge_hour_override=None,
        )
    )
    charge_service.charge_allowed = charge_allowed
    reserved_service = MagicMock()
    reserved_service.compute_current_value = MagicMock(return_value=5500)
    reserved_service.prefer_battery_first = prefer_battery_first
    return Ems(
        dod_policy=DodPolicy(),
        grid_export=GridExportManager(),
        water_heater=WaterHeaterManager(),
        battery_schedule_service=service,
        battery_charge_service=charge_service,
        water_heater_reserved_service=reserved_service,
        dod_repository=MagicMock(),
        dod_logger=MagicMock(),
        dod_actuator=MagicMock(),
        goodwe_ems_actuator=MagicMock(),
    )


class TestPreferBatteryFirstOverride:
    """prefer_battery_first override: escalated reserved + bonus gate.

    When ON: reserved escalates per tier (>7: 5500; >2: 2000; ==2: 600) except
    POSITIVE intervention. At limit>2: heaters fire ONLY when export_bonus
    passes the gate (≥1000W, hysteresis ≥500W). At limit<=2: gate IGNORED
    (legacy — battery near-full, no heater-vs-battery conflict at low tiers).

    Use case: prefer battery charging on uncertain days; intermittent PV peaks
    don't trigger short heater bursts because gate filters noise-bonus.
    """

    def test_baseline_only_firing_blocked_by_bonus_gate(self):
        """prefer_battery_first=True + limit=7 + no export → OFF (gate closed).

        Reserved escalates to 2000 (battery-first at >2 tier).
        pv=3000 → heater_budget=1000 < SMALL → baseline=OFF.
        bonus=0 → gate closed → target=OFF regardless.
        """
        mgr = _ems_with_prefer_battery_first(prefer_battery_first=True)
        mgr.update_state(
            _state(
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=50.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on is False
        assert mgr.water_heater.should_turn_on_small is False
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_running_via_bonus is False

    def test_allows_heaters_when_bonus_gate_open(self):
        """prefer_battery_first=True + bonus passes gate → fires upgrade level."""
        # charge_limit=7, prefer_battery_first=True → reserved=2000 (escalated).
        # pv=3000 → heater_budget=1000 → baseline=OFF.
        # exported_energy=2 kWh @ NOON_50 (10 min left = 600s)
        # → bonus = 2000/(600/3600) = 12000W (no cap)
        # → gate_open (12000 >= 1000) → effective = 1000+12000 = 13000 → BOTH
        # → target=BOTH (upgrade beats baseline=OFF).
        mgr = _ems_with_prefer_battery_first(prefer_battery_first=True)
        mgr.update_state(
            _state(
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=50.0,
                exported_energy_hourly=2.0,
                now=NOON_50,
            )
        )
        assert mgr.water_heater.should_turn_on is True
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_upgrade_active is True
        assert mgr.water_heater.heater_running_via_bonus is True

    def test_gate_ignored_at_low_charge_limit(self):
        """prefer_battery_first=True at limit=2 → gate IGNORED, baseline fires.

        At limit==2, reserved still escalates (600 instead of 300) but gate
        does NOT apply (legacy semantic: battery near-full, no conflict).
        Need higher PV to push baseline above escalated reserved.
        """
        # charge_limit=2, prefer_battery_first=True → reserved=600 (escalated).
        # pv=2500 → heater_budget=1900 ≥ SMALL → baseline=SMALL.
        # Gate doesn't apply at limit<=2 → target=baseline=SMALL.
        mgr = _ems_with_prefer_battery_first(prefer_battery_first=True)
        mgr.update_state(
            _state(
                consumption_minus_pv=-2500.0,
                battery_charge_limit=2.0,
                battery_soc=98.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_baseline == "small_is_on"
        assert mgr.water_heater.heater_running_via_bonus is False

    def test_overrides_skip_upgrade_for_charge_limit_high(self):
        """prefer_battery_first=True + charge_limit=18 + bonus passes gate.

        Without prefer_battery_first, skip_upgrade=True (charge_limit > 7) blocks
        export_bonus. With prefer_battery_first=True, skip_upgrade is nullified —
        heaters fire when gate opens.
        """
        # charge_limit=18, prefer=True → reserved=5500 (high_reserve at >7).
        # pv=5500 → heater_budget=0 → baseline=OFF.
        # exported=2 kWh @ NOON (60min left) → bonus=2000W (no cap).
        # gate_open (2000 >= 1000) → effective=2000 → SMALL.
        mgr = _ems_with_prefer_battery_first(prefer_battery_first=True)
        mgr.update_state(
            _state(
                consumption_minus_pv=-5500.0,
                battery_charge_limit=18.0,
                battery_soc=50.0,
                exported_energy_hourly=2.0,
                now=NOON,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_baseline == "both_are_off"
        assert mgr.water_heater.heater_upgrade_active is True
        assert mgr.water_heater.heater_running_via_bonus is True

    def test_disabled_preserves_baseline_firing(self):
        """prefer_battery_first=False (default) → baseline fires normally (legacy)."""
        # Same conditions as test_baseline_only_firing_blocked_by_bonus_gate
        # but with override OFF: reserved=1000 (not escalated) → heater_budget=2000
        # → baseline=SMALL → fires.
        mgr = _ems_with_prefer_battery_first(prefer_battery_first=False)
        mgr.update_state(
            _state(
                consumption_minus_pv=-3000.0,
                battery_charge_limit=7.0,
                battery_soc=50.0,
                exported_energy_hourly=0.0,
            )
        )
        assert mgr.water_heater.should_turn_on_small is True
        assert mgr.water_heater.heater_baseline == "small_is_on"
        assert mgr.water_heater.heater_running_via_bonus is False
