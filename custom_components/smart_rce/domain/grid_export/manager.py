"""GridExportManager — orchestrator for POSITIVE / NEGATIVE intervention sessions.

Exposes 4 read-only properties consumed by sensors:
- intervention_active (bool, diagnostic)
- intervention_direction (POSITIVE/NEGATIVE/None)
- recommended_ems_mode (str: "auto" | "discharge_battery" | "charge_battery")
- recommended_xset (int | None) — W

Plus mutable field:
- last_decision_reason (str | None)

Listener in infrastructure/goodwe_ems_actuator.py reacts to sensor changes
and calls number.goodwe_ems_power_limit + select.goodwe_ems_mode.

Active window: post_charge → next day 7:00 (POSITIVE skips pre_charge —
BatteryManager rules there; NEGATIVE works in pre_charge).

Architecture — two-layer separation of concerns:
1. **Manager (this class)** — global cross-cutting concerns:
   - none_present_core (defensive — required state fields are None)
   - ems_interventions_blocked (global block for both directions)
   - hour_rollover (continue lifecycle)
   - end_of_hour_cleanup (continue lifecycle, exit ≥ XX:59:50)
   - too_late_in_hour (entry block, ≥ XX:59:40)
   - other_ems_automation_active_this_hour (entry block)
   - balance range routing (POSITIVE: > BALANCE_GATE_KWH, NEGATIVE: < entry_threshold)
2. **Intervention (PositiveIntervention / NegativeIntervention)** — intervention-
   specific preconditions (SoC thresholds, toggle, pre_charge_window, balance
   recovery exit, feasibility per bucket type).

Decision tree (`grid_export_strategy_mode`):
- "charge_adaptive" → default active (POSITIVE and NEGATIVE)
- "disabled"        → manager evaluates but intervention off (diagnostic only)

Defensive: when `state.pv_available` or `battery_charge_limit` is None
(e.g. after HA restart, sensors unavailable for ~25-50ms) → no-op, manager
returns to AUTO, listener restores registers to AUTO.
"""

from __future__ import annotations

from datetime import datetime, time
import logging
from typing import Final

from custom_components.smart_rce.domain.ems_operation import EmsOperation
from custom_components.smart_rce.domain.grid_export import negative, positive
from custom_components.smart_rce.domain.grid_export.intervention import (
    Intervention,
    InterventionDirection,
)
from custom_components.smart_rce.domain.grid_export.negative import NegativeIntervention
from custom_components.smart_rce.domain.grid_export.positive import PositiveIntervention
from custom_components.smart_rce.domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)


class GridExportManager:
    """Orchestrator — global guards + range routing + delegation to active intervention."""

    AUTO_MODE: Final[str] = "auto"

    # Strategy modes (input_select.smart_rce_grid_export_strategy_mode)
    STRATEGY_MODE_DISABLED: Final[str] = "disabled"
    STRATEGY_MODE_CHARGE_ADAPTIVE: Final[str] = "charge_adaptive"

    # Global lifecycle thresholds (cross-cutting both interventions)
    LATE_HOUR_MINUTE: Final[int] = 59
    LATE_HOUR_ENTRY_SECOND: Final[int] = 40  # too_late_in_hour entry block
    LATE_HOUR_EXIT_SECOND: Final[int] = 50  # end_of_hour_cleanup exit

    # Logging — throttle DEBUG snapshot when nothing changes (60s).
    DEBUG_LOG_THROTTLE_SEC: Final[int] = 60

    def __init__(self) -> None:
        self._active: Intervention | None = None
        self.last_decision_reason: str | None = None
        # Disabled override — when strategy_mode != charge_adaptive, manager
        # forces AUTO regardless of computed intervention. Field tracks if
        # current state is forced override (for sensors).
        self._disabled_override_active: bool = False
        # Throttling for DEBUG snapshots (same as BatteryManager)
        self._last_log_snapshot: tuple | None = None
        self._last_log_ts: datetime | None = None

    @property
    def intervention_active(self) -> bool:
        return self._active is not None and not self._disabled_override_active

    @property
    def intervention_direction(self) -> InterventionDirection | None:
        if self._active is None or self._disabled_override_active:
            return None
        return self._active.direction

    @property
    def recommended_ems_mode(self) -> str:
        if self._active is None or self._disabled_override_active:
            return self.AUTO_MODE
        return self._active.recommended_mode

    @property
    def recommended_xset(self) -> int | None:
        if self._active is None or self._disabled_override_active:
            return None
        return self._active.recommended_xset

    @property
    def _intervention_started_hour(self) -> int | None:
        """Backward-compat read API (tests). Started hour of active intervention."""
        return self._active.started_hour if self._active else None

    def get_active_intervention(self) -> InterventionDirection | None:
        """Return current intervention direction (POSITIVE/NEGATIVE/None).

        Used by WaterHeaterManager to differentiate reserved (higher reserved
        for NEGATIVE — heaters preferred off).
        """
        return self.intervention_direction

    def reset_intervention(self, reason: str) -> None:
        """Drop active intervention — exposed for pre-shutdown / external pause.

        Equivalent to internal `_set_neutral`, but as a public domain method.
        After call, `intervention_active=False`, `recommended_ems_mode=auto`,
        and `last_decision_reason=reason`. Next `update()` re-evaluates from
        scratch. Used by `Ems.async_on_hass_stop` to leave the inverter in
        a deterministic state before HA shutdown (avoids stale `_active` if
        smart_rce process dies mid-intervention, since manager state is not
        persisted).
        """
        self._set_neutral(reason)

    def update(
        self,
        state: InputState,
        *,
        ems_interventions_blocked: bool = False,
        battery_charge_allowed: bool = True,
        ems_schedule_active_this_hour: bool = False,
        start_charge_hour_override: time | None = None,
    ) -> EmsOperation:
        """Re-evaluate intervention state — returns target EmsOperation.

        Manager keeps internal mutable state (`_active`, `_disabled_override_active`,
        `last_decision_reason`) for sensor property accessors; the returned
        `EmsOperation` is the canonical end-of-pipeline value consumed by
        `GoodweEmsActuator` and (Etap F) `Ems._resolve_ems_operation`.
        """
        self._update_state(
            state,
            ems_interventions_blocked=ems_interventions_blocked,
            battery_charge_allowed=battery_charge_allowed,
            ems_schedule_active_this_hour=ems_schedule_active_this_hour,
            start_charge_hour_override=start_charge_hour_override,
        )
        return self.current_ems_operation()

    def current_ems_operation(self) -> EmsOperation:
        """Snapshot of current recommendation as an EmsOperation."""
        if self._active is None or self._disabled_override_active:
            return EmsOperation.neutral(reason=self.last_decision_reason)
        return EmsOperation.from_grid_intervention(
            ems_mode=self._active.recommended_mode,  # type: ignore[arg-type]
            power_limit_w=self._active.recommended_xset,
            reason=self.last_decision_reason,
        )

    def _update_state(
        self,
        state: InputState,
        *,
        ems_interventions_blocked: bool = False,
        battery_charge_allowed: bool = True,
        ems_schedule_active_this_hour: bool = False,
        start_charge_hour_override: time | None = None,
    ) -> None:
        """In-place state mutation — see `update` for description.

        Flow:
        1. Global guards (none_present_core, interventions_blocked,
           schedule_active_this_hour, other_ems_automation_active_this_hour)
           → set_neutral + return
        2. Active intervention: tick (hour_rollover, end_of_hour, delegate)
           Or: try_enter (too_late_in_hour, balance routing)
        3. Apply disabled_override if strategy_mode != charge_adaptive
        4. Log
        """
        prev_active = self.intervention_active
        prev_mode = self.recommended_ems_mode
        prev_xset = self.recommended_xset

        # --- Global guards (cross-cutting both directions) ---
        if self._none_present_core(state):
            # Skip disabled_override — without core inputs the override has
            # no meaning (state.grid_export_strategy_mode may also be None).
            self._set_neutral("none_present")
            self._disabled_override_active = False
            self._log_after_update(
                state, prev_active, prev_mode, prev_xset, battery_charge_allowed
            )
            return

        if ems_interventions_blocked:
            self._set_neutral("ems_interventions_blocked")
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(
                state, prev_active, prev_mode, prev_xset, battery_charge_allowed
            )
            return

        # smart_rce BatterySchedule active this hour (engaged OR recently
        # disengaged within current clock hour) — step aside to avoid racing
        # the inverter back into intervention immediately after slot cleanup.
        # Etap C: replaced legacy HA template binary_sensor.ems_other_automation_active_this_hour
        # with derived signal from BatterySchedule (`is_active_this_hour`).
        if ems_schedule_active_this_hour:
            self._set_neutral("schedule_active_this_hour")
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(
                state, prev_active, prev_mode, prev_xset, battery_charge_allowed
            )
            return

        # Legacy external EMS automation signal — kept in parallel with
        # schedule_active_this_hour during migration period (Etap C deploy
        # → Etap D drops this field). Both signals will indicate the same
        # condition until legacy automations migrate to schedule slots.
        if state.other_ems_automation_active_this_hour is True:
            self._set_neutral("other_automation_active")
            self._apply_disabled_override_if_needed(state)
            self._log_after_update(
                state, prev_active, prev_mode, prev_xset, battery_charge_allowed
            )
            return

        # --- Continue / Entry routing ---
        if self._active is not None:
            self._tick_active(
                state,
                battery_charge_allowed=battery_charge_allowed,
                start_charge_hour_override=start_charge_hour_override,
            )
        else:
            self._try_enter(
                state,
                battery_charge_allowed=battery_charge_allowed,
                start_charge_hour_override=start_charge_hour_override,
            )

        self._apply_disabled_override_if_needed(state)
        self._log_after_update(
            state, prev_active, prev_mode, prev_xset, battery_charge_allowed
        )

    @staticmethod
    def _none_present_core(state: InputState) -> bool:
        """Fields required to decide on entry/exit (gates).

        When any is None → manager cannot operate; update calls set_neutral
        and skips disabled_override (override also pointless without core inputs).
        """
        return (
            state.now is None
            or state.exported_energy_hourly is None
            or state.battery_soc is None
            or state.pv_power is None
        )

    def _tick_active(
        self,
        state: InputState,
        *,
        battery_charge_allowed: bool,
        start_charge_hour_override: time | None,
    ) -> None:
        """Continue path — global exit checks first, then delegate to intervention."""
        # Hour rollover (utility_meter resets hourly balance on the full hour,
        # each hour = a separate intervention decision).
        if state.now.hour != self._active.started_hour:
            self._set_neutral("hour_rollover")
            return
        # End of hour cleanup (now ≥ XX:59:50).
        if self._is_end_of_hour(state):
            self._set_neutral("end_of_hour_cleanup")
            return
        # Delegate to intervention — intervention-specific exits + recompute.
        # Uniform Protocol signature — NEGATIVE ignores start_charge_hour_override
        # internally (no pre-charge window concern). NO isinstance check —
        # that would break after `live_reload()` re-imports the class (instance
        # of OLD class fails `isinstance(NEW_class)`); same pattern as
        # `Direction.is_discharge` string compare rule (CLAUDE.md).
        result = self._active.continue_or_exit(
            state,
            battery_charge_allowed=battery_charge_allowed,
            start_charge_hour_override=start_charge_hour_override,
        )
        if result.is_exit:
            self._set_neutral(result.exit_reason)
        else:
            # self._active mutated in place — sync last_decision_reason.
            self.last_decision_reason = self._active.last_reason

    def _try_enter(
        self,
        state: InputState,
        *,
        battery_charge_allowed: bool,
        start_charge_hour_override: time | None,
    ) -> None:
        """Entry path — global entry blocks first, then balance range routing.

        Note: `other_ems_automation_active_this_hour` is handled by the global
        guard in `update()` (cross-cutting, applies to both entry and continue
        paths) — no need to re-check here.
        """
        # Late hour entry block (now ≥ XX:59:40).
        if self._is_too_late_for_entry(state):
            self.last_decision_reason = "too_late_in_hour"
            return

        # Balance range routing — mutually exclusive.
        # POSITIVE: balance > +0.06 (excessive export → CHARGE_BATTERY)
        # NEGATIVE: balance < entry_threshold (-0.05 pre-45min, 0.0 post-45min)
        # Deadzone (-0.05..+0.06): no intervention applies.
        balance = state.exported_energy_hourly
        if balance > positive.BALANCE_GATE_KWH:
            result = PositiveIntervention.try_enter(
                state,
                battery_charge_allowed=battery_charge_allowed,
                start_charge_hour_override=start_charge_hour_override,
            )
        elif balance < negative.entry_threshold(state):
            result = NegativeIntervention.try_enter(
                state, battery_charge_allowed=battery_charge_allowed
            )
        else:
            self.last_decision_reason = f"balance_in_deadzone_{balance:.3f}"
            return

        if result.is_blocked:
            self.last_decision_reason = result.block_reason
        else:
            self._active = result.intervention
            self.last_decision_reason = result.intervention.last_reason

    @classmethod
    def _is_end_of_hour(cls, state: InputState) -> bool:
        return (
            state.now.minute >= cls.LATE_HOUR_MINUTE
            and state.now.second >= cls.LATE_HOUR_EXIT_SECOND
        )

    @classmethod
    def _is_too_late_for_entry(cls, state: InputState) -> bool:
        return (
            state.now.minute >= cls.LATE_HOUR_MINUTE
            and state.now.second >= cls.LATE_HOUR_ENTRY_SECOND
        )

    # --- common helpers ---

    def _set_neutral(self, reason: str) -> None:
        """Reset to neutral (no active intervention) with given reason. Idempotent."""
        self._active = None
        self.last_decision_reason = reason

    def _apply_disabled_override_if_needed(self, state: InputState) -> None:
        """If strategy_mode = 'disabled' (or None) → override main outputs.

        Manager already exposed recommended_* via properties. Properties check
        the `_disabled_override_active` flag — when active, they return
        AUTO/None regardless of self._active. Here we set the flag and enrich
        reason with diagnostic info "what would have been".

        Defensive: None → treat as "disabled" (safe default when helper not
        yet configured).
        """
        mode = state.grid_export_strategy_mode
        if mode == self.STRATEGY_MODE_CHARGE_ADAPTIVE:
            self._disabled_override_active = False
            return  # active mode — main outputs preserved

        # mode is "disabled" or None — override
        # Capture would-be values BEFORE setting flag (properties depend on it).
        would_be_mode = self.recommended_ems_mode
        would_be_xset = self.recommended_xset
        would_be_reason = self.last_decision_reason
        prefix = (
            "disabled" if mode == self.STRATEGY_MODE_DISABLED else "no_strategy_mode"
        )
        if would_be_mode == self.AUTO_MODE:
            self.last_decision_reason = f"{prefix} ({would_be_reason})"
        else:
            xset_str = f" {would_be_xset}W" if would_be_xset else ""
            self.last_decision_reason = (
                f"{prefix} (would: {would_be_mode}{xset_str}, {would_be_reason})"
            )
        self._disabled_override_active = True

    # --- logging ---

    def _log_after_update(
        self,
        state: InputState,
        prev_active: bool,
        prev_mode: str,
        prev_xset: int | None,
        battery_charge_allowed: bool,
    ) -> None:
        """INFO transition + DEBUG snapshot (throttled)."""
        if (
            prev_active != self.intervention_active
            or prev_mode != self.recommended_ems_mode
            or prev_xset != self.recommended_xset
        ):
            _LOGGER.info(
                "GridExportManager transition: active %s→%s, mode %s→%s, "
                "xset %s→%s, reason=%s",
                prev_active,
                self.intervention_active,
                prev_mode,
                self.recommended_ems_mode,
                prev_xset,
                self.recommended_xset,
                self.last_decision_reason,
            )
        self._maybe_log_snapshot(state, battery_charge_allowed)

    def _maybe_log_snapshot(
        self, state: InputState, battery_charge_allowed: bool
    ) -> None:
        """Log DEBUG snapshot when key fields change OR throttle interval elapsed."""
        snapshot = (
            self.intervention_active,
            self.recommended_ems_mode,
            self.recommended_xset,
            self.last_decision_reason,
        )
        now = state.now
        should_log = (
            self._last_log_snapshot is None
            or snapshot != self._last_log_snapshot
            or self._last_log_ts is None
            or (
                now is not None
                and (now - self._last_log_ts).total_seconds()
                >= self.DEBUG_LOG_THROTTLE_SEC
            )
        )
        if not should_log:
            return
        pv_avail = state.pv_available
        _LOGGER.debug(
            "GridExportManager: now=%s active=%s mode=%s xset=%s reason=%s | "
            "strategy=%s hourly=%s soc=%s pv=%s pv_avg2m=%s pv_avail=%s "
            "charge_limit=%s charge_allowed=%s",
            now.strftime("%H:%M:%S") if now else "?",
            self.intervention_active,
            self.recommended_ems_mode,
            self.recommended_xset,
            self.last_decision_reason,
            state.grid_export_strategy_mode,
            state.exported_energy_hourly,
            state.battery_soc,
            state.pv_power,
            state.pv_power_avg_2_minutes,
            int(pv_avail) if pv_avail is not None else None,
            state.battery_charge_limit,
            battery_charge_allowed,
        )
        self._last_log_snapshot = snapshot
        self._last_log_ts = now
