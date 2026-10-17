"""Tests for RCE domain parsing (`RceDayPrices`)."""

from datetime import date

from custom_components.smart_rce.domain.rce import RceDayPrices


def test_the_autumn_clock_change_does_not_break_the_price_fetch():
    """PSE marks the repeated hour `02a:15:00`, which is not ISO and raises.

    Left unhandled it takes out the whole day: EMS would run without prices, and
    the deposit could never value 2026-10-25 — its fetch watermark would stop
    there permanently, since a day it cannot value is a day it refuses to pass.
    """
    prices = RceDayPrices.create_from_json(
        {
            "value": [
                {
                    "dtime": f"2026-10-25 02a:{minute:02d}:00",
                    "publication_ts": "2026-10-24 14:00:00",
                    "rce_pln": 100.0,
                }
                for minute in (15, 30, 45)
            ]
            + [
                {
                    "dtime": "2026-10-25 03a:00:00",
                    "publication_ts": "2026-10-24 14:00:00",
                    "rce_pln": 200.0,
                }
            ]
        }
    )

    assert prices is not None
    assert prices.day == date(2026, 10, 25)
    assert prices.hour_price == (125.0,)
