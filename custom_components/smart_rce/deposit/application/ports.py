"""Ports of the deposit context — what it needs from the outside world.

Both are driven ports: the context asks, adapters answer. `HourlyPriceProvider`
is also the cross-context bridge to `ems` (ADR-025 #3) — declared structurally so
nothing here imports `ems`; the factory hands in something that fits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    import datetime

    from ..domain.meter_reading import HourReading


class HourlyPriceProvider(Protocol):
    """Hourly RCE for a past day, in PLN/MWh, negative prices clamped to zero."""

    async def async_prices_for(
        self, day: datetime.date
    ) -> Mapping[int, float] | None: ...


class MeterReadingsProvider(Protocol):
    """Hourly balanced meter readings for a closed day range, inclusive."""

    async def async_readings_for(
        self, start: datetime.date, end: datetime.date
    ) -> Mapping[datetime.date, Mapping[int, HourReading]]: ...
