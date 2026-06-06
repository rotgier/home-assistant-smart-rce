"""Battery schedule I/O — `BatteryScheduleInput` + `BatteryOperation`."""

from __future__ import annotations

from dataclasses import dataclass

from ..ems_operation import EmsOperation


@dataclass(frozen=True)
class BatteryScheduleInput:
    """Subset of system input the schedule service needs each tick.

    Domain VO — no HA dependencies. Application layer (Ems body) translates
    the HA `InputState` snapshot to this via a one-line factory call.

    Etap 0: just SoC. Future extensions:
    - battery_power_w (dynamic rate adjustment per actual discharge rate)
    - any future signals service needs to make decisions
    """

    battery_soc: float | None


@dataclass(frozen=True)
class BatteryOperation:
    """Schedule output: EmsOperation + local battery management metadata.

    HAS-A `EmsOperation` (Goodwe inverter target — consumed by
    GoodweEmsActuator via `.ems_op`) plus `needs_charge_toggle` (local
    `switch.battery_charge_max_current_toggle` BMS guard — consumed by
    BatteryChargePolicy, separate concern from Goodwe writes).

    Composition over inheritance — schedule **produces** an EmsOperation
    + extra metadata; it is not itself an inverter target. Caller
    (`Ems._resolve_ems_operation`) extracts `.ems_op` when it needs the
    pure inverter target.

    `ems_op.source="schedule"` for both slot-driven and one-shot ops;
    `ems_op.reason` carries identity: `"slot=DISCHARGE_EVENING"` /
    `"oneshot=DISCHARGE"` / None when idle. Diagnostic-only — no
    programmatic parsing required.

    NO `ems_override_active` field — `schedule.ems_interventions_blocked`
    is the canonical source of truth (read by Ems body and passed
    explicitly to DodPolicy/GridExportManager). NO `dod_force` — DodPolicy
    reacts via `INTERVENTIONS_BLOCKED` phase.
    """

    ems_op: EmsOperation
    needs_charge_toggle: bool = False

    @property
    def is_idle(self) -> bool:
        """Forward to ems_op — engagement is driven by inverter target state."""
        return self.ems_op.is_idle

    @classmethod
    def idle(cls) -> BatteryOperation:
        return cls(ems_op=EmsOperation.neutral(), needs_charge_toggle=False)

    # Sources construct BatteryOperation themselves via
    # `BatteryScheduleEntry.to_battery_operation()` and
    # `OneShotOperation.to_battery_operation()` — keeps the translation
    # knowledge with the source class (Tell-Don't-Ask), so BatteryOperation
    # doesn't have to know its possible sources.
