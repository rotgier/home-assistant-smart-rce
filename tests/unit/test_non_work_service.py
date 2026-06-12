"""Unit tests for NonWorkService (compose start/end, pending edges, drift)."""

from datetime import time
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce.garden.application.non_work_service import (
    NonWorkService,
)
from custom_components.smart_rce.garden.domain.non_work import (
    NonWorkHours,
    NonWorkSchedule,
)

TARGET = NonWorkHours(time(20, 35), time(10, 5))
GHOST = NonWorkHours(time(4, 31), time(20, 49))


def _service(target: NonWorkHours | None = TARGET) -> tuple[NonWorkService, MagicMock]:
    repo = MagicMock()
    repo.schedule = NonWorkSchedule(target)
    repo.persist = AsyncMock()
    return NonWorkService(repo), repo


async def test_set_start_composes_with_existing_end() -> None:
    service, repo = _service()

    await service.set_start(time(21, 0))

    assert repo.schedule.target == NonWorkHours(time(21, 0), time(10, 5))
    repo.persist.assert_awaited_once()


async def test_set_end_composes_with_existing_start() -> None:
    service, repo = _service()

    await service.set_end(time(9, 0))

    assert repo.schedule.target == NonWorkHours(time(20, 35), time(9, 0))
    repo.persist.assert_awaited_once()


async def test_no_change_skips_persist() -> None:
    service, repo = _service()

    await service.set_target(TARGET)  # identical

    repo.persist.assert_not_awaited()


async def test_first_edge_is_pending_until_both_set() -> None:
    service, repo = _service(target=None)

    await service.set_start(time(20, 35))

    assert repo.schedule.target is None  # no half-target persisted
    assert service.start == time(20, 35)  # pending edge visible in UI
    assert service.end is None
    repo.persist.assert_not_awaited()

    await service.set_end(time(10, 5))

    assert repo.schedule.target == TARGET
    repo.persist.assert_awaited_once()


async def test_both_pending_edges_in_end_first_order() -> None:
    service, repo = _service(target=None)

    await service.set_end(time(10, 5))
    await service.set_start(time(20, 35))

    assert repo.schedule.target == TARGET


async def test_drift_false_without_target_or_cloud() -> None:
    service, _ = _service(target=None)
    assert service.drift is False  # no target, no cloud

    service.update_cloud_state(GHOST)
    assert service.drift is False  # cloud known, target still unset

    service_with_target, _ = _service()
    assert service_with_target.drift is False  # target known, cloud unknown


async def test_drift_tracks_cloud_vs_target() -> None:
    service, repo = _service()

    service.update_cloud_state(TARGET)
    assert service.drift is False

    service.update_cloud_state(GHOST)
    assert service.drift is True
    assert service.cloud == GHOST

    service.update_cloud_state(TARGET)  # cloud recovered
    assert service.drift is False

    repo.persist.assert_not_awaited()  # cloud observation never persists


async def test_update_cloud_state_notifies_only_on_change() -> None:
    service, _ = _service()
    notified = []
    service.add_listener(lambda: notified.append(1))

    service.update_cloud_state(GHOST)
    service.update_cloud_state(GHOST)  # same value — no second notify

    assert len(notified) == 1
