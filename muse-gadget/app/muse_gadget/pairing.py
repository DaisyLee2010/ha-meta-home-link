"""BLE pairing session crypto, protocol v5 (community / confirm_app).

Port of the SDK's
``musegadget/{pairing,ble_setup}.py``): P-256 ECDH, HKDF-SHA256 session
keys, AES-256-GCM records. Transport-agnostic: a BLE GATT layer feeds it
packets and sends what it returns. Needs ``cryptography`` (HA core dep).
"""
from __future__ import annotations

import base64
import os
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .noise import hkdf_expand, hkdf_full, sha256

MODEL = "hatch_link"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_chunks(data: bytes, mtu: int) -> list[bytes]:
    """Chunk framing (0xFE, index, total), as in ble_framing.py."""
    notify_max = min(mtu - 3 if mtu > 3 else 20, 160)
    usable = max(1, notify_max - 3)
    total = max(1, (len(data) + usable - 1) // usable)
    out = []
    for i in range(total):
        part = data[i * usable : (i + 1) * usable]
        out.append(bytes([0xFE, i, total]) + part)
    return out


class ChunkAssembler:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._total = 0
        self._next = 0

    def feed(self, packet: bytes) -> bytes | None:
        if len(packet) < 3 or packet[0] != 0xFE:
            return packet
        index, total = packet[1], packet[2]
        if total == 0:
            self._reset()
            return None
        if index == 0:
            self._reset()
            self._total = total
        if index != self._next or index >= self._total:
            self._reset()
            return None
        self._buf += packet[3:]
        self._next = index + 1
        if self._next < self._total:
            return None
        msg = bytes(self._buf)
        self._reset()
        return msg

    def _reset(self) -> None:
        self._buf.clear()
        self._total = 0
        self._next = 0


class Status(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _encode_point(pub: ec.EllipticCurvePublicKey) -> bytes:
    numbers = pub.public_numbers()
    return b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


class PairingSession:
    """One pairing session; mirrors ``Pairing.Session`` states 0-3."""

    def __init__(self, node_id: str, device_id: str, mac: str, firmware: str, sdk_token: str | None):
        self.node_id, self.device_id, self.mac = node_id, device_id, mac
        self.firmware, self.sdk_token = firmware, sdk_token
        self._key: ec.EllipticCurvePrivateKey | None = None
        self._rx_key: bytes | None = None
        self._tx_key: bytes | None = None
        self._session_id = ""
        self._rx_counter = 0
        self._tx_counter = 0
        self._state = 0  # 0 idle, 1 wait-finished, 2 ready, 3 provisioning
        self._deadline = 0.0
        self._generation = 0

    def confirmed(self) -> bool:
        self._expire()
        return self._state in (2, 3)

    def device_info(self) -> dict:
        return {
            "device_id": self.device_id, "mac": self.mac, "model": MODEL,
            "pairing_protocol": 5, "pairing_auth": "none",
            "pairing_auth_epoch": 0, "pairing_policy": "confirm_app",
        }

    def handle_hello(self, msg: dict) -> dict:
        if msg.get("version") != 5 or msg.get("pairing_auth") != "none" \
                or msg.get("pairing_policy") != "confirm_app":
            raise Status("error_pairing_invalid_hello")
        self._reset_locked()
        try:
            mobile_pub_raw = b64url_decode(msg["mobile_pub"])
            mobile_nonce = b64url_decode(msg["mobile_nonce"])
            if len(mobile_pub_raw) != 65 or mobile_pub_raw[0] != 4 or len(mobile_nonce) != 16:
                raise ValueError("bad hello")
            mobile_pub = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), mobile_pub_raw)
            self._key = ec.generate_private_key(ec.SECP256R1())
            device_pub = _encode_point(self._key.public_key())
            device_nonce = os.urandom(16)
            transcript = "\n".join([
                "hatch-link-pairing-v5", "version=5",
                "initiator_role=mobile", "responder_role=link",
                f"device_id={self.device_id}", f"node_id={self.node_id}",
                f"mac={self.mac}", f"model={MODEL}",
                f"firmware_version={self.firmware}",
                "selected_cipher_suite=p256-hkdf-sha256-aes-gcm-v1",
                "pairing_auth=none", "pairing_auth_epoch=0",
                "pairing_policy=confirm_app", "confirm_timeout_seconds=0",
                f"mobile_pub={b64url(mobile_pub_raw)}",
                f"device_pub={b64url(device_pub)}",
                f"mobile_nonce={b64url(mobile_nonce)}",
                f"device_nonce={b64url(device_nonce)}",
            ])
            transcript_hash = sha256(transcript.encode())
            secret = self._key.exchange(ec.ECDH(), mobile_pub)
            salt = sha256(mobile_nonce, device_nonce, transcript_hash)
            session_secret = hkdf_full(salt, secret, b"hatch-link ble setup v1", 32)
            self._rx_key = hkdf_expand(session_secret, b"mobile->device", 32)
            self._tx_key = hkdf_expand(session_secret, b"device->mobile", 32)
            sid = sha256(b"hatch-link session id v1", transcript_hash, secret)
            self._session_id = b64url(sid[:16])
            self._rx_counter = self._tx_counter = 0
            self._state = 1
            self._deadline = time.time() + 60
            return {
                "type": "pairing_ready", "version": 5,
                "device_id": self.device_id, "node_id": self.node_id,
                "mac": self.mac, "model": MODEL,
                "firmware_version": self.firmware,
                "pairing_auth": "none", "pairing_auth_epoch": 0,
                "pairing_policy": "confirm_app",
                "device_pub": b64url(device_pub),
                "device_nonce": b64url(device_nonce),
                "transcript_hash": b64url(transcript_hash),
                "session_id": self._session_id,
            }
        except Status:
            raise
        except Exception as err:
            self._reset_locked()
            raise Status("error_pairing_invalid_hello") from err

    def decrypt(self, envelope: dict) -> str:
        if self._expire() or self._state == 0:
            self._reset_locked()
            raise Status("error_pairing_decrypt")
        try:
            if envelope["session_id"] != self._session_id:
                raise ValueError("session")
            counter = int(envelope["counter"])
            if counter != self._rx_counter:
                raise ValueError("counter")
            sealed = b64url_decode(envelope["ciphertext"]) + b64url_decode(envelope["tag"])
            pt = self._aes_record(self._rx_key, 0, counter, sealed, decrypt=True)
            self._rx_counter += 1
            return pt.decode()
        except Exception as err:
            self._reset_locked()
            raise Status("error_pairing_decrypt") from err

    def handle_client_finished(self, command: dict) -> int:
        ok = len(command) == 1 and command.get("action") == "pairing_client_finished" \
            and not self._expire() and self._state == 1 and self._rx_counter == 1
        if not ok:
            self._reset_locked()
            return 0
        self._generation += 1
        self._state = 2
        self._deadline = time.time() + 120
        return self._generation

    def mark_provisioning(self) -> int:
        if not self._expire() and self._state == 2:
            self._generation += 1
            self._state = 3
            self._deadline = time.time() + 120
        return self._generation if self._state == 3 else 0

    def commit_provisioning(self, generation: int, commit) -> bool:
        if not generation or generation != self._generation or self._expire() or self._state != 3:
            return False
        commit()
        return True

    def encrypt_json(self, plaintext: str, generation: int = 0) -> dict | None:
        try:
            if (generation and generation != self._generation) or self._expire() or self._state == 0:
                return None
            counter = self._tx_counter
            sealed = self._aes_record(self._tx_key, 1, counter, plaintext.encode(), decrypt=False)
            self._tx_counter += 1
            return {
                "type": "pairing_encrypted", "session_id": self._session_id,
                "counter": str(counter),
                "ciphertext": b64url(sealed[:-16]), "tag": b64url(sealed[-16:]),
            }
        except Exception:
            return None

    def encrypt_status(self, status: str, generation: int = 0) -> dict | None:
        import json
        msg: dict = {"type": "status", "status": status}
        if self.sdk_token and status == "pairing_confirmed":
            msg["sdk_token"] = self.sdk_token
        return self.encrypt_json(json.dumps(msg), generation)

    def _aes_record(self, key, direction, counter, data, decrypt):
        iv = bytes([direction]) + b"\x00\x00\x00" + counter.to_bytes(8, "big")
        aad = f"hatch-link ble setup v1|{self._session_id}|{'m2d' if direction == 0 else 'd2m'}|{counter}".encode()
        aes = AESGCM(key)
        return aes.decrypt(iv, data, aad) if decrypt else aes.encrypt(iv, data, aad)

    def _expire(self) -> bool:
        if self._state == 0 or time.time() <= self._deadline:
            return False
        self._clear_locked()
        return True

    def _reset_locked(self) -> None:
        self._generation += 1
        self._clear_locked()

    def _clear_locked(self) -> None:
        self._state = 0
        self._deadline = 0.0
        self._rx_key = self._tx_key = None
        self._session_id = ""
        self._rx_counter = self._tx_counter = 0
