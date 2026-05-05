"""EmsRcePrices — Ems-side RCE state + lifecycle.

Wrapper na pure-data `RcePrices` z dodaną logiką:
- `current_price` — cena dla bieżącej godziny (refreshed at hour tick)
- `restore_today/tomorrow` — partial update z sensor cache attributes

Pure domain (no HA imports). Konsumowany przez `Ems` w composition root.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .rce import RceDayPrices, RcePrices


class EmsRcePrices:
    """Ems-side wrapper around RcePrices — adds current_price + lifecycle ops."""

    def __init__(self) -> None:
        self.rce_prices: RcePrices | None = None
        self.current_price: float | None = None

    def update(self, now: datetime, rce_prices: RcePrices) -> None:
        self.rce_prices = rce_prices
        self._refresh_current_price(now)

    def update_hourly(self, now: datetime) -> None:
        self._refresh_current_price(now)

    def restore_today(self, prices_attr: list[dict], now: datetime) -> None:
        day_prices = RceDayPrices.from_sensor_attr(prices_attr)
        if day_prices is None:
            return
        if self.rce_prices is None:
            self.rce_prices = RcePrices(fetched_at=now, today=day_prices, tomorrow=None)
        else:
            self.rce_prices = replace(self.rce_prices, today=day_prices)
        self._refresh_current_price(now)

    def restore_tomorrow(self, prices_attr: list[dict], now: datetime) -> None:
        day_prices = RceDayPrices.from_sensor_attr(prices_attr)
        if day_prices is None:
            return
        if self.rce_prices is None:
            self.rce_prices = RcePrices(fetched_at=now, today=None, tomorrow=day_prices)
        else:
            self.rce_prices = replace(self.rce_prices, tomorrow=day_prices)

    def _refresh_current_price(self, now: datetime) -> None:
        if (
            self.rce_prices
            and self.rce_prices.today
            and self.rce_prices.today.hour_price
            and now.hour < len(self.rce_prices.today.hour_price)
        ):
            self.current_price = self.rce_prices.today.hour_price[now.hour]
