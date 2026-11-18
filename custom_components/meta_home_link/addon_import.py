"""Import the pairing the Muse Gadget add-on publishes, plus payloads.

Pure Python (no Home Assistant imports) so the add-on handoff is
unit-testable alone. The add-on writes ``pairing.json``,
``identity.json`` and ``status.json`` into ``/share/muse-gadget/``;
``load_addon_share`` validates them with the same rules as the
paste-in config flow and never raises — a missing or half-written
share simply means "no add-on pairing yet".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config_helpers import parse_identity, parse_pairing_credentials
from .const import ADDON_SHARE_DIR


@dataclass(frozen=True)
class AddonImport:
    pairing: dict
    mac: str | None
    status: dict


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def load_addon_share(share_dir: str | Path = ADDON_SHARE_DIR) -> AddonImport | None:
    """Return the add-on's pairing import, or None when not usable yet."""
    base = Path(share_dir)
    pairing_text = _read(base / "pairing.json")
    if not pairing_text:
        return None
    pairing, error = parse_pairing_credentials(pairing_text)
    if error is not None or pairing is None:
        return None
    mac, identity_error = parse_identity(_read(base / "identity.json"))
    if identity_error is not None:
        mac = None  # identity is advisory; the pairing still imports
    status: dict = {}
    status_text = _read(base / "status.json")
    if status_text:
        try:
            parsed = json.loads(status_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            status = parsed
    return AddonImport(pairing=pairing, mac=mac, status=status)


def addon_share_available(share_dir: str | Path = ADDON_SHARE_DIR) -> bool:
    return load_addon_share(share_dir) is not None


def build_devices_payload(devices, states: dict, ha_version: str,
                          instance_name: str) -> dict:
    """The snapshot the integration pushes to the add-on's /api/devices."""
    return {
        "instance_name": instance_name,
        "ha_version": ha_version,
        "devices": [
            {
                "entity_id": d.entity_id,
                "name": d.friendly_name,
                "domain": d.domain,
                "read_only": d.read_only,
            }
            for d in devices
        ],
        "states": states,
    }
