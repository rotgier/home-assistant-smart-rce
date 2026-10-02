"""The scheduler's failure guard — what happens when a run raises.

A run is fired by `async_track_time_change`, which never looks at the
coroutine again. Before the guard existed, anything raised inside one became
"Task exception was never retrieved" in the log and nothing else: no slots
moved, no message arrived, and the evening kept the previous day's windows.
Two runs were lost that way on 02.10 before anyone noticed the timestamp on a
Telegram.
"""

from datetime import datetime
from unittest.mock import AsyncMock, Mock

from custom_components.smart_rce.infrastructure import evening_plan_scheduler
from custom_components.smart_rce.infrastructure.evening_plan_scheduler import (
    EveningPlanScheduler,
)
import pytest

NOW = datetime(2026, 10, 2, 21, 5)


@pytest.fixture
def reported(monkeypatch):
    """Collect the failure notifications the guard decides to send."""
    sent = []

    async def _capture(hass, *, run, error):
        sent.append((run, type(error).__name__))

    monkeypatch.setattr(
        evening_plan_scheduler,
        "notify_evening_plan_failed",
        AsyncMock(side_effect=_capture),
    )
    return sent


@pytest.fixture
def scheduler():
    return EveningPlanScheduler(Mock(), Mock())


async def test_a_crash_is_reported_instead_of_vanishing(scheduler, reported):
    async def boom(now):
        raise KeyError("DISCHARGE_EVENING_EARLY")

    await scheduler._guarded(boom, "tura wieczorna")(NOW)

    assert reported == [("tura wieczorna", "KeyError")]


async def test_a_crash_does_not_propagate(scheduler, reported):
    """The guard swallows it — an unraised exception is the whole point."""

    async def boom(now):
        raise RuntimeError("nope")

    await scheduler._guarded(boom, "przegląd popołudniowy")(NOW)  # must not raise


async def test_only_the_first_failure_of_an_outage_speaks(scheduler, reported):
    """The sweep fires every five minutes; one broken deploy must not spam."""

    async def boom(now):
        raise KeyError("x")

    guarded = scheduler._guarded(boom, "przegląd popołudniowy")
    for _ in range(12):
        await guarded(NOW)

    assert len(reported) == 1


async def test_a_clean_run_rearms_the_alarm(scheduler, reported):
    """Recovery resets the flag, so a fresh outage is announced again."""

    async def boom(now):
        raise KeyError("x")

    async def fine(now):
        return None

    await scheduler._guarded(boom, "tura wieczorna")(NOW)
    await scheduler._guarded(fine, "tura wieczorna")(NOW)
    await scheduler._guarded(boom, "tura wieczorna")(NOW)

    assert len(reported) == 2


async def test_a_successful_run_says_nothing(scheduler, reported):
    async def fine(now):
        return None

    await scheduler._guarded(fine, "tura wieczorna")(NOW)

    assert reported == []
