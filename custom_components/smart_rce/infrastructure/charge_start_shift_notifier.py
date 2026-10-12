"""Reports every confirmed move of the forecast-driven charge start to Telegram.

"Charge start" here is the moment the battery starts taking the PV surplus —
not a grid charge (the afternoon top-up from the grid is a separate slot and
does not change). A move under a hand-set start is reported as one that
would have happened, so the forecast can still be judged against the day.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..domain.charge_start_shift import to_hhmm
from .battery_schedule_notifier import NOTIFY_TEXT_SCRIPT

if TYPE_CHECKING:
    from ..application.charge_start_shift_service import ShiftReport

_LOGGER = logging.getLogger(__name__)

TITLE: Final = "EMS — start ładowania z PV"


class ChargeStartShiftNotifier:
    """Driven adapter — fire-and-forget Telegram message per move."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    @callback
    def notify(self, report: ShiftReport) -> None:
        self._entry.async_create_background_task(
            self._hass, self._send(report), name="smart_rce_charge_start_shift_notify"
        )

    async def _send(self, report: ShiftReport) -> None:
        try:
            await self._hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": NOTIFY_TEXT_SCRIPT,
                    "variables": {"title": TITLE, "message": render(report)},
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 — reporting must never break the shift
            _LOGGER.exception("ChargeStartShiftNotifier: notify failed")


def render(report: ShiftReport) -> str:
    """Message text: the move, then why (need vs forecast surplus)."""
    move = report.move
    way = "wcześniej" if move.after < move.before else "później"
    change = f"{to_hhmm(move.before)} → {to_hhmm(move.after)} ({way})"
    if move.manual_start is not None:
        head = (
            f"Start BY SIĘ zmienił: {change}, ale obowiązuje ręczny "
            f"{move.manual_start:%H:%M}"
        )
    else:
        head = f"Start: {change}"
    return (
        f"{head}\n"
        f"SOC {report.soc:.0f}% — do pełna {_kwh(report.need_kwh)}; "
        f"prognoza nadwyżki {to_hhmm(move.after)}–{report.window_end_hour}:00: "
        f"{_kwh(report.surplus_kwh)} (Live − 0,9 kWh/h)\n"
        f"Start z cen: {to_hhmm(report.s0)}"
    )


def _kwh(value: float) -> str:
    return f"{value:.1f}".replace(".", ",") + " kWh"
