"""Minimal Bluetooth Mesh network/transport/access layer (unsegmented).

Enough to publish Light Lightness and Scene unacknowledged messages to unicast
or group addresses using a NetKey + AppKey, and to read back the Light Lightness
Status and proxy Filter Status messages that come the other way.

Those are the only models the bridge speaks. A node that expects Generic OnOff
is not driven by anything here, and one that answers in it is not understood;
see `Bridge._on_pdu`, which names an unhandled opcode once rather than counting
it as silence.

Why unsegmented, on purpose
---------------------------
The lower transport layer can split a large upper transport PDU over several
PDUs and reassemble it at the far end, with its own acknowledgements and
retransmissions. This bridge implements none of that and does not need to: the
largest message it sends is five octets of access payload (a Light Lightness Set
Unacknowledged) and an unsegmented access message carries eleven. See
MAX_UNSEGMENTED_ACCESS, which turns "we quietly built a PDU nobody can decode"
into an exception where the payload was made.

The receive side cannot make the same assumption, because what arrives is
whatever the other nodes decided to send. A segmented message is reported
through `Decoded.segmented` rather than dropped in silence: the bridge can
neither reassemble nor acknowledge one, and "the light never answers" and "the
light answers in a form the bridge throws away" are two different faults that
look identical in a log which mentions neither.

This is a different mechanism from the GATT proxy SAR in app/ble.py, which
splits a *network* PDU across BLE notifications and is implemented in both
directions. With a 23-octet ATT MTU nearly every message on this network uses
that one, so it is anything but theoretical.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import NamedTuple

from cryptography.hazmat.primitives import cmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

# Access opcodes
OP_LIGHTNESS_SET_UNACK = b"\x82\x4d"
OP_SCENE_RECALL_UNACK = b"\x82\x42"

# A Secure Network beacon is 22 bytes: type, flags, network ID, IV index, CMAC.
SECURE_BEACON_LEN = 22

# Beacon flags (Mesh Profile 3.9.3). Bit 0 is the Key Refresh flag, which this
# bridge does not act on; bit 1 says an IV update is in progress.
BEACON_FLAG_IV_UPDATE = 0x02

# Mesh Profile 3.10.5: ignore a beacon whose IV index is more than 42 ahead of
# ours. The spec allows one IV update per 96 hours, so 42 is over three years of
# drift; anything larger is not a network we fell behind, it is a number someone
# is feeding us.
IV_INDEX_MAX_AHEAD = 42

# The most access payload an *unsegmented* access message can carry: the upper
# transport PDU is at most 15 octets, of which 4 are the TransMIC. Anything
# longer has to be segmented, which this bridge does not do - see the module
# docstring. Nothing it sends comes close, and the guard exists because the
# failure mode otherwise is invisible: an over-long payload still produces a
# well-formed-looking PDU, every node discards it without a word, and the only
# symptom is lights that ignore one particular command.
MAX_UNSEGMENTED_ACCESS = 11

# Proxy configuration opcodes (Mesh Profile 6.5). The bridge sends the first
# two and reads the third.
PROXY_SET_FILTER_TYPE = 0x00
PROXY_ADD_ADDRESSES = 0x01
PROXY_FILTER_STATUS = 0x03
FILTER_WHITELIST = 0x00


def aes_cmac(key: bytes, msg: bytes) -> bytes:
    c = cmac.CMAC(algorithms.AES(key))
    c.update(msg)
    return c.finalize()


def aes_ecb(key: bytes, block: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block) + enc.finalize()


def s1(m: bytes) -> bytes:
    return aes_cmac(bytes(16), m)


def k2(n: bytes, p: bytes = b"\x00") -> tuple[int, bytes, bytes]:
    """Returns (NID, encryption key, privacy key)."""
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    r = (t1 + t2 + t3)[-33:]  # mod 2^263
    return r[0] & 0x7F, r[1:17], r[17:33]


def k3(n: bytes) -> bytes:
    """Network ID (8 bytes)."""
    t = aes_cmac(s1(b"smk3"), n)
    return aes_cmac(t, b"id64\x01")[-8:]


def k4(n: bytes) -> int:
    """AID (6 bits) of an application key."""
    t = aes_cmac(s1(b"smk4"), n)
    return aes_cmac(t, b"id6\x01")[-1] & 0x3F


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    return aes_cmac(aes_cmac(salt, n), p)


def beacon_key(n: bytes) -> bytes:
    """Secure Network beacon authentication key: k1(N, s1("nkbk"), "id128" || 0x01)."""
    return k1(n, s1(b"nkbk"), b"id128\x01")


@dataclass
class Keys:
    net_key: bytes
    app_key: bytes

    def __post_init__(self) -> None:
        self.nid, self.enc_key, self.priv_key = k2(self.net_key)
        self.network_id = k3(self.net_key)
        self.beacon_key = beacon_key(self.net_key)
        self.aid = k4(self.app_key)


class Beacon(NamedTuple):
    """What an authenticated Secure Network beacon tells us.

    `iv_update` matters as much as the number itself. While it is set the
    network is mid-IV-update and `iv_index` is the *new* value, which nodes
    accept but do not yet transmit with; see `Bridge._on_iv`.
    """
    iv_index: int
    iv_update: bool


def parse_secure_beacon(keys: Keys, body: bytes) -> Beacon | None:
    """An *authenticated* Secure Network beacon, else None.

    The IV index is persisted and never walked back, so an unauthenticated
    beacon is a permanent denial of service: one forged number and every PDU we
    build afterwards is rejected by the whole mesh. Both the network ID and the
    64-bit CMAC are therefore checked before the value is believed.
    """
    if len(body) < SECURE_BEACON_LEN or body[0] != 0x01:
        return None
    if not hmac.compare_digest(body[2:10], keys.network_id):
        return None
    if not hmac.compare_digest(aes_cmac(keys.beacon_key, body[1:14])[:8], body[14:22]):
        return None
    return Beacon(int.from_bytes(body[10:14], "big"),
                  bool(body[1] & BEACON_FLAG_IV_UPDATE))


def build_access_pdu(keys: Keys, iv_index: int, seq: int, src: int, dst: int,
                     ttl: int, access_payload: bytes) -> bytes:
    """Encrypt an access message and wrap it in an obfuscated network PDU.

    Raises ValueError for a payload that would not fit an unsegmented message;
    see MAX_UNSEGMENTED_ACCESS.
    """
    if len(access_payload) > MAX_UNSEGMENTED_ACCESS:
        raise ValueError(
            f"access payload is {len(access_payload)} octets, and an unsegmented access "
            f"message carries at most {MAX_UNSEGMENTED_ACCESS}. This bridge does not segment, "
            "so the PDU would be built, sent, and discarded by every node without a word.")
    seq3 = seq.to_bytes(3, "big")
    src2 = src.to_bytes(2, "big")
    iv4 = iv_index.to_bytes(4, "big")

    app_nonce = b"\x01\x00" + seq3 + src2 + dst.to_bytes(2, "big") + iv4
    upper = AESCCM(keys.app_key, tag_length=4).encrypt(app_nonce, access_payload, None)
    transport = bytes([0x40 | keys.aid]) + upper  # SEG=0, AKF=1, AID

    net_nonce = b"\x00" + bytes([ttl & 0x7F]) + seq3 + src2 + b"\x00\x00" + iv4
    enc = AESCCM(keys.enc_key, tag_length=4).encrypt(
        net_nonce, dst.to_bytes(2, "big") + transport, None)

    pecb = aes_ecb(keys.priv_key, b"\x00" * 5 + iv4 + enc[:7])
    plain = bytes([ttl & 0x7F]) + seq3 + src2
    obf = bytes(a ^ b for a, b in zip(plain, pecb[:6]))
    return bytes([((iv_index & 1) << 7) | keys.nid]) + obf + enc


# --- access payload helpers -------------------------------------------------

def lightness_set(lightness: int, tid: int) -> bytes:
    return OP_LIGHTNESS_SET_UNACK + int(lightness).to_bytes(2, "little") + bytes([tid & 0xFF])


def scene_recall(scene: int, tid: int) -> bytes:
    return OP_SCENE_RECALL_UNACK + int(scene).to_bytes(2, "little") + bytes([tid & 0xFF])


OP_LIGHTNESS_GET = b"\x82\x4b"
OP_LIGHTNESS_STATUS = b"\x82\x4e"


def lightness_get() -> bytes:
    return OP_LIGHTNESS_GET


def build_proxy_config_pdu(keys: Keys, iv_index: int, seq: int, src: int,
                           opcode: int, params: bytes) -> bytes:
    """Proxy configuration message (CTL=1, TTL=0, DST=0, proxy nonce, 64-bit NetMIC)."""
    seq3, src2, iv4 = seq.to_bytes(3, "big"), src.to_bytes(2, "big"), iv_index.to_bytes(4, "big")
    nonce = b"\x03\x00" + seq3 + src2 + b"\x00\x00" + iv4
    enc = AESCCM(keys.enc_key, tag_length=8).encrypt(
        nonce, b"\x00\x00" + bytes([opcode & 0x7F]) + params, None)
    pecb = aes_ecb(keys.priv_key, b"\x00" * 5 + iv4 + enc[:7])
    plain = bytes([0x80]) + seq3 + src2  # CTL=1, TTL=0
    obf = bytes(a ^ b for a, b in zip(plain, pecb[:6]))
    return bytes([((iv_index & 1) << 7) | keys.nid]) + obf + enc


def set_filter_type_whitelist() -> tuple[int, bytes]:
    return PROXY_SET_FILTER_TYPE, b"\x00"


def add_filter_addresses(addrs: list[int]) -> tuple[int, bytes]:
    return PROXY_ADD_ADDRESSES, b"".join(a.to_bytes(2, "big") for a in addrs)


class Decoded(NamedTuple):
    """An authenticated network PDU from our network.

    `seq` and `iv_index` are carried out of the decoder rather than discarded
    because replay protection is impossible without them: a message is new only
    if its (iv_index, seq) is above everything already seen from that source.
    They come out of the *authenticated* header, so neither can be edited by a
    replayer without breaking the NetMIC.

    `segmented` marks a message that belongs to us and whose payload is not
    here: it is spread over several lower transport PDUs. `payload` is empty
    then, and nothing else in the tuple is affected.
    """
    src: int
    dst: int
    seq: int
    iv_index: int
    payload: bytes
    segmented: bool = False


class FilterStatus(NamedTuple):
    """A proxy's answer to a filter change (Mesh Profile 6.5.1.4)."""
    src: int
    filter_type: int
    list_size: int


def _iv_candidates(iv_index: int, ivi: int, accept_next: bool) -> list[int]:
    """Which IV indices a PDU with this IVI bit could have been sent with.

    The IVI bit is only the bottom bit of the IV index, so a PDU whose bit does
    not match ours was sent with `iv_index - 1`. During an IV update the network
    also carries messages sent with `iv_index + 1`, which has that same bit;
    without `accept_next` those are all silently undecodable, which looks
    exactly like the lights having stopped answering.

    Negative candidates are dropped here. IV index 0 with the IVI bit set has no
    previous index to fall back to, and `(-1).to_bytes(4, "big")` used to raise
    OverflowError straight out of a BLE notification callback.
    """
    out = [iv_index] if ivi == (iv_index & 1) else [iv_index - 1]
    if accept_next and ivi == ((iv_index + 1) & 1):
        # First: during an update the newer index is the likelier sender, and
        # both are cheap enough that the order only decides which one wins.
        out.insert(0, iv_index + 1)
    return [iv for iv in out if iv >= 0]


def _deobfuscate(keys: Keys, iv: int, pdu: bytes) -> bytes:
    """The six header octets the privacy key hides: CTL/TTL, SEQ, SRC."""
    pecb = aes_ecb(keys.priv_key, b"\x00" * 5 + iv.to_bytes(4, "big") + pdu[7:14])
    return bytes(a ^ b for a, b in zip(pdu[1:7], pecb[:6]))


def decode_access(keys: Keys, iv_index: int, pdu: bytes,
                  accept_next: bool = False) -> Decoded | None:
    """Decode a received network PDU carrying an access message.

    Returns a `Decoded`, or None when the PDU is not ours or not decodable.
    See `_iv_candidates` for what `accept_next` is for.
    """
    if len(pdu) < 18 or pdu[0] & 0x7F != keys.nid:
        return None
    for iv in _iv_candidates(iv_index, pdu[0] >> 7, accept_next):
        out = _decode_with_iv(keys, iv, pdu)
        if out is not None:
            return out
    return None


def _decode_with_iv(keys: Keys, iv: int, pdu: bytes) -> Decoded | None:
    hdr = _deobfuscate(keys, iv, pdu)
    if hdr[0] >> 7:  # control message
        return None
    seq3, src2 = hdr[1:4], hdr[4:6]
    nonce = b"\x00" + hdr[:1] + seq3 + src2 + b"\x00\x00" + iv.to_bytes(4, "big")
    try:
        dec = AESCCM(keys.enc_key, tag_length=4).decrypt(nonce, pdu[7:], None)
    except Exception:  # noqa: BLE001
        return None
    dst2, transport = dec[:2], dec[2:]
    if not transport[0] & 0x40 or transport[0] & 0x3F != keys.aid:
        return None  # a device key, or an application key that is not ours
    src, dst, seq = (int.from_bytes(src2, "big"), int.from_bytes(dst2, "big"),
                     int.from_bytes(seq3, "big"))
    if transport[0] & 0x80:
        # Segmented. The NetMIC checked out, so this really is a message from
        # our network on our application key; the payload is simply not in this
        # PDU. Reported rather than dropped so the bridge can say so once.
        return Decoded(src, dst, seq, iv, b"", True)
    anonce = b"\x01\x00" + seq3 + src2 + dst2 + iv.to_bytes(4, "big")
    try:
        payload = AESCCM(keys.app_key, tag_length=4).decrypt(anonce, transport[1:], None)
    except Exception:  # noqa: BLE001
        return None
    return Decoded(src, dst, seq, iv, payload)


def decode_proxy_config(keys: Keys, iv_index: int, pdu: bytes,
                        accept_next: bool = False) -> FilterStatus | None:
    """Read a proxy's Filter Status out of a proxy configuration PDU.

    The only proxy configuration message a client ever receives. Without it the
    bridge has no way to tell a filter the proxy applied from one it dropped:
    the two writes that set the filter go out as Write Without Response, so the
    send path confirms nothing beyond the bytes having left the host, and a
    proxy that ignored them leaves a bridge that believes it is being forwarded
    status messages and will never see one again.

    Returns None for anything that is not a readable Filter Status, including
    the other proxy configuration opcodes - all of which are client-to-server.
    """
    # 1 IVI/NID + 6 obfuscated + 2 DST + 1 opcode + 3 parameters + 8 NetMIC.
    if len(pdu) < 21 or pdu[0] & 0x7F != keys.nid:
        return None
    for iv in _iv_candidates(iv_index, pdu[0] >> 7, accept_next):
        hdr = _deobfuscate(keys, iv, pdu)
        if not hdr[0] >> 7:  # proxy configuration is a control message
            continue
        seq3, src2 = hdr[1:4], hdr[4:6]
        nonce = b"\x03\x00" + seq3 + src2 + b"\x00\x00" + iv.to_bytes(4, "big")
        try:
            dec = AESCCM(keys.enc_key, tag_length=8).decrypt(nonce, pdu[7:], None)
        except Exception:  # noqa: BLE001
            continue
        # DST is 0x0000 on every proxy configuration message: it is addressed
        # to the GATT link itself, not to a node.
        if dec[:2] != b"\x00\x00" or len(dec) < 6:
            continue
        if dec[2] & 0x7F != PROXY_FILTER_STATUS:
            continue
        return FilterStatus(int.from_bytes(src2, "big"), dec[3],
                            int.from_bytes(dec[4:6], "big"))
    return None
