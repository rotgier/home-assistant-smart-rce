"""WorkdayCalendarReader — driven adapter for HA workday calendar.

Wraps `calendar.get_events` service call against `calendar.workday_calendar`
(provided by HA's `workday` integration with Polish holidays configured).
Returns a `set[date]` of workdays in a lookback window so the domain
function `walk_back_workdays` can walk back N actual workdays — including
holiday awareness — instead of the prior heuristic that just skipped
weekends.

Hexagonal pattern: **driven adapter (outbound)** — application service
asks "which dates were workdays in this window?" via a pure-data contract;
the concrete impl uses HA service API + parses the response.

No fallback: if the calendar returns no events (entity missing/misnamed,
service errored, integration not configured), the reader returns an empty
set. Callers detect this and log a clear warning rather than masking the
problem with a heuristic — surfaces broken calendar config quickly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import WORKDAY_CALENDAR_ENTITY

_LOGGER = logging.getLogger(__name__)


class WorkdayCalendarReader:
    """Reads workday dates from HA workday calendar via the calendar service."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def fetch_workdays(self, today: date, lookback_days: int = 30) -> set[date]:
        """Fetch workday dates in `[today - lookback_days, today)`.

        Returns a set of dates marked as workday by the calendar in the
        window. Empty set on any error / missing entity — caller decides
        how to handle (typically: warning + return None for prev-day
        sensors so the missing-config is visible).
        """
        tz = dt_util.DEFAULT_TIME_ZONE
        start = datetime.combine(
            today - timedelta(days=lookback_days), time(0, 0), tzinfo=tz
        )
        end = datetime.combine(today, time(0, 0), tzinfo=tz)

        try:
            response = await self._hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": WORKDAY_CALENDAR_ENTITY,
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 — defensive: don't crash recompute path
            _LOGGER.exception(
                "calendar.get_events failed for %s — returning empty workday set",
                WORKDAY_CALENDAR_ENTITY,
            )
            return set()

        if not response:
            return set()

        entity_data = response.get(WORKDAY_CALENDAR_ENTITY, {})
        events = entity_data.get("events", []) if isinstance(entity_data, dict) else []
        workdays = {
            d for d in (_parse_event_date(e, tz) for e in events) if d is not None
        }
        _LOGGER.debug(
            "WorkdayCalendarReader: %d workdays in window %s..%s",
            len(workdays),
            start.date(),
            end.date(),
        )
        return workdays


def _parse_event_date(event: Any, tz: Any) -> date | None:
    """Extract a workday date from a single calendar.get_events event entry.

    Workday events are all-day; the service may surface `start` either as
    a date-only ISO string ("2026-05-09") or as a datetime ("2026-05-09T00:00:00+02:00").
    All-day events anchored to local midnight already encode the right day,
    so a tz-aware parse + `.date()` works for both shapes.
    """
    if not isinstance(event, dict):
        return None
    raw = event.get("start")
    if raw is None:
        return None
    if isinstance(raw, dict):
        # Some HA versions wrap as {"date": "..."} or {"dateTime": "..."}.
        raw = raw.get("date") or raw.get("dateTime")
        if raw is None:
            return None
    if not isinstance(raw, str):
        return None
    try:
        # Date-only path (YYYY-MM-DD) — fromisoformat handles it on 3.11+.
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            return date.fromisoformat(raw)
        dt = datetime.fromisoformat(raw)
    except ValueError:
        _LOGGER.debug("Unrecognized calendar event start: %r", raw)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).date()
