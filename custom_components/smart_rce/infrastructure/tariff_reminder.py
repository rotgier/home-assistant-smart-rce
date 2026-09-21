"""Nags about the tariff table between the URE decision and the new year.

URE publishes tariffs for the coming year around 15-17 December (verified for
2023, 2024 and 2025), effective 1 January. That leaves a fortnight to copy the
new rates in — and nothing else in the system notices if it does not happen:
prices keep flowing, slots keep being planned, only the break-even used for
those decisions is quietly wrong.

Silencing needs no button. The reminder asks the table whether it already
covers the coming year, so adding the row is what stops the messages.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Final

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_change

from ..tariff.table import covers, latest_month

_LOGGER = logging.getLogger(__name__)

NOTIFY_DOMAIN: Final = "script"
NOTIFY_TEXT: Final = "notify_text"
NOTIFY_ALERT: Final = "notify_alert"
TITLE: Final = "EMS — taryfa na nowy rok"

# From the URE decision (mid-December) the new rates are knowable; from the
# 26th the deadline is close enough to warrant a phone call.
_FIRST_DAY: Final = 17
_ESCALATE_DAY: Final = 26
_DECEMBER: Final = 12
_HOURS: Final[list[int]] = [12, 21]
_MINUTE: Final = 30


class TariffReminder:
    """Reminds, from 17 December, that next year's rates are not in the table yet."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def start(self) -> CALLBACK_TYPE:
        """Subscribe the two daily checks. Returns an unsubscribe."""
        return async_track_time_change(
            self._hass, self._check, hour=_HOURS, minute=_MINUTE, second=0
        )

    async def _check(self, now: datetime) -> None:
        if not self._is_nagging_season(now.date()):
            return
        next_year = date(now.year + 1, 1, 15)
        if covers(next_year):
            _LOGGER.debug("Tariff table already covers %s — no reminder", next_year)
            return
        await self._nag(now.date())

    @staticmethod
    def _is_nagging_season(today: date) -> bool:
        """December, from the day URE decisions are normally out."""
        return today.month == _DECEMBER and today.day >= _FIRST_DAY

    async def _nag(self, today: date) -> None:
        """Text until the 26th, then a call — the rates bite on 1 January."""
        urgent = today.day >= _ESCALATE_DAY
        service = NOTIFY_ALERT if urgent else NOTIFY_TEXT
        days_left = (date(today.year + 1, 1, 1) - today).days
        message = (
            f"Tabela taryfowa kończy się na {latest_month()}. "
            f"Do wejścia nowych stawek: {days_left} dni. "
            "Dopisz wiersz w tariff/tariff_table.json (netto, energia "
            "i dystrybucja per strefa) — przypomnienia ucichną same."
        )
        try:
            await self._hass.services.async_call(
                NOTIFY_DOMAIN,
                service,
                {"title": TITLE, "message": message},
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - a failed reminder must not break setup
            _LOGGER.exception("Tariff reminder notification failed")
