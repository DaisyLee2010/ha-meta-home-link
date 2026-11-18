"""Pure helpers for the config flow (no Home Assistant imports)."""
from __future__ import annotations

import json
import re

from .const import DEFAULT_API_ROOT, DEFAULT_NOISE_HOST

_MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_NODE_ID_RE = re.compile(r"[a-z]+-[0-9a-f]{6}")


def valid_sdk_token(token: str | None) -> bool:
    return bool(token) and token.startswith("mgst_") and len(token) > 8


def parse_pairing_credentials(text: str | None) -> tuple[dict | None, str | None]:
    """Parse the pasted pairing JSON into a pairing dict.

    Returns (pairing, None) on success or (None, error_key) where
    error_key is one of: ``credentials_required``, ``credentials_invalid``,
    ``credentials_incomplete``. Accepts either the raw credential keys
    (access_token, refresh_token, ...) or a whole pairing record.
    """
    if not text or not text.strip():
        return None, "credentials_required"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "credentials_invalid"
    if not isinstance(data, dict):
        return None, "credentials_invalid"
    access = str(data.get("access_token") or "").strip()
    refresh = str(data.get("refresh_token") or "").strip()
    if not access or not refresh:
        return None, "credentials_incomplete"
    pairing = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "device",
        "username": str(data.get("username") or ""),
        "api_url_v2": str(data.get("api_url_v2") or DEFAULT_API_ROOT),
        "noise_host": str(data.get("noise_host") or DEFAULT_NOISE_HOST),
    }
    # Newer SDK clients store the node id the tokens were issued for;
    # keep it so registration presents that id, not a fresh one.
    node_id = str(data.get("node_id") or "").strip()
    if _NODE_ID_RE.fullmatch(node_id):
        pairing["node_id"] = node_id
    return pairing, None


def parse_identity(text: str | None) -> tuple[str | None, str | None]:
    """Parse the SDK client's identity.json (``{"mac": "aa:bb:…"}``).

    Returns (mac, None), (None, None) when the field was left blank
    (a fresh identity will be generated), or (None, error_key).
    The pairing tokens were issued for the node id derived from this
    MAC, so importing it keeps the pairing valid on this host.
    """
    if not text or not text.strip():
        return None, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "identity_invalid"
    mac = str(data.get("mac") or "").strip().lower() if isinstance(data, dict) else ""
    if not _MAC_RE.fullmatch(mac):
        return None, "identity_invalid"
    return mac, None


def merge_friendly_names(
    exposed: list[str],
    submitted: dict[str, str] | None,
    ha_names: dict[str, str] | None = None,
) -> dict[str, str]:
    """Keep only non-blank aliases for still-exposed entities.

    A submitted name equal to the entity's HA name is dropped, so the
    stored config only holds names the user actually customised.
    """
    ha_names = ha_names or {}
    out: dict[str, str] = {}
    for entity_id in exposed:
        name = str((submitted or {}).get(entity_id) or "").strip()
        if name and name != (ha_names.get(entity_id) or "").strip():
            out[entity_id] = name
    return out
