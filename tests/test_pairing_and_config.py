"""Pairing-session crypto (mobile side recomputed independently) + config helpers."""
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from meta_home_link.config_helpers import merge_friendly_names, parse_pairing_credentials, valid_sdk_token
from meta_home_link.muse_link import Identity, generate_identity
from meta_home_link.noise import hkdf_full, sha256
from meta_home_link.pairing import ChunkAssembler, PairingSession, b64url, b64url_decode, encode_chunks


def test_chunks_roundtrip():
    data = json.dumps({"hello": "x" * 500}).encode()
    assembler = ChunkAssembler()
    out = None
    for packet in encode_chunks(data, 100):
        result = assembler.feed(packet)
        if result is not None:
            out = result
    assert out == data


def test_identity_shape_matches_portal_conventions():
    identity = generate_identity()
    assert identity.node_id.startswith("homelink-")
    assert identity.device_id == f"hatch-link:{identity.mac}"
    assert identity.ble_name.startswith("MuseGadget")
    fixed = Identity("aa:bb:cc:dd:ee:ff")
    assert fixed.node_id == "homelink-ddeeff"
    assert fixed.ble_name == "MuseGadgetDDEEFF"


def test_pairing_session_full_handshake():
    session = PairingSession("homelink-aabbcc", "hatch-link:aa:bb:cc:dd:ee:ff",
                             "aa:bb:cc:dd:ee:ff", "1.0.0", "mgst_testtoken123")
    mobile = ec.generate_private_key(ec.SECP256R1())
    mobile_pub_raw = b"\x04" + mobile.public_key().public_numbers().x.to_bytes(32, "big") \
        + mobile.public_key().public_numbers().y.to_bytes(32, "big")
    mobile_nonce = bytes(range(16))
    ready = session.handle_hello({
        "version": 5, "pairing_auth": "none", "pairing_policy": "confirm_app",
        "mobile_pub": b64url(mobile_pub_raw), "mobile_nonce": b64url(mobile_nonce),
    })
    assert ready["type"] == "pairing_ready"

    # Mobile side: recompute session keys from the transcript alone.
    device_pub_raw = b64url_decode(ready["device_pub"])
    device_nonce = b64url_decode(ready["device_nonce"])
    device_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), device_pub_raw)
    secret = mobile.exchange(ec.ECDH(), device_pub)
    transcript = "\n".join([
        "hatch-link-pairing-v5", "version=5", "initiator_role=mobile", "responder_role=link",
        f"device_id={session.device_id}", f"node_id={session.node_id}", f"mac={session.mac}",
        "model=hatch_link", "firmware_version=1.0.0",
        "selected_cipher_suite=p256-hkdf-sha256-aes-gcm-v1",
        "pairing_auth=none", "pairing_auth_epoch=0", "pairing_policy=confirm_app",
        "confirm_timeout_seconds=0",
        f"mobile_pub={b64url(mobile_pub_raw)}", f"device_pub={b64url(device_pub_raw)}",
        f"mobile_nonce={b64url(mobile_nonce)}", f"device_nonce={b64url(device_nonce)}",
    ])
    transcript_hash = sha256(transcript.encode())
    assert b64url(transcript_hash) == ready["transcript_hash"]
    salt = sha256(mobile_nonce, device_nonce, transcript_hash)
    session_secret = hkdf_full(salt, secret, b"hatch-link ble setup v1", 32)
    expand = lambda info: HKDFExpand(algorithm=hashes.SHA256(), length=32, info=info).derive(session_secret)
    m2d, d2m = expand(b"mobile->device"), expand(b"device->mobile")
    sid = ready["session_id"]

    def mobile_encrypt(counter, plaintext):
        iv = bytes([0]) + b"\x00\x00\x00" + counter.to_bytes(8, "big")
        aad = f"hatch-link ble setup v1|{sid}|m2d|{counter}".encode()
        sealed = AESGCM(m2d).encrypt(iv, plaintext, aad)
        return {"session_id": sid, "counter": str(counter),
                "ciphertext": b64url(sealed[:-16]), "tag": b64url(sealed[-16:])}

    plain = session.decrypt(mobile_encrypt(0, b'{"action":"pairing_client_finished"}'))
    generation = session.handle_client_finished(json.loads(plain))
    assert generation > 0 and session.confirmed()

    envelope = session.encrypt_status("pairing_confirmed", generation)
    iv = bytes([1]) + b"\x00\x00\x00" + int(envelope["counter"]).to_bytes(8, "big")
    aad = f"hatch-link ble setup v1|{sid}|d2m|{envelope['counter']}".encode()
    sealed = b64url_decode(envelope["ciphertext"]) + b64url_decode(envelope["tag"])
    status = json.loads(AESGCM(d2m).decrypt(iv, sealed, aad))
    assert status == {"type": "status", "status": "pairing_confirmed", "sdk_token": "mgst_testtoken123"}


def test_parse_pairing_credentials():
    pairing, err = parse_pairing_credentials('{"access_token":"a","refresh_token":"r"}')
    assert err is None and pairing["access_token"] == "a" and pairing["noise_host"]
    assert parse_pairing_credentials("")[1] == "credentials_required"
    assert parse_pairing_credentials("not json")[1] == "credentials_invalid"
    assert parse_pairing_credentials('{"access_token":"a"}')[1] == "credentials_incomplete"
    assert valid_sdk_token("mgst_123456789") and not valid_sdk_token("nope")


def test_merge_friendly_names():
    merged = merge_friendly_names(
        ["light.k", "light.b"],
        {"light.k": "Kitchen Light", "light.b": "Bedroom lamp", "light.gone": "Ghost"},
        {"light.b": "Bedroom lamp"},
    )
    # Custom alias kept; name identical to HA name dropped; stale entity dropped.
    assert merged == {"light.k": "Kitchen Light"}
