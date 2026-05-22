"""DodPolicy observability — driven adapter for logging.

Domain (`DodPolicy`) stays pure (zero `_LOGGER`, zero throttling). This
adapter reads `policy.target_dod + current_phase + _prev_block` after
each `ems.update_state` and emits logs when relevant fields change.

Hexagonal pattern: **driven adapter (outbound)** — domain dictates
"snapshot view", concrete impl emits to Python logging. ADR-018.
"""

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Final

from homeassistant.core import callback

from ..domain.dod_policy import DodPolicy
from ..domain.input_state import InputState

_LOGGER = logging.getLogger(__name__)

# Min interval (sec) between full DEBUG snapshots when key fields
# (phase + target_dod + _prev_block) don't change. Prevents log spam
# every tick when nothing happens.
DOD_POLICY_LOG_THROTTLE_SEC: Final[int] = 60


@dataclass
class _DodPolicyLogThrottle:
    """Throttling state for DEBUG snapshot — not part of domain."""

    last_snapshot_key: tuple | None = None
    last_log_ts: datetime | None = None


class DodPolicyLogger:
    """Driven adapter — log INFO transitions + throttled DEBUG snapshot.

    Registered as listener via `ems.async_add_listener(log_if_changed)`.
    After each `ems.update_state` reads policy fields and emits logs when
    relevant fields change.

    Emits:
    1. **Restored INFO** (one-shot) — first log_if_changed after async_restore.
    2. **target_dod transition INFO** — when target_dod flips (e.g. 0↔90
       or override → policy value).
    3. **Throttled DEBUG snapshot** — phase/target_dod/_prev_block dump,
       max once per DOD_POLICY_LOG_THROTTLE_SEC when fields don't change.
    """

    def __init__(self, policy: DodPolicy) -> None:
        self._policy = policy
        self._prev_target_dod: int | None = None
        self._prev_phase_value: str | None = None
        self._throttle = _DodPolicyLogThrottle()
        self._restored_logged = False

    @callback
    def log_if_changed(self, state: InputState) -> None:
        """Emit logs after policy update (called explicitly from Ems body).

        `state` passed by caller — eliminates back-reference to Ems and keeps
        the logger's contract narrow (only what it needs to read).
        """
        if state.now is None:
            return

        curr_target = self._policy.target_dod
        curr_phase_value = self._policy.current_phase.value

        # First "real" snapshot after start — log restored state.
        if not self._restored_logged:
            self._restored_logged = True
            _LOGGER.info(
                "DodPolicy restored: target_dod=%d phase=%s prev_block=%s",
                curr_target,
                curr_phase_value,
                self._policy._prev_block,  # noqa: SLF001 — diagnostic read
            )

        # INFO transition when target_dod flips
        if self._prev_target_dod is not None and self._prev_target_dod != curr_target:
            _LOGGER.info(
                "DodPolicy: target_dod %d → %d (phase=%s, prev_block=%s)",
                self._prev_target_dod,
                curr_target,
                curr_phase_value,
                self._policy._prev_block,  # noqa: SLF001 — diagnostic read
            )

        self._prev_target_dod = curr_target
        self._prev_phase_value = curr_phase_value

        self._maybe_log_snapshot(state, curr_target, curr_phase_value)

    def _maybe_log_snapshot(
        self, state: InputState, target_dod: int, phase_value: str
    ) -> None:
        """Throttled DEBUG snapshot — log on change or timeout.

        Reduces log spam — full dump max once per DOD_POLICY_LOG_THROTTLE_SEC
        when nothing changes.
        """
        prev_block = self._policy._prev_block  # noqa: SLF001 — diagnostic read
        snapshot_key = (phase_value, target_dod, prev_block)
        now = state.now
        if now is None:
            return
        should_log = (
            self._throttle.last_snapshot_key is None
            or snapshot_key != self._throttle.last_snapshot_key
            or self._throttle.last_log_ts is None
            or (now - self._throttle.last_log_ts).total_seconds()
            >= DOD_POLICY_LOG_THROTTLE_SEC
        )
        if not should_log:
            return

        exported = state.exported_energy_hourly
        pv_avail_5m = state.pv_available_5min
        _LOGGER.debug(
            "DodPolicy[%s] now=%s target_dod=%d prev_block=%s "
            "exported=%s(%+dWh) pv_avail_5m=%s "
            "DoD_inverter=%s override=%s",
            phase_value,
            now.strftime("%H:%M:%S"),
            target_dod,
            prev_block,
            f"{exported:+.3f}kWh" if exported is not None else "None",
            int(exported * 1000) if exported is not None else 0,
            f"{pv_avail_5m:+.0f}W" if pv_avail_5m is not None else "None",
            state.depth_of_discharge,
            state.dod_override,
        )
        self._throttle.last_snapshot_key = snapshot_key
        self._throttle.last_log_ts = now
