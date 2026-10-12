"""BatteryChargeService — application orchestrator + use case facade.

Public API consumed by HA entities (select, time, sensors) and Ems:
- `update(schedule_op)` — per-tick orchestration; caches latest
  `BatteryOperation` so derived properties (`charge_allowed`,
  `target_modbus_value`) can be queried lazily by sensors/actuator.
  Returns `BatteryChargeUpdateResult` (atomic snapshot for Ems).
- `set_charge_allowed_override(mode)` / `set_start_charge_hour_manual(value)`
  — UI mutators (async; persist + notify).
- `refresh_start_charge(computed_today, now)` — single sync entry point
  called after every `ChargeSlots` recompute: promotes a due tomorrow-plan,
  then auto-syncs the override (moved by today's forecast shift, if any)
  unless it was hand-set for today.
- `evaluate_forecast_shift(now, s0, candidate, soc)` — one 5-minute
  evaluation of the forecast-driven charge start.
- `add_listener(cb)` — single-registry refresh hook (inherited from `Service`).

Repository is the internal collaborator (owns + persists policy). Service
does NOT track `_last_charge_allowed` shadow state — the actuator's
state-diff (target vs Modbus readback cache) is the single source for
write triggers. Sensors fire `async_write_ha_state` from the Ems-level
listener and HA core dedupes on attribute equality.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import callback

from ..domain.battery_charge_policy import (
    ChargeStartPlan,
    ChargeStartSource,
    OverrideMode,
)
from ..domain.battery_schedule import BatteryOperation
from ..domain.charge_slots import ChargeWindowParams
from ..domain.charge_start_shift import ChargeStartShift, to_hhmm, to_minutes, to_time
from ..infrastructure.battery_charge_repository import BatteryChargeRepository
from .service import Service

if TYPE_CHECKING:
    from ..infrastructure.battery_charge_current_actuator import (
        BatteryChargeCurrentActuator,
    )


@dataclass(frozen=True)
class BatteryChargeUpdateResult:
    """Return value of `BatteryChargeService.update`.

    Atomic capture of values Ems needs to pass to downstream managers
    (DodPolicy, GridExport) this tick — taken AFTER policy decisions
    settle. Lazy property reads on the service still work for external
    consumers (sensors).
    """

    charge_allowed: bool
    start_charge_hour_override: time | None


@dataclass(frozen=True)
class ForecastShiftMove:
    """The forecast moved today's charge start (minutes of the day).

    `manual_start` is the hand-set start that stays in force instead — the
    move is then only reported ("would have moved").
    """

    before: int
    after: int
    manual_start: time | None


_LOGGER = logging.getLogger(__name__)


class BatteryChargeService(Service[BatteryChargeRepository]):
    """Application service. HASS-unaware — dependencies injected at construction."""

    def __init__(
        self,
        repo: BatteryChargeRepository,
        clock: Callable[[], datetime],
        actuator: BatteryChargeCurrentActuator,
        initial_schedule_op: BatteryOperation = BatteryOperation.idle(),
    ) -> None:
        super().__init__(repo)
        self._clock = clock
        self._actuator = actuator
        # `_last_schedule_op` is cached for sensor property reads (charge_allowed
        # below). Factory passes `initial_schedule_op` from BatteryScheduleService
        # so the first post-reload sensor read reflects the persisted engagement
        # (not `idle()` default) — matches the reconstruct-from-storage pattern
        # in BatteryScheduleService.__init__.
        self._last_schedule_op: BatteryOperation = initial_schedule_op

    @callback
    def update(self, schedule_op: BatteryOperation) -> BatteryChargeUpdateResult:
        """Per-tick hook called from Ems.update_state."""
        self._last_schedule_op = schedule_op
        self._actuator.apply_if_changed(schedule_op, self._clock())
        return BatteryChargeUpdateResult(
            charge_allowed=self._repo.policy.charge_allowed(self._clock(), schedule_op),
            start_charge_hour_override=self._repo.policy.start_charge_hour_override,
        )

    # ─── Properties (sensor / actuator queries) ───

    @property
    def charge_allowed(self) -> bool:
        return self._repo.policy.charge_allowed(self._clock(), self._last_schedule_op)

    @property
    def modbus_current_value(self) -> float | None:
        return self._repo.policy.modbus_current_value

    @property
    def charge_allowed_override(self) -> OverrideMode:
        return self._repo.policy.charge_allowed_override

    @property
    def start_charge_hour_override(self) -> time | None:
        return self._repo.policy.start_charge_hour_override

    @property
    def start_charge_manual_day(self) -> date | None:
        return self._repo.policy.start_charge_manual_day

    @property
    def tomorrow_plan(self) -> ChargeStartPlan | None:
        return self._repo.policy.tomorrow_plan

    @property
    def forecast_shift(self) -> ChargeStartShift | None:
        """Today's forecast shift (None before the first evaluation of the day)."""
        shift = self._repo.policy.forecast_shift
        if shift is None or shift.day != self._clock().date():
            return None
        return shift

    @property
    def start_charge_source(self) -> ChargeStartSource:
        """Whether today's / tomorrow's start is hand-set — resolved against today."""
        return self._repo.policy.start_charge_source(self._clock().date())

    def start_charge_details(self) -> dict[str, str | None]:
        """Human readout of today's start and a hand-set start for tomorrow.

        `summary` names today's start with its source ("11:00 → 09:00 z
        prognozy"); `tomorrow_manual` is set only when tomorrow was pinned by
        hand — a separate fact, not folded into the source.
        """
        policy = self._repo.policy
        start = policy.start_charge_hour_override
        shift = self.forecast_shift
        source = self.start_charge_source
        summary: str | None
        if start is None:
            summary = None
        elif source is ChargeStartSource.FORECAST and shift is not None:
            summary = f"{to_hhmm(shift.s0)} → {start:%H:%M} z prognozy"
        elif source is ChargeStartSource.MANUAL:
            summary = f"{start:%H:%M} ręcznie"
            if shift is not None and shift.is_shifted:
                summary += f" (prognoza: {to_hhmm(shift.start)})"
        else:
            summary = f"{start:%H:%M} z cen RCE"
        plan = policy.tomorrow_plan
        return {
            "summary": summary,
            "tomorrow_manual": f"{plan.value:%H:%M}" if plan is not None else None,
        }

    @property
    def start_charge_is_manual_today(self) -> bool:
        return self._repo.policy.start_charge_manual_day == self._clock().date()

    @property
    def initial_charge_hours(self) -> int:
        return self._repo.policy.initial_charge_hours

    @property
    def charge_extend_threshold(self) -> float:
        return self._repo.policy.charge_extend_threshold

    @property
    def charge_absolute_cheap_price(self) -> float:
        return self._repo.policy.charge_absolute_cheap_price

    @property
    def charge_base_window_shift_minutes(self) -> int:
        return self._repo.policy.charge_base_window_shift_minutes

    @property
    def charge_window_params(self) -> ChargeWindowParams:
        return self._repo.policy.charge_window_params()

    # ─── User mutators ───

    async def set_charge_allowed_override(self, mode: OverrideMode) -> None:
        """UI-driven select option change. Persists + notifies listeners on delta."""
        await self._persist_and_notify(
            self._repo.policy.set_charge_allowed_override(mode)
        )

    async def set_start_charge_hour_manual(self, value: time) -> None:
        """UI-driven change of TODAY's start — marks the value as hand-set.

        The mark is what stops the automatic sync from reverting it on the
        next price refresh or after a restart; it expires by itself once the
        date rolls over.
        """
        await self._persist_and_notify(
            self._repo.policy.set_start_charge_hour_manual(value, self._clock().date())
        )

    async def set_tomorrow_plan(self, value: time) -> None:
        """UI-driven change of TOMORROW's start — pins a plan to tomorrow's date.

        Today's override is left alone; the plan is promoted by
        `refresh_start_charge` on the day it names.
        """
        await self._persist_and_notify(
            self._repo.policy.set_tomorrow_plan(
                value, self._clock().date() + timedelta(days=1)
            )
        )

    async def force_sync_start_charge(self, value: time | None) -> None:
        """Realign both days with the recomputed windows, dropping every manual mark.

        Called by `Ems` after a user changes a charge-window param. That is an
        explicit "recompute this for me", so BOTH days go back to following
        RCE: today's hand-set mark is cleared and a pinned plan for tomorrow
        is dropped — keeping the plan would hide the very effect the user is
        trying to see. Clearing happens even when `value` is None (no prices
        for today), so the marks never outlive the request.
        """
        today = self._clock().date()
        changed = self._repo.policy.clear_start_charge_manual()
        changed |= self._repo.policy.clear_tomorrow_plan()
        if value is not None:
            # The forecast shift starts over from the new window too — unless it
            # is locked: then the battery is already taking PV and stays so.
            shift = self.forecast_shift
            if shift is not None:
                changed |= shift.restart(to_minutes(value))
            changed |= self._repo.policy.set_start_charge_hour_override(
                self._effective_start(value, today)
            )
        await self._persist_and_notify(changed)

    async def set_initial_charge_hours(self, value: int) -> None:
        """UI-driven select change for the base charge-window length.

        Persists + notifies on delta. ChargeSlots recompute + start force-sync
        are driven by the caller (Ems) — this service owns only the persisted
        knob, not the ChargeSlots aggregate. Same applies to the three setters
        below.
        """
        await self._persist_and_notify(
            self._repo.policy.set_initial_charge_hours(value)
        )

    async def set_charge_extend_threshold(self, value: float) -> None:
        """UI-driven number change for the earlier-window extend threshold."""
        await self._persist_and_notify(
            self._repo.policy.set_charge_extend_threshold(value)
        )

    async def set_charge_absolute_cheap_price(self, value: float) -> None:
        """UI-driven number change for the absolute-cheap extend price."""
        await self._persist_and_notify(
            self._repo.policy.set_charge_absolute_cheap_price(value)
        )

    async def set_charge_base_window_shift_minutes(self, value: int) -> None:
        """UI-driven number change for the base-window start shift (minutes)."""
        await self._persist_and_notify(
            self._repo.policy.set_charge_base_window_shift_minutes(value)
        )

    @callback
    def refresh_start_charge(self, computed_today: time | None, now: datetime) -> None:
        """Single entry point after a `ChargeSlots` recompute — promote, then sync.

        Order matters and is enforced here rather than left to callers: a plan
        due today must land BEFORE the auto-sync runs, otherwise the computed
        value would briefly occupy the override and the actuator could act on
        it. Both steps compare against persisted state only, so the outcome is
        the same whether this runs at midnight, after a restart, or on the
        first tick of a day HA slept through.
        """
        today = now.date()
        self._promote_due_plan(today)
        changed = self._repo.policy.drop_stale_forecast_shift(today)
        if computed_today is not None:
            changed |= self._follow_s0(computed_today, today)
            # Hand-set for today — automation keeps its hands off.
            if self._repo.policy.start_charge_manual_day != today:
                changed |= self._repo.policy.set_start_charge_hour_override(
                    self._effective_start(computed_today, today)
                )
        self._save_if_changed_and_notify(changed)

    @callback
    def _promote_due_plan(self, today: date) -> None:
        """Move a plan naming today into the override; drop it once it is past."""
        plan = self._repo.policy.tomorrow_plan
        if plan is None or not (plan.is_due(today) or plan.is_stale(today)):
            return
        changed = self._repo.policy.clear_tomorrow_plan()
        if plan.is_due(today):
            changed |= self._repo.policy.set_start_charge_hour_manual(
                plan.value, plan.day
            )
            _LOGGER.info("Promoted manual charge start %s for %s", plan.value, plan.day)
        else:
            _LOGGER.info(
                "Dropping stale charge start plan %s for %s (today is %s)",
                plan.value,
                plan.day,
                today,
            )
        self._save_if_changed_and_notify(changed)

    def _effective_start(self, computed_today: time, today: date) -> time:
        """Return the computed start, or today's forecast shift of it."""
        shift = self._repo.policy.forecast_shift
        if shift is None or shift.day != today:
            return computed_today
        return to_time(shift.start)

    @callback
    def evaluate_forecast_shift(
        self, now: datetime, s0: time, candidate: int, soc: float | None
    ) -> ForecastShiftMove | None:
        """Take one evaluation of today's shift; apply and return a move.

        The shift is evaluated (and persisted, with its lock baseline) even
        while today's start is hand-set — the move is then only returned, so it
        can be reported, and the hand-set start stays in force.
        """
        today = now.date()
        policy = self._repo.policy
        changed = self._follow_s0(s0, today)
        # Persist a fresh shift too, so the sensor survives a restart.
        changed |= policy.forecast_shift is None or policy.forecast_shift.day != today
        shift = policy.forecast_shift_for(today, to_minutes(s0))
        before = shift.to_dict()
        old_start = shift.start
        moved = shift.evaluate(to_minutes(now.time()), candidate, soc)
        changed |= shift.to_dict() != before
        manual = policy.start_charge_manual_day == today
        if not manual:
            changed |= policy.set_start_charge_hour_override(to_time(shift.start))
        self._save_if_changed_and_notify(changed)
        if not moved:
            return None
        return ForecastShiftMove(
            before=old_start,
            after=shift.start,
            manual_start=policy.start_charge_hour_override if manual else None,
        )

    @callback
    def _follow_s0(self, computed_today: time, today: date) -> bool:
        """Restart today's shift when the windows moved S0 under it."""
        shift = self._repo.policy.forecast_shift
        s0 = to_minutes(computed_today)
        if shift is None or shift.day != today or shift.s0 == s0:
            return False
        return shift.restart(s0)
