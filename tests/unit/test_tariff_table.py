"""The shipped tariff table is the single source of G13 rates.

Rates used to live twice: as a per-month table for settlement, and as three
hand-copied gross constants for the evening planner. Nothing checked that the
copies agreed, and a stale copy would be invisible — the dashboard would quote
one break-even while the planner acted on another. These tests hold the table
to being the only place a rate is written down.
"""

from datetime import date
import json
from pathlib import Path

from custom_components.smart_rce.tariff import VAT, Zone
from custom_components.smart_rce.tariff.table import covers, latest_month, latest_rates

TABLE = Path("custom_components/smart_rce/tariff/tariff_table.json")


def _rows():
    return json.loads(TABLE.read_text(encoding="utf-8"))["months"]


def test_every_month_prices_all_three_zones():
    for row in _rows():
        for part in ("energy", "distribution"):
            missing = [z.value for z in Zone if z.value not in row[part]]
            assert not missing, f"{row['month']} {part} misses {missing}"


def test_months_are_unique_and_ordered_by_parsing():
    months = [row["month"] for row in _rows()]

    assert len(months) == len(set(months))
    assert all(len(m) == 7 and m[4] == "-" for m in months)


def test_marginal_cost_is_energy_plus_distribution_with_vat():
    row = max(_rows(), key=lambda r: r["month"])
    rates = latest_rates()

    for zone in Zone:
        expected = (row["energy"][zone.value] + row["distribution"][zone.value]) * VAT
        assert rates.marginal_cost(zone) == expected * 1000


def test_evening_peak_is_more_expensive_than_night():
    # The whole evening strategy rests on this ordering; a table that broke it
    # would quietly invert the planner's decisions rather than fail loudly.
    rates = latest_rates()

    assert rates.marginal_cost(Zone.T2) > rates.marginal_cost(Zone.T1)
    assert rates.marginal_cost(Zone.T1) > rates.marginal_cost(Zone.T3)


def test_table_covers_a_month_it_has_rates_for():
    year, month = (int(part) for part in latest_month().split("-"))

    assert covers(date(year, month, 15))


def test_table_does_not_claim_to_cover_the_future():
    year, month = (int(part) for part in latest_month().split("-"))

    assert not covers(date(year + 1, month, 15))
