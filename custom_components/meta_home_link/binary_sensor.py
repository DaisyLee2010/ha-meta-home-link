"""Binary sensor: is Muse currently connected to this home?"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_HUB, DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = hass.data[DOMAIN][entry.entry_id][DATA_HUB]
    async_add_entities([MetaHomeLinkConnectedSensor(hub, entry)])


class MetaHomeLinkConnectedSensor(CoordinatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:link-variant"

    def __init__(self, hub, entry) -> None:
        super().__init__(hub)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connected"
        self._attr_name = "Muse connected"

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("connected"))

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": f"Meta Home Link ({self._entry.title})",
            "manufacturer": "Meta",
            "model": "Muse Home Link (Home Assistant)",
        }
