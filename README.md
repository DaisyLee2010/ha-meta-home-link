# Meta Home Link — Home Assistant as a Muse gadget

Expose **only the Home Assistant devices you choose** to Meta's
[Muse](https://gadgets.muse.ai), each under a friendly name Muse
will use (for example, `light.kitchen_main` as "Kitchen light").
Nothing not picked is visible to Muse.

Community project, not affiliated with Meta. Read the Gadget SDK
Terms at [gadgets.muse.ai/settings/sdk-tokens](https://gadgets.muse.ai/settings/sdk-tokens)
before pairing. See [NOTICE](NOTICE) for SDK attribution.

## How it fits together

- **Muse Gadget add-on** — pairs this host with the Muse app over
  Bluetooth LE and holds the encrypted gadget link, chat and voice.
- **Meta Home Link integration** (this repo, via HACS) — imports
  the add-on's pairing automatically, lets you pick exposed
  devices, and executes Muse's commands in Home Assistant.

Without the add-on the integration still works: you pair once with
the SDK's Linux client and paste the credentials — see
[Manual pairing](docs/pairing-manual.md).

## Requirements

- Home Assistant 2024.6 or newer.
- A Muse account and a Gadget SDK token (`mgst_…`) from
  [gadgets.muse.ai/settings/sdk-tokens](https://gadgets.muse.ai/settings/sdk-tokens).
- For the add-on path: a host with Bluetooth LE that can advertise
  (most Pi / HA Yellow / x86 hosts). Otherwise use manual pairing.

## Install

### 1. Muse Gadget add-on

1. Settings → Add-ons → Add-on Store → ⋮ → **Repositories** → add
   `https://github.com/DaisyLee2010/ha-meta-home-link`.
2. Install **Muse Gadget**, set an API token and your SDK token,
   start it, and pair it in the Muse app (Settings → Devices →
   Developer mode → Add Device → `MuseGadgetXXXXXX`).
   Details: [muse-gadget/DOCS.md](muse-gadget/DOCS.md).

### 2. Meta Home Link via HACS

1. HACS → ⋮ → **Custom repositories** →
   `https://github.com/DaisyLee2010/ha-meta-home-link`, category
   **Integration** → Add.
2. Download **Meta Home Link** (latest release), restart Home
   Assistant.
3. Settings → Devices & services → **Add integration** → Meta
   Home Link.

No HACS? Download `meta_home_link.zip` from
[Releases](https://github.com/DaisyLee2010/ha-meta-home-link/releases)
into `config/custom_components/meta_home_link/` and restart.

## Set up

1. **Link** — name the home, enter the SDK token, choose the
   "Muse Gadget add-on" link owner, and enter the same API token
   as in the add-on. A paired add-on's credentials are imported
   here automatically (the manual paste step is skipped).
2. **Devices exposed to Muse** — pick exactly what Muse may see.
3. **Friendly names** — optionally rename each device for Muse.

Later, **Configure** re-opens steps 2–3 without re-pairing.
Status is under `sensor.<name>_muse_link_status`,
`sensor.<name>_devices_exposed_to_muse`, and
`binary_sensor.<name>_muse_connected`. The
`meta_home_link.send_message` service sends a chat turn through
the add-on and fires a `meta_home_link_chat_reply` event.

## Supported devices

| Domain | Muse can do |
|---|---|
| `light` | on/off/toggle, brightness |
| `switch`, `fan`, `humidifier`, `vacuum`, `media_player` | on/off (fan: speed %; media: volume %, play/pause/stop/next/previous) |
| `cover` | set position 0–100 |
| `climate` | set temperature (optional HVAC mode) |
| `lock` | lock / unlock |
| `scene`, `script` | activate |
| `button`, `input_button` | press |
| `sensor`, `binary_sensor` | read-only: state only |

Registered commands: `device.guide`, `device.health`,
`home.list_devices`, `home.get_state`, `home.turn_on`,
`home.turn_off`, `home.toggle`, `home.set_brightness`,
`home.set_temperature`, `home.set_cover_position`,
`home.set_fan_percentage`, `home.set_volume`, `home.media_control`,
`home.lock`, `home.unlock`, `home.press`, `home.activate`, plus
`gadget.show_text` / `gadget.get_display` on the add-on.

## Security & limits

- Allow-list only: unpicked devices are invisible to Muse, and
  only a small set of state attributes (brightness, temperature,
  volume…) is ever sent — never identifiers or credentials.
- `home.unlock` works on any lock you expose; if that is too much,
  do not expose locks. Pairing tokens are stored like other
  integration tokens — treat backups accordingly.
- One pairing can be live on only one host at a time. State
  changes are not pushed; Muse asks when it needs them.
- The wire protocol is unit-tested; it has not yet been tested
  against a live Muse VM in CI.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install cryptography aiohttp pytest
.venv/bin/python -m pytest tests -q
```

The protocol/exposure code has no Home Assistant imports and is
tested without HA installed. GitHub Actions run the tests plus
hassfest and HACS validation.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
