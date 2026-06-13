"""Unit tests for rain timing — RainState domain + RainService orchestration."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce.garden.application.rain_service import RainService
from custom_components.smart_rce.garden.domain.rain import DEFAULT_DRY_HOURS, RainState

NOW = datetime(2026, 6, 13, 9, 0, tzinfo=UTC)


# --- RainState domain ---


def test_dry_at_none_without_rain_end() -> None:
    assert RainState().dry_at is None


def test_dry_at_is_rain_end_plus_dry_hours() -> None:
    state = RainState(rain_ended_at=NOW, dry_hours=4.5)
    assert state.dry_at == NOW + timedelta(hours=4, minutes=30)


def test_record_dry_transition_changed_flag() -> None:
    state = RainState()
    assert state.record_dry_transition(NOW) is True
    assert state.rain_ended_at == NOW
    assert state.record_dry_transition(NOW) is False  # same → no change


def test_set_dry_hours_changed_flag() -> None:
    state = RainState()
    assert state.set_dry_hours(6.0) is True
    assert state.dry_hours == 6.0
    assert state.set_dry_hours(6.0) is False


def test_serialization_roundtrip() -> None:
    state = RainState(rain_ended_at=NOW, dry_hours=3.5)
    restored = RainState.from_dict(state.to_dict())
    assert restored.rain_ended_at == NOW
    assert restored.dry_hours == 3.5


def test_from_dict_empty_defaults() -> None:
    restored = RainState.from_dict({})
    assert restored.rain_ended_at is None
    assert restored.dry_hours == DEFAULT_DRY_HOURS


# --- RainService ---


def _service() -> tuple[RainService, MagicMock]:
    repo = MagicMock()
    repo.state = RainState()
    repo.persist = AsyncMock()
    repo.save_if_changed = MagicMock()
    return RainService(repo), repo


def test_first_dry_reading_no_transition() -> None:
    service, repo = _service()

    service.observe(currently_wet=False, now=NOW)

    assert repo.state.rain_ended_at is None
    repo.save_if_changed.assert_not_called()


def test_wet_then_dry_records_transition() -> None:
    service, repo = _service()

    service.observe(currently_wet=True, now=NOW)  # arms _was_wet
    service.observe(currently_wet=False, now=NOW + timedelta(minutes=5))

    assert repo.state.rain_ended_at == NOW + timedelta(minutes=5)
    repo.save_if_changed.assert_called_once()


def test_staying_wet_no_transition() -> None:
    service, repo = _service()

    service.observe(currently_wet=True, now=NOW)
    service.observe(currently_wet=True, now=NOW + timedelta(minutes=5))

    assert repo.state.rain_ended_at is None
    repo.save_if_changed.assert_not_called()
    assert service.currently_wet is True


async def test_set_dry_hours_persists() -> None:
    service, repo = _service()

    await service.set_dry_hours(6.0)

    assert repo.state.dry_hours == 6.0
    repo.persist.assert_awaited_once()


def test_dry_at_reflects_state_after_transition() -> None:
    service, repo = _service()
    repo.state.dry_hours = 4.5

    service.observe(currently_wet=True, now=NOW)
    service.observe(currently_wet=False, now=NOW)

    assert service.dry_at == NOW + timedelta(hours=4, minutes=30)
