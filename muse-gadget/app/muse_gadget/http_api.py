"""Local HTTP API: the integration's import + device + invoke channel.

Programmatic endpoints (``/api/*``) need the shared token from the
add-on options (``X-Gadget-Token``). The ingress UI (``/``, ``/ui/*``)
needs none — Home Assistant already authenticates ingress.

Flow: the add-on owns the Muse link. ``home.*`` invokes from Claude
are queued here; the integration long-polls ``GET /api/invokes``,
executes them in Home Assistant, and posts results back. The
integration pushes its exposed-device snapshot to ``POST /api/devices``
so ``device.guide``/``device.health`` answer locally.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from aiohttp import web

from .exposure import ExposureRegistry, normalise_exposure

_LOGGER = logging.getLogger(__name__)
INVOKE_TIMEOUT = 20.0
INTEGRATION_FRESH_SECONDS = 90.0


class InvokeQueue:
    """Pending Muse invokes waiting for the integration to execute."""

    def __init__(self) -> None:
        self._pending: dict[str, dict] = {}
        self._futures: dict[str, asyncio.Future] = {}
        self._event = asyncio.Event()
        self.last_poll_at: float | None = None

    @property
    def integration_fresh(self) -> bool:
        return self.last_poll_at is not None and \
            time.monotonic() - self.last_poll_at < INTEGRATION_FRESH_SECONDS

    async def call(self, command: str, params: dict, timeout: float = INVOKE_TIMEOUT):
        invoke_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[invoke_id] = {
            "id": invoke_id, "command": command, "params": params,
            "created_at": time.time(),
        }
        self._futures[invoke_id] = future
        self._event.set()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(invoke_id, None)
            self._futures.pop(invoke_id, None)

    async def poll(self, wait_seconds: float = 25.0) -> list[dict]:
        self.last_poll_at = time.monotonic()
        if not self._pending:
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            self.last_poll_at = time.monotonic()
        return list(self._pending.values())

    def resolve(self, invoke_id: str, ok: bool, payload, error) -> bool:
        future = self._futures.get(invoke_id)
        if future is None or future.done():
            return False
        future.set_result((ok, payload, error))
        return True


def command_specs() -> dict[str, dict]:
    """commands_v2 for the add-on gadget (home parity + gadget extras)."""
    from .exposure import command_specs as home_specs

    specs = home_specs()
    specs["gadget.show_text"] = {
        "description": "Show a short message on this gadget's display (add-on ingress page).",
        "required": {"text": {"type": "string", "description": "The short message to show."}},
        "optional": {},
    }
    specs["gadget.get_display"] = {
        "description": "Read the message currently shown on this gadget's display.",
        "required": {}, "optional": {},
    }
    return specs


class GadgetState:
    """Mutable add-on state shared by the link, BLE and HTTP layers."""

    def __init__(self, store, instance_name: str) -> None:
        self.store = store
        self.instance_name = instance_name
        self.status = "starting"
        self.last_error: str | None = None
        self.display_text = ""
        self.display_updated_at: float | None = None
        self.link = None  # GadgetLinkClient | None
        self.queue = InvokeQueue()
        self.pairing_open = False
        self.ble_error: str | None = None
        self.started_at = time.monotonic()
        cached = store.read_devices()
        self.devices_payload: dict = cached

    # ------------------------------------------------------------ devices
    def set_devices(self, payload: dict) -> None:
        self.devices_payload = payload
        self.store.save_devices(payload)

    def registry(self) -> ExposureRegistry:
        devices = self.devices_payload.get("devices") or []
        exposed = [d.get("entity_id", "") for d in devices if isinstance(d, dict)]
        names = {d.get("entity_id", ""): d.get("name", "")
                 for d in devices if isinstance(d, dict)}
        ha_names = {eid: "" for eid in exposed}
        return ExposureRegistry(normalise_exposure(exposed, names, ha_names))

    def states(self) -> dict:
        return self.devices_payload.get("states") or {}

    # -------------------------------------------------------------- invoke
    async def handle_invoke(self, command: str, params: dict):
        try:
            if command == "device.guide":
                guide = self.registry().guide(self.instance_name, self.states())
                return True, {"format": "markdown", "guide": guide}, None
            if command == "device.health":
                return True, {
                    "home_assistant": self.devices_payload.get("ha_version", "unknown"),
                    "uptime_ms": int((time.monotonic() - self.started_at) * 1000),
                    "exposed_devices": len(self.registry().devices),
                    "connected": True,
                    "integration_connected": self.queue.integration_fresh,
                }, None
            if command == "gadget.show_text":
                text = str(params.get("text") or "").strip()
                if not text:
                    return False, None, "Say what to show."
                self.display_text = text[:500]
                self.display_updated_at = time.time()
                return True, {"shown": True, "text": self.display_text}, None
            if command == "gadget.get_display":
                return True, {"text": self.display_text,
                              "updated_at": self.display_updated_at}, None
            if command.startswith("home."):
                if not self.queue.integration_fresh:
                    return False, None, (
                        "Home Assistant's Meta Home Link integration is not "
                        "connected to this gadget right now.")
                return await self.queue.call(command, params)
            return False, None, f"Unknown command: {command}"
        except asyncio.TimeoutError:
            return False, None, "Home Assistant did not answer in time."
        except Exception as err:  # noqa: BLE001 - report to Muse
            _LOGGER.exception("invoke failed: %s", command)
            return False, None, f"This gadget could not do that: {err}"

    def snapshot(self) -> dict:
        identity = self.store.identity()
        return {
            "status": self.status,
            "paired": self.store.pairing() is not None,
            "connected": bool(self.link and self.link.registered),
            "ble_name": identity.ble_name,
            "node_id": identity.node_id,
            "pairing_open": self.pairing_open,
            "ble_error": self.ble_error,
            "integration_connected": self.queue.integration_fresh,
            "exposed_count": len(self.registry().devices),
            "display_text": self.display_text,
            "last_error": self.last_error or (self.link.last_error if self.link else None),
        }


_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Muse Gadget</title>
<style>body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:42rem;padding:0 1rem}
#display{font-size:1.6rem;padding:1rem;border:1px solid #888;border-radius:.5rem;min-height:3rem}
.row{display:flex;gap:.5rem;margin-top:1rem}input{flex:1;padding:.5rem}button{padding:.5rem 1rem}
pre{white-space:pre-wrap;background:#111;color:#eee;padding:1rem;border-radius:.5rem;min-height:6rem}
</style></head><body>
<h1>Muse Gadget</h1>
<p id="status">Loading...</p>
<h2>Display</h2><div id="display">(nothing shown yet)</div>
<h2>Talk to Muse</h2>
<div class="row"><input id="msg" placeholder="Ask Muse..."><button id="send">Send</button></div>
<div class="row"><button id="pair">Open pairing window</button>
<button id="history">Refresh history</button></div>
<pre id="out"></pre><audio id="voice" controls></audio>
<script>
const out=document.getElementById('out');
async function tick(){const r=await fetch('ui/status');const s=await r.json();
document.getElementById('status').textContent=
 (s.paired?'Paired as '+s.ble_name+' ('+s.node_id+')':'Not paired yet')+
 ' - link: '+s.status+(s.integration_connected?' - Home Assistant connected':' - Home Assistant not connected')+
 (s.ble_error?' - Bluetooth: '+s.ble_error:'');
document.getElementById('display').textContent=s.display_text||'(nothing shown yet)';}
document.getElementById('send').onclick=async()=>{
 const m=document.getElementById('msg').value;out.textContent='Thinking...';
 const r=await fetch('ui/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m})});
 const d=await r.json();out.textContent=d.text||d.error||'(no reply)';
 if(d.message_id){const a=document.getElementById('voice');a.src='ui/voice?message_id='+encodeURIComponent(d.message_id);a.play().catch(()=>{});}};
document.getElementById('pair').onclick=async()=>{const r=await fetch('ui/pair',{method:'POST'});out.textContent=JSON.stringify(await r.json());tick();};
document.getElementById('history').onclick=async()=>{const r=await fetch('ui/history');out.textContent=JSON.stringify(await r.json(),null,2);};
tick();setInterval(tick,5000);
</script></body></html>"""


def build_app(state: GadgetState, token: str, *, on_pair, on_unpair) -> web.Application:
    app = web.Application()

    def _authed(request: web.Request) -> bool:
        return not token or request.headers.get("X-Gadget-Token") == token

    def _require(request: web.Request) -> None:
        if not _authed(request):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "bad token"}),
                                       content_type="application/json")

    async def _json(request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------- /api (token)
    async def health(request):
        snap = state.snapshot()
        return web.json_response({"status": snap["status"], "paired": snap["paired"],
                                  "connected": snap["connected"]})

    async def get_status(request):
        _require(request)
        return web.json_response(state.snapshot())

    async def post_devices(request):
        _require(request)
        payload = await _json(request)
        state.set_devices(payload)
        return web.json_response({"stored": len(payload.get("devices") or [])})

    async def get_devices(request):
        _require(request)
        return web.json_response(state.devices_payload)

    async def poll_invokes(request):
        _require(request)
        try:
            wait = float(request.query.get("wait", "25"))
        except ValueError:
            wait = 25.0
        invokes = await state.queue.poll(min(max(wait, 0.0), 30.0))
        return web.json_response({"invokes": invokes})

    async def post_result(request):
        _require(request)
        payload = await _json(request)
        resolved = state.queue.resolve(
            request.match_info["invoke_id"],
            bool(payload.get("ok")), payload.get("payload"), payload.get("error"))
        if not resolved:
            raise web.HTTPNotFound(text=json.dumps({"error": "unknown invoke"}),
                                   content_type="application/json")
        return web.json_response({"resolved": True})

    async def post_chat(request):
        _require(request)
        payload = await _json(request)
        return web.json_response(await _chat(payload))

    async def get_history(request):
        _require(request)
        return web.json_response(await _history(request))

    async def get_tts(request):
        _require(request)
        return await _tts(request)

    async def post_pair(request):
        _require(request)
        return web.json_response(await _pair())

    async def post_unpair(request):
        _require(request)
        on_unpair()
        return web.json_response({"unpaired": True})

    # ------------------------------------------------------------ shared
    async def _chat(payload: dict) -> dict:
        message = str(payload.get("message") or "").strip()
        if not message:
            return {"error": "Say something first."}
        if state.link is None or not state.link.registered:
            return {"error": "Not connected to Muse yet."}
        try:
            return await state.link.send_chat(message)
        except Exception as err:  # noqa: BLE001
            return {"error": str(err)}

    async def _history(request) -> dict:
        if state.link is None or not state.link.registered:
            return {"error": "Not connected to Muse yet."}
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        try:
            return await state.link.fetch_history(limit)
        except Exception as err:  # noqa: BLE001
            return {"error": str(err)}

    async def _tts(request) -> web.Response:
        message_id = request.query.get("message_id", "")
        if not message_id or state.link is None or not state.link.registered:
            raise web.HTTPBadRequest(text="message_id required / not connected")
        audio = await state.link.stream_tts(message_id)
        return web.Response(body=audio, content_type="audio/mpeg")

    async def _pair() -> dict:
        result = await on_pair()
        return result if isinstance(result, dict) else {"pairing_open": True}

    # ------------------------------------------------------------- ingress
    async def index(request):
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def ui_status(request):
        return web.json_response(state.snapshot())

    async def ui_chat(request):
        return web.json_response(await _chat(await _json(request)))

    async def ui_history(request):
        return web.json_response(await _history(request))

    async def ui_voice(request):
        return await _tts(request)

    async def ui_pair(request):
        return web.json_response(await _pair())

    app.router.add_get("/health", health)
    app.router.add_get("/api/status", get_status)
    app.router.add_post("/api/devices", post_devices)
    app.router.add_get("/api/devices", get_devices)
    app.router.add_get("/api/invokes", poll_invokes)
    app.router.add_post("/api/invokes/{invoke_id}/result", post_result)
    app.router.add_post("/api/chat", post_chat)
    app.router.add_get("/api/chat/history", get_history)
    app.router.add_get("/api/voice/tts", get_tts)
    app.router.add_post("/api/pair", post_pair)
    app.router.add_post("/api/unpair", post_unpair)
    app.router.add_get("/", index)
    app.router.add_get("/ui/status", ui_status)
    app.router.add_post("/ui/chat", ui_chat)
    app.router.add_get("/ui/history", ui_history)
    app.router.add_get("/ui/voice", ui_voice)
    app.router.add_post("/ui/pair", ui_pair)
    return app
