"""Ems.set_charge_hours_override — force-sync of start_charge_hour_override.

A user-driven select change must bypass the midnight sticky-gate and align
`start_charge_hour_override` with the freshly recomputed today-window start,
so today's charge decisions (which read start_charge_hour_override, not
charge_slots) take effect immediately.
"""

from datetime import date, datetime, time, timezone
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


async def test_force_syncs_start_to_forced_window_start():
    ems = _ems_with_real_charge_service()
    prices = [100.0] * 24
    prices[12] = prices[13] = 10.0  # cheapest 2h valley at 12:00-14:00
    _set_today(ems, prices)

    await ems.set_charge_hours_override(2)

    assert ems.charge_slots.today is not None
    assert ems.charge_slots.today.start_hour == 12.0
    # The real charge decision input is now aligned with the displayed window.
    assert ems.battery_charge_service.start_charge_hour_override == time(12, 0)


async def test_force_sync_bypasses_sticky_gate_outside_midnight():
    # NOW is 09:30 — outside [00:00, 06:00). With a pre-existing manual start,
    # the sticky-gate would normally block; the user-driven path must override.
    ems = _ems_with_real_charge_service()
    ems.battery_charge_service._repo.policy.start_charge_hour_override = time(11, 30)
    prices = [100.0] * 24
    prices[12] = prices[13] = 10.0
    _set_today(ems, prices)

    await ems.set_charge_hours_override(2)

    assert ems.battery_charge_service.start_charge_hour_override == time(12, 0)


async def test_back_to_auto_realigns_start():
    ems = _ems_with_real_charge_service()
    prices = [100.0] * 24
    prices[12] = prices[13] = 10.0
    _set_today(ems, prices)
    await ems.set_charge_hours_override(2)
    assert ems.battery_charge_service.start_charge_hour_override == time(12, 0)

    await ems.set_charge_hours_override(None)  # Auto

    # Auto path realigns start to the adaptive window start (no exception, set).
    assert ems.charge_slots.today is not None
    expected = ems.charge_slots.today.start_datetime.time()
    assert ems.battery_charge_service.start_charge_hour_override == expected
