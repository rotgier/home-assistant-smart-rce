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
    def __init__(self):
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
    # winner_3_consecutive_hours_price = max(
    #     winner.price,
    #     winner.mean_price_consecutive_hours[2],
    # )
    winner_3_consecutive_hours_max_price = max(prices[winner.hour : winner.hour + 3])
    winner_consecutive_hours: int = 2
    for consecutive_hours in range(3, MAX_CONSECUTIVE_HOURS):
        candidate: HourPrices = min_consecutive_prices[consecutive_hours]
        if candidate.hour == winner.hour:
            winner = min_consecutive_prices[consecutive_hours]
            winner_consecutive_hours = consecutive_hours
        elif candidate.hour < winner.hour:
            winner_price = max(
                winner.price,
                winner.mean_price_consecutive_hours[winner_consecutive_hours],
            )
            if (
                candidate.price < 100
                or candidate.price - winner_3_consecutive_hours_max_price < 40
            ):
                winner = min_consecutive_prices[consecutive_hours]
                winner_consecutive_hours = consecutive_hours
    return winner, winner_consecutive_hours, min_consecutive_prices


def calculate_consecutive_prices(prices: list[float]) -> list[HourPrices]:
    result: list[HourPrices] = [
        HourPrices(hour=hour, price=price) for hour, price in enumerate(prices)
    ]
    for hour in range(6, 16):
        for consecutive_hours in range(MAX_CONSECUTIVE_HOURS):
            avg = mean(prices[hour : hour + consecutive_hours + 1])
            result[hour].mean_price_consecutive_hours[consecutive_hours] = avg
    return result


def present_winner(prices: RceDayPrices):
    winner, winner_consecutive_hours, min_prices_start_hours = find_start_charge_hour(
        prices
    )
    csv_builder = CsvTextBuilder()

    writer = csv.writer(csv_builder, delimiter="\t")
    # writer.writerow([""] * 8 + [prices.prices[0]["datetime"].date()])

    for hour in range(24):
        suffix = ""
        current_price = prices.prices[hour]["price"]
        current_day = prices.prices[0]["datetime"].date()
        if hour == 0:
            suffix_list = [current_day]
        else:
            suffix_list = [""]
        suffix_list.extend([hour, str(current_price).replace(".", ",")])
        for consecutive_hours in reversed(range(2, MAX_CONSECUTIVE_HOURS)):
            start_hour_of_consecutive_hours = min_prices_start_hours[
                consecutive_hours
            ].hour
            if (
                hour >= start_hour_of_consecutive_hours
                and hour <= start_hour_of_consecutive_hours + consecutive_hours
            ):
                hours = f"H{consecutive_hours + 1}"
                suffix += hours

                if hour == start_hour_of_consecutive_hours:
                    mean_price_consecutive_hours = f"{min_prices_start_hours[consecutive_hours].mean_price_consecutive_hours[consecutive_hours]:3.2f}"
                    suffix += f" {mean_price_consecutive_hours}   "
                    if (
                        hour == winner.hour
                        and consecutive_hours == winner_consecutive_hours
                    ):
                        to_add = f"WIN H{winner_consecutive_hours + 1}   "
                        suffix += to_add
                        suffix_list.append(f"{hours}*")
                    else:
                        suffix_list.append(f"{hours}")

                else:
                    if consecutive_hours == winner_consecutive_hours:
                        suffix_list.append(f"{hours}*")
                    else:
                        suffix_list.append(f"{hours}")
                    suffix += "   "
            else:
                suffix_list.append("")
        rounded_current_price_size = round(current_price / 10)
        current_price_size = max(rounded_current_price_size, 0)
        if current_price_size:
            suffix_list.append("*" * current_price_size)
        else:
            suffix_list.append("|")
        suffix_list.append(current_day)
        writer.writerow(suffix_list)
    #     print(f"{hour:2}: {prices.prices[hour]['price']:7.2f}    {suffix}")
    #
    # print("CSV:")
    # for line in csv_builder.csv_string:
    #     print(line)

    return csv_builder.csv_string
