from collections.abc import Callable

from .domain.ems import InputState

# @dataclass
# class LOL:
#     water_heater_is_on: bool | None = None

#     battery_soc: float | None = None
#     battery_power_2_minutes: float | None = None
#     consumption_minus_pv_2_minutes: float | None = None
#     exported_energy_hourly: float | None = None


# # TODO Does this class need to be @dataclass ?
# @dataclass
# class InputStateMapper(InputState):
#     def set_water_heater_is_on(self, state: str) -> None:
#         # TODO this string is probably "on" / "off" so this mapping is WRONG! :|
#         self.water_heater_is_on = map_bool(state)

#     def set_battery_soc(self, state: str) -> None:
#         self.battery_soc = map_float(state)

#     def set_battery_power_2_minutes(self, state: str) -> None:
#         self.battery_power_2_minutes = map_float(state)

#     def set_consumption_minus_pv_2_minutes(self, state: str) -> None:
#         self.consumption_minus_pv_2_minutes = map_float(state)

#     def set_exported_energy_hourly(self, state: str) -> None:
#         self.exported_energy_hourly = map_float(state)


def map_bool(string: str) -> bool | None:
    try:
        return bool(string)
    except (ValueError, TypeError):
        return None


def map_float(string: str) -> bool | None:
    try:
        return float(string)
    except (ValueError, TypeError):
        return None


def set_water_heater_is_on(input: InputState, state: str) -> None:
    # TODO this string is probably "on" / "off" so this mapping is WRONG! :|
    input.water_heater_big_is_on = map_bool(state)


def set_battery_soc(input: InputState, state: str) -> None:
    input.battery_soc = map_float(state)


def set_battery_power_2_minutes(input: InputState, state: str) -> None:
    input.battery_power_2_minutes = map_float(state)


def set_consumption_minus_pv_2_minutes(input: InputState, state: str) -> None:
    input.consumption_minus_pv_2_minutes = map_float(state)


def set_exported_energy_hourly(input: InputState, state: str) -> None:
    input.exported_energy_hourly = map_float(state)


HASS_STATE_MAPPER: dict[str, Callable[[InputState, str], None]] = {
    "switch.grzalka_wody_local": set_water_heater_is_on,
    "sensor.battery_state_of_charge": set_battery_soc,
    "sensor.battery_power_avg_2_minutes": set_battery_power_2_minutes,
    "sensor.house_consumption_minus_pv_avg_2_minutes": set_consumption_minus_pv_2_minutes,
    "sensor.total_export_import_hourly": set_exported_energy_hourly,
}

input_state = InputState()


HASS_STATE_MAPPER["switch.grzalka_wody_local"](input_state, "true")
HASS_STATE_MAPPER["sensor.battery_state_of_charge"](input_state, "96")

check = 2
