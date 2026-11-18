# Meta Home Link — a Home Assistant integration for Muse

Expose **only the Home Assistant devices you choose** to Meta's
[Muse](https://gadgets.muse.ai), through the
[Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk)
gadget link — and give each one a **friendly name** Muse will use
(for example, expose `light.kitchen_main` as “Kitchen light”).

This is a community integration, not affiliated with or endorsed by
Meta. Muse and Home Link pairing use Meta's Gadget SDK terms; read
them at [gadgets.muse.ai](https://gadgets.muse.ai/settings/sdk-tokens)
before pairing. See [NOTICE](NOTICE) for SDK attribution.

## What it does

- Registers Home Assistant with Muse as a gadget
  (`link.register`, Noise_XX over WSS, protocol as in the Gadget SDK).
- You pick the exposed devices in the UI; **nothing else is visible
  to Muse** — no entities, states, or history leak beyond your list.
- Each exposed device may have a friendly name. Muse refers to
  devices by that name, so “Kitchen light” works even when the
  entity id is `light.kitchen_main_2`.
- Muse can list devices, read states, and control them:
  on/off, brightness, temperature, covers, fans, locks, media
  players, scenes, scripts, and buttons (see table below).
- Provides `sensor.<name>_muse_link_status`,
  `sensor.<name>_devices_exposed_to_muse`, and
  `binary_sensor.<name>_muse_connected` so you can see link health
  and automate on it.

## Requirements

- Home Assistant 2024.6 or newer.
- A Muse account and a Gadget SDK token (`mgst_…`) from
  [gadgets.muse.ai/settings/sdk-tokens](https://gadgets.muse.ai/settings/sdk-tokens).
- Pairing credentials for this link (next section). The integration
  uses only Home Assistant core dependencies (`aiohttp`,
  `cryptography`); there are no extra pip requirements.

## Pairing — read this first

A Muse gadget gets device credentials (`access_token` /
`refresh_token`) from the Muse phone app during BLE pairing
(Developer mode → Settings → Devices → add the `MuseGadget…`
device). Home Assistant hosts usually cannot complete that BLE
handshake from a browser config flow, so this integration takes the
paired credentials directly:

1. Pair this link as a gadget using the Gadget SDK's Linux client
   (or another SDK gadget build) with your SDK token, on a trusted
   network — community pairing cannot prove a device is genuine.
2. Copy the resulting pairing record (the SDK client keeps
   `access_token`, `refresh_token`, and optionally `api_url_v2` /
   `noise_host`).
3. Paste that JSON into step 2 of the config flow below. It is
   stored only in this config entry on your Home Assistant host.

The BLE pairing-session crypto (protocol v5) is included in
`pairing.py` and unit-tested, ready for a future BLE GATT transport;
v1 does not advertise a BLE peripheral itself.

## Install

### HACS (recommended, easiest)

1. In Home Assistant, open HACS → Integrations → ⋮ (top right) →
   **Custom repositories**.
2. Paste this repository's GitHub URL, choose category
   **Integration**, and add it.
3. Find **Meta Home Link** in HACS, install it, then restart
   Home Assistant.
4. Settings → Devices & services → **Add integration** →
   **Meta Home Link** (see Set up below).

HACS installs from the release zip (`meta_home_link.zip`), so
updates arrive through HACS like any other integration.

### Release zip (no HACS)

1. Download `meta_home_link.zip` from this repository's
   **Releases** page.
2. Unzip it so you have `config/custom_components/meta_home_link/`
   on your Home Assistant host.
3. Restart Home Assistant, then add the integration as above.

### Git clone (for tinkering)

```sh
cd <home-assistant-config>/custom_components
git clone https://github.com/DaisyLee2010/ha-meta-home-link.git meta-home-link-tmp
cp -r meta-home-link-tmp/custom_components/meta_home_link .
rm -rf meta-home-link-tmp
```

Restart Home Assistant afterwards.

## Set up

Settings → Devices & services → **Add integration** → **Meta Home Link**:

| Step | What to enter |
|---|---|
| 1. Link | A name for this home/link, and your SDK token (`mgst_…`). |
| 2. Pairing credentials | The pairing JSON from Pairing above. |
| 3. Devices exposed to Muse | Pick exactly which entities Muse may see and control. |
| 4. Friendly names | One box per picked device: keep its Home Assistant name, or type a friendlier one. Clearing a box falls back to the Home Assistant name. |

Later, **Configure** on the integration re-opens steps 3–4, so you
can add/remove devices or rename them any time without re-pairing.
Changes reload the link and re-register the new device list with
Muse.

## Supported devices

| Domain | Muse can do |
|---|---|
| `light` | on/off/toggle, brightness |
| `switch`, `fan`, `humidifier`, `vacuum`, `media_player` | on/off (fan: speed %, media: volume %, play/pause/stop/next/previous) |
| `cover` | set position 0–100 |
| `climate` | set temperature (optional HVAC mode) |
| `lock` | lock / unlock |
| `scene`, `script` | activate |
| `button`, `input_button` | press |
| `sensor`, `binary_sensor` | read-only: state only |

Commands registered with Muse: `device.guide`, `device.health`,
`home.list_devices`, `home.get_state`, `home.turn_on`,
`home.turn_off`, `home.toggle`, `home.set_brightness`,
`home.set_temperature`, `home.set_cover_position`,
`home.set_fan_percentage`, `home.set_volume`, `home.media_control`,
`home.lock`, `home.unlock`, `home.press`, `home.activate`.
The guide Muse reads lists your exposed devices by friendly name,
with current states, and tells it not to guess unlisted devices.

## Security notes

- Exposure is allow-list only: if it is not picked, Muse cannot
  see or control it. Review the list like you would for Alexa or
  Google Assistant exposure.
- `home.unlock` works on any lock you expose. If that is more than
  you want, do not expose locks — there is no per-device read-only
  switch yet.
- Pairing credentials are stored in the config entry (Home
  Assistant's `.storage`), like other integration tokens. Treat
  backups accordingly.
- Only a small set of state attributes is sent to Muse (for
  example brightness, temperature, volume); identifiers, addresses,
  and credentials are never included.

## Limitations

- v1 imports pairing credentials as JSON; it does not advertise
  BLE for pairing itself (see Pairing above).
- Muse asks for states when it needs them (`home.get_state` /
  `home.list_devices`); state changes are not pushed to Muse.
- One link per config entry. Add the integration again for a
  second home/VM — each entry gets its own gadget identity.
- Not yet tested against a live Muse VM in CI; the wire protocol
  (protobuf envelope, Noise XX, pairing crypto) is covered by unit
  tests derived from the SDK behaviour.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install cryptography aiohttp pytest
.venv/bin/python -m pytest tests -q
```

The protocol and exposure logic (`proto.py`, `noise.py`,
`pairing.py`, `exposure.py`, `config_helpers.py`) has no Home
Assistant imports and is fully unit-tested without HA installed.
GitHub Actions run the tests plus hassfest and HACS validation.

Releases and issues live on this repository's GitHub pages.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
