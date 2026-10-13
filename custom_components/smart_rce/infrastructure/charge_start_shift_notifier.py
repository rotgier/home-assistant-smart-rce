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
    from ..application.charge_start_shift_service import RewindReport, ShiftReport

_LOGGER = logging.getLogger(__name__)

TITLE: Final = "EMS — start ładowania z PV"
REWIND_TITLE: Final = "EMS — przerwać ładowanie z PV?"
# Polish TTS on the call; the EN variant is for English messages.
NOTIFY_ALERT_PL_SCRIPT: Final = "script.notify_alert"


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

    @callback
    def alert_rewind(self, report: RewindReport) -> None:
        """Phone call: charging could pause until a later start — user decides."""
        self._entry.async_create_background_task(
            self._hass,
            self._send_script(
                NOTIFY_ALERT_PL_SCRIPT, REWIND_TITLE, render_rewind(report)
            ),
            name="smart_rce_charge_rewind_alert",
        )

    async def _send(self, report: ShiftReport) -> None:
        await self._send_script(NOTIFY_TEXT_SCRIPT, TITLE, render(report))

    async def _send_script(self, script: str, title: str, message: str) -> None:
        try:
            await self._hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": script,
                    "variables": {"title": title, "message": message},
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 — reporting must never break the shift
            _LOGGER.exception("ChargeStartShiftNotifier: %s failed", script)


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


def render_rewind(report: RewindReport) -> str:
    """Alert text: what charges now, the later start and how to take it."""
    return (
        f"Ładowanie z PV trwa od {to_hhmm(report.start)} (SOC {report.soc:.0f}%). "
        f"Start o {to_hhmm(report.suggestion)} nadal napełni baterię: do pełna "
        f"{_kwh(report.need_kwh)}, nadwyżka {to_hhmm(report.suggestion)}–"
        f"{report.window_end_hour}:00 wg Live {_kwh(report.live_kwh)}, "
        f"wg Extrap {_kwh(report.extrap_kwh)}.\n"
        f"Żeby przerwać: ustaw ręcznie start {to_hhmm(report.suggestion)} "
        f"(EMS Battery Charge Start Hour Override).\n"
        f"Alarm {report.alert_no}/{report.max_alerts} dziś."
    )


def _kwh(value: float) -> str:
    return f"{value:.1f}".replace(".", ",") + " kWh"
