"""Muse gadget link for the add-on: link.register + invokes + chat/voice.

Superset of the integration's ``muse_link.py``: as well as answering
``link.invoke`` (device.guide, device.health, home.*, gadget.*) it can
initiate requests over the Noise transport — ``POST /chat/stream``,
``GET /chat/history``, TTS streaming and the ``/chat/subscribe``
NDJSON event stream — the same paths Portal Muse uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import uuid
from urllib.parse import quote

import aiohttp

from . import __version__
from .noise import Initiator
from .proto import Transport
from .store import Identity

_LOGGER = logging.getLogger(__name__)
USER_AGENT = f"musegadget/{__version__} (Muse Gadget add-on; Home Assistant)"
DEFAULT_NOISE_HOST = "hatch.metaaivm.com"
DEFAULT_API_ROOT = "https://api.muse.ai"


def api_root(pairing: dict | None) -> str:
    v2 = (pairing or {}).get("api_url_v2") or ""
    return (v2 if v2.startswith("https://") else DEFAULT_API_ROOT).rstrip("/")


def valid_pairing(pairing: dict | None) -> bool:
    return bool(pairing) and bool(pairing.get("access_token")) and bool(pairing.get("refresh_token"))


async def _read_json(resp: aiohttp.ClientResponse) -> dict:
    text = await resp.text()
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


async def fetch_vms(session: aiohttp.ClientSession, access_token: str, root: str) -> list[dict]:
    async with session.get(
        f"{root}/fetch_vms",
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-API-Version": "1.0.0",
            "User-Agent": USER_AGENT,
        },
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status == 401:
            raise PermissionError("HTTP 401")
        data = await _read_json(resp)
    out = []
    for entry in data.get("vm_list") or []:
        vm_url = entry.get("vm_ws_url") or entry.get("vm_url") or ""
        vm_token = entry.get("vm_auth_token") or ""
        if vm_url and vm_token:
            out.append({
                "vm_url": vm_url, "vm_auth_token": vm_token,
                "vm_name": entry.get("vm_name", ""), "vm_id": entry.get("vm_id", ""),
                "is_default": bool(entry.get("default", False)),
            })
    return out


class TurnTracker:
    """Matches one assistant reply to the user message just sent."""

    def __init__(self) -> None:
        self._user_message_id = ""
        self._streamed: list[str] = []
        self._message_id: str | None = None
        self._final: str | None = None
        self.done = False

    def begin(self, user_message_id: str = "") -> None:
        self._user_message_id = user_message_id
        self._streamed, self._message_id, self._final, self.done = [], None, None, False

    def bind(self, user_message_id: str) -> None:
        self._user_message_id = user_message_id

    def on_event(self, event: dict) -> None:
        payload = event.get("payload") or {}
        name = event.get("event", "")
        parent = payload.get("reply_to_message_id", "")
        mid = payload.get("message_id", "")
        if self._user_message_id and parent and parent != self._user_message_id:
            return
        if not mid and self._message_id:
            mid = self._message_id
        if name == "delta.text_append":
            if self._message_id is None:
                self._message_id = mid
            text = payload.get("text", "")
            if text and (self._message_id is None or mid == self._message_id or not mid):
                self._streamed.append(text)
        elif name in ("delta.message_done", "message.assistant"):
            if self._message_id is None:
                self._message_id = mid
            completed = payload.get("display_text", "") or payload.get("content", "")
            if completed:
                self._final = completed
            if name == "delta.message_done":
                self.done = True

    @property
    def text(self) -> str:
        return self._final if self._final is not None else "".join(self._streamed)

    @property
    def message_id(self) -> str | None:
        return self._message_id


class _Pending:
    """One in-flight HTTP-over-Noise request (Portal Muse's Pending)."""

    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.status = 0
        self.body = bytearray()
        self.error: Exception | None = None

    def on_frame(self, frame) -> None:
        if frame.kind == "reset":
            self.error = ConnectionError("stream reset")
            self.done.set()
            return
        if frame.kind == "response":
            self.status = frame.status
        if frame.body:
            self.body += frame.body
        if frame.end_body:
            self.done.set()

    def fail(self) -> None:
        if self.error is None:
            self.error = ConnectionError("session ended")
        self.done.set()


class GadgetLinkClient:
    """One gadget session at a time; ``run_forever`` reconnects."""

    def __init__(self, *, pairing: dict, identity: Identity, sdk_token: str | None,
                 instance_name: str, commands: dict, invoke_cb, status_cb,
                 chat_event_cb=None, save_pairing_cb=None) -> None:
        self._pairing = pairing
        self._identity = identity
        self._sdk_token = sdk_token
        self._name = instance_name
        self._commands = commands
        self._invoke_cb = invoke_cb  # async (command, params) -> (ok, payload, error)
        self._status_cb = status_cb  # (status: str) -> None
        self._chat_event_cb = chat_event_cb  # (event: dict) -> None
        self._save_pairing_cb = save_pairing_cb
        self.connected = False
        self.registered = False
        self.started_at = time.monotonic()
        self.last_error: str | None = None
        self.tracker = TurnTracker()
        self._transport: Transport | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._control_stream = -1
        self._control_buf = bytearray()
        self._register_id = ""
        self._send_lock = asyncio.Lock()
        self._closed: asyncio.Event | None = None
        self._requests: dict[int, _Pending] = {}
        self._streams: dict[int, object] = {}
        self._ndjson_buf = ""

    # ------------------------------------------------------------ public
    async def run_forever(self) -> None:
        backoff = 2.0
        while True:
            try:
                await self._run_once()
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except PermissionError:
                self.last_error = "Muse rejected this link's login - pair again"
                self._status_cb("auth_failed")
                await asyncio.sleep(30)
            except Exception as err:  # noqa: BLE001 - reconnect loop
                self.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.debug("Muse link session failed", exc_info=True)
                self._status_cb("reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
            finally:
                self.connected = self.registered = False
                self._transport = None
                self._ws = None
                self._control_stream = -1
                for pending in self._requests.values():
                    pending.fail()
                self._requests.clear()
                self._streams.clear()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    # ------------------------------------------------------------- session
    async def _run_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            pairing = self._pairing
            try:
                vms = await fetch_vms(session, pairing["access_token"], api_root(pairing))
            except PermissionError:
                pairing = await self._refresh(session, pairing)
                if pairing is None:
                    raise PermissionError("refresh failed") from None
                vms = await fetch_vms(session, pairing["access_token"], api_root(pairing))
            if not vms:
                self._status_cb("no_vm")
                await asyncio.sleep(15)
                return
            vm = next((v for v in vms if v["is_default"]), vms[0])
            await self._run_session(session, pairing, vm)

    async def _refresh(self, session, pairing):
        try:
            raw_refresh = pairing["refresh_token"].rsplit(":", 1)[-1]
            body = {"device_id": self._identity.device_id}
            if self._sdk_token:
                body["sdk_token"] = self._sdk_token
            async with session.post(
                f"{api_root(pairing)}/device_token/refresh",
                json=body,
                headers={"Authorization": f"hatch_refresh:{raw_refresh}", "User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    return None
                data = await _read_json(resp)
            payload = data.get("payload") or data
            if payload.get("access_token") and payload.get("refresh_token"):
                pairing = {**pairing, "access_token": payload["access_token"],
                           "refresh_token": payload["refresh_token"]}
                self._pairing = pairing
                if self._save_pairing_cb:
                    self._save_pairing_cb(pairing)
                return pairing
        except Exception:  # noqa: BLE001
            _LOGGER.debug("token refresh failed", exc_info=True)
        return None

    async def _run_session(self, session, pairing, vm) -> None:
        noise_host = pairing.get("noise_host") or DEFAULT_NOISE_HOST
        vm_id = vm.get("vm_id") or vm.get("vm_name") or ""
        url = f"wss://{noise_host}/v1/noise?vm_id={quote(vm_id, safe='')}&app_id=hatch-web"
        self._status_cb("connecting")
        self._closed = asyncio.Event()
        async with session.ws_connect(
            url, headers={"Authorization": f"Bearer {vm['vm_auth_token']}"},
            timeout=20, heartbeat=30,
        ) as ws:
            self._ws = ws
            reader = asyncio.create_task(self._reader(ws))
            try:
                initiator = Initiator()
                await ws.send_bytes(initiator.write_message_1())
                msg2 = await self._await_handshake(reader)
                if msg2 is None:
                    raise ConnectionError("Noise handshake closed early")
                initiator.read_message_2(msg2)
                await ws.send_bytes(initiator.write_message_3())
                send_c, recv_c = initiator.split()
                self._transport = Transport(send_c, recv_c)
                control = self._transport.stream_request("POST", "/link-control", self._headers())
                self._control_stream = control.stream_id
                await self._send_frames(control.frames)
                self._register_id = str(uuid.uuid4())
                await self._send_control(self._register_message(self._register_id))
                self._status_cb("registering")
                asyncio.create_task(self._open_subscribe())
                await self._closed.wait()
            finally:
                reader.cancel()
                self._status_cb("disconnected")

    async def _reader(self, ws) -> None:
        self._handshake_frame: bytes | None = None
        self._handshake_event = asyncio.Event()
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._on_frame(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            self._handshake_event.set()
            if self._closed is not None:
                self._closed.set()

    async def _await_handshake(self, reader: asyncio.Task) -> bytes | None:
        await asyncio.wait_for(self._handshake_event.wait(), timeout=20)
        return self._handshake_frame

    def _on_frame(self, data: bytes) -> None:
        try:
            if self._transport is None:
                self._handshake_frame = data
                self._handshake_event.set()
                return
            frame = self._transport.decrypt_frame(data)
            if frame is None:
                return
            if frame.stream_id == self._control_stream and self._control_stream >= 0:
                if frame.kind == "reset":
                    if self._closed is not None:
                        self._closed.set()
                    return
                if frame.body:
                    self._control_buf += frame.body
                    self._drain_control()
                return
            sink = self._streams.get(frame.stream_id)
            if sink is not None:
                sink(frame)  # type: ignore[operator]
                return
            pending = self._requests.get(frame.stream_id)
            if pending is not None:
                pending.on_frame(frame)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("frame handling failed", exc_info=True)
            if self._closed is not None:
                self._closed.set()

    # -------------------------------------------------------------- control
    def _drain_control(self) -> None:
        buf = self._control_buf
        off = 0
        while len(buf) - off >= 4:
            (length,) = struct.unpack_from("<I", buf, off)
            if length > 4 * 1024 * 1024:
                raise ValueError("control message too large")
            if len(buf) - off < 4 + length:
                break
            if length:
                try:
                    message = json.loads(bytes(buf[off + 4 : off + 4 + length]))
                except json.JSONDecodeError:
                    message = {}
                if isinstance(message, dict):
                    asyncio.create_task(self._handle_control(message))
            off += 4 + length
        del buf[:off]

    async def _handle_control(self, message: dict) -> None:
        if message.get("id") == self._register_id and "method" not in message:
            if message.get("error") or message.get("ok", True) is False:
                self.last_error = "Muse refused this link's registration - try pairing again"
                self._status_cb("register_failed")
                if self._closed is not None:
                    self._closed.set()
            else:
                self.connected = self.registered = True
                self._status_cb("ready")
            return
        event = message.get("event", "")
        if event in ("link.unpaired", "node.unpaired"):
            self._status_cb("unpaired")
            if self._closed is not None:
                self._closed.set()
            return
        if message.get("method") == "link.invoke":
            invoke_id = message.get("id", "")
            command = message.get("command", "")
            params = message.get("params") or {}
            try:
                ok, payload, error = await self._invoke_cb(command, params)
            except Exception as err:  # noqa: BLE001
                ok, payload, error = False, None, str(err)
            reply: dict = {"method": "link.result", "id": invoke_id, "ok": ok}
            if payload is not None:
                reply["payload"] = payload
            if error:
                reply["error"] = error
            try:
                await self._send_control(reply)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("link.result send failed", exc_info=True)

    def _register_message(self, request_id: str) -> dict:
        identity = self._identity
        params = {
            "node_id": identity.node_id,
            "display_name": identity.ble_name,
            "platform": "homeassistant",
            "version": __version__,
            "device_family": "gadget",
            "model_id": "muse-gadget-addon",
            "metadata": {"model_name": self._name},
            "is_wakeup_supported": False,
            "commands_v2": self._commands,
        }
        return {"type": "req", "id": request_id, "method": "link.register", "params": params}

    # ------------------------------------------------------ HTTP over Noise
    @staticmethod
    def _headers(*extra: tuple[str, str]) -> list[tuple[str, str]]:
        headers = list(extra)
        headers += [("x-request-id", str(uuid.uuid4())), ("x-app-id", "musegadget")]
        return headers

    async def _send_frames(self, frames: list[bytes]) -> None:
        if self._ws is None:
            raise ConnectionError("not connected")
        async with self._send_lock:
            for frame in frames:
                await self._ws.send_bytes(frame)

    async def _send_control(self, message: dict) -> None:
        if self._transport is None:
            raise ConnectionError("not connected")
        raw = json.dumps(message).encode()
        framed = struct.pack("<I", len(raw)) + raw
        await self._send_frames(self._transport.body_chunk(self._control_stream, framed, False))

    async def request(self, method: str, path: str, body: bytes = b"",
                      headers: list[tuple[str, str]] | None = None,
                      timeout: float = 60) -> tuple[int, bytes]:
        """One HTTP request over the Noise transport (chat/history/voice)."""
        if self._transport is None:
            raise ConnectionError("not connected")
        encrypted = self._transport.http_request(method, path, headers or self._headers(), body)
        pending = _Pending()
        self._requests[encrypted.stream_id] = pending
        try:
            await self._send_frames(encrypted.frames)
            await asyncio.wait_for(pending.done.wait(), timeout=timeout)
        finally:
            self._requests.pop(encrypted.stream_id, None)
        if pending.error is not None:
            raise pending.error
        return pending.status, bytes(pending.body)

    async def _open_subscribe(self) -> None:
        try:
            if self._transport is None:
                return
            encrypted = self._transport.http_request(
                "POST", "/chat/subscribe",
                self._headers(("Content-Type", "application/json"),
                              ("Accept", "application/x-ndjson")),
                b"{}",
            )

            def _sink(frame) -> None:
                if frame.body:
                    self._ndjson_buf += frame.body.decode("utf-8", "replace")
                    while "\n" in self._ndjson_buf:
                        line, self._ndjson_buf = self._ndjson_buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict) and event.get("type") == "event":
                            self.tracker.on_event(event)
                            if self._chat_event_cb:
                                self._chat_event_cb(event)

            self._streams[encrypted.stream_id] = _sink
            await self._send_frames(encrypted.frames)
        except Exception:  # noqa: BLE001 - session loop reconnects
            _LOGGER.debug("chat subscribe failed", exc_info=True)

    # ------------------------------------------------------------ chat API
    async def send_chat(self, message: str, timeout: float = 60) -> dict:
        """Send one chat turn; returns {text, message_id} once done."""
        self.tracker.begin()
        body = json.dumps({
            "message": message,
            "output_modality": "voice",
            "device_id": self._identity.node_id,
            "device": "mcu-wearable",
        }).encode()
        status, raw = await self.request(
            "POST", "/chat/stream", body,
            self._headers(("Content-Type", "application/json")), timeout=timeout)
        if not 200 <= status < 300:
            raise ConnectionError(f"Muse rejected the message (HTTP {status})")
        try:
            ack = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            ack = {}
        user_id = str(ack.get("message_id") or ack.get("id") or "")
        if user_id:
            self.tracker.bind(user_id)
        deadline = time.monotonic() + timeout
        while not self.tracker.done and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return {"text": self.tracker.text, "message_id": self.tracker.message_id,
                "done": self.tracker.done}

    async def fetch_history(self, limit: int = 20) -> dict:
        status, raw = await self.request(
            "GET", f"/chat/history?limit={int(limit)}", b"", self._headers())
        if not 200 <= status < 300:
            raise ConnectionError(f"history failed (HTTP {status})")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {"history": data}

    async def stream_tts(self, message_id: str, timeout: float = 120) -> bytes:
        """Muse's voice (MP3) for one assistant message."""
        encoded = quote(message_id, safe="")
        for prefix in ("/api/voice/tts-stream", "/voice/tts-stream"):
            status, raw = await self.request(
                "GET", f"{prefix}?message_id={encoded}", b"",
                self._headers(("Accept", "audio/mpeg")), timeout=timeout)
            if status == 404 and prefix.startswith("/api"):
                continue
            if not 200 <= status < 300:
                raise ConnectionError(f"voice failed (HTTP {status})")
            return raw
        raise ConnectionError("voice not found")
