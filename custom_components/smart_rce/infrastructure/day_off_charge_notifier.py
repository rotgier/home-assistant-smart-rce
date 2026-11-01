"""Reports the day-off charge decision to Telegram (06:41 always, 07:41 on a change).

No line may begin with "start" — CallMeBot's firewall answers 403 (see
`charge_start_shift_notifier`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..application.day_off_charge_service import describe
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
    """Message text: the decision, the variants against no charging, the inputs."""
    decision = report.decision
    run = "walidacja 07:41" if report.validation else "decyzja 06:41"
    head = f"{decision.day:%d.%m} ({run}): {describe(decision.chosen)}"
    if report.validation and report.changed:
        head += " — ZMIANA"
    base = decision.variant(NONE).value_zl
    variants = " • ".join(
        f"{describe(v.key)} {_zl(v.value_zl - base)} ({_kwh(v.charged_kwh)} do baterii)"
        for v in decision.variants
        if v.key != NONE
    )
    lines = [
        head,
        f"Wobec braku ładowania: {variants}",
        (
            f"Nadwyżka PV 07-{report.t2_hour}: {_kwh(report.surplus_kwh)} (Live Soft "
            f"minus p60 zużycia; zużycie modelu 08-16: "
            f"{_kwh(report.model_consumption_08_15_kwh)})"
        ),
        (
            f"RCE brutto w dzień {_zl_kwh(report.day_rce_min)}-"
            f"{_zl_kwh(report.day_rce_max)}, wieczorem max "
            f"{_zl_kwh(report.evening_rce_max)} ({report.evening_rce_hour}:00), "
            f"zakup {_zl_kwh(report.buy_price)}"
        ),
        (
            f"SOC {report.soc:.0f}%, S0 {report.s0_hour}:00, sprzedaż wieczorem: "
            + ", ".join(f"{h}:00" for h in report.discharge_hours)
        ),
    ]
    if report.manual_start is not None:
        lines.append(
            f"Ręczny start {report.manual_start} ma pierwszeństwo — bez zmian."
        )
    return "\n".join(lines)


def _zl(value: float) -> str:
    return f"{value:+.2f} zł".replace(".", ",").replace("-", "−")


def _kwh(value: float) -> str:
    return f"{value:.1f} kWh".replace(".", ",")


def _zl_kwh(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " zł/kWh"
