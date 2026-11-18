# Muse Gadget add-on

Turns this Home Assistant host into a **Muse gadget**: it pairs with
the Muse app over Bluetooth LE, holds the encrypted Noise gadget
link, and answers Muse's home-control commands. The **Meta Home
Link** integration then imports the pairing this add-on creates —
no separate Linux pairing machine, no pasted JSON.

Community add-on, not affiliated with Meta. Read the Gadget SDK
Terms at gadgets.muse.ai/settings/sdk-tokens before pairing.

## Install

1. Add this repository as a Home Assistant add-on store
   (Settings → Add-ons → ⋮ → Repositories), install **Muse Gadget**,
   and start it. The host needs Bluetooth LE that can advertise
   (the built-in radio on most Pi / HA Yellow / x86 hosts works;
   `bluetoothctl show` should list `Roles: peripheral`).
2. Set an **API token** in the add-on options (any long random
   string) and the same token's flow in the integration below.
3. Open the add-on's web UI (ingress): it shows the BLE name
   (`MuseGadgetXXXXXX`) and pairing state.

## Pair (one time)

1. Muse app → **Settings → Devices → Developer mode** on.
2. **Settings → Devices → Add Device** (the +), pick the
   `MuseGadgetXXXXXX` name from the add-on page, accept the
   community-device warning, and pick the Wi-Fi network offered
   (the host is already online; no password needed).
3. The add-on page flips to **Paired**. It writes `pairing.json` and
   `identity.json` to `/share/muse-gadget/` — that is the handoff.

Only one host may hold a pairing: do not run the SDK's Linux client
or another gadget with the same pairing at the same time.

## Connect the integration

Install **Meta Home Link** (HACS / this repo), then
Settings → Devices & services → **Add integration** → Meta Home
Link. When this add-on has paired, the flow detects
`/share/muse-gadget/` and imports the pairing automatically —
pick your exposed devices and friendly names as usual. The
integration pushes those devices to this add-on and executes
Muse's `home.*` commands; the add-on serves `device.guide` /
`device.health` locally and forwards the rest.

## What Muse can do

- `device.guide`, `device.health`, `home.list_devices`,
  `home.get_state`, `home.turn_on/off`, `home.toggle`,
  `home.set_brightness`, `home.set_temperature`,
  `home.set_cover_position`, `home.set_fan_percentage`,
  `home.set_volume`, `home.media_control`, `home.lock`,
  `home.unlock`, `home.press`, `home.activate` — over only the
  devices the integration exposes.
- `gadget.show_text` / `gadget.get_display` — put a short message
  on the add-on's display page.
- Chat & voice: the add-on page can send a chat turn, read history,
  and play Muse's voice reply; the integration's
  `meta_home_link.send_message` service does the same from
  automations.

## Options

| Option | What it is |
|---|---|
| Link name | Name shown to Muse for this home. |
| Gadget SDK token | `mgst_…` from gadgets.muse.ai/settings/sdk-tokens. |
| API token | Shared secret with the integration (`X-Gadget-Token`). |
| API port | Local API port (default 8099). Do not publish it. |
| Open pairing on start | Advertise until paired. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Add-on page says Bluetooth unavailable | Host has no BLE peripheral radio, BlueZ is stopped, or another add-on owns the adapter. Pairing import via the SDK Linux client + pasted JSON in the integration still works. |
| Integration says add-on not connected | Same API token in both places, add-on started, and the integration entry uses the add-on link owner. |
| Link flaps connected/reconnecting | Another host is using the same pairing — stop it. |
