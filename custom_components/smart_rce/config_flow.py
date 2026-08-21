"""Config flow for Smart RCE.

Setup itself needs no input. The options flow exists for one thing: TAURON
eLicznik credentials, which the `deposit` context uses for its daily fetch.
They live here rather than in a file so HA stores them encrypted in `.storage`
(ADR-025 #2) — and leaving them blank simply disables the fetch.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

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

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> SmartRceOptionsFlow:
        return SmartRceOptionsFlow()


class SmartRceOptionsFlow(OptionsFlow):
    """TAURON eLicznik credentials for the deposit context."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USERNAME, default=current.get(CONF_USERNAME, "")
                    ): str,
                    vol.Optional(
                        CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")
                    ): str,
                }
            ),
        )
