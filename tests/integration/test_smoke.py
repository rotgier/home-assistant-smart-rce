"""Smoke test — weryfikuje że integration test infrastructure działa.

Najprostszy możliwy test: init_integration podnosi smart_rce config_entry
do stanu LOADED bez błędów. Jeśli ten test pada, infra wymaga fixów
(mock RCE, PV forecast, weather, etc.).
"""

from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import init_integration


async def test_setup_entry_loads(
    hass: HomeAssistant,
    mock_rce_api: AsyncMock,
    set_smart_rce_inputs,
) -> None:
    """Smart_rce setup_entry → LOADED state."""
    set_smart_rce_inputs()
    entry = await init_integration(hass)
    assert entry.state is ConfigEntryState.LOADED
