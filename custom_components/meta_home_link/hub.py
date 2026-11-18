"""Hub: owns the Muse link task and executes home.* commands in HA."""
from __future__ import annotations

import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_EXPOSED_ENTITIES,
    CONF_FRIENDLY_NAMES,
    CONF_NAME,
    CONF_PAIRING,
    CONF_SDK_TOKEN,
    DEFAULT_NAME,
)
from .exposure import (
    ExposureRegistry,
    PlanError,
    command_specs,
    normalise_exposure,
    plan_service,
)
from .muse_link import Identity, MuseLinkClient, valid_pairing

_LOGGER = logging.getLogger(__name__)

# Attributes safe to tell Muse about (no ids, tokens or addresses).
_SAFE_ATTRS = (
    "brightness", "color_temp_kelvin", "temperature", "current_temperature",
    "hvac_mode", "fan_mode", "percentage", "current_position", "is_locked",
    "volume_level", "media_title", "unit_of_measurement",
)


class MetaHomeLinkHub(DataUpdateCoordinator[dict]):
    """Coordinator + command executor for one config entry."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(hass, _LOGGER, name=f"Meta Home Link ({entry.title})")
        self.entry = entry
        self._task = None
        self._client: MuseLinkClient | None = None
        self._started = time.monotonic()
        self.status: str = "not_paired" if not valid_pairing(self.pairing) else "starting"
        self.registry = self._build_registry()

    # ------------------------------------------------------------ config
    @property
    def _cfg(self) -> dict:
        return {**self.entry.data, **self.entry.options}

    @property
    def pairing(self) -> dict:
        return self._cfg.get(CONF_PAIRING) or {}

    @property
    def instance_name(self) -> str:
        return self._cfg.get(CONF_NAME) or DEFAULT_NAME

    def _build_registry(self) -> ExposureRegistry:
        ha_names = {}
        for entity_id in self._cfg.get(CONF_EXPOSED_ENTITIES) or []:
            state = self.hass.states.get(entity_id)
            if state is not None:
                ha_names[entity_id] = state.attributes.get("friendly_name", "")
        return ExposureRegistry(normalise_exposure(
            self._cfg.get(CONF_EXPOSED_ENTITIES),
            self._cfg.get(CONF_FRIENDLY_NAMES),
            ha_names,
        ))

    def states_snapshot(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for device in self.registry.devices:
            state = self.hass.states.get(device.entity_id)
            if state is None:
                out[device.entity_id] = {"state": "unavailable", "attributes": {}}
                continue
            attrs = {k: state.attributes[k] for k in _SAFE_ATTRS if k in state.attributes}
            out[device.entity_id] = {"state": state.state, "attributes": attrs}
        return out

    # -------------------------------------------------------------- link
    async def async_start(self) -> None:
        if not valid_pairing(self.pairing):
            self.status = "not_paired"
            self.async_set_updated_data(self.snapshot())
            return
        identity = Identity(self._cfg["mac"], self.pairing.get("node_id"))
        self._client = MuseLinkClient(
            pairing=self.pairing,
            identity=identity,
            sdk_token=self._cfg.get(CONF_SDK_TOKEN),
            instance_name=self.instance_name,
            commands=command_specs(),
            invoke_cb=self._handle_invoke,
            status_cb=self._on_status,
            save_pairing_cb=self._save_pairing,
        )
        self._task = self.entry.async_create_background_task(
            self.hass, self._client.run_forever(), name="meta_home_link_muse_link"
        )

    async def async_stop(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _save_pairing(self, pairing: dict) -> None:
        data = {**self.entry.data, CONF_PAIRING: pairing}
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    def _on_status(self, status: str) -> None:
        self.status = status
        self.async_set_updated_data(self.snapshot())

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "connected": bool(self._client and self._client.registered),
            "exposed_count": len(self.registry.devices),
            "last_error": self._client.last_error if self._client else None,
        }

    async def _async_update_data(self) -> dict:
        # Rebuild the registry so renamed/re-added entities are picked up.
        self.registry = self._build_registry()
        return self.snapshot()

    # ------------------------------------------------------------ invokes
    async def _handle_invoke(self, command: str, params: dict):
        try:
            if command == "device.guide":
                guide = self.registry.guide(self.instance_name, self.states_snapshot())
                return True, {"format": "markdown", "guide": guide}, None
            if command == "device.health":
                from homeassistant.const import __version__ as ha_version
                return True, {
                    "home_assistant": ha_version,
                    "uptime_ms": int((time.monotonic() - self._started) * 1000),
                    "exposed_devices": len(self.registry.devices),
                    "connected": True,
                }, None
            if command == "home.list_devices":
                return True, {"devices": self.registry.list_payload(self.states_snapshot())}, None
            device = self.registry.resolve(params.get("device"))
            if device is None:
                names = ", ".join(d.friendly_name for d in self.registry.devices) or "none"
                return False, None, f"No exposed device called '{params.get('device')}'. Exposed: {names}."
            if command == "home.get_state":
                snap = self.states_snapshot().get(device.entity_id, {})
                return True, {"name": device.friendly_name, "entity_id": device.entity_id,
                              "state": snap.get("state", "unknown"),
                              "attributes": snap.get("attributes", {})}, None
            svc_domain, service, data = plan_service(command, device, params)
            await self.hass.services.async_call(svc_domain, service, data, blocking=True)
            snap = self.states_snapshot().get(device.entity_id, {})
            return True, {"name": device.friendly_name, "entity_id": device.entity_id,
                          "state": snap.get("state", "unknown")}, None
        except PlanError as err:
            return False, None, str(err)
        except Exception as err:  # noqa: BLE001 - report to Muse, not crash link
            _LOGGER.exception("home command failed: %s", command)
            return False, None, f"Home Assistant could not do that: {err}"
