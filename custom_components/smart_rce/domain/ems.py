"""Energy Management System logic."""

import csv
from dataclasses import dataclass, field
from statistics import mean
from typing import Final

from custom_components.smart_rce.domain.rce import RceDayPrices

MAX_CONSECUTIVE_HOURS: Final[int] = 8


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


def find_start_charge_hour(day_prices: RceDayPrices):
    """Find start charge hour."""
    prices: list[float] = [item["price"] for item in day_prices.prices]
    consecutive_prices: list[HourPrices] = calculate_consecutive_prices(prices)
    min_consecutive_prices = [None] * MAX_CONSECUTIVE_HOURS
    for consecutive_hours in range(MAX_CONSECUTIVE_HOURS):
        min_consecutive_prices[consecutive_hours] = min(
            consecutive_prices,
            key=lambda x: x.mean_price_consecutive_hours[consecutive_hours],
        )
    winner: HourPrices = min_consecutive_prices[2]
    winner_3_consecutive_hours_max_price = max(prices[winner.hour : winner.hour + 3])
    winner_consecutive_hours: int = 2
    for consecutive_hours in range(3, MAX_CONSECUTIVE_HOURS):
        candidate: HourPrices = min_consecutive_prices[consecutive_hours]
        if candidate.hour == winner.hour:
            winner = min_consecutive_prices[consecutive_hours]
            winner_consecutive_hours = consecutive_hours
        elif candidate.hour < winner.hour:
            if (
                candidate.price < 100
                or candidate.price - winner_3_consecutive_hours_max_price < 40
            ):
                winner = min_consecutive_prices[consecutive_hours]
                winner_consecutive_hours = consecutive_hours
    return winner_consecutive_hours, min_consecutive_prices


def calculate_consecutive_prices(prices: list[float]) -> list[HourPrices]:
    result: list[HourPrices] = [
        HourPrices(hour=hour, price=price) for hour, price in enumerate(prices)
    ]
    for hour in range(6, 16):
        for consecutive_hours in range(MAX_CONSECUTIVE_HOURS):
            avg = mean(prices[hour : hour + consecutive_hours + 1])
            result[hour].mean_price_consecutive_hours[consecutive_hours] = avg
    return result


def create_csv(prices: RceDayPrices):
    winner_consecutive_hours, min_prices_start_hours = find_start_charge_hour(prices)

    csv_builder = CsvTextBuilder()
    writer = csv.writer(csv_builder, delimiter="\t")

    for hour in range(24):
        current_day = prices.prices[0]["datetime"].date()
        current_price = prices.prices[hour]["price"]

        row = [current_day] if hour == 0 else [""]
        row.extend([hour, str(current_price).replace(".", ",")])

        for consecutive_hours in reversed(range(2, MAX_CONSECUTIVE_HOURS)):
            start_hour = min_prices_start_hours[consecutive_hours].hour
            if start_hour <= hour <= start_hour + consecutive_hours:
                hours = f"H{consecutive_hours + 1}"
                if consecutive_hours == winner_consecutive_hours:
                    row.append(f"{hours}*")
                else:
                    row.append(f"{hours}")
            else:
                row.append("")

        current_price_size = max(round(current_price / 10), 0)
        row.append("*" * current_price_size if current_price_size else "|")
        row.append(current_day)

        writer.writerow(row)

    return csv_builder.csv_string
