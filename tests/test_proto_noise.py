"""Protocol chain tests: protobuf envelope, noise chunking, Noise XX."""
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from meta_home_link import proto
from meta_home_link.noise import CipherState, Initiator, _raw_public, hkdf2
from meta_home_link.noise import _SymmetricState


def _response_envelope(stream_id, status, body, end_body=True):
    response = proto._var_field(1, status) + proto._bytes_field(3, body) + proto._bool_field(4, end_body)
    frame = proto._var_field(1, stream_id) + proto._bytes_field(3, response)
    return proto._bytes_field(1, frame)


def test_transport_decrypts_server_response_frame():
    key = bytes(range(32))
    send, recv = CipherState(), CipherState()
    send.set_key(key)
    recv.set_key(key)
    transport = proto.Transport(CipherState(), recv)
    envelope = _response_envelope(7, 200, b"hello")
    wire = [send.encrypt(b"", f) for f in proto.encode_noise_frames(envelope)]
    frame = next(f for f in (transport.decrypt_frame(w) for w in wire) if f is not None)
    assert frame.stream_id == 7 and frame.status == 200 and frame.body == b"hello"


def test_frame_decoder_reassembles_large_payload():
    payload = b"x" * (proto.MAX_CHUNK * 2 + 10)
    decoder = proto.FrameDecoder()
    frames = proto.encode_noise_frames(payload)
    assert len(frames) == 3
    assert decoder.decode(frames[0]) is None
    assert decoder.decode(frames[1]) is None
    assert decoder.decode(frames[2]) == payload


def test_request_frame_shape():
    svc = proto.encode_request_frame(3, "POST", "/link-control", [("x-app-id", "musegadget")], b"", False)
    reader = proto._Reader(svc)
    field_no, _ = reader.key()
    assert field_no == 2  # outgoing service envelope: payload in field 2


class _Responder:
    """Minimal Noise XX responder, the mirror of Initiator."""

    def __init__(self):
        self._ss = _SymmetricState()
        self._s = X25519PrivateKey.generate()
        self._e = None
        self._re = None

    def read_message_1(self, msg):
        self._re = X25519PublicKey.from_public_bytes(msg)
        self._ss.mix_hash(msg)
        self._ss.decrypt_and_hash(b"")

    def write_message_2(self):
        self._e = X25519PrivateKey.generate()
        e_pub = _raw_public(self._e)
        self._ss.mix_hash(e_pub)
        self._ss.mix_key(self._e.exchange(self._re))
        enc_s = self._ss.encrypt_and_hash(_raw_public(self._s))
        self._ss.mix_key(self._s.exchange(self._re))
        return e_pub + enc_s + self._ss.encrypt_and_hash(b"")

    def read_message_3(self, msg):
        peer_raw = self._ss.decrypt_and_hash(msg[:48])
        peer = X25519PublicKey.from_public_bytes(peer_raw)
        self._ss.mix_key(self._e.exchange(peer))
        self._ss.decrypt_and_hash(msg[48:])

    def split(self):
        send, recv = self._ss.split()
        return recv, send  # responder: send=c2, recv=c1 (see test below)


def test_noise_xx_handshake_and_bidirectional_transport():
    initiator, responder = Initiator(), _Responder()
    responder.read_message_1(initiator.write_message_1())
    initiator.read_message_2(responder.write_message_2())
    responder.read_message_3(initiator.write_message_3())
    i_send, i_recv = initiator.split()
    r_send, r_recv = responder.split()
    # Both sides derive the same pair: initiator sends on c1, responder on c2.
    client = proto.Transport(i_send, i_recv)

    request = client.http_request("POST", "/link-control", [("x-app-id", "musegadget")], b"")
    plain = [r_recv.decrypt(b"", f) for f in request.frames]
    decoder = proto.FrameDecoder()
    payload = next(d for d in (decoder.decode(p) for p in plain) if d)
    assert b"/link-control" in payload

    envelope = _response_envelope(request.stream_id, 200, b"ok")
    wire = [r_send.encrypt(b"", f) for f in proto.encode_noise_frames(envelope)]
    frame = next(f for f in (client.decrypt_frame(w) for w in wire) if f)
    assert frame.status == 200 and frame.body == b"ok"


def test_hkdf2_is_deterministic_and_distinct():
    a1, a2 = hkdf2(b"\x00" * 32, b"ikm")
    b1, _ = hkdf2(b"\x00" * 32, b"other")
    assert a1 != a2 and a1 != b1 and len(a1) == 32
