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
    water_heater_big_is_on: bool | None = None
    water_heater_small_is_on: bool | None = None

    battery_soc: float | None = None
    battery_charge_limit: float | None = None  # A (ampery z BMS)
    battery_power_2_minutes: float | None = None
    consumption_minus_pv_2_minutes: float | None = None
    exported_energy_hourly: float | None = None
    heater_mode: str | None = None
    depth_of_discharge: float | None = (
        None  # % (number.goodwe_depth_of_discharge_on_grid)
    )


class WaterHeaterManager:
    BIG_POWER: int = 3000
    SMALL_POWER: int = 1500
    BOTH_POWER: int = 4500
    BATTERY_VOLTAGE: int = 290

    BOTH_ARE_ON: str = "both_are_on"
    BIG_IS_ON: str = "big_is_on"
    SMALL_IS_ON: str = "small_is_on"
    BOTH_ARE_OFF: str = "both_are_off"

    # Hierarchia stanów do porównania
    _STATE_ORDER: dict[str, int] = {
        "both_are_off": 0,
        "small_is_on": 1,
        "big_is_on": 2,
        "both_are_on": 3,
    }

    _UPGRADE_MAP: dict[str, str] = {
        "both_are_off": "small_is_on",
        "small_is_on": "big_is_on",
        "big_is_on": "both_are_on",
        "both_are_on": "both_are_on",
    }

    def __init__(self) -> None:
        self.should_turn_on: bool = False
        self.should_turn_off: bool = False
        self.should_turn_on_small: bool = False
        self.should_turn_off_small: bool = False
        self.should_block_battery_charge: bool = False
        self._hourly_balance_negative: bool = False
        # BALANCED diagnostics
        self.balanced_heater_budget: float | None = None
        self.balanced_baseline: str | None = None
        self.balanced_upgrade_active: bool = False

    def update(self, state: InputState) -> None:
        if self._none_present(state):
            return

        current_state = self._current_state(state)
        target = self._determine_target(state, current_state)

        self.should_turn_on = target in (self.BIG_IS_ON, self.BOTH_ARE_ON)
        self.should_turn_off = target in (self.SMALL_IS_ON, self.BOTH_ARE_OFF)
        self.should_turn_on_small = target in (self.SMALL_IS_ON, self.BOTH_ARE_ON)
        self.should_turn_off_small = target in (self.BIG_IS_ON, self.BOTH_ARE_OFF)
        self.should_block_battery_charge = (
            self._hourly_balance_negative
            and state.depth_of_discharge is not None
            and state.depth_of_discharge == 0
            and state.battery_charge_limit is not None
            and state.battery_charge_limit >= 2
        )
        # BALANCED diagnostics
        mode = state.heater_mode or "WASTED"
        if mode != "BALANCED":
            self.balanced_heater_budget = None
            self.balanced_baseline = None
            self.balanced_upgrade_active = False
        elif self._hourly_balance_negative:
            pv_available = -state.consumption_minus_pv_2_minutes
            self.balanced_heater_budget = -pv_available
            self.balanced_baseline = "negative_energy"
            self.balanced_upgrade_active = False

    def _current_state(self, state: InputState) -> str:
        if state.water_heater_big_is_on and state.water_heater_small_is_on:
            return self.BOTH_ARE_ON
        if state.water_heater_big_is_on:
            return self.BIG_IS_ON
        if state.water_heater_small_is_on:
            return self.SMALL_IS_ON
        return self.BOTH_ARE_OFF

    def _determine_target(self, state: InputState, current_state: str) -> str:
        pv_available = -state.consumption_minus_pv_2_minutes
        battery_soc = state.battery_soc
        battery_charge_limit = state.battery_charge_limit
        exported_energy = state.exported_energy_hourly * 1000  # kWh → Wh

        # GUARD: Ochrona bilansu godzinowego (tylko w trybie charge-only, DoD=0%)
        if state.depth_of_discharge is not None and state.depth_of_discharge == 0:
            if exported_energy < 0:
                self._hourly_balance_negative = True
                return self.BOTH_ARE_OFF

            if self._hourly_balance_negative and exported_energy < 50:
                return self.BOTH_ARE_OFF

            self._hourly_balance_negative = False
        else:
            self._hourly_balance_negative = False

        mode = state.heater_mode or "WASTED"

        if mode == "ASAP":
            target = self._asap_target(
                pv_available, battery_charge_limit, current_state
            )
        elif mode == "BALANCED":
            return self._balanced_target(
                pv_available,
                battery_charge_limit,
                battery_soc,
                exported_energy,
                current_state,
            )
        else:
            target = self._wasted_target(
                pv_available, battery_charge_limit, current_state
            )

        # Override: exported_energy — nie marnuj skumulowanego eksportu
        # (tylko dla ASAP i WASTED, NIE dla BALANCED)
        if battery_soc >= 90:
            if exported_energy > 300 and pv_available > 0:
                if target in (self.BOTH_ARE_OFF, self.SMALL_IS_ON):
                    target = self.BIG_IS_ON

            if exported_energy > 80:
                if self._STATE_ORDER[current_state] > self._STATE_ORDER[target]:
                    target = current_state

        return target

    def _asap_target(
        self, pv: float, battery_charge_limit: float, current_state: str
    ) -> str:
        battery_full = battery_charge_limit == 0
        thresholds = (1500, 3000, 4500) if battery_full else (1800, 3300, 4800)
        hysteresis = 500

        if pv > thresholds[2] or (
            pv > thresholds[2] - hysteresis and current_state == self.BOTH_ARE_ON
        ):
            return self.BOTH_ARE_ON
        if pv > thresholds[1] or (
            pv > thresholds[1] - hysteresis
            and current_state in (self.BIG_IS_ON, self.BOTH_ARE_ON)
        ):
            return self.BIG_IS_ON
        if pv > thresholds[0] or (
            pv > thresholds[0] - hysteresis
            and current_state in (self.SMALL_IS_ON, self.BIG_IS_ON, self.BOTH_ARE_ON)
        ):
            return self.SMALL_IS_ON
        return self.BOTH_ARE_OFF

    def _wasted_target(
        self, pv: float, battery_charge_limit: float, current_state: str
    ) -> str:
        battery_max_charge = battery_charge_limit * self.BATTERY_VOLTAGE
        pv_surplus = pv - battery_max_charge
        hysteresis = 500

        # pv_surplus nie zależy od stanu grzałek (sensor minus_heaters)
        # Step-up: OFF → BIG → BOTH (small nigdy sam w WASTED)
        if pv_surplus > self.BIG_POWER or (
            pv_surplus > self.BIG_POWER - hysteresis
            and current_state == self.BOTH_ARE_ON
        ):
            return self.BOTH_ARE_ON
        if pv_surplus > 0 or (
            pv_surplus > -hysteresis
            and current_state in (self.BIG_IS_ON, self.BOTH_ARE_ON)
        ):
            return self.BIG_IS_ON
        return self.BOTH_ARE_OFF

    def _balanced_target(
        self,
        pv: float,
        battery_charge_limit: float,
        battery_soc: float,
        exported_energy: float,
        current_state: str,
    ) -> str:
        # Rezerwacja (charge_limit: dyskretne 0, 1, 2, 7, 18A)
        if battery_charge_limit > 7:
            reserved = 3000 if battery_soc < 50 else 2000
        elif battery_charge_limit > 2:
            reserved = 1000
        elif battery_charge_limit == 2:
            reserved = 300
        else:
            reserved = 0

        heater_budget = pv - reserved
        hysteresis = 500

        # Piętro 1 — Baseline (histereza trzyma tylko obecny stan, nie wyższy)
        if heater_budget >= self.BOTH_POWER or (
            heater_budget >= self.BOTH_POWER - hysteresis
            and current_state == self.BOTH_ARE_ON
        ):
            baseline = self.BOTH_ARE_ON
        elif heater_budget >= self.BIG_POWER or (
            heater_budget >= self.BIG_POWER - hysteresis
            and current_state == self.BIG_IS_ON
        ):
            baseline = self.BIG_IS_ON
        elif heater_budget >= self.SMALL_POWER or (
            heater_budget >= self.SMALL_POWER - hysteresis
            and current_state == self.SMALL_IS_ON
        ):
            baseline = self.SMALL_IS_ON
        else:
            baseline = self.BOTH_ARE_OFF

        # Piętro 2 — Upgrade z budżetu eksportu godzinowego
        upgrade = self._UPGRADE_MAP[baseline]
        target = baseline
        if upgrade != baseline:
            if exported_energy > 100 or (
                self._STATE_ORDER[current_state] >= self._STATE_ORDER[upgrade]
                and exported_energy > 30
            ):
                target = upgrade

        # Diagnostyka
        self.balanced_heater_budget = -heater_budget
        self.balanced_baseline = baseline
        self.balanced_upgrade_active = target != baseline

        return target

    def _none_present(self, state: InputState) -> bool:
        return (
            state.water_heater_big_is_on is None
            or state.water_heater_small_is_on is None
            or state.battery_soc is None
            or state.battery_charge_limit is None
            or state.battery_power_2_minutes is None
            or state.consumption_minus_pv_2_minutes is None
            or state.exported_energy_hourly is None
        )


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
        # Rotate: jeśli today jest z innego dnia, przenieś tomorrow → today
        if (
            self.today.start_charge_hour_datetime
            and self.tomorrow.hour_price
            and self.today.start_charge_hour_datetime.date() != now.date()
        ):
            _LOGGER.info(
                "Rotating RCE prices: tomorrow → today (today was %s, now is %s)",
                self.today.start_charge_hour_datetime.date(),
                now.date(),
            )
            self.today = self.tomorrow
            self.tomorrow = EmsDayData.empty()

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

    def restore_rce_today(self, prices_attr: list[dict], now: datetime) -> None:
        """Restore today's RCE prices from sensor attributes."""
        rce_prices = _restore_rce_day_prices(prices_attr)
        if rce_prices:
            self.today = EmsDayData.create(find_charge_hours(rce_prices))
            self.update_hourly(now)

    def restore_rce_tomorrow(self, prices_attr: list[dict]) -> None:
        """Restore tomorrow's RCE prices from sensor attributes."""
        rce_prices = _restore_rce_day_prices(prices_attr)
        if rce_prices:
            self.tomorrow = EmsDayData.create(find_charge_hours(rce_prices))

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
        return datetime.combine(self.day, time(int(hour), minute, 0), TIMEZONE)


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


def _restore_rce_day_prices(prices_attr: list[dict]) -> RceDayPrices | None:
    """Build RceDayPrices from restored sensor attributes."""
    if not prices_attr:
        return None
    prices = [
        {"datetime": datetime.fromisoformat(p["datetime"]), "price": p["price"]}
        for p in prices_attr
    ]
    return RceDayPrices(published_at=None, prices=prices)


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
