"""BLE client for the Newlab Go 48V CCT light driver.

Encapsulates everything reverse-engineered from real HCI snoop captures of
the vendor app (see the project's RE notes for the full writeup):

  - CHAR_AUTH must receive a constant 5-byte token immediately before every
    real write, or the peripheral silently disconnects after ~2 seconds.
  - CHAR_POWER is a dedicated on/off command, independent of brightness and
    CCT. Turning on does NOT restore the last brightness/CCT - the driver
    resets to its own hardware default (confirmed by live testing against
    the physical unit). This class compensates by re-applying the last
    known brightness/CCT immediately after a power-on, so Home Assistant
    behaves the way users expect (resumes where it left off) instead of
    always snapping to the device's own default.
  - CHAR_BRIGHTNESS_CCT carries two independent byte fields in one 4-byte
    write - see const.py for the exact layout.

State is tracked optimistically (assumed authoritative immediately after a
successful write, for instant UI feedback) AND reconciled periodically via
a real GATT Read (see async_refresh_state) - confirmed 2026-08-25 by direct
Read probing that CHAR_POWER and CHAR_BRIGHTNESS_CCT both return the
device's genuine live state, even though the vendor app itself never reads
them back (it just trusts its own locally cached last-sent values, which is
why no Read ever showed up in any capture of the app during RE).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

from .const import (
    AUTH_TOKEN,
    CHAR_AUTH,
    CHAR_BRIGHTNESS_CCT,
    CHAR_POWER,
    MAX_CONNECT_ATTEMPTS,
    POWER_OFF_PAYLOAD,
    POWER_ON_PAYLOAD,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class NewlabState:
    """Best-known state of the light - optimistic after a write, real after a refresh."""

    is_on: bool = False
    brightness: int = 255  # 0-255, Home Assistant's native brightness scale, 1:1 with the device byte
    cct: int = 0  # 0-255, 0 = full warm, 255 = full cool/white


class NewlabBLEError(Exception):
    """Raised when a command to the light fails after retries."""


class NewlabBLEClient:
    """Manages a persistent BLE connection to one Newlab Go light and speaks its protocol."""

    def __init__(self, ble_device: BLEDevice, address: str) -> None:
        self._ble_device = ble_device
        self.address = address
        self._client: BleakClient | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.state = NewlabState()
        self._disconnected_callbacks: list = []

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the BLEDevice reference as Home Assistant's Bluetooth manager re-resolves it."""
        self._ble_device = ble_device

    def register_disconnected_callback(self, callback) -> None:
        """Register a zero-arg callback fired when the connection drops unexpectedly."""
        self._disconnected_callbacks.append(callback)

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _handle_disconnect(self, client: BleakClient) -> None:
        _LOGGER.debug("Newlab light %s disconnected", self.address)
        self._client = None
        for callback in self._disconnected_callbacks:
            callback()

    async def _ensure_connected(self) -> BleakClient:
        """Connect if needed. Cheap no-op if a connection is already live."""
        if self.is_connected:
            return self._client  # type: ignore[return-value]

        async with self._connect_lock:
            # Re-check after acquiring the lock - another task may have
            # already reconnected while we were waiting.
            if self.is_connected:
                return self._client  # type: ignore[return-value]

            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._ble_device,
                    name=self.address,
                    disconnected_callback=self._handle_disconnect,
                    max_attempts=MAX_CONNECT_ATTEMPTS,
                )
            except BleakNotFoundError as err:
                raise NewlabBLEError(
                    f"Newlab light {self.address} not found - is it powered and "
                    "within Bluetooth range of the adapter?"
                ) from err
            except BleakOutOfConnectionSlotsError as err:
                raise NewlabBLEError(
                    "No BLE connection slots free on the adapter - too many concurrent "
                    "connections. Free one up (e.g. disconnect an unused device) and retry."
                ) from err
            except (BleakError, asyncio.TimeoutError) as err:
                raise NewlabBLEError(
                    f"Failed to connect to Newlab light {self.address}: {err}"
                ) from err

            self._client = client
            _LOGGER.debug("Connected to Newlab light %s", self.address)
            return client

    async def _send_auth(self, client: BleakClient) -> None:
        """Send the mandatory auth/keepalive token. Must precede every real command."""
        await client.write_gatt_char(CHAR_AUTH, AUTH_TOKEN, response=True)

    async def _write_with_auth(self, char_uuid: str, payload: bytes, description: str) -> None:
        """Connect if needed, send the auth token, then the real write.

        Serialized via a lock so concurrent Home Assistant service calls
        (e.g. a script rapidly toggling the light) can't interleave the
        auth-token+command pairing, which the firmware requires to stay
        atomic. Retries once on transient BLE errors, forcing a fresh
        connection on retry since a mid-write failure often leaves the
        underlying Bleak client in a bad state.
        """
        async with self._write_lock:
            last_err: Exception | None = None
            for attempt in (1, 2):
                try:
                    client = await self._ensure_connected()
                    await self._send_auth(client)
                    await client.write_gatt_char(char_uuid, payload, response=True)
                    return
                except (BleakError, asyncio.TimeoutError) as err:
                    last_err = err
                    _LOGGER.warning(
                        "Newlab light %s: %s failed (attempt %d/2): %s",
                        self.address,
                        description,
                        attempt,
                        err,
                    )
                    self._client = None  # force a fresh connection on retry
                    if attempt == 1:
                        await asyncio.sleep(0.5)
            raise NewlabBLEError(
                f"Failed to {description} on Newlab light {self.address} after retry: {last_err}"
            ) from last_err

    async def _write_brightness_cct(self) -> None:
        payload = bytes([self.state.cct, self.state.brightness, 0x00, 0x00])
        await self._write_with_auth(CHAR_BRIGHTNESS_CCT, payload, "set brightness/CCT")

    async def async_refresh_state(self) -> None:
        """Read the device's real current state via GATT Read and update self.state.

        Confirmed via direct GATT Read probing (2026-08-25): CHAR_POWER and
        CHAR_BRIGHTNESS_CCT both support a genuine read-back of the device's
        live state. Two real reads taken with only the brightness changed
        between them came back with only the brightness byte differing,
        byte-for-byte matching the change made - not a cached/static value.

        Raises NewlabBLEError on any BLE failure. Does not retry - callers
        (e.g. a periodic poll) can just skip a failed refresh and try again
        next cycle rather than treating one missed read as fatal.
        """
        async with self._write_lock:
            try:
                client = await self._ensure_connected()
                await self._send_auth(client)
                power_raw = await client.read_gatt_char(CHAR_POWER)
                cct_raw = await client.read_gatt_char(CHAR_BRIGHTNESS_CCT)
            except (BleakError, asyncio.TimeoutError) as err:
                raise NewlabBLEError(
                    f"Failed to read state from Newlab light {self.address}: {err}"
                ) from err

        if len(power_raw) < 2 or len(cct_raw) < 2:
            raise NewlabBLEError(
                f"Newlab light {self.address} returned an unexpectedly short state "
                f"read (power={power_raw.hex()}, brightness/cct={cct_raw.hex()})"
            )

        self.state.is_on = power_raw[0] == POWER_ON_PAYLOAD[0] and power_raw[1] == POWER_ON_PAYLOAD[1]
        self.state.cct = cct_raw[0]
        self.state.brightness = cct_raw[1]
        _LOGGER.debug(
            "Newlab light %s state refreshed: is_on=%s brightness=%d cct=%d",
            self.address,
            self.state.is_on,
            self.state.brightness,
            self.state.cct,
        )

    async def async_apply(
        self,
        *,
        turn_on: bool | None = None,
        brightness: int | None = None,
        cct_byte: int | None = None,
    ) -> None:
        """Apply a state change in as few BLE writes as possible.

        turn_on=True powers the light on (if not already on) and always
        re-applies the resulting brightness/CCT afterward, since the
        device's own power-on resets those internally. turn_on=False just
        powers off. brightness/cct_byte, if given, update the cached
        target values *before* any power-on re-apply happens, so a single
        combined write already carries the final desired state instead of
        flashing the old values first.
        """
        if brightness is not None:
            self.state.brightness = max(0, min(255, brightness))
        if cct_byte is not None:
            self.state.cct = max(0, min(255, cct_byte))

        if turn_on is False:
            await self._write_with_auth(CHAR_POWER, POWER_OFF_PAYLOAD, "turn off")
            self.state.is_on = False
            return

        needs_power_on = turn_on is True and not self.state.is_on
        if needs_power_on:
            await self._write_with_auth(CHAR_POWER, POWER_ON_PAYLOAD, "turn on")
            self.state.is_on = True

        if needs_power_on or brightness is not None or cct_byte is not None:
            await self._write_brightness_cct()

    async def async_disconnect(self) -> None:
        """Disconnect cleanly, e.g. when the config entry is unloaded."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except BleakError:
                pass
            finally:
                self._client = None
