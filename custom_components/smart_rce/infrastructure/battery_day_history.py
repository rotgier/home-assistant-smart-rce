"""BatteryDayHistory — a past day's battery bands rebuilt from the recorder.

The recorder keeps ~9 full days, enough for the chart's day picker:

- slot sessions: the last state of the day of the sessions sensor — its
  `sessions` attribute is cumulative over the day (the aggregate drops them
  only after midnight). The sensor exists since 17.10.2026; for earlier days
  the stretches of the engaging sensor stand in (no target, no reason);
- PV charge: `PvChargeRun.replay` over the day's history of the charge-allowed
  gate, the engaging slot (a charge slot from the grid is not PV) and SoC —
  the same rule the live run follows.

Blocking recorder reads go through the recorder's executor.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.util import dt as dt_util

from ..domain.battery_day import BatteryDay
from ..domain.battery_schedule import SlotKind, SlotSession
from ..domain.pv_charge import PvChargeOutlook, PvChargeRun
from ..domain.pv_surplus import surplus_window_end

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

SESSIONS_ENTITY: Final = "sensor.ems_battery_schedule_sessions"
CHARGE_ALLOWED_ENTITY: Final = "binary_sensor.ems_battery_charge_allowed"
ENGAGING_ENTITY: Final = "sensor.ems_battery_schedule_currently_engaging"
SOC_ENTITY: Final = "sensor.battery_state_of_charge"
# The sessions' last state of the day is read from this hour on (the state in
# force at its start comes with it) — not the whole day of minute-by-minute
# `upcoming` changes.
_SESSIONS_FROM: Final = time(20, 0)


class BatteryDayHistory:
    """Rebuilds a past day; empty (`source` "none") once the recorder lost it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_day(self, day: date) -> BatteryDay:
        sessions = await self._sessions(day)
        soc = await self._steps(day, SOC_ENTITY)
        allowed = await self._steps(day, CHARGE_ALLOWED_ENTITY)
        engaging = await self._steps(day, ENGAGING_ENTITY)
        window_end = surplus_window_end(day) * 60
        run = PvChargeRun.replay(
            day,
            gate_open=lambda m: (
                _at(allowed, m) == "on" and not _charging(_at(engaging, m))
            ),
            soc_at=lambda m: _soc(_at(soc, m)),
            window_end=window_end,
        )
        pv = (
            PvChargeOutlook.build(
                day,
                run=run,
                forecast=None,
                now=24 * 60,
                soc=None,
                window_end=window_end,
            )
            if run.start is not None
            else None
        )
        if not sessions:
            sessions = _sessions_from_engaging(day, engaging, soc)
        source = "recorder" if sessions or soc else "none"
        return BatteryDay(day, source, sessions=tuple(sessions), pv=pv)

    async def _sessions(self, day: date) -> list[SlotSession]:
        states = await self._history(
            day, SESSIONS_ENTITY, _SESSIONS_FROM, attributes=True
        )
        if not states:
            return []
        raw = states[-1].attributes.get("sessions") or []
        sessions = (SlotSession.from_dict(dict(item)) for item in raw)
        return [
            s
            for s in sessions
            if s is not None and dt_util.as_local(s.started_at).date() == day
        ]

    async def _steps(self, day: date, entity_id: str) -> list[tuple[int, str]]:
        """(minute of the day, state) in time order; the state carried over is minute 0."""
        states = await self._history(day, entity_id, time(0))
        return [(_minute_on(day, s.last_changed), s.state) for s in states]

    async def _history(
        self, day: date, entity_id: str, since: time, *, attributes: bool = False
    ) -> list[State]:
        tz = dt_util.get_default_time_zone()
        start = datetime.combine(day, since, tzinfo=tz)
        end = min(
            datetime.combine(day, time(0), tzinfo=tz) + timedelta(days=1), dt_util.now()
        )
        result: dict[str, list[Any]] = await get_instance(
            self._hass
        ).async_add_executor_job(
            state_changes_during_period,
            self._hass,
            start,
            end,
            entity_id,
            not attributes,
        )
        return [
            s
            for s in result.get(entity_id, [])
            if s.state not in {"unknown", "unavailable"}
        ]


def _sessions_from_engaging(
    day: date, engaging: list[tuple[int, str]], soc: list[tuple[int, str]]
) -> list[SlotSession]:
    """Sessions of a day before the sessions sensor kept them (before 17.10.2026).

    Each stretch the engaging sensor named a slot is a session; its SoC at
    both ends comes from the SoC history. Target and reason were not kept.
    """
    tz = dt_util.get_default_time_zone()
    midnight = datetime.combine(day, time(0), tzinfo=tz)
    bounds = [*engaging, (24 * 60, "IDLE")]
    sessions: list[SlotSession] = []
    for (start, state), (end, _next) in zip(bounds, bounds[1:], strict=False):
        kind = SlotKind.__members__.get(state)
        if kind is None or end <= start:
            continue
        start_soc = _soc(_at(soc, start))
        if start_soc is None:
            continue
        sessions.append(
            SlotSession(
                slot=kind,
                started_at=midnight + timedelta(minutes=start),
                start_soc=start_soc,
                target_soc=None,
                ended_at=midnight + timedelta(minutes=end),
                end_soc=_soc(_at(soc, end)),
            )
        )
    return sessions


def _minute_on(day: date, moment: datetime) -> int:
    local = dt_util.as_local(moment)
    if local.date() < day:
        return 0
    return local.hour * 60 + local.minute


def _at(steps: list[tuple[int, str]], minute: int) -> str | None:
    """State in force at `minute`; None before the first one."""
    i = bisect_right(steps, minute, key=lambda step: step[0])
    return steps[i - 1][1] if i else None


def _charging(engaging: str | None) -> bool:
    """Tell whether a charge slot (from the grid) was engaged."""
    kind = SlotKind.__members__.get(engaging or "")
    return kind is not None and not kind.direction.is_discharge


def _soc(state: str | None) -> float | None:
    try:
        return float(state) if state is not None else None
    except ValueError:
        return None
