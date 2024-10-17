"""Energy Management System logic."""

from __future__ import annotations

from collections.abc import Callable
import csv
from dataclasses import dataclass
from datetime import date, datetime, time
import logging
from statistics import mean
from typing import Final

from custom_components.smart_rce.domain.rce import TIMEZONE, RceData, RceDayPrices

type CALLBACK_TYPE = Callable[[], None]

_LOGGER = logging.getLogger(__name__)

MAX_CONSECUTIVE_HOURS: Final[int] = 8
INITIAL_BEST_CONSECUTIVE_HOURS: Final[int] = 3
POSSIBLE_CONSECUTIVE_HOURS: Final[range] = range(3, MAX_CONSECUTIVE_HOURS + 1)


@dataclass
class InputState:
    water_heater_is_on: bool | None = None

    battery_soc: float | None = None
    battery_power_2_minutes: float | None = None
    consumption_minus_pv_2_minutes: float | None = None
    exported_energy_hourly: float | None = None


class WaterHeaterManager:
    HEATER_POWER: int = 3000

    def __init__(self) -> None:
        self.should_turn_on: bool = False
        self.should_turn_off: bool = False

        self.should_turn_on_small: bool = False
        self.should_turn_off_small: bool = False

    def update(self, input: InputState):
        self.should_turn_on, self.should_turn_off = self._update(input)

    def _none_present(self, state: InputState) -> bool:
        return (
            state.water_heater_is_on is None
            or state.battery_soc is None
            or state.battery_power_2_minutes is None
            or state.consumption_minus_pv_2_minutes is None
            or state.exported_energy_hourly is None
        )

    def _update(self, state: InputState) -> tuple[bool, bool]:
        turn_on: bool = False
        turn_off: bool = False

        # turn_on_small: bool = False
        # turn_off_small: bool = False

        if self._none_present(state):
            return (turn_on, turn_off)

        water_heater_is_on = state.water_heater_is_on
        pv_available = -state.consumption_minus_pv_2_minutes
        battery_soc = state.battery_soc
        battery_power = -state.battery_power_2_minutes
        # battery_power_expected = pv_available - self.HEATER_POWER
        exported_energy = state.exported_energy_hourly * 1000

        # _LOGGER.debug(
        #     f"water_heater_is_on: {water_heater_is_on} pv_available: {pv_available} battery_soc: {battery_soc} battery_power: {battery_power} battery_power_expected: {battery_power_expected} exported_energy:{exported_energy}"
        # )

        if battery_soc <= 89:
            # more pv than battery can charge
            turn_on = pv_available > 5200 - 1500
            # assert turn_on == (battery_power_expected > 2200)

            # battery could charge faster
            turn_off = battery_power < 1900 - 1000

        elif battery_soc <= 96:
            turn_on = pv_available > 4500 - 1500
            # assert turn_on == (battery_power_expected > 1500)

            turn_off = battery_power < 1000 - 900

        elif battery_soc in [97, 98]:
            turn_on = pv_available > 3500
            # assert turn_on == (battery_power_expected > 500)

            turn_off = battery_power < 400 - 300

        elif battery_soc == 99:
            turn_on = pv_available > 3300
            # assert turn_on == (battery_power_expected > 300)

            turn_off = battery_power < 290 - 200

        elif battery_soc == 100:
            turn_on = pv_available > self.HEATER_POWER
            turn_off = pv_available < 0

        if battery_soc >= 90:
            if exported_energy > 300 and pv_available > 0:
                turn_on = True
                turn_off = False

            if exported_energy > 80 and water_heater_is_on:
                # could check if pv would be available after heater is turned off
                # pv_available + self.HEATER_POWER > 0
                # then water_heater could be turned off because accumulated exported_energy
                # should be used for regular house consumption => without water_heater
                # (but in fact it depends on the consumption speed of the accumulated exported_energy)

                turn_off = False

        return (turn_on, turn_off)


class Ems:
    def __init__(self) -> None:
        self._listeners: dict[CALLBACK_TYPE, CALLBACK_TYPE] = {}
        self._ha: InputState = None
        self.today: EmsDayData = EmsDayData.empty()
        self.tomorrow: EmsDayData = EmsDayData.empty()
        self.rce_data: RceData = None
        self.current_price: float = None
        self.water_heater: WaterHeaterManager = WaterHeaterManager()

    def update_state(self, state: InputState) -> None:
        self.water_heater.update(state)
        self._async_update_listeners()

    def update_hourly(self, now: datetime) -> None:
        if self.today.hour_price:
            self.current_price = self.today.hour_price[now.hour]
            self._async_update_listeners()

    def update_rce(self, now: datetime, data: RceData) -> None:
        if data:
            self.rce_data = data
            if data.today:
                self.today = EmsDayData.create(find_charge_hours(data.today))

            if data.tomorrow:
                self.tomorrow = EmsDayData.create(find_charge_hours(data.tomorrow))
            else:
                self.tomorrow = EmsDayData.empty()

            self.update_hourly(now)

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> Callable[[], None]:
        def remove_listener() -> None:
            self._listeners.pop(remove_listener)

        self._listeners[remove_listener] = update_callback
        return remove_listener

    def _async_update_listeners(self) -> None:
        for update_callback in self._listeners.values():
            update_callback()


class EmsDayPrices:
    def __init__(
        self,
        day: date,
        hour_price: list[float],
        start_charge_hours: dict[int, int],
        best_consecutive_hours: int,
    ) -> None:
        self.day: date = day
        self.hour_price: tuple[float] = tuple(hour_price)
        self._start_charge_hours: dict[int, int] = start_charge_hours
        self.best_consecutive_hours: int = best_consecutive_hours

    def first_hour_of_charge(self, consecutive_hours: int) -> int:
        assert 1 <= consecutive_hours <= MAX_CONSECUTIVE_HOURS
        return self._start_charge_hours[consecutive_hours]

    def last_hour_of_charge(self, consecutive_hours: int) -> int:
        assert 1 <= consecutive_hours <= MAX_CONSECUTIVE_HOURS
        return self._start_charge_hours[consecutive_hours] + consecutive_hours - 1

    def best_start_charge_hour(self) -> float:
        best_hour = self._start_charge_hours[self.best_consecutive_hours]
        if self.best_consecutive_hours == INITIAL_BEST_CONSECUTIVE_HOURS:
            return best_hour - 0.5
        return best_hour

    def best_end_charge_hour(self) -> float:
        best_consecutive = self.best_consecutive_hours
        return self._start_charge_hours[best_consecutive] + best_consecutive

    def hour_to_timestamp(self, hour: int) -> datetime:
        minute = int(hour * 60 % 60)
        return datetime.combine(self.day, time(hour, minute, 0), TIMEZONE)


@dataclass
class EmsDayData:
    hour_price: tuple[float] | None
    start_charge_hour: datetime | None
    start_charge_hour_datetime: datetime | None
    end_charge_hour: datetime | None
    end_charge_hour_datetime: datetime | None

    @classmethod
    def create(cls, prices: EmsDayPrices) -> EmsDayData:
        start_charge_hour = prices.best_start_charge_hour()
        end_charge_hour = prices.best_end_charge_hour()
        return cls(
            start_charge_hour=start_charge_hour,
            start_charge_hour_datetime=prices.hour_to_timestamp(start_charge_hour),
            end_charge_hour=end_charge_hour,
            end_charge_hour_datetime=prices.hour_to_timestamp(end_charge_hour),
            hour_price=prices.hour_price,
        )

    @classmethod
    def empty(cls) -> EmsDayData:
        return EmsDayData(None, None, None, None, None)


class CsvTextBuilder:
    def __init__(self) -> None:
        self.csv_string: list[str] = []

    def write(self, row):
        self.csv_string.append(row.replace("\r\n", ""))


def find_charge_hours(rce_prices: RceDayPrices) -> EmsDayPrices:
    """Find start charge hour."""
    prices: list[float] = [item["price"] for item in rce_prices.prices]
    start_charge_hours: dict[int, int] = calculate_start_charge_hours(prices)
    best_consecutive_hours = find_best_consecutive_hours(prices, start_charge_hours)
    return EmsDayPrices(
        day=rce_prices.prices[0]["datetime"].date(),
        hour_price=prices,
        start_charge_hours=start_charge_hours,
        best_consecutive_hours=best_consecutive_hours,
    )


def calculate_start_charge_hours(prices: list[float]) -> dict[int, int]:
    start_charge_hours: dict[int, int] = {}
    for consecutive_hours in POSSIBLE_CONSECUTIVE_HOURS:
        min_avg = float("inf")
        best_hour = 0
        for hour in range(6, 16):
            avg = mean(prices[hour : hour + consecutive_hours])
            if avg < min_avg:
                min_avg = avg
                best_hour = hour
        start_charge_hours[consecutive_hours] = best_hour
    return start_charge_hours


def find_best_consecutive_hours(
    prices: list[float], start_charge_hours: dict[int, int]
) -> int:
    best_consecutive_hours = INITIAL_BEST_CONSECUTIVE_HOURS
    best_hour: int = start_charge_hours[best_consecutive_hours]

    initial_consecutive_hours_max_price = max(
        prices[best_hour : best_hour + best_consecutive_hours]
    )
    hours_to_check = filter(
        lambda x: x > INITIAL_BEST_CONSECUTIVE_HOURS, POSSIBLE_CONSECUTIVE_HOURS
    )
    for consecutive_hours in hours_to_check:
        candidate: int = start_charge_hours[consecutive_hours]
        if (
            candidate == best_hour
            or candidate < best_hour
            and (
                prices[candidate] < 100
                or prices[candidate] - initial_consecutive_hours_max_price < 45
            )
        ):
            best_consecutive_hours = consecutive_hours

    return best_consecutive_hours


def create_csv(rce_prices: RceDayPrices):
    ems_prices: EmsDayPrices = find_charge_hours(rce_prices)

    csv_builder = CsvTextBuilder()
    writer = csv.writer(csv_builder, delimiter="\t")

    for hour in range(24):
        current_price = ems_prices.hour_price[hour]

        row = [ems_prices.day] if hour == 0 else [""]
        row.append(hour)
        row.append(str(current_price).replace(".", ","))

        for consecutive_hours in reversed(POSSIBLE_CONSECUTIVE_HOURS):
            first_hour = ems_prices.first_hour_of_charge(consecutive_hours)
            last_hour = ems_prices.last_hour_of_charge(consecutive_hours)
            mark = ""
            if first_hour <= hour <= last_hour:
                mark = f"H{consecutive_hours}"
                if consecutive_hours == ems_prices.best_consecutive_hours:
                    # TODO this should be moved to a test
                    if consecutive_hours == 3:
                        assert ems_prices.best_start_charge_hour() == first_hour - 0.5
                    else:
                        assert ems_prices.best_start_charge_hour() == first_hour
                    mark += "*"
            row.append(mark)

        current_price_size = max(round(current_price / 10), 0)
        row.append("*" * current_price_size if current_price_size else "|")
        row.append(ems_prices.day)

        writer.writerow(row)

    return csv_builder.csv_string
