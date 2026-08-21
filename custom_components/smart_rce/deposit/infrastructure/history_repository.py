"""Persists SettlementHistory via HA Store, bootstrapping from the shipped seed.

The seed is only ever a starting point: once the store holds anything, it wins.
That keeps the invoice-reconciled history authoritative on first install without
re-applying it over data the integration has since measured itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from ...infrastructure.repository import Repository
from ..domain.settlement_history import SettlementHistory
from .resources import load_seed_history

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ...infrastructure.async_task_runner import AsyncTaskRunner

_LOGGER = logging.getLogger(__name__)


class HistoryRepository(Repository[SettlementHistory]):
    """Owns the SettlementHistory aggregate."""

    STORAGE_KEY: ClassVar[str] = "deposit_history"

    def __init__(self, hass: HomeAssistant, tasks: AsyncTaskRunner) -> None:
        super().__init__(hass, tasks)
        self._hass = hass
        self._history: SettlementHistory | None = None

    @property
    def history(self) -> SettlementHistory:
        if self._history is None:
            raise RuntimeError("async_restore() must run before use")
        return self._history

    def _get_aggregate(self) -> SettlementHistory:
        return self.history

    async def async_restore(self) -> None:
        """Load persisted history, or bootstrap from the seed on first run."""
        data: dict[str, Any] | None = await self._store.async_load()
        if data:
            self._history = SettlementHistory.from_dict(data)
            _LOGGER.debug(
                "Deposit history restored: %d closed months, %d open days, watermark %s",
                len(self._history.months),
                self._history.elapsed_days,
                self._history.last_data_day,
            )
            return
        seed = await self._hass.async_add_executor_job(load_seed_history)
        self._history = SettlementHistory.from_seed(seed.months)
        _LOGGER.info(
            "Deposit history bootstrapped from seed: %d closed months up to %s",
            len(self._history.months),
            self._history.last_data_day,
        )
        await self.persist()
