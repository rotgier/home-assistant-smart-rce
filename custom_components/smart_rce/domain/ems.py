"""Energy Management System logic."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from statistics import mean
from typing import Final

from aiohttp import ClientSession

from custom_components.smart_rce.domain.rce import RceDayPrices
from custom_components.smart_rce.infrastructure.rce_api import RceApi

MAX_CONSECUTIVE_HOURS: Final[int] = 8


@dataclass(kw_only=True, frozen=False)
class HourPrices:  # noqa: D101
    hour: int
    price: float
    mean_price_consecutive_hours: list[float] = field(
        default_factory=lambda: [float("inf")] * MAX_CONSECUTIVE_HOURS
    )

class CsvTextBuilder(object):
    def __init__(self):
        self.csv_string = []

    def write(self, row):
        self.csv_string.append(row)


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
                candidate.price < 130
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
    winner, winner_consecutive_hours, min_prices_start_hours = (
        find_start_charge_hour(prices)
    )
    print("")
    print("")
    print("")
    print(prices.prices[0]["datetime"].date())
    for hour in range(24):
        suffix = ""
        for consecutive_hours in range(2, MAX_CONSECUTIVE_HOURS):
            if hour == min_prices_start_hours[consecutive_hours].hour:
                suffix = suffix + f"H{consecutive_hours + 1}"
                suffix = (
                        suffix
                        + f" {min_prices_start_hours[consecutive_hours].mean_price_consecutive_hours[consecutive_hours]:3.2f}   "
                )
                if (
                        hour == winner.hour
                        and consecutive_hours == winner_consecutive_hours
                ):
                    suffix = suffix + f"WIN H{winner_consecutive_hours + 1}   "
        print(f"{hour:2}: {prices.prices[hour]['price']:7.2f}    {suffix}")
