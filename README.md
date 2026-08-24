# Newlab Go BLE Light — Home Assistant custom integration

Local control of a Newlab Go BLE-controlled 48V CCT (warm/cool white) LED driver, no cloud, no
app dependency. Confirmed fixture: **Lumen Center Italia "Sibylla M"**
(https://lumencenteritalia.com/en/prodotto/sibylla-s-m-l/), controlled via the "Newlab Go"
Android/iOS app. Protocol reverse-engineered from real Bluetooth HCI captures of the vendor app —
see the project's RE notes for the full writeup of how each value was derived.

## What this gives you

- A `light` entity with `ColorMode.COLOR_TEMP` — on/off, brightness, and color temperature (2700K–6300K
  by default, matching the official Sibylla M datasheet — see "Tuning the color temperature range"
  below).
- Automatic discovery via Home Assistant's Bluetooth integration (matches the device's advertised
  name, "Newlab*") — it should show up under Settings → Devices & Services → Discovered, or you can
  add it manually if discovery doesn't pick it up.
- Uses your existing onboard Bluetooth adapter — no new hardware required.

## Installation

### Via HACS (recommended)

1. HACS → three-dot menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/teflux77/ha-lumen-center-sibylla-m`, Category: **Integration** →
   **Add**.
3. Find "Newlab Go BLE Light" in HACS → **Download**.
4. Restart Home Assistant (Settings → System → Restart).
5. Continue at step 3 below (discovery).

Future updates: `git tag`+push a new version on the repo, bump `version` in `manifest.json` to match,
and HACS will offer the update in its normal update-check flow.

### Manual (without HACS)

1. Copy the `custom_components/newlab_ble/` folder from this package into your Home Assistant
   config directory, so you end up with `<config>/custom_components/newlab_ble/`. On HAOS, the
   easiest way is the **Samba share** or **Studio Code Server** / **File editor** add-on — copy the
   whole `newlab_ble` folder in as-is, don't flatten it.
2. Restart Home Assistant (Settings → System → Restart) so it picks up the new custom component.
3. Go to Settings → Devices & Services. If the light is powered on and in range of the adapter, it
   should appear under **Discovered** within a minute or two — click **Configure** and confirm.
   If it doesn't show up automatically, click **+ Add Integration**, search for "Newlab Go BLE
   Light", and pick it from the list of nearby advertising devices.

## Important operational notes

- **Only one BLE central can hold a connection to the light at a time.** If the Newlab Go phone app
  is connected (even just left open in the background) while Home Assistant tries to connect, one of
  them will get refused or dropped. Keep the phone app closed once Home Assistant is managing the
  light, or expect occasional "not found"/timeout errors when both are trying to use it.
- **State is optimistic between refreshes, and reconciled against the real device every 60s.**
  Right after a command, Home Assistant shows "what we just told the light to do" instantly, without
  waiting on a round trip. Separately, a background GATT Read every 60 seconds (see
  `_STATE_REFRESH_INTERVAL` in `light.py`) confirms the light's actual on/off, brightness, and CCT and
  corrects Home Assistant's display if it drifted — e.g. the light was controlled by the vendor app or
  power-cycled at the wall. A single missed refresh (for instance, the vendor app is currently holding
  the only available BLE connection slot) is not treated as an error — it just retries on the next
  cycle instead of flapping the entity to unavailable.
- **A hardcoded, cleartext "auth" token is required by the device itself** (not something this
  integration added) — see the RE notes for detail. This is a limitation of the light's own firmware,
  not a security feature; nothing to configure here, just worth knowing.

## Tuning the color temperature range

`custom_components/newlab_ble/const.py` hardcodes `MIN_COLOR_TEMP_KELVIN = 2700` and
`MAX_COLOR_TEMP_KELVIN = 6300`, taken directly from the official Lumen Center Italia product page
for the Sibylla M: "LED Dinamic White Control DWC 2700/6300k CRI>90" (30W for the M size). If you're
using this integration with a different fixture running the same Newlab Go protocol, edit those two
constants to match your own datasheet before restarting.

## Example dashboard

`dashboards/example_dashboard.yaml` has ready-to-paste Lovelace card examples — a `light` card and a
`tile` card, both of which natively render the on/off toggle, brightness slider, and color-temperature
slider for `ColorMode.COLOR_TEMP` entities out of the box (no custom card / frontend dependency
needed). Swap in your real `light.*` entity_id and drop it into a dashboard view's YAML, or use it as
a reference for the UI card editor.

## Known limitations / not yet implemented

- Characteristic `...1528...` (handle 0x0013) was never observed in use during RE and isn't wired up
  to anything — if some feature turns out to be missing (an effect/scene mode?), this is the
  remaining unknown to investigate.
- No background reconnect loop — if the connection drops, the next command triggers a fresh connect
  automatically, but the entity won't proactively come back to "available" until something tries to
  use it. Fine for a light you control from automations/dashboards regularly; a coordinator-based
  proactive reconnect could be added later if it becomes annoying in practice.
- `Blinky Button State` (handle 0x000b) is confirmed inert (a leftover from the Nordic SDK example
  firmware this was built on) and isn't exposed as anything.

## Files

```
hacs.json                 HACS repository metadata
LICENSE                    MIT
README.md
dashboards/
└── example_dashboard.yaml  copy-paste Lovelace light/tile card examples
custom_components/newlab_ble/
├── __init__.py        entry setup/unload, forwards to the light platform
├── manifest.json       domain, bluetooth discovery matcher, requirements
├── const.py             UUIDs, protocol constants, color temp range - reference for the RE findings
├── client.py            BLE connection + protocol implementation (bleak-retry-connector based)
├── config_flow.py       discovery + manual setup flow
├── light.py              the light entity itself
├── strings.json          config flow UI text
└── translations/en.json  (same, for HA's translation loader)
```
