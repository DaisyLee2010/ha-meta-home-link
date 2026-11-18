"""Sensors describing the Muse link itself."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_HUB, DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub = hass.data[DOMAIN][entry.entry_id][DATA_HUB]
    async_add_entities([
        MetaHomeLinkStatusSensor(hub, entry),
        MetaHomeLinkExposedSensor(hub, entry),
    ])


class _Base(CoordinatorEntity, SensorEntity):
    def __init__(self, hub, entry, key: str, name: str) -> None:
        super().__init__(hub)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = "mdi:link-variant"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": f"Meta Home Link ({self._entry.title})",
            "manufacturer": "Meta",
            "model": "Muse Home Link (Home Assistant)",
        }


class MetaHomeLinkStatusSensor(_Base):
    def __init__(self, hub, entry) -> None:
        super().__init__(hub, entry, "status", "Muse link status")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("status", "unknown")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"last_error": data.get("last_error")}


class MetaHomeLinkExposedSensor(_Base):
    def __init__(self, hub, entry) -> None:
        super().__init__(hub, entry, "exposed", "Devices exposed to Muse")
        self._attr_icon = "mdi:home-assistant"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("exposed_count", 0)

    @property
    def extra_state_attributes(self):
        hub = self.coordinator
        return {"devices": [f"{d.friendly_name} ({d.entity_id})" for d in hub.registry.devices]}
