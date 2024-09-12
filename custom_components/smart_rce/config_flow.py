"""Config flow to configure the Uptime integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class SmartRceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Smart RCE."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Smart RCE",
                data={},
            )

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
