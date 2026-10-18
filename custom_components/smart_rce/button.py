"""Smart RCE button platform — one-shot operation triggers.

Etap 2F — 4 buttons per direction (charge / discharge):
- `button.smart_rce_oneshot_<dir>_execute` — starts one-shot using stored
  params (target_soc + end_time from number/time entities)
- `button.smart_rce_oneshot_<dir>_cancel` — cancels active one-shot
- `button.ems_evening_plan_recalculate` — recomputes the evening discharge
  plan on demand, for whichever evening comes next
- `button.ems_forecast_score_recompute` — scores today now (forecast scoreboard)

Persistence owned by `BatteryScheduleRepository`.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.dt import now as now_local

from . import SmartRceConfigEntry
from .const import DOMAIN
from .domain.battery_schedule import Direction
from .ems_device import ems_device_info
from .garden.button_entities import build_buttons
from .infrastructure.evening_plan_notifier import notify_evening_plan

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartRceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add smart_rce button entities."""
    async_add_entities(
        [
            EveningPlanRecalculateButton(entry),
            ForecastScoreRecomputeButton(entry),
            OneShotExecuteButton(entry, direction=Direction.DISCHARGE),
            OneShotCancelButton(entry, direction=Direction.DISCHARGE),
            OneShotExecuteButton(entry, direction=Direction.CHARGE),
            OneShotCancelButton(entry, direction=Direction.CHARGE),
            *build_buttons(entry),
        ]
    )


class OneShotExecuteButton(ButtonEntity):
    """Press to start a one-shot operation using stored params.

    Reads `target_soc` and `end_time` from companion number/time entities
    (per direction). No-op if a one-shot is already active (aggregate
    enforces single-active invariant).
    """

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, entry: SmartRceConfigEntry, *, direction: Direction) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._direction = direction
        slug = f"oneshot_{direction.name.lower()}_execute"
        self._attr_name = f"EMS One-Shot {direction.name.title()} Execute"
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self.entity_id = f"button.smart_rce_{slug}"
        self._attr_icon = (
            "mdi:battery-arrow-down"
            if direction.is_discharge
            else "mdi:battery-arrow-up"
        )
        self._attr_device_info = ems_device_info(entry)

    async def async_press(self) -> None:
        await self._service.handle_start_oneshot(self._direction)


class OneShotCancelButton(ButtonEntity):
    """Press to cancel an active one-shot operation. No-op when idle."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, entry: SmartRceConfigEntry, *, direction: Direction) -> None:
        self._entry = entry
        self._service = entry.runtime_data.ems.battery_schedule_service
        self._direction = direction
        slug = f"oneshot_{direction.name.lower()}_cancel"
        self._attr_name = f"EMS One-Shot {direction.name.title()} Cancel"
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self.entity_id = f"button.smart_rce_{slug}"
        self._attr_device_info = ems_device_info(entry)

    async def async_press(self) -> None:
        # Cancel applies regardless of which button is pressed — direction is
        # only used for entity identity. Aggregate stores only one active op.
        await self._service.handle_cancel_oneshot()


class EveningPlanRecalculateButton(ButtonEntity):
    """Recompute the evening discharge plan now, instead of waiting for a run.

    Targets whichever evening comes next: tonight while today's window is
    still open, tomorrow once it has closed. `EveningPlan.plans_tomorrow_at`
    owns that switchover (season, and tonight's last slot) — and the schedule
    dates the evening, so tomorrow's windows cannot fire in hours still open
    today.
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Evening Plan Recalculate"
    _attr_should_poll = False
    _attr_icon = "mdi:calculator-variant-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._entry = entry
        self._ems = entry.runtime_data.ems
        self._attr_unique_id = f"{DOMAIN}_ems_evening_plan_recalculate"
        self.entity_id = "button.ems_evening_plan_recalculate"
        self._attr_device_info = ems_device_info(entry)

    async def async_press(self) -> None:
        result = await self._ems.replan_nearest_evening(now_local())
        if result is None:
            _LOGGER.warning("Evening plan not recomputed — prices or calendar missing")
            return
        _LOGGER.info("Evening plan recomputed on demand: %s", result.plan.reason)
        await notify_evening_plan(
            self.hass,
            result.plan,
            application=result.application,
            for_tomorrow=result.for_tomorrow,
            answer_always=True,
        )


class ForecastScoreRecomputeButton(ButtonEntity):
    """Score today now with the data so far, instead of waiting for a trigger.

    Parts the data cannot settle yet stay pending; after 21:06 it is the final
    score (`ForecastScoreService.async_manual`).
    """

    _attr_has_entity_name = False
    _attr_name = "EMS Forecast Score Recompute"
    _attr_should_poll = False
    _attr_icon = "mdi:scoreboard-outline"

    def __init__(self, entry: SmartRceConfigEntry) -> None:
        self._service = entry.runtime_data.forecast_score
        self._attr_unique_id = f"{DOMAIN}_ems_forecast_score_recompute"
        self.entity_id = "button.ems_forecast_score_recompute"
        self._attr_device_info = ems_device_info(entry)

    async def async_press(self) -> None:
        await self._service.async_manual()
