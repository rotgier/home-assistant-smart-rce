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
  then auto-syncs the override unless it was hand-set for today.
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

from ..domain.battery_charge_policy import ChargeStartPlan, OverrideMode
from ..domain.battery_schedule import BatteryOperation
from ..domain.charge_slots import ChargeWindowParams
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

    async def force_sync_start_charge(self, value: time) -> None:
        """Align today's start with a freshly recomputed window, dropping manual.

        Called by `Ems` after a user changes a charge-window param: that is an
        explicit "recompute this for me", so any hand-set mark is cleared and
        the value goes back to following RCE.
        """
        changed = self._repo.policy.set_start_charge_hour_override(value)
        changed |= self._repo.policy.clear_start_charge_manual()
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
        self._promote_due_plan(now.date())
        if computed_today is None:
            return
        if self._repo.policy.start_charge_manual_day == now.date():
            return  # hand-set for today — automation keeps its hands off
        self._save_if_changed_and_notify(
            self._repo.policy.set_start_charge_hour_override(computed_today)
        )

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
