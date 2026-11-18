"""Add-on entrypoint: BLE pairing + gadget link + local HTTP API.

Options come from ``/data/options.json`` (Home Assistant add-on
options): ``name``, ``sdk_token``, ``api_token``, ``api_port``,
``pair_on_start``. State lives in ``/data``; the integration imports
``pairing.json`` / ``identity.json`` / ``status.json`` from
``/share/muse-gadget/`` and pushes devices to the HTTP API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__
from .gadget_link import GadgetLinkClient, valid_pairing
from .http_api import GadgetState, build_app, command_specs
from .pairing import PairingSession
from .pairing_controller import PairingController
from .store import GadgetStore

_LOGGER = logging.getLogger("muse_gadget")
OPTIONS_PATH = "/data/options.json"


def _load_options() -> dict:
    try:
        data = json.loads(Path(OPTIONS_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


class GadgetService:
    def __init__(self, options: dict, *, data_dir: str = "/data",
                 share_dir: str = "/share/muse-gadget") -> None:
        self.options = options
        self.store = GadgetStore(data_dir, share_dir)
        self.state = GadgetState(self.store, str(options.get("name") or "Home Assistant"))
        self.state.status = "not_paired" if not valid_pairing(self.store.pairing()) else "starting"
        self._link_task: asyncio.Task | None = None
        self._peripheral = None
        self.sdk_token = str(options.get("sdk_token") or "").strip() or None

    # --------------------------------------------------------------- link
    def _on_status(self, status: str) -> None:
        self.state.status = status
        self.store.write_status({"status": status})

    async def start_link(self) -> None:
        pairing = self.store.pairing()
        if not valid_pairing(pairing):
            self.state.status = "not_paired"
            self.store.write_status({"status": "not_paired"})
            return
        if self._link_task is not None and not self._link_task.done():
            return
        identity = self.store.identity()
        client = GadgetLinkClient(
            pairing=pairing, identity=identity, sdk_token=self.sdk_token,
            instance_name=self.state.instance_name, commands=command_specs(),
            invoke_cb=self.state.handle_invoke, status_cb=self._on_status,
            save_pairing_cb=self.store.save_pairing,
        )
        self.state.link = client
        self._link_task = asyncio.ensure_future(client.run_forever())

    async def stop_link(self) -> None:
        if self.state.link is not None:
            await self.state.link.close()
        if self._link_task is not None:
            self._link_task.cancel()
            self._link_task = None
        self.state.link = None

    async def restart_link(self) -> None:
        await self.stop_link()
        await self.start_link()

    # ------------------------------------------------------------- pairing
    async def open_pairing(self) -> dict:
        if self.state.pairing_open:
            return {"pairing_open": True, "ble_name": self.store.identity().ble_name}
        from .ble_bluez import BleUnavailable, BluezPairingPeripheral

        identity = self.store.identity()
        session = PairingSession(
            identity.node_id, identity.device_id, identity.mac,
            __version__, self.sdk_token)

        async def _prepare_save(credentials: dict):
            def _commit(payload: dict) -> None:
                record = {
                    **payload,
                    "token_type": "device",
                    "api_url": payload.get("api_url") or "https://api.muse.ai",
                    "node_id": identity.node_id,
                }
                self.store.save_pairing(record)
            return _commit

        def _factory(send_packets, mtu, disconnect):
            return PairingController(
                session, send_packets=send_packets, mtu=mtu, disconnect=disconnect,
                is_online=lambda: True,
                current_network=lambda: {"ssid": "Home Assistant host network",
                                         "signal": 100, "security": "wpa2"},
                prepare_save=_prepare_save,
                on_complete=lambda: asyncio.ensure_future(self._paired()),
                version=__version__)

        peripheral = BluezPairingPeripheral(identity.ble_name, _factory)
        try:
            await peripheral.start()
        except BleUnavailable as err:
            self.state.ble_error = str(err)
            self.store.write_status({"status": self.state.status, "ble_error": str(err)})
            return {"pairing_open": False, "error": str(err),
                    "ble_name": identity.ble_name}
        self._peripheral = peripheral
        self.state.pairing_open = True
        self.state.ble_error = None
        return {"pairing_open": True, "ble_name": identity.ble_name}

    async def _paired(self) -> None:
        self.state.pairing_open = False
        if self._peripheral is not None:
            await self._peripheral.stop()
            self._peripheral = None
        self.store.mirror_share()
        self.store.write_status({"status": "paired"})
        await self.restart_link()

    def unpair(self) -> None:
        asyncio.ensure_future(self._unpair())

    async def _unpair(self) -> None:
        await self.stop_link()
        if self._peripheral is not None:
            await self._peripheral.stop()
            self._peripheral = None
        self.state.pairing_open = False
        self.store.clear_pairing()
        self.state.status = "not_paired"

    # ---------------------------------------------------------------- run
    async def run(self) -> None:
        self.store.mirror_share()
        self.store.write_status({"status": self.state.status})
        await self.start_link()
        if self.options.get("pair_on_start") and not valid_pairing(self.store.pairing()):
            await self.open_pairing()
        token = str(self.options.get("api_token") or "")
        port = int(self.options.get("api_port") or 8099)
        app = build_app(self.state, token,
                        on_pair=self.open_pairing, on_unpair=self.unpair)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        _LOGGER.info("Muse Gadget API on :%s (paired=%s)",
                     port, valid_pairing(self.store.pairing()))
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop_link()
            await runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    options = _load_options()
    service = GadgetService(options)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
