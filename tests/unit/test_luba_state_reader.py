"""Unit tests for LubaStateReader (hass reads, legacy-parity defaults)."""

from unittest.mock import MagicMock

from custom_components.smart_rce.garden.infrastructure.luba_state_reader import (
    LubaStateReader,
)


def _hass(states: dict[str, str | None]) -> MagicMock:
    hass = MagicMock()

    def _get(entity_id: str) -> MagicMock | None:
        value = states.get(entity_id)
        return MagicMock(state=value) if value is not None else None

    hass.states.get.side_effect = _get
    return hass


def test_reads_battery_and_progress_as_int() -> None:
    reader = LubaStateReader(
        _hass(
            {
                LubaStateReader._BATTERY: "76",
                LubaStateReader._PROGRESS: "9",
            }
        )
    )

    assert reader.read_battery() == 76
    assert reader.read_progress() == 9


def test_unavailable_reads_default_to_zero() -> None:
    reader = LubaStateReader(_hass({LubaStateReader._BATTERY: "unavailable"}))

    assert reader.read_battery() == 0  # legacy Jinja `int(0)` parity
    assert reader.read_progress() == 0  # entity missing entirely


def test_at_dock_via_docked_or_charging() -> None:
    docked = _hass({LubaStateReader._LAWN_MOWER: "docked"})
    charging = _hass(
        {LubaStateReader._LAWN_MOWER: "paused", LubaStateReader._CHARGING: "on"}
    )
    mowing = _hass(
        {LubaStateReader._LAWN_MOWER: "mowing", LubaStateReader._CHARGING: "off"}
    )

    assert LubaStateReader(docked).read_at_dock() is True
    assert LubaStateReader(charging).read_at_dock() is True
    assert LubaStateReader(mowing).read_at_dock() is False
