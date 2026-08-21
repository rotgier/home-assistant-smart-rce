"""value_day — hourly pricing of export and zone assignment of import."""

import datetime

from custom_components.smart_rce.deposit.domain.day_valuation import value_day
from custom_components.smart_rce.deposit.domain.meter_reading import HourReading
from custom_components.smart_rce.deposit.domain.tariff import Zone
from custom_components.smart_rce.deposit.domain.tariff_zones import zone_for
import pytest


def _export(hour_to_kwh: dict[int, float]) -> dict[int, HourReading]:
    return {
        h: HourReading(exported_kwh=kwh, imported_kwh=0.0)
        for h, kwh in hour_to_kwh.items()
    }


def _import(hour_to_kwh: dict[int, float]) -> dict[int, HourReading]:
    return {
        h: HourReading(exported_kwh=0.0, imported_kwh=kwh)
        for h, kwh in hour_to_kwh.items()
    }


def test_export_is_valued_at_the_price_of_its_own_hour():
    """A daily average would misprice this — export concentrates in the peak."""
    record = value_day(
        datetime.date(2026, 8, 20), _export({13: 1.0, 20: 1.0}), {13: 200.0, 20: 1000.0}
    )

    assert record.deposit_earned == pytest.approx((200.0 + 1000.0) / 1000 * 1.23)
    assert record.exported_kwh == pytest.approx(2.0)


def test_coefficient_follows_the_regime_of_the_month():
    early = value_day(datetime.date(2025, 1, 15), _export({12: 1.0}), {12: 1000.0})
    later = value_day(datetime.date(2025, 2, 15), _export({12: 1.0}), {12: 1000.0})

    assert early.deposit_earned == pytest.approx(1.0)
    assert later.deposit_earned == pytest.approx(1.23)


def test_hours_without_a_price_earn_nothing_but_still_count_as_exported():
    record = value_day(datetime.date(2026, 8, 20), _export({12: 5.0}), {})

    assert record.deposit_earned == 0.0
    assert record.exported_kwh == pytest.approx(5.0)


def test_import_lands_in_the_zone_of_the_hour_it_arrived():
    """Thursday in summer: 08:00 is T1, 20:00 is T2, 03:00 is T3."""
    record = value_day(
        datetime.date(2026, 8, 20), _import({8: 1.0, 20: 2.0, 3: 4.0}), {}
    )

    assert record.import_kwh[Zone.T1] == pytest.approx(1.0)
    assert record.import_kwh[Zone.T2] == pytest.approx(2.0)
    assert record.import_kwh[Zone.T3] == pytest.approx(4.0)


def test_weekend_import_is_all_t3():
    record = value_day(datetime.date(2026, 8, 22), _import({8: 1.0, 20: 2.0}), {})

    assert record.import_kwh[Zone.T3] == pytest.approx(3.0)
    assert record.import_kwh[Zone.T1] == 0.0


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime.datetime(2026, 8, 20, 8), Zone.T1),
        (datetime.datetime(2026, 8, 20, 20), Zone.T2),
        (datetime.datetime(2026, 8, 20, 17), Zone.T3),
        (datetime.datetime(2026, 1, 20, 17), Zone.T2),
        (datetime.datetime(2026, 1, 20, 21), Zone.T3),
        (datetime.datetime(2026, 8, 15, 8), Zone.T3),
        (datetime.datetime(2026, 4, 6, 8), Zone.T3),
    ],
)
def test_zone_schedule(moment, expected):
    assert zone_for(moment) is expected
