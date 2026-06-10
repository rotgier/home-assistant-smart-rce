"""Unit tests for NonWorkService (compose start/end, persist+notify, drive actuator)."""

from datetime import time
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce.garden.application.non_work_service import (
    NonWorkService,
)
from custom_components.smart_rce.garden.domain.non_work import (
    NonWorkHours,
    NonWorkSchedule,
)


def _service() -> tuple[NonWorkService, MagicMock, MagicMock]:
    repo = MagicMock()
    repo.schedule = NonWorkSchedule(NonWorkHours(time(20, 35), time(10, 5)))
    repo.persist = AsyncMock()
    actuator = MagicMock()
    actuator.apply = AsyncMock()
    return NonWorkService(repo, actuator), repo, actuator


async def test_set_start_composes_with_existing_end() -> None:
    service, repo, actuator = _service()

    await service.set_start(time(21, 0))

    assert repo.schedule.target == NonWorkHours(time(21, 0), time(10, 5))
    repo.persist.assert_awaited_once()
    actuator.apply.assert_awaited_once()


async def test_set_end_composes_with_existing_start() -> None:
    service, repo, actuator = _service()

    await service.set_end(time(9, 0))

    assert repo.schedule.target == NonWorkHours(time(20, 35), time(9, 0))
    actuator.apply.assert_awaited_once()


async def test_no_change_skips_persist_and_actuator() -> None:
    service, repo, actuator = _service()

    await service.set_target(NonWorkHours(time(20, 35), time(10, 5)))  # identical

    repo.persist.assert_not_awaited()
    actuator.apply.assert_not_awaited()


async def test_set_start_noop_when_target_unset() -> None:
    service, repo, actuator = _service()
    repo.schedule = NonWorkSchedule(target=None)

    await service.set_start(time(21, 0))

    actuator.apply.assert_not_awaited()
