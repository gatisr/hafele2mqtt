"""Access payloads and network PDU construction.

Two kinds of assertion here:

* The access payloads are checked against the opcodes and field order written
  down in the Mesh Model specification, byte for byte. Those are correct or
  incorrect on their own terms.
* The full network PDUs are pinned as golden hex. The crypto underneath them is
  verified against the spec vectors in test_mesh_crypto.py, so these pins exist
  to catch an accidental change to nonce layout, obfuscation or field order.
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from app import mesh

h = bytes.fromhex

NET_KEY = h("7dd7364cd842ad18c17c2b820c84c3d6")
APP_KEY = h("63964771734fbd76e3b40519d1d94a48")


@pytest.fixture
def keys() -> mesh.Keys:
    return mesh.Keys(NET_KEY, APP_KEY)


# --- access payloads --------------------------------------------------------

def test_lightness_set_unack_payload():
    # Light Lightness Set Unacknowledged: opcode 0x824D, lightness uint16 LE, TID.
    assert mesh.lightness_set(0xBEEF, 0x07).hex() == "824defbe07"
    assert mesh.lightness_set(0, 0).hex() == "824d000000"
    assert mesh.lightness_set(65535, 255).hex() == "824dffffff"


def test_scene_recall_unack_payload():
    # Scene Recall Unacknowledged: opcode 0x8242, scene number uint16 LE, TID.
    assert mesh.scene_recall(8, 0x05).hex() == "8242080005"
    assert mesh.scene_recall(0x0123, 0x00).hex() == "8242230100"


def test_lightness_get_payload():
    assert mesh.lightness_get().hex() == "824b"


def test_tid_is_masked():
    assert mesh.lightness_set(1, 0x1FF).hex().endswith("ff")


# --- network PDUs -----------------------------------------------------------

GOLDEN_ACCESS = "68facbd163df4075f1d129c5a22b54d23ab8f27c720be8"
GOLDEN_PROXY_CONFIG = "68b68c22fbe821cbc3f9bc91ed31a5b576f8500c"


def test_access_pdu_golden(keys):
    pdu = mesh.build_access_pdu(keys, iv_index=0x12345678, seq=0x000006,
                                src=0x1201, dst=0xC002, ttl=5,
                                access_payload=mesh.lightness_set(0xFFFF, 1))
    assert pdu.hex() == GOLDEN_ACCESS


def test_access_pdu_header_fields(keys):
    """IVI (bit 7) and NID (bits 6..0) are plaintext in the first octet."""
    for iv_index, ivi in ((0x12345678, 0), (0x12345679, 1)):
        pdu = mesh.build_access_pdu(keys, iv_index, 1, 0x1201, 0xC002, 5,
                                    mesh.lightness_set(1, 1))
        assert pdu[0] >> 7 == ivi
        assert pdu[0] & 0x7F == keys.nid


def test_access_pdu_round_trip(keys):
    """Everything the obfuscation hides must come back out of decode_access."""
    payload = mesh.lightness_set(0x8000, 42)
    pdu = mesh.build_access_pdu(keys, iv_index=7, seq=0x010203,
                                src=0x7FF7, dst=0x0010, ttl=5,
                                access_payload=payload)
    dec = mesh.decode_access(keys, 7, pdu)
    assert (dec.src, dec.dst, dec.payload) == (0x7FF7, 0x0010, payload)
    # Sequence number and IV index are no longer thrown away on the way out:
    # replay protection needs both, and only decode knows which index worked.
    assert (dec.seq, dec.iv_index) == (0x010203, 7)
    assert dec.segmented is False


def test_decode_rejects_foreign_network(keys):
    other = mesh.Keys(h("00112233445566778899aabbccddeeff"), APP_KEY)
    pdu = mesh.build_access_pdu(other, 7, 1, 0x1201, 0x0010, 5, mesh.lightness_set(1, 1))
    assert mesh.decode_access(keys, 7, pdu) is None


def test_decode_rejects_wrong_app_key(keys):
    mixed = mesh.Keys(NET_KEY, h("00112233445566778899aabbccddeeff"))
    pdu = mesh.build_access_pdu(mixed, 7, 1, 0x1201, 0x0010, 5, mesh.lightness_set(1, 1))
    assert mesh.decode_access(keys, 7, pdu) is None


def test_decode_tolerates_previous_iv_index(keys):
    """A node still on the old IV index must still be decodable (IVI parity)."""
    pdu = mesh.build_access_pdu(keys, 6, 1, 0x1201, 0x0010, 5, mesh.lightness_set(1, 1))
    assert mesh.decode_access(keys, 7, pdu) is not None


def test_an_ivi_bit_at_iv_index_zero_is_dropped_not_an_error(keys):
    """There is no IV index below zero, so the fallback would be -1.

    The bridge is deployed with IV_INDEX 0, where anything advertising an odd
    IV index sets the IVI bit. `(iv_index - 1).to_bytes(4, "big")` then raised
    OverflowError straight out of a BLE notification callback, where nothing
    was catching it. A PDU we cannot address is a dropped packet, not a crash.
    """
    pdu = mesh.build_access_pdu(keys, 1, 1, 0x1201, 0x0010, 5, mesh.lightness_set(1, 1))
    assert pdu[0] >> 7 == 1
    assert mesh.decode_access(keys, 0, pdu) is None


def test_proxy_config_pdu_golden(keys):
    op, params = mesh.add_filter_addresses([0x7FF7])
    pdu = mesh.build_proxy_config_pdu(keys, iv_index=0, seq=1, src=0x7FF7,
                                      opcode=op, params=params)
    assert pdu.hex() == GOLDEN_PROXY_CONFIG
    assert pdu[0] & 0x7F == keys.nid


def test_proxy_filter_helpers():
    assert mesh.set_filter_type_whitelist() == (0x00, b"\x00")
    assert mesh.add_filter_addresses([0x7FF7, 0x0010]) == (0x01, h("7ff70010"))


# --- the limits of an unsegmented message ------------------------------------

def test_everything_the_bridge_sends_fits_an_unsegmented_message():
    """The design assumption, asserted rather than believed.

    This bridge implements no lower transport segmentation, which is only
    tenable because nothing it sends needs any. If a new message type is ever
    added that does not fit, this is where that is noticed - rather than in a
    house where one command silently does nothing.
    """
    for payload in (mesh.lightness_set(0xFFFF, 0xFF),
                    mesh.scene_recall(0xFFFF, 0xFF),
                    mesh.lightness_get()):
        assert len(payload) <= mesh.MAX_UNSEGMENTED_ACCESS


def test_an_oversized_access_payload_is_refused_at_the_source(keys):
    """Building it would succeed and every node would discard it in silence.

    An unsegmented access message carries 11 octets. Longer than that, the
    transport layer needs segmentation, and this bridge has none: the PDU is
    still well formed, still authenticates, and is still thrown away by every
    receiver, with the only symptom being one command that does nothing. An
    exception at the point the payload was built says what is wrong.
    """
    too_long = bytes(mesh.MAX_UNSEGMENTED_ACCESS + 1)
    with pytest.raises(ValueError, match="does not segment"):
        mesh.build_access_pdu(keys, 7, 1, 0x7FF7, 0x0010, 5, too_long)
    # And the largest that does fit is built without complaint.
    assert mesh.build_access_pdu(keys, 7, 1, 0x7FF7, 0x0010, 5,
                                 bytes(mesh.MAX_UNSEGMENTED_ACCESS))


def segmented_access(keys: mesh.Keys, iv_index: int, seq: int, src: int, dst: int,
                     ttl: int = 5) -> bytes:
    """The first segment of a segmented access message, as a node would send it.

    Written out rather than patched into a `build_access_pdu` result, because
    the obfuscation is keyed on the network-layer ciphertext: flipping a bit
    inside it and re-encrypting invalidates the header obfuscation, and the
    result decodes to nothing for the wrong reason.

    The upper transport payload of a segment is opaque to the bridge, so what
    is in it does not matter; only the SEG bit, the AKF/AID byte and the NetMIC
    do.
    """
    seq3, src2, dst2 = (seq.to_bytes(3, "big"), src.to_bytes(2, "big"),
                        dst.to_bytes(2, "big"))
    iv4 = iv_index.to_bytes(4, "big")
    # SEG=1, AKF=1, AID; then SZMIC/SeqZero/SegO/SegN and a segment of payload.
    transport = bytes([0xC0 | keys.aid]) + bytes([0x00, 0x08, 0x00]) + bytes(8)
    net_nonce = b"\x00" + bytes([ttl & 0x7F]) + seq3 + src2 + b"\x00\x00" + iv4
    enc = AESCCM(keys.enc_key, tag_length=4).encrypt(net_nonce, dst2 + transport, None)
    pecb = mesh.aes_ecb(keys.priv_key, b"\x00" * 5 + iv4 + enc[:7])
    obf = bytes(a ^ b for a, b in zip(bytes([ttl & 0x7F]) + seq3 + src2, pecb[:6]))
    return bytes([((iv_index & 1) << 7) | keys.nid]) + obf + enc


def test_a_segmented_message_from_our_network_is_reported_not_dropped(keys):
    """A segmented inbound message is a fact the bridge has to be able to state.

    It cannot reassemble one and cannot acknowledge one, so the message is
    lost either way. But "this node answers in a form I discard" and "this node
    never answers" need different fixes, and the old decoder returned None for
    both. The NetMIC has already been checked at this point, so src/seq are
    trustworthy even though the payload is not here.
    """
    pdu = segmented_access(keys, 7, 0x010203, 0x0010, 0x7FF7)
    dec = mesh.decode_access(keys, 7, pdu)
    assert dec is not None
    assert dec.segmented is True
    assert dec.payload == b""
    assert (dec.src, dec.dst, dec.seq, dec.iv_index) == (0x0010, 0x7FF7, 0x010203, 7)


def test_a_segmented_message_on_a_foreign_app_key_is_still_nothing(keys):
    """`segmented` must not become a way past the application key check.

    The SEG test now runs after the AID test, so a segmented message on an
    application key that is not ours is rejected exactly as before rather than
    being reported as a message of ours whose payload went missing.
    """
    other = mesh.Keys(NET_KEY, h("00112233445566778899aabbccddeeff"))
    if other.aid == keys.aid:  # pragma: no cover - the fixed keys do not collide
        pytest.skip("the two application keys happen to share an AID")
    assert mesh.decode_access(keys, 7, segmented_access(other, 7, 1, 0x0010, 0x7FF7)) is None


# --- reading the proxy's answer ----------------------------------------------

def filter_status(keys: mesh.Keys, *, filter_type: int = 0, list_size: int = 1,
                  iv_index: int = 0, seq: int = 7, src: int = 0x0001) -> bytes:
    """A Filter Status as a proxy would send it.

    Built with the bridge's own proxy-config builder because the two directions
    share a wire format exactly; only the opcode and parameters differ.
    """
    return mesh.build_proxy_config_pdu(
        keys, iv_index, seq, src, mesh.PROXY_FILTER_STATUS,
        bytes([filter_type]) + list_size.to_bytes(2, "big"))


def test_filter_status_round_trip(keys):
    st = mesh.decode_proxy_config(keys, 0, filter_status(keys, list_size=1))
    assert st == mesh.FilterStatus(src=0x0001, filter_type=mesh.FILTER_WHITELIST,
                                   list_size=1)


def test_filter_status_reports_a_blacklist_as_a_blacklist(keys):
    """The bridge asks for a whitelist; a proxy answering "blacklist" is a fault.

    Decoding it correctly is what lets the caller tell that apart from no
    answer at all, which is the whole point of reading the status.
    """
    st = mesh.decode_proxy_config(keys, 0, filter_status(keys, filter_type=1, list_size=0))
    assert st.filter_type == 1 and st.list_size == 0


def test_filter_status_on_a_foreign_network_is_refused(keys):
    other = mesh.Keys(h("00112233445566778899aabbccddeeff"), APP_KEY)
    assert mesh.decode_proxy_config(keys, 0, filter_status(other)) is None


def test_the_other_proxy_opcodes_are_not_a_filter_status(keys):
    """Set Filter Type and Add Addresses are client-to-server only.

    Seeing one come back means something is echoing our own writes at us, and
    treating that as confirmation would confirm nothing.
    """
    for op, params in (mesh.set_filter_type_whitelist(),
                       mesh.add_filter_addresses([0x7FF7])):
        pdu = mesh.build_proxy_config_pdu(keys, 0, 7, 0x0001, op, params)
        assert mesh.decode_proxy_config(keys, 0, pdu) is None


def test_an_access_message_is_not_read_as_a_filter_status(keys):
    """The two use different nonces and different MIC lengths; neither decoder
    may accept the other's PDUs."""
    access = mesh.build_access_pdu(keys, 0, 1, 0x0010, 0x7FF7, 5, mesh.lightness_set(1, 1))
    assert mesh.decode_proxy_config(keys, 0, access) is None
    assert mesh.decode_access(keys, 0, filter_status(keys)) is None


def test_a_truncated_filter_status_is_dropped_not_crashed(keys):
    """Short frames arrive: a dropped SAR segment produces exactly this."""
    pdu = filter_status(keys)
    for n in range(len(pdu)):
        assert mesh.decode_proxy_config(keys, 0, pdu[:n]) is None


def test_filter_status_tolerates_the_previous_iv_index(keys):
    """The proxy's own messages are subject to the same IVI parity rule.

    Worth pinning because the confirmation happens at connect time, which is
    exactly when the bridge may still be on the index it wrote down last.
    """
    pdu = filter_status(keys, iv_index=6)
    assert mesh.decode_proxy_config(keys, 7, pdu) is not None
    pdu = filter_status(keys, iv_index=8)
    assert mesh.decode_proxy_config(keys, 7, pdu) is None
    assert mesh.decode_proxy_config(keys, 7, pdu, accept_next=True) is not None
