"""Async Muse gadget link: fetch_vms, Noise_XX over wss, link.register.

Port of the Muse Gadget SDK session layer (see NOTICE). The chat /
voice-stream paths are not ported: this gadget only receives
``link.invoke`` commands (device.guide, device.health, home.*) and
answers them, which is all a Home Assistant bridge needs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from .const import DEFAULT_API_ROOT, DEFAULT_NOISE_HOST, VERSION
from .noise import Initiator
from .proto import Transport

_LOGGER = logging.getLogger(__name__)
USER_AGENT = f"musegadget/{VERSION} (Meta Home Link; Home Assistant)"


@dataclass(frozen=True)
class Identity:
    mac: str

    @property
    def suffix(self) -> str:
        return self.mac.replace(":", "")[6:]

    @property
    def node_id(self) -> str:
        return f"homelink-{self.suffix}"

    @property
    def device_id(self) -> str:
        return f"hatch-link:{self.mac}"

    @property
    def ble_name(self) -> str:
        return f"MuseGadget{self.suffix.upper()}"


def generate_identity() -> Identity:
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] & 0xFC) | 0x02  # locally administered, unicast
    return Identity(":".join(f"{b:02x}" for b in raw))


def api_root(pairing: dict | None) -> str:
    v2 = (pairing or {}).get("api_url_v2") or ""
    return (v2 if v2.startswith("https://") else DEFAULT_API_ROOT).rstrip("/")


def valid_sdk_token(token: str | None) -> bool:
    return bool(token) and token.startswith("mgst_") and len(token) > 8


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


class MuseLinkClient:
    """One gadget session at a time; ``run_forever`` reconnects."""

    def __init__(self, *, pairing: dict, identity: Identity, sdk_token: str | None,
                 instance_name: str, commands: dict, invoke_cb, status_cb,
                 save_pairing_cb=None) -> None:
        self._pairing = pairing
        self._identity = identity
        self._sdk_token = sdk_token
        self._name = instance_name
        self._commands = commands
        self._invoke_cb = invoke_cb  # async (command, params) -> (ok, payload, error)
        self._status_cb = status_cb  # (status: str) -> None
        self._save_pairing_cb = save_pairing_cb
        self.connected = False
        self.registered = False
        self.started_at = time.monotonic()
        self.last_error: str | None = None
        self._transport: Transport | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._control_stream = -1
        self._control_buf = bytearray()
        self._register_id = ""
        self._send_lock = asyncio.Lock()
        self._closed: asyncio.Event | None = None

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
            # This gadget answers invokes only; other streams are ignored.
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
            "version": VERSION,
            "device_family": "gadget",
            "model_id": "meta-home-link",
            "metadata": {"model_name": self._name},
            "is_wakeup_supported": False,
            "commands_v2": self._commands,
        }
        return {"type": "req", "id": request_id, "method": "link.register", "params": params}

    # ---------------------------------------------------------------- wire
    @staticmethod
    def _headers() -> list[tuple[str, str]]:
        return [("x-request-id", str(uuid.uuid4())), ("x-app-id", "musegadget")]

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
