"""Pure helpers for the config flow (no Home Assistant imports)."""
from __future__ import annotations

import json

from .const import DEFAULT_API_ROOT, DEFAULT_NOISE_HOST


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
    return pairing, None


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
