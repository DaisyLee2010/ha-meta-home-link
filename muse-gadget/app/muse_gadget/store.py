"""Add-on state: identity + pairing in /data, mirrored to /share.

The integration auto-imports ``pairing.json`` / ``identity.json`` /
``status.json`` from ``/share/muse-gadget/`` — that folder is the
pairing handoff. Tokens are written only there and in the add-on's
own ``/data`` (both owner-only on a Home Assistant host).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    mac: str
    paired_node_id: str | None = None

    @property
    def suffix(self) -> str:
        return self.mac.replace(":", "")[6:]

    @property
    def node_id(self) -> str:
        return self.paired_node_id or f"homelink-{self.suffix}"

    @property
    def device_id(self) -> str:
        return f"hatch-link:{self.mac}"

    @property
    def ble_name(self) -> str:
        return f"MuseGadget{self.suffix.upper()}"


def generate_identity() -> Identity:
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] & 0xFC) | 0x02  # locally administered, unicast
    return Identity(":".join(f"{b:02x}" for b in raw))


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class GadgetStore:
    """Owns identity/pairing files for the add-on process."""

    def __init__(self, data_dir: str = "/data", share_dir: str = "/share/muse-gadget") -> None:
        self.data_dir = Path(data_dir)
        self.share_dir = Path(share_dir)

    # ---------------------------------------------------------- identity
    def identity(self) -> Identity:
        stored = _read_json(self.data_dir / "identity.json")
        mac = str((stored or {}).get("mac") or "").lower()
        if len(mac.split(":")) == 6 and all(len(p) == 2 for p in mac.split(":")):
            pairing = self.pairing() or {}
            return Identity(mac, str(pairing.get("node_id") or "") or None)
        identity = generate_identity()
        _write_json(self.data_dir / "identity.json", {"mac": identity.mac})
        self.mirror_share()
        return identity

    # ----------------------------------------------------------- pairing
    def pairing(self) -> dict | None:
        pairing = _read_json(self.data_dir / "pairing.json")
        if pairing and pairing.get("access_token") and pairing.get("refresh_token"):
            return pairing
        return None

    def save_pairing(self, pairing: dict) -> None:
        _write_json(self.data_dir / "pairing.json", pairing)
        self.mirror_share(pairing=pairing)

    def clear_pairing(self) -> None:
        for base in (self.data_dir, self.share_dir):
            try:
                (base / "pairing.json").unlink()
            except OSError:
                pass
        self.write_status({"paired": False, "status": "not_paired"})

    # -------------------------------------------------------------- share
    def mirror_share(self, pairing: dict | None = None) -> None:
        """Publish the import files the integration reads from /share."""
        identity = _read_json(self.data_dir / "identity.json")
        if identity:
            _write_json(self.share_dir / "identity.json", identity)
        current = pairing if pairing is not None else self.pairing()
        if current:
            _write_json(self.share_dir / "pairing.json", current)

    def write_status(self, status: dict) -> dict:
        identity = _read_json(self.data_dir / "identity.json") or {}
        payload = {
            "paired": self.pairing() is not None,
            "ble_name": Identity(str(identity.get("mac") or "00:00:00:00:00:00")).ble_name
            if identity.get("mac")
            else None,
            "node_id": Identity(str(identity.get("mac") or "00:00:00:00:00:00")).node_id
            if identity.get("mac")
            else None,
            **status,
        }
        _write_json(self.share_dir / "status.json", payload)
        _write_json(self.data_dir / "status.json", payload)
        return payload

    def read_devices(self) -> dict:
        """Last device snapshot pushed by the integration (cached)."""
        return _read_json(self.data_dir / "devices.json") or {"devices": [], "states": {}}

    def save_devices(self, payload: dict) -> None:
        _write_json(self.data_dir / "devices.json", payload)
