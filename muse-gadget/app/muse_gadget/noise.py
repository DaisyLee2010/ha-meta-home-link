"""Noise_XX_25519_AESGCM_SHA256 initiator.

Port of the Muse Gadget
SDK's ``noise_xx.py``). X25519/AES-GCM come from ``cryptography``, which
Home Assistant core already depends on; nothing else is required.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac_mod.new(key, data, hashlib.sha256).digest()


def hkdf2(ck: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    temp = _hmac(ck, ikm)
    o1 = _hmac(temp, b"\x01")
    o2 = _hmac(temp, o1 + b"\x02")
    return o1, o2


def hkdf_expand(prk: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 HKDF-Expand, as used by the pairing session."""
    return HKDFExpand(algorithm=hashes.SHA256(), length=length, info=info).derive(prk)


def hkdf_full(salt: bytes, ikm: bytes, info: bytes, length: int = 32) -> bytes:
    prk = _hmac(salt, ikm)
    return hkdf_expand(prk, info, length)


def _raw_public(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class CipherState:
    def __init__(self) -> None:
        self._key: bytes | None = None
        self._nonce = 0

    def set_key(self, key: bytes) -> None:
        self._key = key
        self._nonce = 0

    def has_key(self) -> bool:
        return self._key is not None

    def encrypt(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._key is None:
            return plaintext
        nonce = self._nonce
        self._nonce += 1
        # Java port: counter in the last 8 IV bytes, big-endian.
        return AESGCM(self._key).encrypt(nonce.to_bytes(12, "big"), plaintext, ad)

    def decrypt(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._key is None:
            return ciphertext
        nonce = self._nonce
        self._nonce += 1
        return AESGCM(self._key).decrypt(nonce.to_bytes(12, "big"), ciphertext, ad)


class _SymmetricState:
    def __init__(self) -> None:
        name = b"Noise_XX_25519_AESGCM_SHA256"
        padded = name.ljust(32, b"\x00")
        self.h = padded
        self.ck = padded
        self.cipher = CipherState()
        self.mix_hash(b"")

    def mix_hash(self, data: bytes) -> None:
        self.h = sha256(self.h, data)

    def mix_key(self, ikm: bytes) -> None:
        self.ck, key = hkdf2(self.ck, ikm)
        self.cipher.set_key(key)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ct = self.cipher.encrypt(self.h, plaintext)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        pt = self.cipher.decrypt(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return pt

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = hkdf2(self.ck, b"")
        c1, c2 = CipherState(), CipherState()
        c1.set_key(k1)
        c2.set_key(k2)
        return c1, c2


class Initiator:
    """Noise XX initiator, mirroring the SDK Noise XX initiator."""

    def __init__(self) -> None:
        self._ss = _SymmetricState()
        self._e: X25519PrivateKey | None = None
        self._s: X25519PrivateKey | None = None
        self._re: X25519PublicKey | None = None
        self._rs: X25519PublicKey | None = None

    def write_message_1(self) -> bytes:
        self._e = X25519PrivateKey.generate()
        e_pub = _raw_public(self._e)
        self._ss.mix_hash(e_pub)
        self._ss.encrypt_and_hash(b"")
        return e_pub

    def read_message_2(self, msg: bytes) -> bytes:
        if len(msg) < 32 + 48 + 16:
            raise ValueError("Noise message 2 too short")
        re_raw = msg[:32]
        self._re = X25519PublicKey.from_public_bytes(re_raw)
        self._ss.mix_hash(re_raw)
        self._ss.mix_key(self._e.exchange(self._re))
        rs_raw = self._ss.decrypt_and_hash(msg[32:80])
        self._rs = X25519PublicKey.from_public_bytes(rs_raw)
        self._ss.mix_key(self._e.exchange(self._rs))
        return self._ss.decrypt_and_hash(msg[80:])

    def write_message_3(self) -> bytes:
        self._s = X25519PrivateKey.generate()
        enc_s = self._ss.encrypt_and_hash(_raw_public(self._s))
        self._ss.mix_key(self._s.exchange(self._re))
        enc_payload = self._ss.encrypt_and_hash(b"")
        return enc_s + enc_payload

    def split(self) -> tuple[CipherState, CipherState]:
        return self._ss.split()
