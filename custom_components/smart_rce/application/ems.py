"""Energy Management System — orchestrator (composition + listeners + RCE lifecycle)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from custom_components.smart_rce.domain.battery import BatteryManager
from custom_components.smart_rce.domain.charge_slots import ChargeSlots
from custom_components.smart_rce.domain.discharge_slots import DischargeSlots
from custom_components.smart_rce.domain.ems_rce_prices import EmsRcePrices
from custom_components.smart_rce.domain.grid_export import GridExportManager
from custom_components.smart_rce.domain.input_state import InputState
from custom_components.smart_rce.domain.rce import RcePrices
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
        self.rce_prices: EmsRcePrices = EmsRcePrices()
        self.charge_slots: ChargeSlots = ChargeSlots()
        self.discharge_slots: DischargeSlots = DischargeSlots()
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

    def update_rce(self, now: datetime, data: RcePrices) -> None:
        if not data:
            return
        self.rce_prices.update(now, data)
        self.charge_slots.update(data)
        self.update_hourly(now)

    def update_hourly(self, now: datetime) -> None:
        self.rce_prices.update_hourly(now)
        self.charge_slots.rotate_if_day_changed(now)
        self.discharge_slots.update(self.rce_prices.rce_prices, now)
        if self.rce_prices.current_price is not None:
            self._async_update_listeners()

    def restore_rce_today(self, prices_attr: list[dict], now: datetime) -> None:
        """Restore today's RCE prices from sensor attributes."""
        self.rce_prices.restore_today(prices_attr, now)
        self.charge_slots.update(self.rce_prices.rce_prices)
        self.update_hourly(now)

    def restore_rce_tomorrow(self, prices_attr: list[dict]) -> None:
        """Restore tomorrow's RCE prices from sensor attributes."""
        # Now nie jest istotne dla tomorrow restore (current_price czyta z today).
        # EmsRcePrices.restore_tomorrow przyjmuje now żeby utworzyć RcePrices
        # gdy first restore (rce_prices is None) — używamy datetime.now() jako
        # placeholder (nie wpływa na żadną logikę odczytową).
        self.rce_prices.restore_tomorrow(prices_attr, datetime.now())
        self.charge_slots.update(self.rce_prices.rce_prices)

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _async_update_listeners(self) -> None:
        for update_callback in self._listeners.values():
            update_callback()
