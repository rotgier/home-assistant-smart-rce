"""Ems charge-param setters — recompute + force-sync of start_charge_hour_override.

A user-driven change to any charge-window param must bypass the midnight
sticky-gate and align `start_charge_hour_override` with the freshly recomputed
today-window start, so today's charge decisions (which read
start_charge_hour_override, not charge_slots) take effect immediately.
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

from custom_components.smart_rce.application.battery_charge_service import (
    BatteryChargeService,
)
from custom_components.smart_rce.application.ems import Ems
from custom_components.smart_rce.domain.battery_charge_policy import BatteryChargePolicy
from custom_components.smart_rce.domain.grid_export import GridExportManager
from custom_components.smart_rce.domain.rce import RceDayPrices, RcePrices

NOW = datetime(2026, 6, 26, 9, 30, tzinfo=timezone.utc)


class _FakeChargeRepo:
    def __init__(self) -> None:
        self._policy = BatteryChargePolicy()

    @property
    def policy(self) -> BatteryChargePolicy:
        return self._policy

    async def persist(self) -> None:
        pass

    def save_if_changed(self) -> None:
        pass


def _ems_with_real_charge_service() -> Ems:
    charge_service = BatteryChargeService(
        repo=_FakeChargeRepo(), clock=lambda: NOW, actuator=MagicMock()
    )
    return Ems(
        dod_policy=MagicMock(),
        grid_export=GridExportManager(),
        water_heater=MagicMock(),
        battery_schedule_service=MagicMock(),
        battery_charge_service=charge_service,
        water_heater_reserved_service=MagicMock(),
        dod_repository=MagicMock(),
        dod_logger=MagicMock(),
        dod_actuator=MagicMock(),
        goodwe_ems_actuator=MagicMock(),
    )


def _set_today(ems: Ems, prices: list[float]) -> None:
    ems.rce_prices.rce_prices = RcePrices(
        fetched_at=NOW,
        today=RceDayPrices(
            published_at=None, day=date(2026, 6, 26), hour_price=tuple(prices)
        ),
    )


def _cheap_valley_day() -> list[float]:
    prices = [500.0] * 24
    prices[12] = prices[13] = 10.0
    return prices


async def test_set_initial_charge_hours_force_syncs_start():
    ems = _ems_with_real_charge_service()
    _set_today(ems, _cheap_valley_day())

    await ems.set_initial_charge_hours(2)

    assert ems.charge_slots.today is not None
    assert ems.battery_charge_service.initial_charge_hours == 2
    # start_charge_hour_override aligned with the recomputed today-window start.
    assert (
        ems.battery_charge_service.start_charge_hour_override
        == ems.charge_slots.today.start_datetime.time()
    )


async def test_set_extend_threshold_recomputes_and_syncs():
    ems = _ems_with_real_charge_service()
    _set_today(ems, _cheap_valley_day())

    await ems.set_charge_extend_threshold(60.0)

    assert ems.battery_charge_service.charge_extend_threshold == 60.0
    assert ems.charge_slots.today is not None
    assert (
        ems.battery_charge_service.start_charge_hour_override
        == ems.charge_slots.today.start_datetime.time()
    )


async def test_set_base_window_shift_syncs():
    ems = _ems_with_real_charge_service()
    _set_today(ems, _cheap_valley_day())
    # initial=2 → base window is exactly the 2h valley (low anchor, no extend).
    await ems.set_initial_charge_hours(2)

    await ems.set_charge_base_window_shift_minutes(0)

    assert ems.battery_charge_service.charge_base_window_shift_minutes == 0
    assert ems.charge_slots.today is not None
    # shift 0 → integer start (12:00, not 11:30), and override matches it.
    assert ems.charge_slots.today.start_hour == 12.0
    assert (
        ems.battery_charge_service.start_charge_hour_override
        == ems.charge_slots.today.start_datetime.time()
    )


async def test_param_change_bypasses_sticky_gate_outside_midnight():
    # NOW 09:30 — outside [00:00, 06:00); a pre-existing manual start would
    # normally be sticky. User-driven param change must override it.
    ems = _ems_with_real_charge_service()
    _set_today(ems, _cheap_valley_day())

    await ems.set_initial_charge_hours(4)
    first = ems.battery_charge_service.start_charge_hour_override

    await ems.set_charge_base_window_shift_minutes(0)
    second = ems.battery_charge_service.start_charge_hour_override

    # Both writes landed (no sticky block), reflecting the recomputed window.
    assert first is not None
    assert second is not None
    assert second == ems.charge_slots.today.start_datetime.time()
