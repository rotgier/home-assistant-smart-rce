"""BatteryDay — one day of the battery on the price chart, whichever day is picked.

What the chart shades for a day: the slot sessions that ran (with the SoC each
stopped at), the auto runs still ahead, the evening slots set to run, and the
PV charge (to 90% and over the top). Today and tomorrow come from the live
aggregates; a past day is rebuilt from the recorder (`source`). A read model:
nothing steers on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .battery_schedule import BatteryScheduleEntry, SlotSession, UpcomingRun
    from .pv_charge import PvChargeOutlook


@dataclass(frozen=True)
class BatteryDay:
    """The picked day's bands; `source` is "live", "recorder" or "none"."""

    day: date
    source: str
    sessions: tuple[SlotSession, ...] = ()
    upcoming: tuple[UpcomingRun, ...] = ()
    evening: tuple[BatteryScheduleEntry, ...] = ()
    pv: PvChargeOutlook | None = None

    def to_dict(self, tz: tzinfo | None) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "source": self.source,
            "sessions": [s.to_dict() for s in self.sessions],
            "upcoming": [r.to_dict() for r in self.upcoming],
            "evening": [self._window(e, tz) for e in self.evening],
            "pv": self.pv.to_dict(tz) if self.pv is not None else None,
        }

    def _window(self, entry: BatteryScheduleEntry, tz: tzinfo | None) -> dict[str, Any]:
        return {
            "slot": entry.kind.name,
            "start": _stamp(self.day, entry.start, tz),
            "end": _stamp(self.day, entry.end, tz),
            "target": entry.target_soc,
        }


def _stamp(day: date, at: Any, tz: tzinfo | None) -> str:
    # To the second: the dashboard parses these in the browser (WKWebView).
    return datetime.combine(day, at, tzinfo=tz).isoformat(timespec="seconds")
