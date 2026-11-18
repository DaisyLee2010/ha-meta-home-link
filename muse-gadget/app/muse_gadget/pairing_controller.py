"""Pairing setup controller: BLE bytes in, pairing statuses out.

Transport-agnostic port of Portal Muse's ``Pairing.Controller`` /
the SDK's ``ble_setup.py``. A BLE layer (ble_bluez) feeds it packets
and sends what ``send_packets`` hands back; provisioning verifies the
credentials (fetch_vms) before they are saved.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable

import aiohttp

from .gadget_link import api_root, fetch_vms
from .pairing import ChunkAssembler, PairingSession, Status, encode_chunks

_LOGGER = logging.getLogger(__name__)
_SENSITIVE = ("provision", "provision_v2", "wifi_scan", "ota",
              "device.ota", "unpair", "set_wifi", "set_auth")
_PLAINTEXT_ERRORS = ("error_encryption_required", "error_pairing_invalid_hello",
                     "error_pairing_unavailable", "error_pairing_decrypt")


class PairingController:
    def __init__(self, session: PairingSession, *, send_packets: Callable[[list[bytes]], None],
                 mtu: Callable[[], int], disconnect: Callable[[float], None],
                 is_online: Callable[[], bool],
                 current_network: Callable[[], dict | None],
                 prepare_save, on_complete: Callable[[], None],
                 version: str) -> None:
        self._session = session
        self._send_packets = send_packets
        self._mtu = mtu
        self._disconnect = disconnect
        self._is_online = is_online
        self._current_network = current_network
        self._prepare_save = prepare_save  # async (credentials) -> commit callable
        self._on_complete = on_complete
        self._version = version
        self._assembler = ChunkAssembler()
        self._plaintext_blocked = False
        self._provisioning = False

    # -------------------------------------------------------------- input
    def on_packet(self, packet: bytes) -> None:
        message = self._assembler.feed(packet)
        if message is not None:
            self.handle_message(message, decrypted=False)

    def on_disconnect(self) -> None:
        """Session keys survive briefly, as in the SDK."""

    def handle_message(self, raw: bytes, decrypted: bool) -> None:
        try:
            command = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_status("error_invalid_command", 0)
            return
        if not isinstance(command, dict):
            self._send_status("error_invalid_command", 0)
            return
        action = str(command.get("action", ""))
        try:
            if not decrypted and action == "pairing_client_hello":
                ready = self._session.handle_hello(command)
                self._plaintext_blocked = True
                self._send_json(ready)
            elif not decrypted and action == "pairing_encrypted":
                try:
                    plain = self._session.decrypt(command)
                except Status as err:
                    self._send_status(err.status, 0)
                    self._disconnect(0.3)
                    return
                self.handle_message(plain.encode(), decrypted=True)
            elif action == "get_device_info":
                info = {"type": "device_info", "node_id": self._session.node_id,
                        "version": self._version, **self._session.device_info(),
                        "build_sha": "", "network_ready": self._is_online()}
                self._send_json(info)
            elif not decrypted and self._plaintext_blocked:
                return  # ignored, as in the SDK
            elif not decrypted and action in _SENSITIVE:
                self._send_status("error_encryption_required", 0)
            elif decrypted and action == "pairing_client_finished":
                generation = self._session.handle_client_finished(command)
                if generation == 0:
                    self._send_status("error_pairing_decrypt", 0)
                    self._disconnect(0.3)
                else:
                    self._send_status("pairing_confirmed", generation)
            elif decrypted and action in _SENSITIVE and not self._session.confirmed():
                self._send_status("error_pairing_confirm_required", 0)
            elif decrypted and action == "wifi_scan":
                networks = []
                entry = self._current_network() if self._is_online() else None
                if entry:
                    networks.append(entry)
                self._send_encrypted({"type": "wifi_scan_result", "networks": networks}, 0)
            elif decrypted and action == "provision_v2":
                self._handle_provision(command)
            else:
                self._send_status("error_unknown_action", 0)
        except Status as err:
            self._send_status(err.status, 0)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("pairing message failed", exc_info=True)
            self._send_status("error_invalid_command", 0)

    # --------------------------------------------------------- provision
    def _handle_provision(self, command: dict) -> None:
        access = str(command.get("access_token") or "")
        refresh = str(command.get("refresh_token") or "")
        if not isinstance(command.get("ssid"), str) \
                or not isinstance(command.get("password"), str) \
                or not access or not refresh \
                or command.get("token_type") != "device":
            self._send_status("error_missing_credentials", 0)
            return
        if self._provisioning:
            self._send_status("error_operation_in_progress", 0)
            return
        generation = self._session.mark_provisioning()
        if generation == 0:
            self._send_status("error_pairing_confirm_required", 0)
            return
        self._provisioning = True
        credentials = {key: str(command.get(key) or "") for key in (
            "access_token", "refresh_token", "username",
            "api_url", "api_url_v2", "noise_host")}
        import asyncio
        asyncio.ensure_future(self._provision(generation, credentials))

    async def _provision(self, generation: int, credentials: dict) -> None:
        try:
            self._send_status("wifi_connecting", generation)
            if not self._is_online():
                self._send_status("wifi_failed", generation)
                return
            self._send_status("wifi_connected", generation)
            # Verify before saving: the credentials must fetch a VM.
            async with aiohttp.ClientSession() as session:
                vms = await fetch_vms(session, credentials["access_token"],
                                       api_root(credentials))
            if not vms:
                self._send_status("auth_failed", generation)
                self._disconnect(0.5)
                return
            commit = await self._prepare_save(credentials)

            def _commit() -> None:
                commit(credentials)

            if not self._session.commit_provisioning(generation, _commit):
                self._send_status("error_storage", generation)
                self._disconnect(0.5)
                return
            self._send_status("auth_ok", generation)
            self._on_complete()
        except PermissionError:
            self._send_status("auth_failed", generation)
            self._disconnect(0.5)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("provisioning failed", exc_info=True)
            self._send_status("auth_failed", generation)
            self._disconnect(0.5)
        finally:
            self._provisioning = False

    # -------------------------------------------------------------- output
    def _send_status(self, status: str, generation: int) -> None:
        envelope = self._session.encrypt_status(status, generation)
        if envelope is not None:
            self._send_raw(json.dumps(envelope))
            return
        if generation != 0 or self._plaintext_blocked:
            return
        if status not in _PLAINTEXT_ERRORS:
            return
        self._send_packets([status.encode()])

    def _send_json(self, payload: dict) -> None:
        self._send_raw(json.dumps(payload))

    def _send_encrypted(self, payload: dict, generation: int) -> None:
        envelope = self._session.encrypt_json(json.dumps(payload), generation)
        if envelope is not None:
            self._send_raw(json.dumps(envelope))

    def _send_raw(self, text: str) -> None:
        self._send_packets(encode_chunks(text.encode(), self._mtu()))
