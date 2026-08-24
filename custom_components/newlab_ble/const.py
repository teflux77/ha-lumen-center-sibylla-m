"""Constants for the Newlab Go BLE light integration.

Protocol reverse-engineered 2026-08-24 from real Bluetooth HCI snoop
captures of the vendor "Newlab Go" iOS/Android app controlling a 48V CCT
(warm/cool white) LED driver. See the project notes for the full RE
writeup (captures, decode process, live validation results).
"""
from __future__ import annotations

DOMAIN = "newlab_ble"

# The device advertises with a local name starting with this prefix.
# Confirmed via nRF Connect scan and HCI capture against the physical unit.
BLE_LOCAL_NAME_PREFIX = "Newlab"

# --- GATT characteristics -------------------------------------------------
# Base UUID is Nordic's stock "LED Button Service" tutorial UUID
# (785FEABCD123-1523-EFDE-1212) from the nRF5 SDK "Blinky" example -
# Newlab's firmware reuses it verbatim (never renamed) and bolts on 3
# custom characteristics after it.
CHAR_BUTTON_STATE = "00001524-1212-efde-1523-785feabcd123"  # unused - inert demo leftover, no effect
CHAR_BRIGHTNESS_CCT = "00001525-1212-efde-1523-785feabcd123"  # see PAYLOAD note below
CHAR_POWER = "00001526-1212-efde-1523-785feabcd123"  # dedicated on/off, independent of brightness/CCT
CHAR_AUTH = "00001527-1212-efde-1523-785feabcd123"  # keepalive/auth token, required before real writes
CHAR_UNKNOWN_1528 = "00001528-1212-efde-1523-785feabcd123"  # never observed in use in any capture

# CHAR_BRIGHTNESS_CCT payload is 4 bytes carrying TWO INDEPENDENT fields,
# not one 16-bit value:
#   byte 0 = CCT,        0-255, 0 = full warm, 255 = full cool/white
#   byte 1 = brightness, 0-255, 0 = off (via this char), 255 = full
#   bytes 2-3 = always 0x00 0x00 in every capture
#
# Confirmed by comparing two real captures: a brightness-only drag left the
# CCT byte pinned constant the whole time, and a CCT-only drag left the
# brightness byte pinned constant the whole time (including a clean view of
# an accidental brief brightness touch, isolated from the CCT changes
# happening at the same time).

# The peripheral requires this exact 5-byte token written to CHAR_AUTH
# immediately before every real command, or it silently disconnects
# ~2 seconds later. This is a hardcoded, cleartext, unauthenticated
# "secret" sent over an unbonded GATT link - effectively no real access
# control, but that's the device's own design, not something this
# integration can improve on.
AUTH_TOKEN = bytes([0x01]) + b"1234"

# CHAR_POWER payloads - a dedicated on/off command, NOT the same as setting
# brightness to 0 on CHAR_BRIGHTNESS_CCT. Confirmed from the tail of a real
# capture (the app's OFF button wrote this exact payload) and from live
# testing: turning on this way resets the driver to its own hardware
# default rather than restoring the last brightness/CCT, so this
# integration re-applies the last known brightness/CCT right after
# powering on (see client.py).
POWER_ON_PAYLOAD = bytes([0x01, 0x01, 0x00, 0x00])
POWER_OFF_PAYLOAD = bytes([0x00, 0x00, 0x00, 0x00])

# Color temperature range in Kelvin. Confirmed from the official Lumen
# Center Italia product page for this exact fixture (brand/model identified
# 2026-08-24): "Sibylla M", https://lumencenteritalia.com/en/prodotto/sibylla-s-m-l/
# - "LED Dinamic White Control DWC 2700/6300k CRI>90", 30W (M size), Bluetooth
# control via Android/iOS app. Matches everything observed during RE.
MIN_COLOR_TEMP_KELVIN = 2700
MAX_COLOR_TEMP_KELVIN = 6300

# establish_connection() (bleak-retry-connector) manages its own internal
# connect timeout/retry backoff - this only bounds how many attempts it
# makes before giving up.
MAX_CONNECT_ATTEMPTS = 4
