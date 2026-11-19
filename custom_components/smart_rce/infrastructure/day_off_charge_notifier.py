"""Reports the day-off charge decision to Telegram (06:41 always, 07:41 on a change).

No line may begin with "start" — CallMeBot's firewall answers 403 (see
`charge_start_shift_notifier`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..domain.day_off_charge import NONE
from .battery_schedule_notifier import NOTIFY_TEXT_SCRIPT

if TYPE_CHECKING:
    from ..application.day_off_charge_service import DayOffReport

_LOGGER = logging.getLogger(__name__)

TITLE: Final = "EMS — dzień wolny: ładować z PV?"


class DayOffChargeNotifier:
    """Driven adapter — fire-and-forget Telegram message per reported run."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    @callback
    def notify(self, report: DayOffReport) -> None:
        self._entry.async_create_background_task(
            self._hass, self._send(report), name="smart_rce_day_off_charge_notify"
        )

    async def _send(self, report: DayOffReport) -> None:
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
        except Exception:  # noqa: BLE001 — reporting must never break the decision
            _LOGGER.exception("DayOffChargeNotifier: notify failed")


def render(report: DayOffReport) -> str:
    """Message text: the decision first, then why, the other variants, the context.

    Reworked 19.11.2026 (user): the decision used to sit at the end of a long
    first line, the variants were deltas "against no charging" whatever was
    chosen, and S0 / Live Soft / p60 meant nothing to the reader.
    """
    decision = report.decision
    run = "walidacja 07:41" if report.validation else "decyzja 06:41"
    head = f"🔋 Dzień wolny {decision.day:%d.%m} ({run}): {_decision(report)}"
    if report.validation and report.changed:
        head += " — ZMIANA"
    chosen = decision.variant(decision.chosen).value_zl
    others = " · ".join(
        f"{_variant(report, v.key)} {_zl(v.value_zl - chosen)}"
        for v in decision.variants
        if v.key != decision.chosen
    )
    lines = [
        head,
        _why(report),
        f"Inne warianty względem wybranego: {others}",
        (
            f"Wieczorem sprzedaż: {', '.join(f'{h}:00' for h in report.discharge_hours)}. "
            f"SOC {report.soc:.0f}%."
        ),
    ]
    if report.manual_start is not None:
        lines.append(
            f"Ręczny start {report.manual_start} ma pierwszeństwo — bez zmian."
        )
    return "\n".join(lines)


def _decision(report: DayOffReport) -> str:
    chosen = report.decision.chosen
    if chosen == NONE:
        return "NIE ładujemy baterii z PV"
    return f"ładujemy baterię z PV ({_variant(report, chosen)})"


def _variant(report: DayOffReport, key: str) -> str:
    if key == NONE:
        return "bez ładowania"
    if key.startswith("from_"):
        return f"od {key[5:]}:00"
    return f"od {report.s0_hour}:00 (wg cen)"


def _why(report: DayOffReport) -> str:
    sun = f"nadwyżka PV {_kwh(report.surplus_kwh)}"
    day = f"{_zl_kwh(report.day_rce_min)}–{_zl_kwh(report.day_rce_max)}"
    if report.decision.chosen == NONE:
        return (
            f"Dlaczego: {sun}, eksport w dzień płaci {day}, a bateria wieczorem "
            f"pokryłaby głównie dom w T3 ({_zl_kwh(report.buy_price)})."
        )
    return (
        f"Dlaczego: {sun}; przechowana do wieczora (max "
        f"{_zl_kwh(report.evening_rce_max)} o {report.evening_rce_hour}:00) jest "
        f"warta więcej niż eksport w dzień ({day})."
    )


def _zl(value: float) -> str:
    return f"{value:+.2f} zł".replace(".", ",").replace("-", "−")


def _kwh(value: float) -> str:
    return f"{value:.1f} kWh".replace(".", ",")


def _zl_kwh(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " zł/kWh"
