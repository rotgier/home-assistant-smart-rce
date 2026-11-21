"""Reports the day-off charge decision to Telegram (06:41 always, 07:41 on a change).

The evening before a day off, 21:06, the preview of tomorrow's decision goes
out under the workday "noc i poranek" title (user's decision 21.11.2026).

No line may begin with "start" — CallMeBot's firewall answers 403 (see
`charge_start_shift_notifier`).
"""

from __future__ import annotations

from datetime import date
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
PREVIEW_TITLE: Final = "EMS — noc i poranek"
NIGHT: Final = "Noc bez ładowania z sieci, dom bierze z baterii do 10%."


class DayOffChargeNotifier:
    """Driven adapter — fire-and-forget Telegram message per reported run."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    @callback
    def notify(self, report: DayOffReport) -> None:
        title = PREVIEW_TITLE if report.preview else TITLE
        self._send_later(title, render(report))

    @callback
    def notify_preview_unavailable(self, day: date) -> None:
        """21:06 before a day off, when tomorrow's prices or forecast are missing."""
        self._send_later(PREVIEW_TITLE, render_unavailable(day))

    def _send_later(self, title: str, message: str) -> None:
        self._entry.async_create_background_task(
            self._hass,
            self._send(title, message),
            name="smart_rce_day_off_charge_notify",
        )

    async def _send(self, title: str, message: str) -> None:
        try:
            await self._hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": NOTIFY_TEXT_SCRIPT,
                    "variables": {"title": title, "message": message},
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
    if report.preview:
        head = (
            f"🔋 Jutro {decision.day:%d.%m} dzień wolny (zapowiedź, decyzja o 06:41): "
            f"{_decision(report)}"
        )
    else:
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
        *([NIGHT] if report.preview else []),
        _why(report),
        f"Inne warianty względem wybranego: {others}",
        f"{_evening(report)} {_soc(report)}",
    ]
    if report.manual_start is not None:
        lines.append(
            f"Ręczny start {report.manual_start} ma pierwszeństwo — bez zmian."
        )
    return "\n".join(lines)


def render_unavailable(day: date) -> str:
    """Preview text when tomorrow's prices or forecast are not there yet."""
    return (
        f"🔋 Jutro {day:%d.%m} dzień wolny (zapowiedź): podgląd niedostępny — "
        "brak cen RCE albo prognozy PV na jutro. Decyzja o ładowaniu z PV o 06:41.\n"
        f"{NIGHT}"
    )


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


def _evening(report: DayOffReport) -> str:
    if not report.evening_planned:
        return "Wieczorem sprzedaż: brak, bateria wieczorem dla domu."
    hours = ", ".join(f"{h}:00" for h in report.discharge_hours)
    return f"Wieczorem sprzedaż: {hours}."


def _soc(report: DayOffReport) -> str:
    if report.preview:
        return f"SOC o 06:41 ok. {report.soc:.0f}% (rzut)."
    return f"SOC {report.soc:.0f}%."


def _why(report: DayOffReport) -> str:
    sun = f"nadwyżka PV {_kwh(report.surplus_kwh)}"
    day = f"{_zl_kwh(report.day_rce_min)}–{_zl_kwh(report.day_rce_max)}"
    if report.decision.chosen == NONE:
        return (
            f"Dlaczego: {sun}, eksport w dzień płaci {day}, a bateria wieczorem "
            f"pokryłaby głównie dom w T3 ({_zl_kwh(report.buy_price)})."
        )
    if not report.evening_planned:
        # No sale planned: the valuation still gives the battery back in the
        # dearest evening hour (`DayOffChargeService._discharge_hours`) — say so.
        return (
            f"Dlaczego: {sun}; wycena liczy ją jak oddaną wieczorem o "
            f"{report.evening_rce_hour}:00 po {_zl_kwh(report.evening_rce_max)}, "
            f"więcej niż eksport w dzień ({day})."
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
