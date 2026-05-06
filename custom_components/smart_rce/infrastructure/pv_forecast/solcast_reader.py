"""SolcastReader — driving adapter dla 3 wariantów Solcast forecast.

Reads Solcast PV forecast attributes z HA state machine. Application service
decyduje *kiedy* czytać który wariant (time-of-day branching), Reader wie
*jak* (entity_id + attribute name = infrastructure detail, nie wycieka
do application).

Hexagonal pattern: **driving adapter (inbound)** — adapts HA `states` API
do pure domain types (`SolcastPeriod`).
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.core import HomeAssistant

from ...domain.pv_forecast import SolcastPeriod

_SOLCAST_AT_6_ENTITY: Final = "sensor.solcast_forecast_at_6"
_SOLCAST_LIVE_ENTITY: Final = "sensor.solcast_pv_forecast_prognoza_na_dzisiaj"
_SOLCAST_TOMORROW_ENTITY: Final = "sensor.solcast_pv_forecast_prognoza_na_jutro"


class SolcastReader:
    """Reads Solcast forecast variants z HA state machine."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    @property
    def entity_ids(self) -> tuple[str, str, str]:
        """Entity IDs do wiringa state_changed listenerów (composition root)."""
        return (
            _SOLCAST_AT_6_ENTITY,
            _SOLCAST_LIVE_ENTITY,
            _SOLCAST_TOMORROW_ENTITY,
        )

    def read_at_6(self) -> list[SolcastPeriod] | None:
        """Read morning snapshot (Solcast forecast fetched przy ~06:00)."""
        return self._read(_SOLCAST_AT_6_ENTITY, "forecast")

    def read_live(self) -> list[SolcastPeriod] | None:
        """Read live forecast (continuously updated by Solcast integration)."""
        return self._read(_SOLCAST_LIVE_ENTITY, "detailedForecast")

    def read_tomorrow(self) -> list[SolcastPeriod] | None:
        """Read tomorrow forecast (published po RCE publication ~14:00)."""
        return self._read(_SOLCAST_TOMORROW_ENTITY, "detailedForecast")

    def _read(self, entity_id: str, attr_name: str) -> list[SolcastPeriod] | None:
        state = self._hass.states.get(entity_id)
        if not state:
            return None
        forecast_attr = state.attributes.get(attr_name)
        if not forecast_attr:
            return None
        return _parse_solcast_forecast(forecast_attr)


def _parse_solcast_forecast(
    forecast_attr: list[dict[str, Any]],
) -> list[SolcastPeriod]:
    return [
        SolcastPeriod(
            period_start=str(item["period_start"]),
            pv_estimate=item["pv_estimate"],
            pv_estimate10=item["pv_estimate10"],
            pv_estimate90=item["pv_estimate90"],
        )
        for item in forecast_attr
    ]
