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
from .const import DOMAIN, GROSS_MULTIPLIER
from .coordinator import SmartRceDataUpdateCoordinator
from .domain.ems import Ems
from .pv_forecast_coordinator import PvForecastCoordinator
from .weather_forecast_history import WeatherForecastHistory

CURRENCY_PLN: Final = "zł"
UNIQUE_ID_PREFIX = DOMAIN

PARALLEL_UPDATES = 1


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=False, kw_only=True)
class SmartRceSensorDescription(SensorEntityDescription):
    key: str = field(init=False)
    value_fn: Callable[[Ems], str | int | float | datetime | None]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda _: {}
    restore_fn: Callable[[Ems, dict[str, Any]], None] | None = None

    def __post_init__(self):
        self.key = self.name.lower().replace(" ", "_")


def _avg_price(ems: Ems, day: str) -> float | None:
    rce_data = ems.rce_data
    if not rce_data:
        return None
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    if not day_prices or not day_prices.prices:
        return None
    prices = [p["price"] for p in day_prices.prices]
    return round(sum(prices) / len(prices), 2)


def _prices_attr(ems: Ems, day: str) -> dict[str, Any]:
    rce_data = ems.rce_data
    if not rce_data:
        return {}
    day_prices = rce_data.today if day == "today" else rce_data.tomorrow
    if not day_prices or not day_prices.prices:
        return {}
    return {
        "prices": [
            {
                "datetime": p["datetime"].isoformat(),
                "price": p["price"],
            }
            for p in day_prices.prices
        ]
    }


def _restore_prices_today(ems: Ems, attrs: dict[str, Any]) -> None:
    prices = attrs.get("prices")
    if prices:
        from homeassistant.util.dt import now as now_local

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
        value_fn=lambda ems: ems.current_price,
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
            round(
                ems.rce_data.max_upcoming_peak(now_local()).price * GROSS_MULTIPLIER,
                2,
            )
            if ems.rce_data and ems.rce_data.max_upcoming_peak(now_local())
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Max Upcoming Peak Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-outline",
        value_fn=lambda ems: (
            ems.rce_data.max_upcoming_peak(now_local()).datetime
            if ems.rce_data and ems.rce_data.max_upcoming_peak(now_local())
            else None
        ),
    ),
    SmartRceSensorDescription(
        name="Morning Discharge Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:weather-sunset-up",
        value_fn=lambda ems: (
            ems.rce_data.best_morning_discharge_slot(now_local()).datetime
            if ems.rce_data and ems.rce_data.best_morning_discharge_slot(now_local())
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
                ems.rce_data.best_morning_discharge_slot(now_local()).price
                * GROSS_MULTIPLIER,
                2,
            )
            if ems.rce_data and ems.rce_data.best_morning_discharge_slot(now_local())
            else None
        ),
    ),
    ####
    #### TODAY
    ####
    SmartRceSensorDescription(
        name="Start Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.today.start_charge_hour,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.today.start_charge_hour_datetime,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.today.end_charge_hour,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Today Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.today.end_charge_hour_datetime,
    ),
    ####
    #### TOMORROW
    ####
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.tomorrow.start_charge_hour,
    ),
    SmartRceSensorDescription(
        name="Start Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.tomorrow.start_charge_hour_datetime,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ems: ems.tomorrow.end_charge_hour,
    ),
    SmartRceSensorDescription(
        name="End Charge Hour Tomorrow Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda ems: ems.tomorrow.end_charge_hour_datetime,
    ),
)


EMS_UNIQUE_ID_PREFIX = "ems"


@dataclass(frozen=False, kw_only=True)
class EmsSensorDescription(SensorEntityDescription):
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add Smart RCE sensors."""
    coordinator = entry.runtime_data.rce_coordinator
    ems = entry.runtime_data.ems
    pv_forecast = entry.runtime_data.pv_forecast_coordinator

    sensors: list[SensorEntity] = [
        SmartRceSensor(coordinator, ems, description)
        for description in SENSOR_DESCRIPTIONS
    ]

    weather_history = entry.runtime_data.weather_forecast_history
    weather_coordinator = entry.runtime_data.weather_coordinator

    sensors.extend(
        [
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Weather Adjusted PV At 6",
                lambda pv: pv.adjusted_at_6.total_kwh if pv.adjusted_at_6 else None,
                lambda pv: _pv_forecast_attrs(pv.adjusted_at_6),
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Weather Adjusted PV Live",
                lambda pv: pv.adjusted_live.total_kwh if pv.adjusted_live else None,
                lambda pv: _pv_forecast_attrs(pv.adjusted_live),
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Weather Adjusted PV Tomorrow At 6",
                lambda pv: pv.adjusted_tomorrow.total_kwh
                if pv.adjusted_tomorrow
                else None,
                lambda pv: _pv_forecast_attrs(pv.adjusted_tomorrow),
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Weather Adjusted PV Tomorrow Live",
                lambda pv: pv.adjusted_tomorrow_live.total_kwh
                if pv.adjusted_tomorrow_live
                else None,
                lambda pv: _pv_forecast_attrs(pv.adjusted_tomorrow_live),
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC At 6",
                lambda pv: pv.target_soc.value if pv.target_soc else None,
                lambda pv: _target_soc_trace_attrs(pv.target_soc),
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Live",
                lambda pv: pv.target_soc_live.value if pv.target_soc_live else None,
                lambda pv: _target_soc_trace_attrs(pv.target_soc_live),
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Tomorrow At 6",
                lambda pv: pv.target_soc_tomorrow.value
                if pv.target_soc_tomorrow
                else None,
                lambda pv: _target_soc_trace_attrs(pv.target_soc_tomorrow),
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Tomorrow Live",
                lambda pv: pv.target_soc_tomorrow_live.value
                if pv.target_soc_tomorrow_live
                else None,
                lambda pv: _target_soc_trace_attrs(pv.target_soc_tomorrow_live),
                unit="%",
            ),
            # Prev-workday instrumentation (Etap A) — today
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Prev Day 1",
                lambda pv: pv.target_soc_prev_days[0].value
                if pv.target_soc_prev_days[0]
                else None,
                lambda pv: _target_soc_trace_attrs(
                    pv.target_soc_prev_days[0], pv.consumption_profiles[0]
                ),
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Prev Day 2",
                lambda pv: pv.target_soc_prev_days[1].value
                if pv.target_soc_prev_days[1]
                else None,
                lambda pv: _target_soc_trace_attrs(
                    pv.target_soc_prev_days[1], pv.consumption_profiles[1]
                ),
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Prev Day 3",
                lambda pv: pv.target_soc_prev_days[2].value
                if pv.target_soc_prev_days[2]
                else None,
                lambda pv: _target_soc_trace_attrs(
                    pv.target_soc_prev_days[2], pv.consumption_profiles[2]
                ),
                unit="%",
            ),
            # Prev-workday instrumentation — tomorrow
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Tomorrow Prev Day 1",
                lambda pv: pv.target_soc_tomorrow_prev_days[0].value
                if pv.target_soc_tomorrow_prev_days[0]
                else None,
                lambda pv: _target_soc_trace_attrs(
                    pv.target_soc_tomorrow_prev_days[0], pv.consumption_profiles[0]
                ),
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Tomorrow Prev Day 2",
                lambda pv: pv.target_soc_tomorrow_prev_days[1].value
                if pv.target_soc_tomorrow_prev_days[1]
                else None,
                lambda pv: _target_soc_trace_attrs(
                    pv.target_soc_tomorrow_prev_days[1], pv.consumption_profiles[1]
                ),
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Tomorrow Prev Day 3",
                lambda pv: pv.target_soc_tomorrow_prev_days[2].value
                if pv.target_soc_tomorrow_prev_days[2]
                else None,
                lambda pv: _target_soc_trace_attrs(
                    pv.target_soc_tomorrow_prev_days[2], pv.consumption_profiles[2]
                ),
                unit="%",
            ),
            # Max safety sensors — max(live, prev_day_1..N)
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Max",
                lambda pv: pv.target_soc_max,
                lambda _pv: {},
                unit="%",
            ),
            PvForecastSensor(
                pv_forecast,
                coordinator,
                "Target Battery SOC Tomorrow Max",
                lambda pv: pv.target_soc_tomorrow_max,
                lambda _pv: {},
                unit="%",
            ),
            WeatherForecastHistorySensor(
                weather_history,
                weather_coordinator,
                coordinator,
            ),
        ]
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

    Jeśli profile przekazany i ma source_date, dodaje 'profile_date' attribute
    (informuje z którego workday-a wzięty consumption profile).
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


class SmartRceSensor(CoordinatorEntity[SmartRceDataUpdateCoordinator], RestoreSensor):
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

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self.ems.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

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


class PvForecastSensor(SensorEntity):
    """Sensor for weather-adjusted PV forecast data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        pv_forecast: PvForecastCoordinator,
        rce_coordinator: SmartRceDataUpdateCoordinator,
        name: str,
        value_fn: Callable[[PvForecastCoordinator], float | int | None],
        attr_fn: Callable[[PvForecastCoordinator], dict[str, Any]],
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

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self._pv_forecast.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

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
        weather_coordinator: Any,
        rce_coordinator: SmartRceDataUpdateCoordinator,
    ) -> None:
        self._weather_history = weather_history
        self._weather_coordinator = weather_coordinator
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_weather_forecast_history"
        self._attr_device_info = rce_coordinator.device_info
        self._attr_native_value: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore from RestoreSensor
        last_sensor_data = await self.async_get_last_sensor_data()
        if last_sensor_data and last_sensor_data.native_value:
            self._attr_native_value = last_sensor_data.native_value

        last_state = await self.async_get_last_state()
        if last_state:
            hours_attr = last_state.attributes.get("hours")
            if hours_attr:
                from homeassistant.util.dt import now as now_local

                self._weather_history.restore(hours_attr, now_local().date())

        # Listen for weather updates
        @callback
        def on_weather_update() -> None:
            self._handle_weather_update()

        remove_listener = self._weather_coordinator.async_add_listener(
            on_weather_update
        )
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

        _LOGGER.debug(
            "Setup of Weather Forecast History sensor %s (unique_id: %s)",
            self.entity_id,
            self._attr_unique_id,
        )

    @callback
    def _handle_weather_update(self) -> None:
        """Handle weather forecast update."""
        from homeassistant.util.dt import now as now_local

        now = now_local()
        today = now.date()

        # Update hours from forecast, get diff if changed
        result = self._weather_history.update_from_forecast(
            self._weather_coordinator.forecast_hourly, today, now
        )
        if result:
            diff_text, is_initial = result
            self.hass.loop.create_task(
                self._async_save_diff(now, diff_text, is_initial)
            )

        # Check if state should change (new hour)
        current_hour_str = f"{now.hour:02d}:00"
        current_value = self._attr_native_value or ""
        if not current_value.startswith(current_hour_str):
            condition = self._weather_history.get_condition(now.hour)
            self._attr_native_value = f"{current_hour_str} {condition}"

        self.async_write_ha_state()

    async def _async_save_diff(
        self, now: datetime, diff_text: str, is_initial: bool
    ) -> None:
        """Save forecast diff to file."""
        import aiofiles

        config_dir = self.hass.config.config_dir
        tag = "initial" if is_initial else "diff"
        filename = f"forecast_{tag}_{now.strftime('%Y-%m-%dT%H:%M')}.txt"
        path = f"{config_dir}/smart_rce/{filename}"
        async with aiofiles.open(path, mode="w", encoding="utf-8") as f:
            await f.write(diff_text)

    @property
    def native_value(self) -> str | None:
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"hours": self._weather_history.hours_attribute}


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

        @callback
        def listener() -> None:
            self.async_write_ha_state()

        remove_listener = self.ems.async_add_listener(listener)
        setattr(remove_listener, "_hass_callback", True)
        self.async_on_remove(remove_listener)

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
