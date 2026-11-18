"""Noise transport protobuf envelope.

Port of the Muse Gadget
SDK's ``musegadget/noise/{_proto,envelope,framing,transport}.py``).
Pure Python, no Home Assistant imports, so it is unit-testable alone.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

MAX_CHUNK = 65489


# ---------------------------------------------------------------- protobuf
def _varint(value: int) -> bytes:
    out = bytearray()
    v = value & ((1 << 64) - 1)
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def _var_field(field_no: int, value: int) -> bytes:
    return _key(field_no, 0) + _varint(value) if value else b""


def _bytes_field(field_no: int, data: bytes | None) -> bytes:
    if not data:
        return b""
    return _key(field_no, 2) + _varint(len(data)) + data


def _str_field(field_no: int, text: str | None) -> bytes:
    return _bytes_field(field_no, text.encode() if text else None)


def _bool_field(field_no: int, value: bool) -> bytes:
    return _key(field_no, 0) + _varint(1) if value else b""


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.off = 0

    def done(self) -> bool:
        return self.off >= len(self.d)

    def varint(self) -> int:
        v = 0
        shift = 0
        while self.off < len(self.d):
            b = self.d[self.off]
            self.off += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                return v
            shift += 7
        raise ValueError("truncated varint")

    def key(self) -> tuple[int, int]:
        k = self.varint()
        return k >> 3, k & 7

    def bytes(self) -> bytes:
        length = self.varint()
        out = self.d[self.off : self.off + length]
        self.off += length
        return out

    def skip(self, wire: int) -> None:
        if wire == 0:
            self.varint()
        elif wire == 1:
            self.off += 8
        elif wire == 2:
            self.off += self.varint()
        elif wire == 5:
            self.off += 4
        else:
            raise ValueError(f"bad wire type {wire}")


# ------------------------------------------------------- service envelope
def encode_request_frame(stream_id, verb, path, headers, body, end_body):
    req = _str_field(1, verb) + _str_field(2, path)
    for name, value in headers or []:
        req += _bytes_field(3, _str_field(1, name) + _str_field(2, value))
    req += _bytes_field(4, body) + _bool_field(5, end_body)
    frame = _var_field(1, stream_id) + _bytes_field(2, req)
    return _bytes_field(2, frame)  # service DAEMON=0 omitted, payload field 2


def encode_body_chunk_frame(stream_id, data, end_body):
    chunk = _bytes_field(1, data) + _bool_field(2, end_body)
    frame = _var_field(1, stream_id) + _bytes_field(4, chunk)
    return _bytes_field(2, frame)


def encode_reset_frame(stream_id):
    frame = _var_field(1, stream_id) + _bytes_field(5, _var_field(1, 1))
    return _bytes_field(2, frame)


@dataclass
class Frame:
    stream_id: int = 0
    kind: str = ""
    status: int = 0
    body: bytes | None = None
    end_body: bool = False


def decode_service_frame(payload: bytes) -> Frame:
    f = Frame()
    r = _Reader(payload)
    while not r.done():
        field_no, wire = r.key()
        if field_no == 1:
            f.stream_id = r.varint()
        elif field_no == 3:
            f.kind = "response"
            _parse_response(r.bytes(), f)
        elif field_no == 4:
            f.kind = "body_chunk"
            _parse_chunk(r.bytes(), f)
        elif field_no == 5:
            f.kind = "reset"
            r.skip(2)
        elif field_no == 2:
            f.kind = "request"
            r.skip(2)
        else:
            r.skip(wire)
    return f


def _parse_response(data: bytes, f: Frame) -> None:
    r = _Reader(data)
    while not r.done():
        field_no, wire = r.key()
        if field_no == 1:
            f.status = r.varint()
        elif field_no == 3:
            f.body = r.bytes()
        elif field_no == 4:
            f.end_body = r.varint() != 0
        else:
            r.skip(wire)


def _parse_chunk(data: bytes, f: Frame) -> None:
    r = _Reader(data)
    while not r.done():
        field_no, wire = r.key()
        if field_no == 1:
            f.body = r.bytes()
        elif field_no == 2:
            f.end_body = r.varint() != 0
        else:
            r.skip(wire)


def decode_service_response(data: bytes) -> bytes:
    r = _Reader(data)
    payload = b""
    while not r.done():
        field_no, wire = r.key()
        if field_no == 1:
            payload = r.bytes()
        else:
            r.skip(wire)
    return payload


# -------------------------------------------------------- noise chunking
def encode_noise_frames(data: bytes) -> list[bytes]:
    chunk_id = secrets.randbits(63)
    total = max(1, (len(data) + MAX_CHUNK - 1) // MAX_CHUNK)
    out = []
    for i in range(total):
        part = data[i * MAX_CHUNK : (i + 1) * MAX_CHUNK]
        frame = _var_field(1, chunk_id) + _var_field(2, i) + _var_field(3, total)
        out.append(frame + _bytes_field(4, part))
    return out


class FrameDecoder:
    def __init__(self) -> None:
        self._pending: dict[int, list[bytes | None]] = {}

    def decode(self, frame_bytes: bytes) -> bytes | None:
        r = _Reader(frame_bytes)
        chunk_id, index, total, payload = 0, 0, 1, b""
        while not r.done():
            field_no, wire = r.key()
            if field_no == 1:
                chunk_id = r.varint()
            elif field_no == 2:
                index = r.varint()
            elif field_no == 3:
                total = r.varint()
            elif field_no == 4:
                payload = r.bytes()
            else:
                r.skip(wire)
        if total <= 1:
            return payload
        chunks = self._pending.setdefault(chunk_id, [None] * total)
        chunks[index] = payload
        if any(c is None for c in chunks):
            return None
        del self._pending[chunk_id]
        return b"".join(c for c in chunks if c is not None)


# -------------------------------------------------------------- transport
@dataclass
class Encrypted:
    stream_id: int
    frames: list[bytes] = field(default_factory=list)


class Transport:
    """Encrypts service envelopes with the post-handshake cipher pair."""

    def __init__(self, send_cipher, recv_cipher) -> None:
        self._send = send_cipher
        self._recv = recv_cipher
        self._decoder = FrameDecoder()
        self._next_stream_id = 1

    def _encrypt_all(self, svc_bytes: bytes) -> list[bytes]:
        return [self._send.encrypt(b"", f) for f in encode_noise_frames(svc_bytes)]

    def http_request(self, verb, path, headers, body) -> Encrypted:
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        svc = encode_request_frame(stream_id, verb, path, headers, body, True)
        return Encrypted(stream_id, self._encrypt_all(svc))

    def stream_request(self, verb, path, headers) -> Encrypted:
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        svc = encode_request_frame(stream_id, verb, path, headers, b"", False)
        return Encrypted(stream_id, self._encrypt_all(svc))

    def body_chunk(self, stream_id, data, end_body) -> list[bytes]:
        return self._encrypt_all(encode_body_chunk_frame(stream_id, data, end_body))

    def reset(self, stream_id) -> list[bytes]:
        return self._encrypt_all(encode_reset_frame(stream_id))

    def decrypt_frame(self, ciphertext: bytes) -> Frame | None:
        plain = self._recv.decrypt(b"", ciphertext)
        reassembled = self._decoder.decode(plain)
        if reassembled is None:
            return None
        payload = decode_service_response(reassembled)
        if not payload:
            raise ValueError("empty service payload")
        return decode_service_frame(payload)
