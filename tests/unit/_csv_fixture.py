"""CSV diagnostic fixture for charge-hours snapshot tests.

Generates per-day charge-hours table from `RceDayPrices` — used by
`test_ems.py::test_find_charge_hours` to compare against checked-in
fixtures (`tests/fixtures/charge_hours_*.csv`). Test-only — żyło
wcześniej w `domain/ems.py` z TODO komentarzem "this should be moved
to a test".

Buduje raw `start_charge_hours` dict per N=3..8 + best_consecutive_hours
przez bezpośrednie wywołanie module-level helpers z `charge_slots.py` —
ChargeWindow (output dla domain) zawiera tylko best, a CSV diagnostic
prezentuje wszystkie opcje N=3..8 dla porównania.
"""

from __future__ import annotations

import csv

from custom_components.smart_rce.domain.charge_slots import (
    DEFAULT_HEATER_RCE_THRESHOLD,
    DEFAULT_INITIAL_HOURS,
    MAX_CONSECUTIVE_HOURS,
    calculate_start_charge_hours,
    find_best_consecutive_hours,
    shift_earlier_if_cheap,
)
from custom_components.smart_rce.domain.rce import RceDayPrices

_POSSIBLE_CONSECUTIVE_HOURS = range(DEFAULT_INITIAL_HOURS, MAX_CONSECUTIVE_HOURS + 1)


class CsvTextBuilder:
    def __init__(self) -> None:
        self.csv_string: list[str] = []

    def write(self, row):
        self.csv_string.append(row.replace("\r\n", ""))


def create_csv(rce_prices: RceDayPrices):
    prices: list[float] = list(rce_prices.hour_price)
    start_charge_hours = calculate_start_charge_hours(prices)
    best_n = find_best_consecutive_hours(prices, start_charge_hours)
    new_n, shifted_start = shift_earlier_if_cheap(
        prices, start_charge_hours[best_n], best_n, DEFAULT_HEATER_RCE_THRESHOLD
    )
    start_charge_hours[new_n] = shifted_start
    best_consecutive_hours = new_n
    day = rce_prices.day

    csv_builder = CsvTextBuilder()
    writer = csv.writer(csv_builder, delimiter="\t")

    for hour in range(24):
        current_price = prices[hour]

        row = [day] if hour == 0 else [""]
        row.append(hour)
        row.append(str(current_price).replace(".", ","))

        for consecutive_hours in reversed(_POSSIBLE_CONSECUTIVE_HOURS):
            first_hour = start_charge_hours[consecutive_hours]
            last_hour = first_hour + consecutive_hours - 1
            mark = ""
            if first_hour <= hour <= last_hour:
                mark = f"H{consecutive_hours}"
                if consecutive_hours == best_consecutive_hours:
                    if consecutive_hours == DEFAULT_INITIAL_HOURS:
                        assert first_hour == shifted_start
                    mark += "*"
            row.append(mark)

        current_price_size = max(round(current_price / 10), 0)
        row.append("*" * current_price_size if current_price_size else "|")
        row.append(day)

        writer.writerow(row)

    return csv_builder.csv_string
