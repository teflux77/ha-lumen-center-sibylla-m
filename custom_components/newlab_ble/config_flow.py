"""Config flow for the Newlab Go BLE light integration.

Supports both automatic Bluetooth discovery (matched by the manifest's
"bluetooth" matcher against the device's advertised local name) and a
manual fallback that lists any currently-advertising Newlab-named devices.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult

from .const import BLE_LOCAL_NAME_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)


class NewlabConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and manual setup of a Newlab Go BLE light."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a Bluetooth discovery matched by the manifest's bluetooth matcher."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or BLE_LOCAL_NAME_PREFIX
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to confirm adding the discovered light."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_name or BLE_LOCAL_NAME_PREFIX,
                data={"address": self._discovered_address},
            )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._discovered_name or BLE_LOCAL_NAME_PREFIX},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual setup: pick from any Newlab-named devices currently advertising."""
        if user_input is not None:
            address = user_input["address"]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_devices[address],
                data={"address": address},
            )

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass):
            address = discovery_info.address
            if address in current_addresses:
                continue
            name = discovery_info.name or ""
            if not name.startswith(BLE_LOCAL_NAME_PREFIX):
                continue
            self._discovered_devices[address] = name

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required("address"): vol.In(self._discovered_devices)}
            ),
        )
