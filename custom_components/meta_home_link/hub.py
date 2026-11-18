"""Hub: owns the Muse link task and executes home.* commands in HA.

Two link owners: ``integration`` (this hub runs the Noise link
itself) and ``addon`` (the Muse Gadget add-on runs the link; this
hub pushes its exposed-device snapshot to the add-on, long-polls
for ``home.*`` invokes, executes them here, and posts results back).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .addon_import import build_devices_payload
from .const import (
    CONF_ADDON_API_URL,
    CONF_ADDON_TOKEN,
    CONF_EXPOSED_ENTITIES,
    CONF_FRIENDLY_NAMES,
    CONF_LINK_OWNER,
    CONF_NAME,
    CONF_PAIRING,
    CONF_SDK_TOKEN,
    DEFAULT_ADDON_API_URL,
    DEFAULT_NAME,
    LINK_OWNER_ADDON,
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
        self._addon_session: aiohttp.ClientSession | None = None
        self._addon_status: dict = {}
        self._addon_last_payload: str = ""
        self._started = time.monotonic()
        self.status: str = "not_paired" if not valid_pairing(self.pairing) else "starting"
        if self.link_owner == LINK_OWNER_ADDON:
            self.status = "addon_starting"
        self.registry = self._build_registry()

    @property
    def link_owner(self) -> str:
        return str(self._cfg.get(CONF_LINK_OWNER) or "integration")

    @property
    def addon_api_url(self) -> str:
        return str(self._cfg.get(CONF_ADDON_API_URL) or DEFAULT_ADDON_API_URL).rstrip("/")

    def _addon_headers(self) -> dict:
        token = str(self._cfg.get(CONF_ADDON_TOKEN) or "")
        return {"X-Gadget-Token": token} if token else {}

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
        if self.link_owner == LINK_OWNER_ADDON:
            self._task = self.entry.async_create_background_task(
                self.hass, self._addon_loop(), name="meta_home_link_addon_link"
            )
            return
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
        if self._addon_session is not None:
            await self._addon_session.close()
            self._addon_session = None

    # ----------------------------------------------------- add-on channel
    def _session(self) -> aiohttp.ClientSession:
        if self._addon_session is None:
            self._addon_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=35))
        return self._addon_session

    async def _addon_loop(self) -> None:
        """Push devices, poll invokes, post results — forever."""
        while True:
            try:
                await self._push_devices()
                async with self._session().get(
                    f"{self.addon_api_url}/api/status", headers=self._addon_headers(),
                ) as resp:
                    if resp.status == 200:
                        self._addon_status = await resp.json()
                        self.status = str(self._addon_status.get("status") or "addon")
                        self.async_set_updated_data(self.snapshot())
                async with self._session().get(
                    f"{self.addon_api_url}/api/invokes",
                    params={"wait": "25"}, headers=self._addon_headers(),
                ) as resp:
                    if resp.status != 200:
                        self.status = "addon_unreachable"
                        self.async_set_updated_data(self.snapshot())
                        await asyncio.sleep(5)
                        continue
                    data = await resp.json()
                for invoke in data.get("invokes") or []:
                    ok, payload, error = await self._handle_invoke(
                        str(invoke.get("command", "")), invoke.get("params") or {})
                    async with self._session().post(
                        f"{self.addon_api_url}/api/invokes/{invoke.get('id')}/result",
                        json={"ok": ok, "payload": payload, "error": error},
                        headers=self._addon_headers(),
                    ):
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - retry loop
                self.status = "addon_unreachable"
                self._addon_status = {"last_error": f"{type(err).__name__}: {err}"}
                _LOGGER.debug("add-on channel failed", exc_info=True)
                self.async_set_updated_data(self.snapshot())
                await asyncio.sleep(5)

    async def _push_devices(self) -> None:
        from homeassistant.const import __version__ as ha_version
        self.registry = self._build_registry()
        payload = build_devices_payload(
            self.registry.devices, self.states_snapshot(), ha_version,
            self.instance_name)
        encoded = json.dumps(payload, sort_keys=True)
        if encoded == self._addon_last_payload:
            return
        async with self._session().post(
            f"{self.addon_api_url}/api/devices", json=payload,
            headers=self._addon_headers(),
        ) as resp:
            if resp.status == 200:
                self._addon_last_payload = encoded

    async def async_send_chat(self, message: str) -> dict:
        """Send one chat turn through whichever link owner is active."""
        if self.link_owner == LINK_OWNER_ADDON:
            async with self._session().post(
                f"{self.addon_api_url}/api/chat", json={"message": message},
                headers=self._addon_headers(),
            ) as resp:
                data = await resp.json()
                return data if isinstance(data, dict) else {"error": "bad reply"}
        return {"error": "Chat needs the Muse Gadget add-on as link owner."}

    def _save_pairing(self, pairing: dict) -> None:
        data = {**self.entry.data, CONF_PAIRING: pairing}
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    def _on_status(self, status: str) -> None:
        self.status = status
        self.async_set_updated_data(self.snapshot())

    def snapshot(self) -> dict:
        if self.link_owner == LINK_OWNER_ADDON:
            return {
                "status": self.status,
                "connected": bool(self._addon_status.get("connected")),
                "exposed_count": len(self.registry.devices),
                "last_error": self._addon_status.get("last_error"),
                "link_owner": LINK_OWNER_ADDON,
            }
        return {
            "status": self.status,
            "connected": bool(self._client and self._client.registered),
            "exposed_count": len(self.registry.devices),
            "last_error": self._client.last_error if self._client else None,
            "link_owner": "integration",
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
