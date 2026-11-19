"""Reports the workday night charge to Telegram: a preview and the latched decision.

21:06 (after the evening plan's 21:05 run) — tomorrow's morning, as planned so
far; 04:01 (the night charge starts at 04:00 and the dear-day choice latches) —
today's, as it will run. Text only, no call (user's decision 19.11.2026).

No line may begin with "start" — CallMeBot's firewall answers 403 (see
`charge_start_shift_notifier`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..domain.dear_day_choice import MIN_ADVANTAGE_ZL, DearDayVariant
from .battery_schedule_notifier import NOTIFY_TEXT_SCRIPT

if TYPE_CHECKING:
    from ..application.night_charge_report import NightChargeReport

_LOGGER = logging.getLogger(__name__)

TITLE: Final = "EMS — noc i poranek"


class NightChargeNotifier:
    """Driven adapter — fire-and-forget Telegram message per report."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry

    @callback
    def notify(self, report: NightChargeReport) -> None:
        self._entry.async_create_background_task(
            self._hass, self._send(report), name="smart_rce_night_charge_notify"
        )

    async def _send(self, report: NightChargeReport) -> None:
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
        except Exception:  # noqa: BLE001 — reporting must never break the schedule
            _LOGGER.exception("NightChargeNotifier: notify failed")


def render(report: NightChargeReport) -> str:
    """Message text: the decision first, then why, then the morning sale."""
    when = (
        f"Dziś {report.day:%d.%m} (zatrzaśnięte o 04:00)"
        if report.latched
        else f"Jutro {report.day:%d.%m} (zapowiedź)"
    )
    start, end = report.window
    lines = [
        f"🔋 {when}: noc ładuje do {report.target}%",
        *_why(report),
        _morning(report),
        f"Okno nocnego ładowania {start:%H:%M}–{end:%H:%M}, rezerwa T1 {report.reserve}%.",
    ]
    return "\n".join(lines)


def _why(report: NightChargeReport) -> list[str]:
    floor = report.floor
    bar = f"{report.export_threshold:.0f} zł/MWh"
    if floor is not None and floor.dear_day:
        lines = [
            (
                f"Drogi dzień: każda godzina {report.first_hour:02d}–"
                f"{report.t2_hour - 1:02d}:00 płaci co najmniej {bar}."
            )
        ]
        choice = report.dear_choice
        if choice is None:
            lines.append("Wybór pełna bateria / zwykły dzień jeszcze niepoliczony.")
        elif choice.chosen is DearDayVariant.FULL:
            lines.append(
                f"Pełna bateria {_zl(choice.full.value_zl)} wobec zwykłego dnia "
                f"{_zl(choice.normal.value_zl)} (zwykły musiałby być lepszy o "
                f"{_zl_plain(MIN_ADVANTAGE_ZL)}): bateria pokrywa T1, rano bez sprzedaży."
            )
        else:
            lines.append(
                f"Zwykły dzień {_zl(choice.normal.value_zl)} wobec pełnej baterii "
                f"{_zl(choice.full.value_zl)}: noc tylko do rezerwy T1, rano zwykła sprzedaż."
            )
        return lines
    cheap = _hours(report.cheap)
    if floor is None:
        return [
            f"Tanie godziny (poniżej {bar}): {cheap}. Prognoza PV jeszcze niepoliczona."
        ]
    sun = f"nadwyżka PV {_kwh(floor.surplus_kwh)} w tanich godzinach ({cheap})"
    if report.target > report.reserve:
        return [
            f"Słabe słońce: {sun} dopełni baterię dopiero od {floor.fill}%.",
            "Noc ładuje ponad rezerwę T1, żeby w T1 nie kupować z sieci.",
        ]
    return [
        f"Zwykły dzień: {sun} dopełni baterię.",
        (
            "Noc tylko do rezerwy T1, resztę miejsca zostawia słońcu "
            f"(eksport w tych godzinach płaci poniżej {bar})."
        ),
    ]


def _morning(report: NightChargeReport) -> str:
    plan = report.morning_plan
    if plan is not None:
        return (
            f"Poranna sprzedaż: {plan.hour:02d}:00 do {plan.target}% "
            f"({_zl_kwh(plan.gross_price / 1000)})."
        )
    why = report.morning_why_none or "brak planu"
    return f"Poranna sprzedaż: brak — {why}."


def _hours(hours: tuple[int, ...]) -> str:
    """Hours as ranges: (9, 10, 11, 14) -> '09–11, 14'."""
    if not hours:
        return "brak"
    ranges: list[tuple[int, int]] = []
    for h in hours:
        if ranges and h == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], h)
        else:
            ranges.append((h, h))
    return ", ".join(f"{a:02d}" if a == b else f"{a:02d}–{b:02d}" for a, b in ranges)


def _zl(value: float) -> str:
    return f"{value:+.2f} zł".replace(".", ",").replace("-", "−")


def _zl_plain(value: float) -> str:
    return f"{value:.2f} zł".replace(".", ",")


def _kwh(value: float) -> str:
    return f"{value:.1f} kWh".replace(".", ",")


def _zl_kwh(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " zł/kWh"
