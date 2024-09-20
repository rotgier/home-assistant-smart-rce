"""Energy Management System logic."""

import csv
from dataclasses import dataclass, field
from datetime import date
from statistics import mean
from typing import Final

from custom_components.smart_rce.domain.rce import RceDayPrices

MAX_CONSECUTIVE_HOURS: Final[int] = 8


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

    def first_hour_of_charge(self, consecutive_hours: int):
        assert 1 <= consecutive_hours <= MAX_CONSECUTIVE_HOURS
        return self._start_charge_hours[consecutive_hours]

    def last_hour_of_charge(self, consecutive_hours: int):
        assert 1 <= consecutive_hours <= MAX_CONSECUTIVE_HOURS
        return self._start_charge_hours[consecutive_hours] + consecutive_hours - 1


@dataclass(kw_only=True, frozen=False)
class HourPrices:
    hour: int
    price: float
    mean_price_consecutive_hours: list[float] = field(
        default_factory=lambda: [float("inf")] * MAX_CONSECUTIVE_HOURS
    )


class CsvTextBuilder:
    def __init__(self) -> None:
        self.csv_string: list[str] = []

    def write(self, row):
        self.csv_string.append(row.replace("\r\n", ""))


def find_charge_hours(day_prices: RceDayPrices) -> EmsDayPrices:
    """Find start charge hour."""
    prices: list[float] = [item["price"] for item in day_prices.prices]
    consecutive_prices: list[HourPrices] = calculate_consecutive_prices(prices)
    min_consecutive_prices: list[HourPrices] = [None] * MAX_CONSECUTIVE_HOURS

    start_charge_hours: dict[int, int] = {}
    for consecutive_hours in range(MAX_CONSECUTIVE_HOURS):
        min_consecutive_prices[consecutive_hours] = min(
            consecutive_prices,
            key=lambda x: x.mean_price_consecutive_hours[consecutive_hours],
        )
        start_charge_hours[consecutive_hours + 1] = min_consecutive_prices[
            consecutive_hours
        ].hour

    best_consecutive_hours = find_best_consecutive_hours(prices, min_consecutive_prices)

    return EmsDayPrices(
        day=day_prices.prices[0]["datetime"].date(),
        hour_price=prices,
        start_charge_hours=start_charge_hours,
        best_consecutive_hours=best_consecutive_hours + 1,
    )


def find_best_consecutive_hours(
    prices: list[float], min_consecutive_prices: list[HourPrices]
) -> int:
    winner: HourPrices = min_consecutive_prices[2]
    winner_3_consecutive_hours_max_price = max(prices[winner.hour : winner.hour + 3])
    best_consecutive_hours: int = 2
    for consecutive_hours in range(3, MAX_CONSECUTIVE_HOURS):
        candidate: HourPrices = min_consecutive_prices[consecutive_hours]
        if candidate.hour == winner.hour:
            winner = min_consecutive_prices[consecutive_hours]
            best_consecutive_hours = consecutive_hours
        elif candidate.hour < winner.hour:
            if (
                candidate.price < 100
                or candidate.price - winner_3_consecutive_hours_max_price < 40
            ):
                winner = min_consecutive_prices[consecutive_hours]
                best_consecutive_hours = consecutive_hours

    return best_consecutive_hours


def calculate_consecutive_prices(prices: list[float]) -> list[HourPrices]:
    result: list[HourPrices] = [
        HourPrices(hour=hour, price=price) for hour, price in enumerate(prices)
    ]
    for hour in range(6, 16):
        for consecutive_hours in range(MAX_CONSECUTIVE_HOURS):
            avg = mean(prices[hour : hour + consecutive_hours + 1])
            result[hour].mean_price_consecutive_hours[consecutive_hours] = avg
    return result


def create_csv(rce_prices: RceDayPrices):
    ems_prices: EmsDayPrices = find_charge_hours(rce_prices)

    csv_builder = CsvTextBuilder()
    writer = csv.writer(csv_builder, delimiter="\t")

    for hour in range(24):
        current_price = ems_prices.hour_price[hour]

        row = [ems_prices.day] if hour == 0 else [""]
        row.extend([hour, str(current_price).replace(".", ",")])

        for consecutive_hours in range(MAX_CONSECUTIVE_HOURS, 2, -1):
            first_hour = ems_prices.first_hour_of_charge(consecutive_hours)
            last_hour = ems_prices.last_hour_of_charge(consecutive_hours)
            mark = ""
            if first_hour <= hour <= last_hour:
                mark = f"H{consecutive_hours}"
                if consecutive_hours == ems_prices.best_consecutive_hours:
                    mark += "*"
            row.append(mark)

        current_price_size = max(round(current_price / 10), 0)
        row.append("*" * current_price_size if current_price_size else "|")
        row.append(ems_prices.day)

        writer.writerow(row)

    return csv_builder.csv_string
