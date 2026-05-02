"""Grid Export Manager — orchestrator dla POSITIVE / NEGATIVE strategii.

Wystawia 4 pola czytane przez sensory:
- intervention_active (bool, diagnostic)
- recommended_ems_mode (str: "auto" | "discharge_battery" | "charge_battery")
- recommended_xset (int | None) — W
- last_decision_reason (str)

Listener w adapter.py reaguje na zmiany sensorów i wywołuje
number.goodwe_ems_power_limit + select.goodwe_ems_mode.

Active window: post_charge → next day 7:00 (POSITIVE skip pre_charge — tam
BatteryManager rządzi; NEGATIVE działa w pre_charge).

Strategie wydzielone do osobnych klas:
- `PositiveStrategy` (`grid_export_positive.py`) — STANDBY / charge_adaptive
- `NegativeStrategy` (`grid_export_negative.py`) — adaptive buckets target +1500W

Decision tree (`grid_export_strategy_mode`):
- "charge_adaptive" → domyślne aktywne (POSITIVE i NEGATIVE)
- "disabled"        → manager evaluuje, ale intervention off (diagnostic only)

Defensive: gdy `state.pv_available` lub `battery_charge_limit` są None
(np. po HA restart, sensory unavailable przez ~25-50ms) → no-op, manager
wraca do AUTO, listener wraca rejestry do AUTO.
"""

from __future__ import annotations

from enum import StrEnum
import logging
from typing import Final

from custom_components.smart_rce.domain.grid_export_negative import (
    NegativeResolution,
    NegativeStrategy,
)
from custom_components.smart_rce.domain.grid_export_positive import PositiveStrategy
from custom_components.smart_rce.domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)


class InterventionDirection(StrEnum):
    """Kierunek aktywnej interwencji GridExportManager.

    POSITIVE — bilans hourly nadmiernie pozytywny (eksport > 0.06 kWh),
    manager wymusza CHARGE_BATTERY (lub STANDBY przy niskim PV) by zjeść saldo.

    NEGATIVE — bilans hourly negatywny (import netto), manager wymusza
    adaptive charge/discharge by ustabilizować meter ≈ +1500W eksport.
    """

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class GridExportManager:
    """Orchestrator — wiąże PositiveStrategy + NegativeStrategy."""

    # Mode constants (Goodwe EMS) — wystawione na recommended_ems_mode.
    AUTO_MODE: Final[str] = "auto"
    CHARGE_MODE: Final[str] = "charge_battery"
    DISCHARGE_MODE: Final[str] = "discharge_battery"

    # Strategy modes (input_select.smart_rce_grid_export_strategy_mode)
    STRATEGY_MODE_DISABLED: Final[str] = "disabled"
    STRATEGY_MODE_CHARGE_ADAPTIVE: Final[str] = "charge_adaptive"

    # Logowanie — throttle DEBUG snapshot gdy nic się nie zmienia (60s).
    DEBUG_LOG_THROTTLE_SEC: Final[int] = 60

    def __init__(self) -> None:
        self.intervention_active: bool = False
        self.intervention_direction: InterventionDirection | None = None
        self.recommended_ems_mode: str = self.AUTO_MODE
        self.recommended_xset: int | None = None
        self.last_decision_reason: str | None = None
        self._intervention_started_hour: int | None = None
        self._positive: Final = PositiveStrategy()
        self._negative: Final = NegativeStrategy()
        # Throttling dla DEBUG snapshotów (jak w BatteryManager)
        self._last_log_snapshot: tuple | None = None
        self._last_log_ts = None  # type: ignore[var-annotated]

    def get_active_intervention(self) -> InterventionDirection | None:
        """Aktualny kierunek interwencji (POSITIVE/NEGATIVE/None).

        Używane przez WaterHeaterManager do dyfferencjacji reserved (większy
        reserved przy NEGATIVE — grzałki off priorytetowo).
        """
        if not self.intervention_active:
            return None
        return self.intervention_direction

    def update(self, state: InputState) -> None:
        """Re-evaluate intervention state from current InputState.

        Called reactively by Ems on every state change.
        """
        prev_active = self.intervention_active
        prev_mode = self.recommended_ems_mode
        prev_xset = self.recommended_xset

        # None-guard: brak core inputs → set_neutral, skip disabled_override
        # (manager nie może działać, override też nie ma sensu).
        if self._none_present_core(state):
            self._set_neutral("none_present")
            self._log_after_update(state, prev_active, prev_mode, prev_xset)
            return

        self._update_core(state)
        self._apply_disabled_override_if_needed(state)
        self._log_after_update(state, prev_active, prev_mode, prev_xset)

    def _update_core(self, state: InputState) -> None:
        """Dispatch — continue branch (gdy active) lub entry branch."""
        if self.intervention_active:
            self._continue_intervention(state)
            return
        self._try_enter(state)

    @staticmethod
    def _none_present_core(state: InputState) -> bool:
        """Pola wymagane do podjęcia DECYZJI o entry/exit (gates)."""
        return (
            state.now is None
            or state.exported_energy_hourly is None
            or state.battery_soc is None
            or state.pv_power is None
        )

    def _continue_intervention(self, state: InputState) -> None:
        """Continue branch — hour rollover check + dispatch po direction.

        Hour rollover: utility_meter resetuje balans hourly o pełnej godzinie,
        każda godzina = osobna decyzja czy intervention.
        """
        if state.now.hour != self._intervention_started_hour:
            self._set_neutral("hour_rollover")
            return
        if self.intervention_direction is InterventionDirection.NEGATIVE:
            self._continue_negative(state)
        else:
            self._continue_positive(state)

    def _continue_positive(self, state: InputState) -> None:
        """Continue POSITIVE — exit check + resolve + commit."""
        exit_reason = self._positive.exit_reason(state)
        if exit_reason is not None:
            self._set_neutral(exit_reason)
            return
        self._resolve_and_commit_positive(state)

    def _continue_negative(self, state: InputState) -> None:
        """Continue NEGATIVE — resolve+clamp z hysteresis, exit check, commit.

        Exit_reason wymaga post-clamp xset_signed (z resolution), więc resolve
        PRZED exit check.
        """
        resolution = self._negative.resolve_for_continue(
            state, self.recommended_ems_mode, self.recommended_xset
        )
        if resolution is None:
            self._set_neutral("none_pv_available")
            return
        exit_reason = self._negative.exit_reason(state, resolution.xset_signed)
        if exit_reason is not None:
            self._set_neutral(exit_reason)
            return
        self._commit_negative(resolution)

    def _try_enter(self, state: InputState) -> None:
        """Entry branch — sprawdź POSITIVE/NEGATIVE entry, neutral z join'em.

        POSITIVE i NEGATIVE są mutually exclusive (różne progi hourly), kolejność
        sprawdzania arbitralna.
        """
        pos_block = self._positive.entry_block_reason(state)
        if pos_block is None:
            self._enter_positive(state)
            return
        neg_block = self._negative.entry_block_reason(state)
        if neg_block is None:
            self._enter_negative(state)
            return
        # Neither direction — pokazuje DLACZEGO żaden kierunek nie wszedł.
        self._set_neutral(f"pos:{pos_block} | neg:{neg_block}")

    def _enter_positive(self, state: InputState) -> None:
        """Enter POSITIVE intervention — mark state + resolve + commit."""
        self.intervention_active = True
        self.intervention_direction = InterventionDirection.POSITIVE
        self._intervention_started_hour = state.now.hour
        self._resolve_and_commit_positive(state)

    def _resolve_and_commit_positive(self, state: InputState) -> None:
        """Resolve PositiveStrategy + commit do recommended_* (lub set_neutral).

        Common helper dla _enter_positive (entry) i _continue_positive (continue).
        Resolution z mode=None sygnalizuje exit (np. none_pv_available).
        """
        resolution = self._positive.resolve(state, self.recommended_xset)
        if resolution.mode is None:
            self._set_neutral(resolution.reason)
            return
        mode, xset, reason = resolution.build_output()
        self.recommended_ems_mode = mode
        self.recommended_xset = xset
        self.last_decision_reason = reason

    def _enter_negative(self, state: InputState) -> None:
        """Enter NEGATIVE intervention — mark state + fresh resolve + commit.

        Wchodzimy z AUTO (clean state) — fresh lookup zamiast matchować przez
        hysteresis do tego co było wcześniej.
        """
        self.intervention_active = True
        self.intervention_direction = InterventionDirection.NEGATIVE
        self._intervention_started_hour = state.now.hour
        resolution = self._negative.resolve_for_entry(state)
        if resolution is None:
            self._set_neutral("none_pv_available")
            return
        self._commit_negative(resolution)

    def _set_neutral(self, reason: str) -> None:
        """Reset to AUTO mode with given reason. Idempotent.

        Multi-caller helper — wywoływany przez _update_core (none_present, neither),
        _continue_intervention (hour_rollover), _continue_positive/negative (exit),
        _enter_negative (none_pv_available), _try_enter (neither),
        _resolve_and_commit_positive (exit signal). Last caller w pliku =
        _enter_negative, umieszczone zaraz po nim.
        """
        self.intervention_active = False
        self.intervention_direction = None
        self.recommended_ems_mode = self.AUTO_MODE
        self.recommended_xset = None
        self.last_decision_reason = reason
        self._intervention_started_hour = None

    def _commit_negative(self, resolution: NegativeResolution) -> None:
        """Build NEGATIVE output (z resolution.build_output) i zapisz do recommended_*.

        Common helper dla _enter_negative (entry) i _continue_negative (continue).
        """
        mode, xset, reason = resolution.build_output()
        self.recommended_ems_mode = mode
        self.recommended_xset = xset
        self.last_decision_reason = reason

    # --- common helpers ---

    def _apply_disabled_override_if_needed(self, state: InputState) -> None:
        """Jeśli strategy_mode = 'disabled' (lub None) → override main outputs.

        Manager już wystawił recommended_* (would-be decision). Teraz nadpisujemy
        na AUTO/None (intervention off, listener cofa Goodwe), zachowując
        diagnostykę w last_decision_reason.

        Defensive: None → traktuj jak "disabled" (safe default gdy helper
        jeszcze nieskonfigurowany).
        """
        mode = state.grid_export_strategy_mode
        if mode == self.STRATEGY_MODE_CHARGE_ADAPTIVE:
            return  # active mode — main outputs zostawiamy
        # mode is "disabled" or None — override
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
        self.intervention_active = False
        self.intervention_direction = None
        self.recommended_ems_mode = self.AUTO_MODE
        self.recommended_xset = None

    # --- logging ---

    def _log_after_update(
        self,
        state: InputState,
        prev_active: bool,
        prev_mode: str,
        prev_xset: int | None,
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
        self._maybe_log_snapshot(state)

    def _maybe_log_snapshot(self, state: InputState) -> None:
        """Log DEBUG snapshot gdy key fields się zmienią LUB minął throttle interval."""
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
            "charge_limit=%s toggle=%s",
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
            state.battery_charge_toggle_on,
        )
        self._last_log_snapshot = snapshot
        self._last_log_ts = now
