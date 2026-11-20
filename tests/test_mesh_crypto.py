"""Mesh key derivation against the published Bluetooth Mesh specification vectors.

These are the sample data in the Mesh Profile specification (section 8.1, "Sample
data"). They are public, and reproducing them is the strongest evidence we have
that the crypto in app/mesh.py is actually Bluetooth Mesh and not merely
self-consistent.
"""
from __future__ import annotations

from app import mesh

h = bytes.fromhex


def test_s1_sample():
    # Mesh Profile 8.1.2: s1("test")
    assert mesh.s1(b"test").hex() == "b73cefbd641ef2ea598c2b6efb62f79c"


def test_k2_master_credentials():
    # Mesh Profile 8.1.3: N = f7a2..., P = 0x00 (master security material)
    nid, enc, priv = mesh.k2(h("f7a2a44f8e8a8029064f173ddc1e2b00"))
    assert nid == 0x7F
    assert enc.hex() == "9f589181a0f50de73c8070c7a6d27f46"
    assert priv.hex() == "4c715bd4a64b938f99b453351653124f"


def test_k2_friendship_credentials():
    # Mesh Profile 8.1.4: same N, P = 010203040506070809
    nid, enc, priv = mesh.k2(h("f7a2a44f8e8a8029064f173ddc1e2b00"),
                             h("010203040506070809"))
    assert nid == 0x73
    assert enc.hex() == "11efec0642774992510fb5929646df49"
    assert priv.hex() == "d4d7cc0dfa772d836a8df9df5510d7a7"


def test_k3_network_id():
    # Mesh Profile 8.1.5: k3 of the same net key
    assert mesh.k3(h("f7a2a44f8e8a8029064f173ddc1e2b00")).hex() == "ff046958233db014"


def test_k4_aid():
    # Mesh Profile 8.1.6
    assert mesh.k4(h("3216d1509884b533248541792b877f98")) == 0x38


def test_sample_network_key_material():
    # Mesh Profile 8.3 sample messages use NetKey 7dd7364c... and AppKey 63964771...
    nid, enc, priv = mesh.k2(h("7dd7364cd842ad18c17c2b820c84c3d6"))
    assert nid == 0x68
    assert enc.hex() == "0953fa93e7caac9638f58820220a398e"
    assert priv.hex() == "8b84eedec100067d670971dd2aa700cf"
    assert mesh.k3(h("7dd7364cd842ad18c17c2b820c84c3d6")).hex() == "3ecaff672f673370"
    assert mesh.k4(h("63964771734fbd76e3b40519d1d94a48")) == 0x26


def test_k1_sample():
    # Mesh Profile 8.1.7: k1(N, SALT, P)
    assert mesh.k1(h("3216d1509884b533248541792b877f98"),
                   h("2ba14ffa0df84a2831938d57d276cab4"),
                   h("5a09d60797eeb4478aada59db3352a0d")).hex() == \
        "f6ed15a8934afbe7d83e8dcb57fcf5d7"


def test_beacon_key_sample():
    # Mesh Profile 8.4.3: the beacon key for the sample network, k1(NetKey,
    # s1("nkbk"), "id128" || 0x01).
    keys = mesh.Keys(h("7dd7364cd842ad18c17c2b820c84c3d6"),
                     h("63964771734fbd76e3b40519d1d94a48"))
    assert keys.beacon_key.hex() == "5423d967da639a99cb02231a83f7d254"


def test_secure_network_beacon_sample():
    """Mesh Profile 8.4.3, the specification's own Secure Network beacon.

    Flags 0x00, the sample network ID, IV index 0x12345678 and the 64-bit
    authentication value the spec publishes for them. If this passes, the
    authentication added for forged beacons is really Bluetooth Mesh and not
    just self-consistent.
    """
    keys = mesh.Keys(h("7dd7364cd842ad18c17c2b820c84c3d6"),
                     h("63964771734fbd76e3b40519d1d94a48"))
    beacon = h("01" "00" "3ecaff672f673370" "12345678" "8ea261582f364f6f")
    assert mesh.parse_secure_beacon(keys, beacon) == mesh.Beacon(0x12345678, False)


def test_keys_dataclass_derives_everything():
    keys = mesh.Keys(h("7dd7364cd842ad18c17c2b820c84c3d6"),
                     h("63964771734fbd76e3b40519d1d94a48"))
    assert keys.nid == 0x68
    assert keys.aid == 0x26
    assert keys.network_id.hex() == "3ecaff672f673370"
    assert keys.enc_key.hex() == "0953fa93e7caac9638f58820220a398e"
    assert keys.priv_key.hex() == "8b84eedec100067d670971dd2aa700cf"
