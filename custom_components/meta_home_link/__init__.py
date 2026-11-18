"""Meta Home Link: expose chosen Home Assistant devices to Muse.

The user picks which entities Muse may see (and optional friendly
names for them) in the config/options flow. This entry then runs a
Muse gadget link - the same Noise_XX ``link.register`` protocol the
Muse Gadget SDK uses - and answers Muse's
``home.*`` invokes with real Home Assistant service calls.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DATA_HUB, DOMAIN, PLATFORMS
from .hub import MetaHomeLinkHub
from .muse_link import valid_pairing

_LOGGER = logging.getLogger(__name__)


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
    if not valid_pairing(hub.pairing):
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
