# Manual pairing (without the Muse Gadget add-on)

Only needed if you are not using the add-on, which pairs in place.

## Why this exists

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
