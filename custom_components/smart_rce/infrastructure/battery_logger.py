"""BatteryManager observability — driven adapter dla logging.

Domain (`BatteryManager`) stays pure (zero `_LOGGER`, zero throttling).
Tutaj czytamy `manager.diagnostic_snapshot(state)` po każdym
`ems.update_state` i emitujemy logi gdy relevant fields się zmienią.

Wzorzec hexagonal: **driven adapter (outbound)** — domain dictates
"snapshot view", konkretna impl emituje do Python logging. Patrz ADR-018.
"""

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.core import callback

from ..application.ems import Ems
from ..domain.battery import BatteryManager

_LOGGER = logging.getLogger(__name__)

# Min interval (sec) between full DEBUG snapshots gdy stan key fields
# (phase + block_discharge) się NIE zmienia. Zapobiega spamowaniu logów
# co tick gdy nic się nie dzieje.
BATTERY_LOG_THROTTLE_SEC: Final[int] = 60


@dataclass
class _BatteryLogThrottle:
    """Throttling state dla DEBUG snapshot — nie część domain."""

    last_snapshot_key: tuple | None = None
    last_log_ts: datetime | None = None


class BatteryManagerLogger:
    """Driven adapter — log INFO transitions + throttled DEBUG snapshot.

    Registered jako listener via `ems.async_add_listener(log_if_changed)`.
    Po każdym `ems.update_state` czyta `manager.diagnostic_snapshot(state)`
    i emituje logi gdy relevant fields się zmieniły.

    Emit logów:
    1. **None-present DEBUG** — gdy phase=="none-present" (skip update)
    2. **Restored INFO** (jednorazowo) — pierwszy log_if_changed po
       async_restore wykrywa restored state.
    3. **Block_discharge transition INFO** — gdy `block_discharge` flipuje
    4. **Hour-start reset DEBUG** — pre-charge specific
    5. **Throttled DEBUG snapshot** — pełny dump key fields, max raz na
       BATTERY_LOG_THROTTLE_SEC gdy phase/block_discharge nie zmieni
    """

    def __init__(self, manager: BatteryManager, ems: Ems) -> None:
        self._manager = manager
        self._ems = ems
        self._prev: dict[str, Any] | None = None
        self._throttle = _BatteryLogThrottle()
        self._restored_logged = False

    @callback
    def log_if_changed(self) -> None:
        """Read diagnostic_snapshot, emit logs po zmianach (registered as ems listener)."""
        state = self._ems.last_input_state
        if state is None:
            return

        curr = self._manager.diagnostic_snapshot(state)
        prev = self._prev
        self._prev = curr

        if curr["phase"] == "none-present":
            _LOGGER.debug(
                "BatteryManager.update skipped (none_present): exported=%s now=%s",
                curr["exported_energy_hourly"],
                curr["now"],
            )
            return

        # Pierwszy "real" snapshot po starcie — log restored state jeśli
        # block_discharge=True (był persisted z poprzedniej sesji).
        if not self._restored_logged:
            self._restored_logged = True
            _LOGGER.info(
                "BatteryManager restored: block_discharge=%s last_hour=%s",
                curr["block_discharge"],
                curr["last_hour_seen"],
            )

        # INFO transition gdy block_discharge się flipuje
        if prev is not None and prev["block_discharge"] != curr["block_discharge"]:
            _LOGGER.info(
                "BatteryManager: block_discharge %s → %s (reason: %s)",
                prev["block_discharge"],
                curr["block_discharge"],
                curr["phase"],
            )

        # DEBUG hour-start reset (pre-charge specific)
        if (
            curr["phase"] == "pre-charge"
            and prev is not None
            and prev["last_hour_seen"] != curr["last_hour_seen"]
            and curr["now"] is not None
        ):
            _LOGGER.debug(
                "BatteryManager[pre-charge]: hour-start reset (hour=%d) "
                "→ block_discharge=False",
                curr["now"].hour,
            )

        self._maybe_log_snapshot(curr)

    def _maybe_log_snapshot(self, curr: dict[str, Any]) -> None:
        """Throttled DEBUG snapshot — log po zmianie phase/block_discharge lub timeout.

        Reduces log spam — pełny dump key fields max raz na
        BATTERY_LOG_THROTTLE_SEC gdy nic się nie zmienia.
        """
        snapshot_key = (curr["phase"], curr["block_discharge"])
        now = curr["now"]
        if now is None:
            return

        should_log = (
            self._throttle.last_snapshot_key is None
            or snapshot_key != self._throttle.last_snapshot_key
            or self._throttle.last_log_ts is None
            or (now - self._throttle.last_log_ts).total_seconds()
            >= BATTERY_LOG_THROTTLE_SEC
        )
        if not should_log:
            return

        exported = curr["exported_energy_hourly"]
        pv_avail_5m = curr["pv_available_5min"]
        _LOGGER.debug(
            "BatteryManager[%s] now=%s exported=%+.3fkWh(%+dWh) pv_avail_5m=%s "
            "DoD=%s toggle=%s charge_limit=%s override_window=%s | "
            "block_discharge=%s",
            curr["phase"],
            now.strftime("%H:%M:%S"),
            exported if exported is not None else 0.0,
            int(exported * 1000) if exported is not None else 0,
            f"{pv_avail_5m:+.0f}W" if pv_avail_5m is not None else "None",
            curr["depth_of_discharge"],
            curr["battery_charge_toggle_on"],
            curr["battery_charge_limit"],
            curr["start_charge_hour_override"],
            curr["block_discharge"],
        )
        self._throttle.last_snapshot_key = snapshot_key
        self._throttle.last_log_ts = now
