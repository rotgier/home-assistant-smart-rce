"""Smart RCE Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import now as now_local

from . import SmartRceConfigEntry
from .application.ems import Ems
from .application.pv_forecast_service import PvForecastService
from .const import DOMAIN, GROSS_MULTIPLIER
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.weather_forecast_history import WeatherForecastHistory

CURRENCY_PLN: Final = "zł"
UNIQUE_ID_PREFIX = DOMAIN
EMS_UNIQUE_ID_PREFIX = "ems"

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


# === Setup ===


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Smart RCE sensors."""
    coordinator = entry.runtime_data.rce_coordinator
    ems = entry.runtime_data.ems
    pv_forecast = entry.runtime_data.pv_forecast
    weather_history = entry.runtime_data.weather_forecast_history
    weather_listener = entry.runtime_data.weather_listener

    sensors: list[SensorEntity] = [
        SmartRceSensor(coordinator, ems, description)
        for description in SENSOR_DESCRIPTIONS
    ]

    sensors.extend(_build_pv_forecast_sensors(pv_forecast, coordinator))
    sensors.append(
        WeatherForecastHistorySensor(weather_history, weather_listener, coordinator)
    )

    from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

    ems_device_info = DeviceInfo(
        name="EMS",
        identifiers={("ems", entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
    )
    sensors.extend(
        EmsSensor(ems_device_info, ems, description)
        for description in EMS_SENSOR_DESCRIPTIONS
    )

    async_add_entities(sensors)


# === RCE prices sensors ===


class SmartRceSensor(CoordinatorEntity[SmartRceDataUpdateCoordinator], RestoreSensor):
    """Sensor reading current/historical RCE prices + charge/discharge slots from Ems."""

    _attr_has_entity_name = True
    entity_description: SmartRceSensorDescription

    def __init__(
        self,
        coordinator: SmartRceDataUpdateCoordinator,
        ems: Ems,
        description: SmartRceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{description.key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if self.entity_description.restore_fn:
            last_state = await self.async_get_last_state()
            if last_state:
                self.entity_description.restore_fn(self.ems, last_state.attributes)

        _register_state_writer(self, self.ems)
        self._handle_coordinator_update()
        _LOGGER.debug(
            "Setup of RCE Smart sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> str | int | float | datetime | None:
        return self.entity_description.value_fn(self.ems)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attr_fn(self.ems)


@dataclass(frozen=False, kw_only=True)
class SmartRceSensorDescription(SensorEntityDescription):
    """Description schema dla SmartRceSensor — value_fn/attr_fn lambdas + optional restore_fn."""

    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | datetime | None]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda _: {}
    restore_fn: Callable[[Ems, dict[str, Any]], None] | None = None

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


def _avg_price(ems: Ems, day: str) -> float | None:
    """Thin wrapper — delegate to RceDayPrices.avg_price domain property."""
    rce_data = ems.rce_prices.rce_prices
    if not rce_data:
        return None
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    return day_prices.avg_price if day_prices else None


def _prices_attr(ems: Ems, day: str) -> dict[str, Any]:
    rce_data = ems.rce_prices.rce_prices
    if not rce_data:
        return {}
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    if not day_prices or not day_prices.hour_price:
        return {}
    return {
        "prices": [
            {
                "datetime": day_prices.datetime_at_hour(hour).isoformat(),
                "price": price,
            }
            for hour, price in enumerate(day_prices.hour_price)
        ]
    }


def _restore_prices_today(ems: Ems, attrs: dict[str, Any]) -> None:
    prices = attrs.get("prices")
    if prices:
        ems.restore_rce_today(prices, now_local())


def _restore_prices_tomorrow(ems: Ems, attrs: dict[str, Any]) -> None:
    prices = attrs.get("prices")
    if prices:
        ems.restore_rce_tomorrow(prices)


SENSOR_DESCRIPTIONS: tuple[SmartRceSensorDescription, ...] = (
    SmartRceSensorDescription(
        name="Current Price",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.rce_prices.current_price,
    ),
    SmartRceSensorDescription(
        name="Prices Today",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _avg_price(ems, "today"),
        attr_fn=lambda ems: _prices_attr(ems, "today"),
        restore_fn=_restore_prices_today,
    ),
    SmartRceSensorDescription(
        name="Prices Tomorrow",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: _avg_price(ems, "tomorrow"),
        attr_fn=lambda ems: _prices_attr(ems, "tomorrow"),
        restore_fn=_restore_prices_tomorrow,
    ),
    SmartRceSensorDescription(
        name="Max Upcoming Peak Gross",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cash-clock",
        value_fn=lambda ems: (
            round(ems.discharge_slots.max_upcoming_peak.price * GROSS_MULTIPLIER, 2)
            if ems.discharge_slots.max_upcoming_peak
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Max Upcoming Peak Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
        value_fn=lambda ems: (
            ems.discharge_slots.max_upcoming_peak.datetime
            if ems.discharge_slots.max_upcoming_peak
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Morning Discharge Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:weather-sunset-up",
        value_fn=lambda ems: (
            ems.discharge_slots.best_morning_discharge_slot.datetime
            if ems.discharge_slots.best_morning_discharge_slot
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Morning Discharge Price",
        native_unit_of_measurement=f"{CURRENCY_PLN}/{UnitOfEnergy.MEGA_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:cash-clock",
        value_fn=lambda ems: (
            round(
                ems.discharge_slots.best_morning_discharge_slot.price
                * GROSS_MULTIPLIER,
                2,
            )
            if ems.discharge_slots.best_morning_discharge_slot
            else None
        ),
    ),
    # --- Today charge slots ---
    SmartRceSensorDescription(
        name="Start Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.today.start_hour
        if ems.charge_slots.today
        else None,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.today.start_datetime
        if ems.charge_slots.today
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.today.end_hour
        if ems.charge_slots.today
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.today.end_datetime
        if ems.charge_slots.today
        else None,
    ),
    # --- Tomorrow charge slots ---
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.tomorrow.start_hour
        if ems.charge_slots.tomorrow
        else None,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.tomorrow.start_datetime
        if ems.charge_slots.tomorrow
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.charge_slots.tomorrow.end_hour
        if ems.charge_slots.tomorrow
        else None,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.charge_slots.tomorrow.end_datetime
        if ems.charge_slots.tomorrow
        else None,
    ),
)


# === PV forecast sensors ===


class PvForecastSensor(SensorEntity):
    """Sensor for weather-adjusted PV forecast data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        pv_forecast: PvForecastService,
        rce_coordinator: SmartRceDataUpdateCoordinator,
        name: str,
        value_fn: Callable[[PvForecastService], float | int | None],
        attr_fn: Callable[[PvForecastService], dict[str, Any]],
        unit: str | None = None,
    ) -> None:
        self._pv_forecast = pv_forecast
        self._value_fn = value_fn
        self._attr_fn = attr_fn
        self._attr_name = name
        key = name.lower().replace(" ", "_")
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{key}"
        self._attr_device_info = rce_coordinator.device_info
        if unit:
            self._attr_native_unit_of_measurement = unit
        else:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        _register_state_writer(self, self._pv_forecast)
        _LOGGER.debug(
            "Setup of PV Forecast sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @property
    def native_value(self) -> float | int | None:
        return self._value_fn(self._pv_forecast)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attr_fn(self._pv_forecast)


def _build_pv_forecast_sensors(
    pv_forecast: PvForecastService,
    coordinator: SmartRceDataUpdateCoordinator,
) -> list[PvForecastSensor]:
    """Instantiate all PV forecast + target SOC sensors (kWh + percent variants)."""
    return [
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV At 6",
            lambda pv: pv.forecast.adjusted_at_6.total_kwh
            if pv.forecast.adjusted_at_6
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_at_6),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV Live",
            lambda pv: pv.forecast.adjusted_live.total_kwh
            if pv.forecast.adjusted_live
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_live),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV Tomorrow At 6",
            lambda pv: pv.forecast.adjusted_tomorrow.total_kwh
            if pv.forecast.adjusted_tomorrow
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_tomorrow),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Weather Adjusted PV Tomorrow Live",
            lambda pv: pv.forecast.adjusted_tomorrow_live.total_kwh
            if pv.forecast.adjusted_tomorrow_live
            else None,
            lambda pv: _pv_forecast_attrs(pv.forecast.adjusted_tomorrow_live),
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC At 6",
            lambda pv: pv.forecast.target_soc.value if pv.forecast.target_soc else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Live",
            lambda pv: pv.forecast.target_soc_live.value
            if pv.forecast.target_soc_live
            else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_live),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow At 6",
            lambda pv: pv.forecast.target_soc_tomorrow.value
            if pv.forecast.target_soc_tomorrow
            else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_tomorrow),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Live",
            lambda pv: pv.forecast.target_soc_tomorrow_live.value
            if pv.forecast.target_soc_tomorrow_live
            else None,
            lambda pv: _target_soc_trace_attrs(pv.forecast.target_soc_tomorrow_live),
            unit="%",
        ),
        # --- Prev-workday instrumentation (Etap A) — today ---
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Prev Day 1",
            lambda pv: pv.forecast.target_soc_prev_days[0].value
            if pv.forecast.target_soc_prev_days[0]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_prev_days[0],
                pv.forecast.consumption_profiles[0],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Prev Day 2",
            lambda pv: pv.forecast.target_soc_prev_days[1].value
            if pv.forecast.target_soc_prev_days[1]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_prev_days[1],
                pv.forecast.consumption_profiles[1],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Prev Day 3",
            lambda pv: pv.forecast.target_soc_prev_days[2].value
            if pv.forecast.target_soc_prev_days[2]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_prev_days[2],
                pv.forecast.consumption_profiles[2],
            ),
            unit="%",
        ),
        # --- Prev-workday instrumentation — tomorrow ---
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Prev Day 1",
            lambda pv: pv.forecast.target_soc_tomorrow_prev_days[0].value
            if pv.forecast.target_soc_tomorrow_prev_days[0]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_tomorrow_prev_days[0],
                pv.forecast.consumption_profiles[0],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Prev Day 2",
            lambda pv: pv.forecast.target_soc_tomorrow_prev_days[1].value
            if pv.forecast.target_soc_tomorrow_prev_days[1]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_tomorrow_prev_days[1],
                pv.forecast.consumption_profiles[1],
            ),
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Prev Day 3",
            lambda pv: pv.forecast.target_soc_tomorrow_prev_days[2].value
            if pv.forecast.target_soc_tomorrow_prev_days[2]
            else None,
            lambda pv: _target_soc_trace_attrs(
                pv.forecast.target_soc_tomorrow_prev_days[2],
                pv.forecast.consumption_profiles[2],
            ),
            unit="%",
        ),
        # --- Max safety sensors — max(live, prev_day_1..N) ---
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Max",
            lambda pv: pv.forecast.target_soc_max,
            lambda _pv: {},
            unit="%",
        ),
        PvForecastSensor(
            pv_forecast,
            coordinator,
            "Target Battery SOC Tomorrow Max",
            lambda pv: pv.forecast.target_soc_tomorrow_max,
            lambda _pv: {},
            unit="%",
        ),
    ]


def _pv_forecast_attrs(forecast) -> dict[str, Any]:
    if not forecast:
        return {}
    return {
        "forecast": [
            {
                "period_start": p.period_start,
                "pv_estimate_adjusted": p.pv_estimate_adjusted,
            }
            for p in forecast.forecast
        ]
    }


def _target_soc_trace_attrs(result, profile=None) -> dict[str, Any]:
    """Trace for target_soc_* sensors: per-bucket pv/cons/balance/cumulative + is_min.

    If profile is given and has source_date, adds 'profile_date' attribute
    (informs which prev-workday consumption profile was used).
    """
    if not result or not result.buckets:
        return {}
    attrs: dict[str, Any] = {
        "buckets": [
            {
                "period": b.period,
                "pv": b.pv_kwh,
                "cons": b.cons_kwh,
                "balance": b.balance,
                "cumulative": b.cumulative,
                "is_min": b.is_min,
            }
            for b in result.buckets
        ]
    }
    if profile is not None and profile.source_date is not None:
        attrs["profile_date"] = profile.source_date.isoformat()
    return attrs


# === Weather forecast history sensor ===


class WeatherForecastHistorySensor(RestoreSensor):
    """Sensor tracking hourly weather forecast conditions throughout the day.

    State changes once per hour (e.g. "07:00 cloudy").
    Attribute 'hours' updates every ~5 min from wetteronline forecast.

    Recorder saves ~30 entries/day: 24 state changes (hourly) + ~6 attribute-only
    changes (when WetterOnline updates forecast for future hours).
    To get the forecast snapshot from the start of hour X, take the FIRST
    recorder entry in that hour (see ADR 013).
    """

    _attr_has_entity_name = True
    _attr_name = "Weather Forecast History"

    def __init__(
        self,
        weather_history: WeatherForecastHistory,
        weather_listener: Any,
        rce_coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        self._weather_history = weather_history
        self._weather_listener = weather_listener
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_weather_forecast_history"
        self._attr_device_info = rce_coordinator.device_info
        self._attr_native_value: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore from RestoreSensor.
        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data and last_sensor_data.native_value:
            self._attr_native_value = last_sensor_data.native_value

        last_state = await self.async_get_last_state()
        if last_state:
            hours_attr = last_state.attributes.get("hours")
            if hours_attr:
                self._weather_history.restore(hours_attr, now_local().date())

        @callback
        def on_weather_update() -> None:
            self._handle_weather_update()

        remove_listener = self._weather_listener.async_add_listener(on_weather_update)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        _LOGGER.debug(
            "Setup of Weather Forecast History sensor %s (unique_id: %s)",
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_weather_update(self) -> None:
        """Refresh sensor state — write side managed by factory."""
        self._attr_native_value = self._weather_history.current_hour_label(now_local())
        self.async_write_ha_state()

    @property
    def native_value(self) -> str | None:
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"hours": self._weather_history.hours_attribute}


# === EMS sensors ===


class EmsSensor(SensorEntity):
    """EMS diagnostic sensor (heater_budget, balanced_baseline)."""

    _attr_has_entity_name = True
    entity_description: EmsSensorDescription

    def __init__(
        self,
        device_info,
        ems: Ems,
        description: EmsSensorDescription,
    ) -> None:
        self._attr_device_info = device_info
        self.ems: Ems = ems
        self.entity_description = description
        self._attr_unique_id = f"{EMS_UNIQUE_ID_PREFIX}_{description.key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        _register_state_writer(self, self.ems)
        self.async_write_ha_state()
        _LOGGER.debug(
            "Setup of EMS sensor %s (%s, unique_id: %s)",
            self.name,
            self.entity_id,
            self._attr_unique_id,
        )

    @cached_property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str | int | float | None:
        return self.entity_description.value_fn(self.ems)


@dataclass(frozen=False, kw_only=True)
class EmsSensorDescription(SensorEntityDescription):
    """Description schema dla EmsSensor — value_fn lambda extracting from Ems."""

    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | None]

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


EMS_SENSOR_DESCRIPTIONS: tuple[EmsSensorDescription, ...] = (
    EmsSensorDescription(
        name="Heater Budget",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.water_heater.balanced_heater_budget,
        icon="mdi:lightning-bolt",
    ),
    EmsSensorDescription(
        name="Balanced Baseline",
        value_fn=lambda ems: ems.water_heater.balanced_baseline,
        icon="mdi:heating-coil",
    ),
    EmsSensorDescription(
        name="Balanced Upgrade Target",
        value_fn=lambda ems: ems.water_heater.balanced_upgrade_target,
        icon="mdi:arrow-up-bold-circle",
    ),
    EmsSensorDescription(
        name="Balanced Export Bonus W",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.water_heater.balanced_export_bonus_w,
        icon="mdi:transmission-tower-export",
    ),
    EmsSensorDescription(
        name="Grid Export Recommended Xset",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.grid_export.recommended_xset,
        icon="mdi:flash",
    ),
    EmsSensorDescription(
        name="Grid Export Recommended EMS Mode",
        value_fn=lambda ems: ems.grid_export.recommended_ems_mode,
        icon="mdi:cog-outline",
    ),
    EmsSensorDescription(
        name="Grid Export Last Decision Reason",
        value_fn=lambda ems: ems.grid_export.last_decision_reason,
        icon="mdi:information-outline",
    ),
)


# === Common helpers ===


def _register_state_writer(sensor: SensorEntity, source: Any) -> None:
    """Register listener that invokes async_write_ha_state on each notify.

    `source` must have `async_add_listener(callback) -> Callable[[], None]`.
    Common pattern for sensors subscribed to Ems / PvForecastService.
    Eliminates 5-line duplication in `async_added_to_hass` per class.
    """

    @callback
    def listener() -> None:
        sensor.async_write_ha_state()

    remove_listener = source.async_add_listener(listener)
    setattr(remove_listener, "_hass_callback", True)
    sensor.async_on_remove(remove_listener)
