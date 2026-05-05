"""Energy Management System logic."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from custom_components.smart_rce.domain.battery import BatteryManager
from custom_components.smart_rce.domain.charge_slots import ChargeSlots
from custom_components.smart_rce.domain.discharge_slots import DischargeSlots
from custom_components.smart_rce.domain.grid_export import GridExportManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import RceData, RceDayPrices
from custom_components.smart_rce.domain.water_heater import WaterHeaterManager

type CALLBACK_TYPE = Callable[[], None]

_LOGGER = logging.getLogger(__name__)


class Ems:
    def __init__(self) -> None:
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        # Last InputState — udostępniane infrastructure adapters (logger,
        # diagnostic readers) które potrzebują dostępu do bieżących wartości
        # wejściowych. Promote z prywatnego `_ha` (unused) na publiczny.
        self.last_input_state: InputState | None = None
        self.charge_slots: ChargeSlots = ChargeSlots()
        self.discharge_slots: DischargeSlots = DischargeSlots()
        self.rce_data: RceData = None
        self.current_price: float = None
        self.battery: BatteryManager = BatteryManager()
        self.water_heater: WaterHeaterManager = WaterHeaterManager()
        self.grid_export: GridExportManager = GridExportManager()

    def update_state(self, state: InputState) -> None:
        # Store przed managers update — listenery (logger, etc.) czytają
        # to po `_async_update_listeners`, więc state musi być świeży.
        self.last_input_state = state
        # battery — oblicza should_block_battery_discharge dla binary_sensor
        # (entity diagnostic). Niezależne od innych managerów po Etap 2.
        self.battery.update(state)
        # grid_export PRZED water_heater — water_heater dostaje aktualny
        # `get_active_intervention()` (POSITIVE → reserved=3500W, NEGATIVE →
        # większy reserved by wymusić grzałki off).
        self.grid_export.update(state)
        self.water_heater.update(
            state,
            self.grid_export.get_active_intervention(),
        )
        self._async_update_listeners()

    def update_hourly(self, now: datetime) -> None:
        self.charge_slots.rotate_if_day_changed(now)
        self.discharge_slots.update(self.rce_data, now)
        if self.rce_data and self.rce_data.today and self.rce_data.today.prices:
            self.current_price = self.rce_data.today.prices[now.hour]["price"]
            self._async_update_listeners()

    def update_rce(self, now: datetime, data: RceData) -> None:
        if data:
            self.rce_data = data
            self.charge_slots.update(data)
            self.update_hourly(now)

    def restore_rce_today(self, prices_attr: list[dict], now: datetime) -> None:
        """Restore today's RCE prices from sensor attributes."""
        rce_prices = _restore_rce_day_prices(prices_attr)
        if rce_prices:
            self.charge_slots.today = ChargeSlots.compute(rce_prices)
            self.update_hourly(now)

    def restore_rce_tomorrow(self, prices_attr: list[dict]) -> None:
        """Restore tomorrow's RCE prices from sensor attributes."""
        rce_prices = _restore_rce_day_prices(prices_attr)
        if rce_prices:
            self.charge_slots.tomorrow = ChargeSlots.compute(rce_prices)

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _async_update_listeners(self) -> None:
        for update_callback in self._listeners.values():
            update_callback()


def _restore_rce_day_prices(prices_attr: list[dict]) -> RceDayPrices | None:
    """Build RceDayPrices from restored sensor attributes."""
    if not prices_attr:
        return None
    prices = [
        {"datetime": datetime.fromisoformat(p["datetime"]), "price": p["price"]}
        for p in prices_attr
    ]
    return RceDayPrices(published_at=None, prices=prices)
