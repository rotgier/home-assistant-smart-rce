"""Integration tests for Smart RCE — full hass setup, exercise HA boundary.

Wzorzec inspirowany `homeassistant/core` repo
`tests/components/accuweather/__init__.py` — pełny `hass` instance,
`MockConfigEntry` + `async_setup_entry`, mock'i dla HTTP boundary.
"""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant


async def init_integration(hass: HomeAssistant) -> MockConfigEntry:
    """Set up smart_rce integration in hass.

    Smart_rce config_flow nie wymaga danych (single_config_entry,
    `data={}`). Wzorzec analogiczny do accuweather init_integration.
    """
    entry = MockConfigEntry(
        domain="smart_rce",
        title="Smart RCE",
        data={},
        unique_id="smart_rce",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
