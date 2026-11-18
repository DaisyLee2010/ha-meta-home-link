"""Meta Home Link: expose chosen Home Assistant devices to Muse.

The user picks which entities Muse may see (and optional friendly
names for them) in the config/options flow. The Muse link itself is
owned either by this integration (Noise_XX ``link.register``, as
before) or by the Muse Gadget add-on, which pairs over BLE and
holds the link while this entry pushes its device snapshot to it,
executes Muse's ``home.*`` invokes, and imports the pairing the
add-on publishes in ``/share/muse-gadget/``.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DATA_HUB, DOMAIN, LINK_OWNER_ADDON, PLATFORMS
from .hub import MetaHomeLinkHub
from .muse_link import valid_pairing

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_MESSAGE = "send_message"
EVENT_CHAT_REPLY = f"{DOMAIN}_chat_reply"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def _send_message(call: ServiceCall) -> None:
        message = str(call.data.get("message") or "").strip()
        if not message:
            return
        for data in (hass.data.get(DOMAIN) or {}).values():
            hub: MetaHomeLinkHub = data[DATA_HUB]
            result = await hub.async_send_chat(message)
            hass.bus.async_fire(EVENT_CHAT_REPLY, {
                "message": message,
                "reply": result.get("text"),
                "message_id": result.get("message_id"),
                "error": result.get("error"),
            })
            return
        _LOGGER.warning("Meta Home Link send_message: no link configured")

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE):
        hass.services.async_register(
            DOMAIN, SERVICE_SEND_MESSAGE, _send_message,
            schema=vol.Schema({vol.Required("message"): cv.string}),
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = MetaHomeLinkHub(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_HUB: hub}
    try:
        await hub.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        raise ConfigEntryNotReady(str(err)) from err
    await hub.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    if hub.link_owner != LINK_OWNER_ADDON and not valid_pairing(hub.pairing):
        _LOGGER.warning(
            "Meta Home Link '%s' has no pairing credentials yet; "
            "reconfigure it to paste them (see the integration README)",
            entry.title,
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data is not None:
        await data[DATA_HUB].async_stop()
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
