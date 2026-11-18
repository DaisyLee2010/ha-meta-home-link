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

## Pairing in detail — read this first

This is the step most people get stuck on, so it is spelled out in
full. Two ideas explain everything below:

- A Muse gadget does not log in with your SDK token. During a
  one-time **BLE pairing** with the Muse phone app, the app gives
  the gadget its own **device credentials** (`access_token` +
  `refresh_token`). Those credentials are what actually connect.
- Home Assistant usually cannot run that BLE handshake from a
  browser (it may have no Bluetooth, and the pairing window is a
  phone-driven flow). So you pair **once** with the Gadget SDK's
  Linux client on a machine with Bluetooth LE, then copy the two
  small state files it writes into this integration's config flow.
  After that, the SDK client is not needed and must be stopped
  (see step 5 — only one host may use a pairing at a time).

### What you need for the one-time pairing

- A Linux machine with Bluetooth LE — a Raspberry Pi 3B+/4/5 or
  Zero 2 W running Raspberry Pi OS is ideal. It only has to live
  long enough to pair; it is not part of the finished setup.
  Check Bluetooth can advertise: `bluetoothctl show` should list
  `Roles: peripheral`.
- That machine on your network (wired is fine), with an account
  that can use `sudo`.
- An SDK token (`mgst_…`) from
  [gadgets.muse.ai/settings/sdk-tokens](https://gadgets.muse.ai/settings/sdk-tokens).
- The Muse app on your phone.

### Step 1 — install the SDK client on the pairing machine

On the pairing machine (from the SDK's own
[Linux README](https://github.com/facebookincubator/muse-gadget-sdk/blob/main/linux/README.md)):

```sh
curl -fsSL https://raw.githubusercontent.com/facebookincubator/muse-gadget-sdk/main/linux/install.sh -o install.sh
less install.sh     # read it first
bash install.sh --sdk-token mgst_…    # paste your real token
```

The installer puts state in `/var/lib/musegadget` (owned by root)
and opens a 10-minute pairing window. If the window closes later,
reopen it any time with:

```sh
sudo musegadget pair
```

### Step 2 — pair it in the Muse app

1. Muse app → **Settings → Devices → Developer mode** on.
2. **Settings → Devices → Add Device** (the **+**). Pick the
   `MuseGadgetXXXXXX` name the installer printed.
3. Accept the community-device warning (community pairing cannot
   prove a device is genuine — pair on a network you trust).
4. When asked for Wi-Fi, pick the network shown; the machine is
   already online, so no password is needed.

Confirm it worked on the pairing machine:

```sh
sudo musegadget info
# node id:   homelink-XXXXXX
# BLE name:  MuseGadgetXXXXXX
# paired:    yes
```

### Step 3 — find the two files you need

Pairing writes its record under `/var/lib/musegadget`. You need
the contents of exactly two files:

| File | What it is | Required here? |
|---|---|---|
| `pairing.json` | The device credentials the app issued | Yes |
| `identity.json` | `{"mac": "…"}` — the identity those credentials were issued for | Strongly recommended |

Print them (note `sudo` — the folder is root-only):

```sh
sudo cat /var/lib/musegadget/pairing.json
sudo cat /var/lib/musegadget/identity.json
```

A real `pairing.json` looks like this (values shortened — do not
edit yours, paste it whole):

```json
{
  "access_token": "…",
  "refresh_token": "…",
  "token_type": "device",
  "username": "…",
  "api_url": "https://api.muse.ai",
  "api_url_v2": "https://api.muse.ai",
  "noise_host": "hatch.metaaivm.com",
  "access_token_saved_at": 1760000000,
  "sdk_token_hint": "mgst_…",
  "node_id": "homelink-1a2b3c"
}
```

Why `identity.json` matters: the tokens are issued **for the node
id derived from that file's MAC** (`homelink-` + its last six hex
digits, or the `node_id` stored in `pairing.json`). If Home
Assistant invented a fresh identity, registration would present a
node id the tokens were never issued for. Pasting `identity.json`
makes Home Assistant present the same identity, so the pairing
stays valid. This is the same trick the SDK documents for
Bluetooth-less boards in its
[Raspberry Pi 2 notes](https://github.com/facebookincubator/muse-gadget-sdk/blob/main/linux/docs/raspberry-pi-2-headless.md).

### Step 4 — paste both files into Home Assistant

In this integration's setup (step 2, “Pairing credentials”):

- **Pairing credentials (JSON)** ← paste the *entire contents* of
  `pairing.json`, braces and all. You do not need to trim it down
  to `access_token`/`refresh_token`; extra keys are ignored.
- **Identity (identity.json contents)** ← paste the entire
  contents of `identity.json` (`{"mac": "aa:bb:cc:dd:ee:ff"}`).
  You may leave it blank, but then a fresh identity is generated
  and a pairing made for a different node id may be refused at
  registration (see Troubleshooting).

Both are stored only in this config entry on your Home Assistant
host. Treat them like passwords: `access_token`/`refresh_token`
give Muse-level access to everything you expose, so never put
them in a GitHub issue, a forum post, or a screenshot, and never
commit a real `pairing.json` anywhere.

### Step 5 — stop the pairing machine using the same pairing

Only **one** host may hold a pairing at a time. If the SDK
client's service keeps running, two live sessions fight over the
same device and Home Assistant will flap between connected and
reconnecting. Once Home Assistant shows
`binary_sensor.<name>_muse_connected` on:

```sh
# on the pairing machine
sudo systemctl disable --now musegadget
sudo musegadget unpair   # deletes only its local pairing.json; Muse is not told
```

Keep that machine's install (and a copy of the two files) somewhere
safe: if you ever need to pair again, pairing needs Bluetooth
again, and re-pairing replaces the credentials.

### Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `credentials_incomplete` in step 2 | The pasted text is not the full `pairing.json`, or it is missing `access_token` or `refresh_token`. Re-copy the whole file with `sudo cat`. |
| `identity_invalid` | The identity box is not the full `identity.json`, or its `mac` is not six hex byte pairs. Paste the file whole, or clear the box. |
| Link status stuck on `reconnecting` / `no_vm` | Usually the pairing machine's `musegadget` service is still running with the same pairing — stop it (step 5). Otherwise run `sudo musegadget doctor` there to check the credentials ever verified. |
| `auth_failed` | The device tokens were revoked or the SDK token they were paired with was revoked. Pair again (`sudo musegadget pair`), then paste the fresh files. |
| Registration refused | Identity mismatch: credentials pasted without `identity.json`, or from a different gadget than the identity. Paste both files from the **same** `/var/lib/musegadget` folder. |
| Connecting from a Pi with no Bluetooth at all | Fully supported: pairing never touches Home Assistant's Bluetooth. Pair on any BLE machine as above, then stop it (step 5). |

The BLE pairing-session crypto (protocol v5) is included in
`pairing.py` and unit-tested, ready for a future BLE GATT
transport; this integration does not advertise a BLE peripheral
itself.

## Install (HACS + add-on)

Two pieces, installed in this order: the **Muse Gadget add-on**
(Home Assistant add-on store — this makes the host a Muse gadget
and pairs it) and the **Meta Home Link integration** (HACS — this
imports the add-on's pairing and exposes your devices). The
integration also works without the add-on; then pairing is the
manual flow further down ("Pairing in detail").

### Step 1 — Muse Gadget add-on (add-on store, not HACS)

1. Settings → Add-ons → Add-on Store → ⋮ (top right) →
   **Repositories**.
2. Add `https://github.com/DaisyLee2010/ha-meta-home-link` and
   close the dialog; **Muse Gadget** appears in the store.
3. Open **Muse Gadget** → Install → set an **API token** (any
   long random string) and your Gadget SDK token under
   Configuration → Start. Open its web UI: it shows the
   `MuseGadgetXXXXXX` name and pairing state. Details:
   [DOCS.md](muse-gadget/DOCS.md).
4. Pair it in the Muse app (Settings → Devices → Developer mode
   on → Add Device → pick that name). The add-on page flips to
   Paired and writes its pairing to `/share/muse-gadget/`.

### Step 2 — Meta Home Link integration (HACS)

1. Open HACS → ⋮ (top right) → **Custom repositories**.
2. Repository: `https://github.com/DaisyLee2010/ha-meta-home-link`,
   category **Integration** → Add.
3. Find **Meta Home Link** in HACS → **Download** (pick the latest
   release, v1.1.0 or newer) → restart Home Assistant when HACS
   asks.
4. Settings → Devices & services → **Add integration** →
   **Meta Home Link**. With the add-on paired, the pairing is
   **imported automatically** — no pasted JSON: choose the
   "Muse Gadget add-on" link owner, enter the same API token as
   in the add-on, then pick your exposed devices and friendly
   names (see Set up below).

HACS installs from the release zip (`meta_home_link.zip`, attached
to each GitHub release), so updates arrive through HACS like any
other integration. Adding this repo to HACS as a custom repository
is the install path until/unless it is accepted into the HACS
default store — the steps above work today either way.

### Release zip (no HACS)

1. Download `meta_home_link.zip` from this repository's
   **Releases** page.
2. Create `config/custom_components/meta_home_link/` on your
   Home Assistant host and unzip the download into that folder
   (the zip holds `manifest.json` at its root, ready to drop in).
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
| 1. Link | A name for this home/link, your SDK token (`mgst_…`), who owns the link ("Muse Gadget add-on" if you installed it, with its API URL/token), |
| 2. Pairing credentials | Skipped when the add-on has paired (imported from `/share/muse-gadget/`). Otherwise paste the whole `pairing.json`, and below it the whole `identity.json`, from the pairing machine's `/var/lib/musegadget/` — see Pairing in detail above. |
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

- Without the add-on, this integration imports pairing
  credentials as JSON and does not advertise BLE for pairing
  itself (see Pairing above). The Muse Gadget add-on does
  advertise BLE and pairs in place.
- Muse asks for states when it needs them (`home.get_state` /
  `home.list_devices`); state changes are not pushed to Muse.
- One link per config entry. Add the integration again for a
  second home/VM; give each entry its own pairing (a pairing can
  only be live on one host at a time).
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
