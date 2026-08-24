"""Light platform for the Newlab Go BLE CCT driver."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .client import NewlabBLEClient, NewlabBLEError
from .const import DOMAIN, MAX_COLOR_TEMP_KELVIN, MIN_COLOR_TEMP_KELVIN

_LOGGER = logging.getLogger(__name__)

# How often to reconcile Home Assistant's optimistic state against the
# device's real state via a GATT Read. Confirmed working 2026-08-25 (see
# client.py). Deliberately not aggressive - each refresh is a full BLE
# connect+read round trip, and this device only accepts one central
# connection at a time, so polling too often increases contention with the
# vendor app or anything else that might connect to it.
_STATE_REFRESH_INTERVAL = timedelta(seconds=60)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the single light entity for this config entry."""
    client: NewlabBLEClient = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NewlabLight(client, entry)])


def _kelvin_to_cct_byte(kelvin: int) -> int:
    """Map Home Assistant's color_temp_kelvin onto the device's 0-255 CCT byte (0=warm, 255=cool)."""
    span = MAX_COLOR_TEMP_KELVIN - MIN_COLOR_TEMP_KELVIN
    fraction = (kelvin - MIN_COLOR_TEMP_KELVIN) / span
    return round(max(0.0, min(1.0, fraction)) * 255)


def _cct_byte_to_kelvin(cct_byte: int) -> int:
    span = MAX_COLOR_TEMP_KELVIN - MIN_COLOR_TEMP_KELVIN
    fraction = max(0, min(255, cct_byte)) / 255
    return round(MIN_COLOR_TEMP_KELVIN + fraction * span)


class NewlabLight(LightEntity):
    """A Newlab Go BLE CCT light.

    State (on/off, brightness, color temperature) is tracked optimistically
    by the underlying NewlabBLEClient for instant UI feedback right after a
    command, and reconciled every _STATE_REFRESH_INTERVAL via a real GATT
    Read (confirmed working - see client.py) to catch drift from the light
    being controlled some other way (the vendor app, a physical power cut,
    etc.). A failed write marks the entity unavailable rather than silently
    pretending it succeeded; a single failed periodic refresh does not,
    since a BLE read failing while e.g. the vendor app holds the only
    available connection slot is expected and transient.
    """

    _attr_has_entity_name = True
    _attr_name = None  # use the device name as the entity's name
    _attr_should_poll = False
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN

    def __init__(self, client: NewlabBLEClient, entry: ConfigEntry) -> None:
        self._client = client
        self._attr_unique_id = entry.unique_id or client.address
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, client.address)},
            connections={(CONNECTION_BLUETOOTH, client.address)},
            name=entry.title,
            manufacturer="Newlab",
            model="Newlab Go BLE CCT driver",
        )
        self._attr_available = True
        self._unsub_refresh: Any = None
        client.register_disconnected_callback(self._handle_disconnected)

    def _handle_disconnected(self) -> None:
        """Fired by the client when the BLE connection drops unexpectedly."""
        self._attr_available = False
        if self.hass is not None:
            self.schedule_update_ha_state()

    @property
    def is_on(self) -> bool | None:
        return self._client.state.is_on

    @property
    def brightness(self) -> int | None:
        return self._client.state.brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        return _cct_byte_to_kelvin(self._client.state.cct)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            brightness = kwargs.get(ATTR_BRIGHTNESS)
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            cct_byte = _kelvin_to_cct_byte(kelvin) if kelvin is not None else None

            await self._client.async_apply(turn_on=True, brightness=brightness, cct_byte=cct_byte)
            self._attr_available = True
        except NewlabBLEError as err:
            self._attr_available = False
            raise HomeAssistantError(str(err)) from err
        finally:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._client.async_apply(turn_on=False)
            self._attr_available = True
        except NewlabBLEError as err:
            self._attr_available = False
            raise HomeAssistantError(str(err)) from err
        finally:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Keep the BLEDevice reference fresh, seed real state, and start periodic refresh."""
        ble_device = async_ble_device_from_address(self.hass, self._client.address, connectable=True)
        if ble_device is not None:
            self._client.set_ble_device(ble_device)

        try:
            await self._client.async_refresh_state()
        except NewlabBLEError as err:
            # Not fatal - fall back to NewlabState's defaults and let the
            # next periodic refresh (or the first command) sort it out.
            _LOGGER.debug(
                "Newlab light %s: initial state read failed, starting from defaults: %s",
                self._client.address,
                err,
            )

        self._unsub_refresh = async_track_time_interval(
            self.hass, self._async_periodic_refresh, _STATE_REFRESH_INTERVAL
        )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the periodic refresh timer when the entity is being removed."""
        if self._unsub_refresh is not None:
            self._unsub_refresh()
            self._unsub_refresh = None

    async def _async_periodic_refresh(self, now: Any) -> None:
        """Reconcile our optimistic state against the device's real state.

        Deliberately does not touch _attr_available on failure - a single
        missed poll (e.g. the vendor app currently holds the only BLE
        connection slot) is expected from time to time and shouldn't flap
        the entity's availability the way a failed *command* should.
        """
        try:
            await self._client.async_refresh_state()
        except NewlabBLEError as err:
            _LOGGER.debug(
                "Newlab light %s: periodic state refresh failed (will retry next interval): %s",
                self._client.address,
                err,
            )
            return
        self.async_write_ha_state()
