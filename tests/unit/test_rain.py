"""Unit tests for rain timing — RainState domain + RainService orchestration."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from custom_components.smart_rce.garden.application.rain_service import RainService
from custom_components.smart_rce.garden.domain.rain import RainEvent, RainState

NOW = datetime(2026, 6, 13, 9, 0, tzinfo=UTC)


# --- RainState domain ---


def test_dry_at_none_without_rain_end() -> None:
    assert RainState().dry_at is None


def test_dry_at_is_rain_end_plus_dry_hours() -> None:
    state = RainState(rain_ended_at=NOW, dry_hours=4.5)
    assert state.dry_at == NOW + timedelta(hours=4, minutes=30)


def test_dry_at_future_while_wet_ignores_stale_rain_end() -> None:
    # A previous shower ended at NOW (stale rain_ended_at). New rain confirms
    # 3 h later — dry_at must anchor on the ONGOING rain (last_wet_at), not the
    # stale end, else the planner reopens its window and resumes into wet grass
    # (regression 2026-07-09).
    state = RainState(rain_ended_at=NOW, dry_hours=3.0)
    state.observe(raw_wet=True, now=_min(180))  # arms wet_since
    state.observe(raw_wet=True, now=_min(191))  # >dwell → confirmed wet
    assert state.is_wet is True
    assert state.dry_at == _min(191) + timedelta(hours=3)  # future, not NOW+3h
    assert state.dry_at > _min(191)


def test_dry_at_uses_rain_end_once_dry() -> None:
    state = RainState(dry_hours=3.0)
    state.observe(raw_wet=True, now=NOW)
    state.observe(raw_wet=True, now=_min(11))  # confirmed wet
    state.observe(raw_wet=False, now=_min(20))  # wet→dry, stamps rain_ended_at
    assert state.is_wet is False
    assert state.dry_at == _min(20) + timedelta(hours=3)


def test_record_dry_transition_changed_flag() -> None:
    state = RainState()
    assert state._record_dry_transition(NOW) is True  # noqa: SLF001
    assert state.rain_ended_at == NOW
    assert (
        state._record_dry_transition(NOW) is False
    )  # same → no change  # noqa: SLF001


def _min(m: int) -> datetime:
    return NOW + timedelta(minutes=m)


def test_observe_dry_first_reading_no_event() -> None:
    state = RainState()
    assert state.observe(raw_wet=False, now=NOW) is RainEvent.NONE
    assert state.rain_ended_at is None
    assert state.is_wet is False


def test_observe_onset_does_not_confirm_within_dwell() -> None:
    state = RainState()
    assert state.observe(raw_wet=True, now=NOW) is RainEvent.NONE  # arms only
    assert state.is_wet is False
    assert state._wet_since == NOW  # noqa: SLF001
    assert state.observe(raw_wet=True, now=_min(5)) is RainEvent.NONE  # 5 < dwell
    assert state.is_wet is False


def test_observe_few_drops_under_dwell_never_confirm() -> None:
    state = RainState()
    state.observe(raw_wet=True, now=NOW)
    state.observe(raw_wet=True, now=_min(3))
    assert state.observe(raw_wet=False, now=_min(4)) is RainEvent.NONE  # cleared
    assert state.is_wet is False
    assert state.rain_ended_at is None  # never confirmed → no rain end
    assert state._wet_since is None  # noqa: SLF001


def test_observe_sustained_rain_confirms_after_dwell() -> None:
    state = RainState()
    state.observe(raw_wet=True, now=NOW)
    assert state.observe(raw_wet=True, now=_min(11)) is RainEvent.RAIN_CONFIRMED
    assert state.is_wet is True
    assert state.rain_ended_at is None  # confirming wet does not stamp end


def test_observe_confirmed_then_dry_records_rain_end() -> None:
    state = RainState()
    state.observe(raw_wet=True, now=NOW)
    state.observe(raw_wet=True, now=_min(11))  # confirmed wet
    assert state.observe(raw_wet=False, now=_min(20)) is RainEvent.RAIN_ENDED
    assert state.rain_ended_at == _min(20)
    assert state.is_wet is False


def test_observe_confirms_at_dwell_boundary() -> None:
    # WET_DWELL = 9 min with `>=`: 3rd 5-min tick (~10 min) reliably confirms,
    # exactly-9 confirms, just-under does not.
    state = RainState()
    state.observe(raw_wet=True, now=NOW)  # wet_since = NOW
    assert state.observe(raw_wet=True, now=_min(8)) is RainEvent.NONE  # 8 < 9
    assert state.observe(raw_wet=True, now=_min(9)) is RainEvent.RAIN_CONFIRMED  # >= 9


def test_observe_staying_confirmed_emits_still_raining() -> None:
    state = RainState()
    state._is_wet = True  # noqa: SLF001
    state._wet_since = NOW  # noqa: SLF001
    # Still raining past dwell: no edge, but last_wet_at (dry_at) advances, so it
    # is an observable event that refreshes the live dry_at sensor.
    assert state.observe(raw_wet=True, now=_min(30)) is RainEvent.STILL_RAINING


def test_transient_fields_not_serialized() -> None:
    state = RainState()
    state._is_wet = True  # noqa: SLF001
    state._wet_since = NOW  # noqa: SLF001
    state._last_wet_at = NOW  # noqa: SLF001
    dumped = state.to_dict()
    assert "is_wet" not in dumped
    assert "wet_since" not in dumped
    assert "last_wet_at" not in dumped
    restored = RainState.from_dict({"is_wet": True, "wet_since": NOW.isoformat()})
    assert restored.is_wet is False  # ignored on load
    assert restored._wet_since is None  # noqa: SLF001


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
    assert restored.dry_hours == RainState._DEFAULT_DRY_HOURS  # noqa: SLF001


# --- RainService ---


def _service() -> tuple[RainService, MagicMock]:
    repo = MagicMock()
    repo.state = RainState()
    repo.persist = AsyncMock()
    repo.save_if_changed = MagicMock()
    return RainService(repo), repo


def test_first_dry_reading_no_transition() -> None:
    service, repo = _service()

    service.observe(raw_wet=False, now=NOW)

    assert repo.state.rain_ended_at is None
    repo.save_if_changed.assert_not_called()


def test_few_drops_never_notify_or_persist() -> None:
    service, repo = _service()
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.observe(raw_wet=True, now=NOW)  # arms dwell, not confirmed
    service.observe(raw_wet=False, now=_min(3))  # cleared before dwell

    assert service.currently_wet is False
    assert notified == []  # never confirmed → sensor never flickered
    repo.save_if_changed.assert_not_called()


def test_confirmed_rain_then_dry_records_transition() -> None:
    service, repo = _service()
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.observe(raw_wet=True, now=NOW)  # arms dwell (NONE)
    service.observe(raw_wet=True, now=_min(11))  # confirm → notify (transient)
    service.observe(raw_wet=False, now=_min(20))  # rain end → persist + notify

    assert repo.state.rain_ended_at == _min(20)
    assert notified == [1, 1]  # confirm + end both observable
    # Only the rain-end persists — confirming wet is transient (no Store write).
    assert repo.save_if_changed.call_count == 1


def test_still_raining_notifies_without_persist() -> None:
    service, repo = _service()
    notified: list[int] = []
    service.add_listener(lambda: notified.append(1))

    service.observe(raw_wet=True, now=NOW)  # arm (NONE)
    service.observe(raw_wet=True, now=_min(11))  # confirm → notify
    service.observe(raw_wet=True, now=_min(13))  # still raining → notify, no persist

    assert notified == [1, 1]
    repo.save_if_changed.assert_not_called()  # transient — live dry_at only
    assert service.dry_at == _min(13) + timedelta(
        hours=RainState._DEFAULT_DRY_HOURS  # noqa: SLF001
    )


async def test_set_dry_hours_persists() -> None:
    service, repo = _service()

    await service.set_dry_hours(6.0)

    assert repo.state.dry_hours == 6.0
    repo.persist.assert_awaited_once()


def test_dry_at_reflects_state_after_transition() -> None:
    service, repo = _service()
    repo.state.dry_hours = 4.5

    service.observe(raw_wet=True, now=NOW)
    service.observe(raw_wet=True, now=_min(11))  # confirmed wet
    service.observe(raw_wet=False, now=_min(20))  # rain ended

    assert service.dry_at == _min(20) + timedelta(hours=4, minutes=30)
