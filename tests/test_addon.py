"""Muse Gadget add-on: /share import, device payload, link, HTTP API."""
import asyncio
import json
from pathlib import Path

import pytest

from meta_home_link.addon_import import build_devices_payload, load_addon_share
from meta_home_link.exposure import normalise_exposure

ROOT = Path(__file__).parent.parent
ADDON = ROOT / "muse-gadget"


# ------------------------------------------------------------ /share import
def test_addon_share_roundtrip(tmp_path):
    from muse_gadget.store import GadgetStore

    store = GadgetStore(str(tmp_path / "data"), str(tmp_path / "share"))
    identity = store.identity()
    store.save_pairing({
        "access_token": "acc", "refresh_token": "ref", "token_type": "device",
        "api_url_v2": "https://api.muse.ai", "noise_host": "hatch.metaaivm.com",
        "node_id": identity.node_id,
    })
    imported = load_addon_share(tmp_path / "share")
    assert imported is not None
    assert imported.pairing["access_token"] == "acc"
    assert imported.pairing["node_id"] == identity.node_id
    assert imported.mac == identity.mac


def test_addon_share_missing_or_incomplete(tmp_path):
    assert load_addon_share(tmp_path) is None
    (tmp_path / "pairing.json").write_text('{"access_token": "only"}')
    assert load_addon_share(tmp_path) is None
    (tmp_path / "pairing.json").write_text("not json")
    assert load_addon_share(tmp_path) is None


def test_build_devices_payload_shape():
    devices = normalise_exposure(
        ["light.kitchen", "sensor.temp"], {"light.kitchen": "Kitchen Light"}, {})
    payload = build_devices_payload(
        devices, {"light.kitchen": {"state": "on", "attributes": {}}},
        "2024.6.0", "My Home")
    assert payload["ha_version"] == "2024.6.0"
    assert payload["devices"][0] == {
        "entity_id": "light.kitchen", "name": "Kitchen Light",
        "domain": "light", "read_only": False}
    assert payload["devices"][1]["read_only"] is True
    assert payload["states"]["light.kitchen"]["state"] == "on"


# ------------------------------------------------------------- chat tracker
def test_turn_tracker_streams_and_finishes():
    from muse_gadget.gadget_link import TurnTracker

    tracker = TurnTracker()
    tracker.begin("user-1")
    tracker.on_event({"event": "delta.text_append",
                      "payload": {"reply_to_message_id": "user-1",
                                  "message_id": "a1", "text": "Kitchen "}})
    tracker.on_event({"event": "delta.text_append",
                      "payload": {"reply_to_message_id": "someone-else",
                                  "message_id": "zz", "text": "IGNORED"}})
    tracker.on_event({"event": "delta.text_append",
                      "payload": {"reply_to_message_id": "user-1",
                                  "message_id": "a1", "text": "light is on"}})
    assert not tracker.done and tracker.text == "Kitchen light is on"
    tracker.on_event({"event": "delta.message_done",
                      "payload": {"reply_to_message_id": "user-1",
                                  "message_id": "a1", "display_text": "Done."}})
    assert tracker.done and tracker.text == "Done." and tracker.message_id == "a1"


# ------------------------------------------------------------- invoke queue
def test_invoke_queue_roundtrip_and_timeout():
    from muse_gadget.http_api import InvokeQueue

    async def _run():
        queue = InvokeQueue()
        assert not queue.integration_fresh

        async def _consumer():
            invokes = await queue.poll(2)
            assert invokes and invokes[0]["command"] == "home.turn_on"
            assert queue.integration_fresh
            assert queue.resolve(invokes[0]["id"], True, {"state": "on"}, None)

        task = asyncio.ensure_future(_consumer())
        ok, payload, error = await queue.call("home.turn_on", {"device": "Kitchen Light"})
        await task
        assert (ok, payload, error) == (True, {"state": "on"}, None)
        with pytest.raises(asyncio.TimeoutError):
            await queue.call("home.toggle", {}, timeout=0.1)
        assert not queue.resolve("no-such-id", True, None, None)

    asyncio.run(_run())


def test_gadget_state_guide_show_text_and_offline_home(tmp_path):
    from muse_gadget.http_api import GadgetState
    from muse_gadget.store import GadgetStore

    async def _run():
        store = GadgetStore(str(tmp_path / "data"), str(tmp_path / "share"))
        state = GadgetState(store, "My Home")
        state.set_devices({
            "ha_version": "2024.6.0",
            "devices": [{"entity_id": "light.kitchen", "name": "Kitchen Light",
                         "domain": "light", "read_only": False}],
            "states": {"light.kitchen": {"state": "off", "attributes": {}}},
        })
        ok, payload, _ = await state.handle_invoke("device.guide", {})
        assert ok and "Kitchen Light (light, currently off)" in payload["guide"]
        ok, payload, _ = await state.handle_invoke(
            "gadget.show_text", {"text": "dinner at seven"})
        assert ok and state.display_text == "dinner at seven"
        # Integration not polling: home.* must fail fast, not hang Muse.
        ok, _, error = await state.handle_invoke(
            "home.turn_on", {"device": "Kitchen Light"})
        assert not ok and "not" in error and "connected" in error
        snap = state.snapshot()
        assert snap["exposed_count"] == 1 and snap["display_text"] == "dinner at seven"

    asyncio.run(_run())


# --------------------------------------------------------------- HTTP API
def test_http_api_token_and_invoke_flow(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from muse_gadget.http_api import GadgetState, build_app
    from muse_gadget.store import GadgetStore

    async def _run():
        store = GadgetStore(str(tmp_path / "data"), str(tmp_path / "share"))
        state = GadgetState(store, "My Home")

        async def _pair():
            return {"pairing_open": True}

        app = build_app(state, "secret", on_pair=_pair, on_unpair=lambda: None)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/health")
            assert resp.status == 200  # unauthenticated, no secrets
            resp = await client.get("/api/status")
            assert resp.status == 401  # token required
            headers = {"X-Gadget-Token": "secret"}
            resp = await client.get("/api/status", headers=headers)
            assert (await resp.json())["ble_name"].startswith("MuseGadget")
            resp = await client.post("/api/devices", headers=headers, json={
                "ha_version": "2024.6.0",
                "devices": [{"entity_id": "light.kitchen", "name": "Kitchen Light",
                             "domain": "light", "read_only": False}],
                "states": {"light.kitchen": {"state": "on", "attributes": {}}}})
            assert (await resp.json())["stored"] == 1

            async def _execute():
                resp = await client.get("/api/invokes", params={"wait": "2"},
                                        headers=headers)
                invokes = (await resp.json())["invokes"]
                assert invokes[0]["command"] == "home.turn_on"
                resp = await client.post(
                    f"/api/invokes/{invokes[0]['id']}/result", headers=headers,
                    json={"ok": True, "payload": {"state": "on"}, "error": None})
                assert resp.status == 200

            task = asyncio.ensure_future(_execute())
            await asyncio.sleep(0.1)  # let the poll register first
            result = await state.handle_invoke(
                "home.turn_on", {"device": "Kitchen Light"})
            await task
            assert result == (True, {"state": "on"}, None)
            resp = await client.get("/")
            assert resp.status == 200 and "Muse Gadget" in await resp.text()
        finally:
            await client.close()

    asyncio.run(_run())


# ----------------------------------------------------------------- layout
def test_addon_layout_and_vendored_sync():
    config = (ADDON / "config.yaml").read_text()
    for needle in ("slug: muse-gadget", "share:rw", "ingress: true",
                   "host_dbus: true", "8099"):
        assert needle in config, needle
    for name in ("Dockerfile", "build.yaml", "run.sh", "DOCS.md", "README.md",
                 "CHANGELOG.md", "translations/en.yaml", "icon.png", "logo.png"):
        assert (ADDON / name).is_file(), name
    assert (ROOT / "repository.yaml").is_file()
    # Vendored protocol modules must not drift from the integration's.
    integration = ROOT / "custom_components" / "meta_home_link"
    for name in ("proto.py", "noise.py", "pairing.py", "exposure.py", "const.py"):
        assert (ADDON / "app" / "muse_gadget" / name).read_bytes() == \
            (integration / name).read_bytes(), name
    manifest = json.loads((integration / "manifest.json").read_text())
    assert manifest["version"] == "1.1.0"
