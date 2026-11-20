"""The proxy link, without a radio.

Two failures are worth a test of their own because neither produces an error at
the time it happens:

- A GATT link left open after a failed setup. The proxy node stops advertising
  while anything is connected to it, so the leaked link makes the node invisible
  to every subsequent scan and the bridge never comes back.
- A disconnect callback from a client we already replaced, which would tear down
  the healthy link that replaced it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import adapter as adapter_mod, ble, mesh
from app.adapter import Adapter, BlueZUnavailable
from app.ble import ProxyLink, cached_proxy_matches

# Mesh Profile sample network, so the network ID below is the spec's own value.
KEYS = mesh.Keys(bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6"),
                 bytes.fromhex("63964771734fbd76e3b40519d1d94a48"))
NET_ID = KEYS.network_id
PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"


class Variant:
    """Stands in for dbus-fast's wrapper, which every property arrives in."""

    def __init__(self, value):
        self.value = value


def device(address: str, *, net_id: bytes = NET_ID, connected: bool = False,
           adapter: str = "hci0", service_type: int = 0x00) -> dict:
    path = f"/org/bluez/{adapter}/dev_" + address.replace(":", "_")
    return {path: {"org.bluez.Device1": {
        "Address": Variant(address),
        "Connected": Variant(connected),
        "ServiceData": Variant({PROXY_SERVICE: Variant(bytes([service_type]) + net_id)}),
    }}}


# --- the BlueZ cache lookup --------------------------------------------------

def test_a_device_on_our_mesh_is_found_through_its_service_data():
    objects = device("DA:DF:34:4C:35:B9", connected=True)
    assert cached_proxy_matches(objects, NET_ID, None) == [
        ("/org/bluez/hci0/dev_DA_DF_34_4C_35_B9", "DA:DF:34:4C:35:B9", True)]


def test_a_device_on_a_different_mesh_is_not_ours():
    objects = device("DA:DF:34:4C:35:B9", net_id=bytes(8), connected=True)
    assert cached_proxy_matches(objects, NET_ID, None) == []


def test_a_node_id_advertisement_is_not_a_network_id():
    # Service data type 0x01 is Node Identity, which carries a hash, not our
    # network id, so the bytes after it mean something else entirely.
    objects = device("DA:DF:34:4C:35:B9", connected=True, service_type=0x01)
    assert cached_proxy_matches(objects, NET_ID, None) == []


def test_the_connected_device_is_offered_first():
    objects = {**device("AA:AA:AA:AA:AA:AA"), **device("BB:BB:BB:BB:BB:BB", connected=True)}
    assert [address for _, address, _ in cached_proxy_matches(objects, NET_ID, None)] == [
        "BB:BB:BB:BB:BB:BB", "AA:AA:AA:AA:AA:AA"]


def test_pinning_an_adapter_excludes_the_others():
    objects = {**device("AA:AA:AA:AA:AA:AA", adapter="hci0"),
               **device("BB:BB:BB:BB:BB:BB", adapter="hci1")}
    assert [a for _, a, _ in cached_proxy_matches(objects, NET_ID, "hci1")] == ["BB:BB:BB:BB:BB:BB"]


@pytest.mark.parametrize("objects", [
    {},
    {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}},                 # not a device
    {"/org/bluez/hci0/dev_X": {"org.bluez.Device1": {}}},            # no properties
    {"/org/bluez/hci0/dev_X": {"org.bluez.Device1": {"Address": Variant("AA:BB")}}},
])
def test_everything_that_is_not_a_mesh_device_is_skipped(objects):
    assert cached_proxy_matches(objects, NET_ID, None) == []


# --- the connection leak -----------------------------------------------------

class FakeClient:
    """A BleakClient that connects and then fails wherever it is told to."""

    mtu_size = 69

    def __init__(self, address, owner, **_kw):
        self.address = address
        self.owner = owner
        self.is_connected = False
        self.disconnected = False
        self.written = []

    async def connect(self):
        # The order is the real one: BlueZ establishes the ACL link and then
        # discovers services, so a failure in here leaves a connection open.
        self.is_connected = True
        if self.owner.connect_fails:
            raise RuntimeError("br-connection-profile-unavailable")

    async def start_notify(self, _char, _cb):
        if self.owner.notify_fails:
            raise RuntimeError("characteristic 2ade not found")

    async def write_gatt_char(self, _char, data, response=False):
        if self.owner.write_fails:
            raise OSError("le-connection-abort-by-local")
        # A hook here lets a test hand control to another task in the middle of
        # a segmented message, which is the only way to see the write lock do
        # its job - a fake that never yields cannot interleave.
        if self.owner.on_write is not None:
            await self.owner.on_write()
        self.written.append(bytes(data))

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False
        if self.owner.on_close is not None:
            await self.owner.on_close()
        if self.owner.disconnect_fails:
            raise OSError("the adapter went away")


@pytest.fixture
def link(settings, monkeypatch):
    settings.proxy_address = "DA:DF:34:4C:35:B9"      # skips the scan entirely
    # connect() waits a second for the first beacon; no beacon is coming here.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))
    lk = ProxyLink(KEYS, settings)
    lk.made = []
    lk.notify_fails = True
    lk.disconnect_fails = False
    lk.write_fails = False
    lk.connect_fails = False
    lk.on_write = None
    lk.on_close = None
    # One adapter, so connect() does not go looking for a real D-Bus.
    lk.adapters = [Adapter("hci0", "AA:AA:AA:AA:AA:01")]

    def factory(address, **kw):
        client = FakeClient(address, lk, **kw)
        lk.made.append(client)
        return client

    async def list_adapters():
        return list(lk.adapters)

    monkeypatch.setattr("app.ble.BleakClient", factory)
    monkeypatch.setattr(adapter_mod, "list_adapters", list_adapters)
    return lk


def test_a_setup_failure_closes_the_link_it_opened(link):
    """Otherwise the proxy stays connected at BlueZ level and stops advertising.

    `supervise()` catches this and calls `_mark_down()`, which only drops our
    reference, so nothing else would ever close that link.
    """
    with pytest.raises(RuntimeError, match="2ade"):
        asyncio.run(link.connect())
    assert link.made[0].disconnected is True
    assert link._client is None
    assert link.connected is False


def test_a_failure_inside_connect_closes_the_link_it_opened(link):
    """connect() is not just opening a socket.

    BlueZ establishes the ACL link and then discovers services inside the same
    call, so a failure in the second half raises with the connection already
    made. While that call sat in front of the cleanup instead of inside it,
    every such attempt abandoned an open GATT link - and the supervisor tries
    again every few seconds for as long as the node misbehaves, so one bad
    proxy was enough to pile them up all evening.
    """
    link.connect_fails = True
    with pytest.raises(RuntimeError, match="br-connection-profile-unavailable"):
        asyncio.run(link.connect())
    assert link.made[0].disconnected is True
    assert link._client is None
    assert link.connected is False


def test_a_failure_after_start_notify_closes_the_link_too(link):
    # The proxy filter is set up in on_connected, after start_notify has
    # succeeded, so this is the other half of the same window.
    async def boom():
        raise RuntimeError("proxy filter rejected")

    link.notify_fails = False
    link.on_connected = boom
    with pytest.raises(RuntimeError, match="proxy filter"):
        asyncio.run(link.connect())
    assert link.made[0].disconnected is True
    assert link._client is None


def test_a_cancellation_during_setup_also_closes_the_link(link):
    # A SIGTERM arriving in this window is a BaseException, not an Exception.
    async def cancelled():
        raise asyncio.CancelledError

    link.notify_fails = False
    link.on_connected = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(link.connect())
    assert link.made[0].disconnected is True


def test_a_teardown_failure_does_not_hide_the_real_error(link):
    # The caller needs to see why the connection failed, not why the cleanup
    # after it did.
    link.disconnect_fails = True
    with pytest.raises(RuntimeError, match="2ade"):      # not the OSError
        asyncio.run(link.connect())
    assert link._client is None


def test_a_successful_connection_marks_the_link_up(link):
    link.notify_fails = False
    seen = []
    link.on_state = seen.append
    asyncio.run(link.connect())
    assert link.connected is True
    assert seen == [True]
    assert link.made[0].disconnected is False


def test_a_link_that_died_during_setup_is_not_announced_as_up(link):
    """connect() returning is not the same as the link still being there.

    The radio can drop between connect() and the end of setup - there is a
    deliberate wait for the first beacon in that window. Reporting "up" anyway
    published availability online over a link that was already gone, and the
    supervisor then sat on _up waiting for a disconnect that had already fired.
    """
    link.notify_fails = False
    seen = []
    link.on_state = seen.append

    async def drop_the_link():
        link.made[0].is_connected = False

    link.on_connected = drop_the_link
    with pytest.raises(ConnectionError, match="dropped while it was being set up"):
        asyncio.run(link.connect())
    assert seen == []
    assert link.connected is False
    assert link._client is None
    assert not link._up.is_set()


# --- secure network beacons ---------------------------------------------------

def beacon(keys: mesh.Keys = KEYS, iv: int = 7, flags: int = 0,
           net_id: bytes | None = None, mac: bytes | None = None) -> bytes:
    """A Secure Network beacon as a proxy PDU body (type 0x01)."""
    head = bytes([0x01, flags]) + (KEYS.network_id if net_id is None else net_id) \
        + iv.to_bytes(4, "big")
    return head + (mesh.aes_cmac(keys.beacon_key, head[1:14])[:8] if mac is None else mac)


def notify(link, body: bytes, ptype: int = 1) -> list[tuple[int, bool]]:
    seen: list[tuple[int, bool]] = []
    link.on_iv_index = lambda iv, upd: seen.append((iv, upd))
    link._notify(None, bytearray(bytes([ptype]) + body))
    return seen


def test_a_beacon_from_our_own_network_is_believed(link):
    assert notify(link, beacon(iv=7)) == [(7, False)]


def test_the_iv_update_flag_is_passed_on_with_the_index(link):
    """Bit 1 of the flags byte, and it changes what the index means.

    While it is set the beacon carries the *next* IV index rather than the one
    the network is transmitting with, so a listener that only took the number
    would move up a year early and stop being understood by every node.
    """
    assert notify(link, beacon(iv=8, flags=0x02)) == [(8, True)]


def test_the_key_refresh_flag_is_not_mistaken_for_an_iv_update(link):
    # Bit 0 is Key Refresh, which this bridge does not act on. Reading the
    # flags byte as a boolean rather than masking bit 1 would make every key
    # refresh look like an IV update.
    assert notify(link, beacon(iv=8, flags=0x01)) == [(8, False)]


def test_a_beacon_from_a_foreign_network_is_ignored(link):
    """An IV index is written to disk and never lowered again.

    So a beacon nobody authenticated is not noise, it is a permanent outage:
    every PDU the bridge builds afterwards is encrypted under an IV index the
    real network has never heard of, and no amount of restarting undoes it.
    """
    stranger = mesh.Keys(bytes(16), bytes(16))
    assert notify(link, beacon(keys=stranger, iv=999999, net_id=stranger.network_id)) == []


def test_a_beacon_with_our_network_id_but_a_forged_mac_is_ignored(link):
    # The network ID is public - it is broadcast in the proxy advertisement -
    # so copying it is free. Only the CMAC proves the sender has the net key.
    assert notify(link, beacon(iv=999999, mac=bytes(8))) == []


def test_a_beacon_whose_mac_covers_different_bytes_is_ignored(link):
    # Authenticated beacon, then the IV index edited in flight.
    forged = bytearray(beacon(iv=7))
    forged[10:14] = (999999).to_bytes(4, "big")
    assert notify(link, bytes(forged)) == []


@pytest.mark.parametrize("body", [
    b"",
    b"\x01",
    beacon()[:21],                       # one byte short of a full beacon
    b"\x02" + beacon()[1:],              # an unprovisioned device beacon
])
def test_anything_that_is_not_a_full_secure_beacon_is_ignored(link, body):
    assert notify(link, body) == []


# --- proxy SAR: reassembling a message split across notifications -------------
#
# With a 23-octet ATT MTU the usable payload of one notification is 20 bytes,
# and the smallest network PDU the bridge exchanges is 22. So this path carries
# every single message on this network; it is not an edge case, it is the
# normal case. The frames arriving here have been authenticated by nothing.

def frames(link) -> list[bytes]:
    """Collect the network PDUs that came back out of the reassembler."""
    got: list[bytes] = []
    link.on_pdu = got.append
    return got


def seg(sar: int, ptype: int, body: bytes) -> bytearray:
    return bytearray(bytes([(sar << 6) | ptype]) + body)


def test_a_message_split_across_notifications_is_put_back_together(link):
    got = frames(link)
    link._notify(None, seg(ble.SAR_FIRST, 0, b"aaaa"))
    link._notify(None, seg(ble.SAR_CONTINUE, 0, b"bbbb"))
    link._notify(None, seg(ble.SAR_LAST, 0, b"cccc"))
    assert got == [b"aaaabbbbcccc"]


def test_the_buffer_is_empty_again_after_a_message_is_delivered(link):
    """Two messages in a row must not be spliced into one.

    The old code assigned the buffer on a first segment and appended on the
    rest, but never cleared it after delivering: a complete (unsegmented)
    message that followed a reassembled one left the previous bytes in place
    for whatever came next.
    """
    got = frames(link)
    link._notify(None, seg(ble.SAR_FIRST, 0, b"first"))
    link._notify(None, seg(ble.SAR_LAST, 0, b"-half"))
    link._notify(None, seg(ble.SAR_FIRST, 0, b"secon"))
    link._notify(None, seg(ble.SAR_LAST, 0, b"d-one"))
    assert got == [b"first-half", b"second-one"]


def test_a_complete_message_does_not_inherit_a_half_received_one(link):
    got = frames(link)
    link._notify(None, seg(ble.SAR_FIRST, 0, b"orphan"))
    link._notify(None, seg(ble.SAR_COMPLETE, 0, b"whole"))
    assert got == [b"whole"]
    assert link._rx == b"" and link._rx_type is None


def test_a_continuation_of_nothing_is_dropped(link):
    """We can join a link mid-message, or lose the first segment to the air.

    Taking the tail of a message as if it were the whole of one produces bytes
    that fail to authenticate, which is indistinguishable in a log from a
    network problem.
    """
    got = frames(link)
    link._notify(None, seg(ble.SAR_CONTINUE, 0, b"tail"))
    link._notify(None, seg(ble.SAR_LAST, 0, b"end"))
    assert got == []


def test_segments_that_disagree_about_their_type_are_dropped(link):
    """Every segment of one message carries the same message type.

    Two that disagree are two interleaved messages, and concatenating them
    makes a third thing that is neither. The old code took the type from the
    *last* segment, so a beacon's tail could decide that a network PDU was a
    beacon.
    """
    got = frames(link)
    beacons_before = link.beacons
    link._notify(None, seg(ble.SAR_FIRST, 0, b"net-pdu"))
    link._notify(None, seg(ble.SAR_LAST, 1, beacon()))
    assert got == []
    assert link.beacons == beacons_before
    assert link._rx_type is None


def test_a_reassembly_that_never_ends_is_abandoned(link):
    """Anyone in radio range can write to this characteristic.

    Without a cap, continuation segments that never end grow a bytearray until
    the host runs out of memory - a way to take the bridge down that needs no
    key, no pairing and no credential.
    """
    got = frames(link)
    link._notify(None, seg(ble.SAR_FIRST, 0, b"x" * 19))
    for _ in range(ble.MAX_PROXY_PDU // 19 + 2):
        link._notify(None, seg(ble.SAR_CONTINUE, 0, b"x" * 19))
    assert len(link._rx) <= ble.MAX_PROXY_PDU
    assert link._rx_type is None
    link._notify(None, seg(ble.SAR_LAST, 0, b"x"))
    assert got == []


def test_a_message_exactly_at_the_cap_still_arrives(link):
    # The bound is a bound, not an off-by-one that eats legitimate traffic.
    got = frames(link)
    link._notify(None, seg(ble.SAR_FIRST, 0, b"a"))
    link._notify(None, seg(ble.SAR_LAST, 0, b"b" * (ble.MAX_PROXY_PDU - 1)))
    assert got == [b"a" + b"b" * (ble.MAX_PROXY_PDU - 1)]


def test_a_dropped_link_does_not_leave_half_a_message_behind(link):
    """The buffer used to survive a disconnect.

    So the tail of a message cut short by a dead link was still sitting there
    when the replacement link delivered the start of something else, and the
    two were spliced into a PDU that failed to authenticate.
    """
    got = frames(link)
    link._notify(None, seg(ble.SAR_FIRST, 0, b"cut-short"))
    link._mark_down()
    assert link._rx == b"" and link._rx_type is None
    link._notify(None, seg(ble.SAR_LAST, 0, b"unrelated"))
    assert got == []


def test_an_empty_notification_is_not_an_error(link):
    link._notify(None, bytearray())
    assert link._rx == b""


# --- proxy configuration messages ---------------------------------------------

def test_a_proxy_configuration_message_reaches_its_handler(link):
    got: list[bytes] = []
    link.on_proxy_config = got.append
    link._notify(None, seg(ble.SAR_COMPLETE, 2, b"filter-status"))
    assert got == [b"filter-status"]


def test_a_segmented_proxy_configuration_message_is_reassembled(link):
    # A Filter Status is 21 octets, which fits one notification - but only just,
    # and nothing guarantees the proxy's MTU, so the path has to work.
    got: list[bytes] = []
    link.on_proxy_config = got.append
    link._notify(None, seg(ble.SAR_FIRST, 2, b"filter"))
    link._notify(None, seg(ble.SAR_LAST, 2, b"-status"))
    assert got == [b"filter-status"]


# --- beacon silence -----------------------------------------------------------

def test_a_link_that_has_never_heard_a_beacon_reports_how_long(link, monkeypatch):
    """Silence here is the one fault with no other symptom.

    Everything keeps working while the IV index does not move; when it does,
    every message the bridge sends is discarded by nodes that moved on without
    it, and nothing in the bridge would ever have mentioned why.
    """
    connected(link)
    assert link.beacon_silence < 1.0
    now = time.monotonic() + ble.BEACON_SILENCE + 1
    monkeypatch.setattr(ble.time, "monotonic", lambda: now)
    assert link.beacon_silence >= ble.BEACON_SILENCE


def test_one_authenticated_beacon_ends_the_silence_for_good(link, monkeypatch):
    connected(link)
    notify(link, beacon(iv=7))
    assert link.beacons == 1
    now = time.monotonic() + ble.BEACON_SILENCE * 10
    monkeypatch.setattr(ble.time, "monotonic", lambda: now)
    assert link.beacon_silence == 0.0


def test_an_unauthenticated_beacon_does_not_end_the_silence(link, monkeypatch):
    # Counting these would let anyone in radio range silence the warning
    # without being able to tell us a single thing about the IV index.
    connected(link)
    assert notify(link, beacon(iv=999999, mac=bytes(8))) == []
    assert link.beacons == 0
    now = time.monotonic() + ble.BEACON_SILENCE + 1
    monkeypatch.setattr(ble.time, "monotonic", lambda: now)
    assert link.beacon_silence >= ble.BEACON_SILENCE


def test_a_link_that_is_down_is_not_called_silent(link):
    # It has not had the chance to hear anything, and "no beacons" would be
    # the second warning about a disconnection that is already reported.
    assert link.connected is False
    assert link.beacon_silence == 0.0


# --- a send that fails on an open connection ---------------------------------

def connected(link):
    """Bring the fixture link up and hand back the client behind it."""
    link.notify_fails = False
    asyncio.run(link.connect())
    return link.made[0]


def test_a_failed_write_closes_the_connection_it_failed_on(link):
    """A write error does not imply the GATT link went away.

    And while it is still open the proxy node does not advertise, so the
    supervisor's next scan cannot see the one node we can reach. Dropping the
    reference without closing it leaves recovery to the BlueZ cache fallback,
    which is a different code path and not always populated.
    """
    client = connected(link)
    link.write_fails = True
    with pytest.raises(OSError):
        asyncio.run(link.send(b"\x00" * 20))
    assert client.disconnected is True
    assert link._client is None
    assert link.connected is False


def test_a_failed_write_still_raises_after_the_cleanup(link):
    # The caller retries on ConnectionError and gives up on nothing else, so
    # swallowing this would turn a dropped radio into a command that silently
    # never happened.
    client = connected(link)
    link.write_fails = True
    link.disconnect_fails = True          # the teardown fails too
    with pytest.raises(OSError, match="le-connection-abort-by-local"):
        asyncio.run(link.send(b"\x00" * 20))
    assert client.disconnected is True
    assert link._client is None


def test_a_successful_send_leaves_the_link_alone(link):
    client = connected(link)
    asyncio.run(link.send(b"\xAB" * 20))
    assert client.written == [b"\x00" + b"\xAB" * 20]
    assert client.disconnected is False
    assert link.connected is True


def test_two_senders_cannot_splice_their_segments_together(link):
    """Proxy SAR has no message id; the proxy reassembles by order alone.

    So a second writer landing between the parts of a segmented message makes
    the proxy join pieces of two different PDUs, and throws both away. The
    bridge really does have two writers: a command holds the bridge's send
    lock, but the proxy filter written from on_connected does not.
    """
    client = connected(link)
    client.mtu_size = 23              # the BLE minimum, so everything segments

    async def hand_over_the_loop():
        for _ in range(4):
            await asyncio.sleep(0)

    link.on_write = hand_over_the_loop

    async def both():
        await asyncio.gather(link.send(b"\xAA" * 40), link.send(b"\xBB" * 40))

    asyncio.run(both())

    assert len(client.written) == 6                  # three segments each
    senders = [w[1:2] for w in client.written]
    assert senders in ([b"\xAA"] * 3 + [b"\xBB"] * 3,
                       [b"\xBB"] * 3 + [b"\xAA"] * 3)
    # And the SAR bits still read first/continuation/last, twice over.
    assert [w[0] & 0xC0 for w in client.written] == [0x40, 0x80, 0xC0] * 2


def test_a_send_that_waited_its_turn_does_not_write_to_the_dead_link(link):
    """Waiting for the lock is waiting long enough for the link to change.

    The caller in front may have been the one whose write failed and tore the
    connection down. Deciding which client to use before the wait would aim
    this write at a connection that no longer exists.
    """
    client = connected(link)

    async def lose_the_link():
        await asyncio.sleep(0)        # let the sender behind us queue up first
        link._client = None
        client.is_connected = False

    link.on_write = lose_the_link

    async def both():
        first = asyncio.create_task(link.send(b"\xAA" * 20))
        second = asyncio.create_task(link.send(b"\xBB" * 20))
        return await asyncio.gather(first, second, return_exceptions=True)

    first, second = asyncio.run(both())
    assert first is None
    assert isinstance(second, ConnectionError)
    assert client.written == [b"\x00" + b"\xAA" * 20]


def test_a_failed_write_does_not_take_down_the_link_that_replaced_it(link):
    """Closing the dead link is an await, and the supervisor reconnects in it.

    Clearing `_client` unconditionally afterwards threw away a connection that
    was working, while leaving it open - and a proxy stops advertising for as
    long as anything is attached to it, so the bridge would then be hunting
    for a node that no scan can see.
    """
    old = connected(link)
    replacement = object()
    link.write_fails = True

    async def the_supervisor_gets_there_first():
        link._client = replacement
        link._up.set()

    link.on_close = the_supervisor_gets_there_first
    with pytest.raises(OSError):
        asyncio.run(link.send(b"\x00" * 20))

    assert old.disconnected is True          # the one that failed is closed
    assert link._client is replacement       # the one that works is untouched
    assert link._up.is_set()


def test_marking_the_link_down_does_no_io(link):
    """`_mark_down` is reached from bleak's own disconnected callback.

    Calling back into the client from there can block on BlueZ, so closing an
    abandoned link is `_close`'s job and not this one's. A disconnect() call
    appearing here would deadlock the exact path it is meant to clean up.
    """
    client = connected(link)
    link._mark_down()
    assert client.disconnected is False
    assert link._client is None
    assert not link._up.is_set()


# --- stale disconnect callbacks ----------------------------------------------

def test_a_disconnect_from_a_replaced_client_is_ignored(link):
    current, abandoned = object(), object()
    link._client = current
    link._up.set()

    link._on_disconnect(abandoned)
    assert link._client is current
    assert link._up.is_set()

    link._on_disconnect(current)
    assert link._client is None
    assert not link._up.is_set()


# --- the backoff, and what is allowed to reset it -----------------------------

class _ScriptDone(BaseException):
    """Ends supervise(), which otherwise runs forever.

    A BaseException on purpose: supervise() catches Exception and treats it as
    a failed attempt, which is exactly what this must not look like.
    """


class _Stub:
    """Stands in for a connected client; `connected` only asks it one thing."""
    is_connected = True


def unreachable() -> ble.LinkDown:
    return ble.LinkDown(ble.STATE_NO_PROXY, "no node is advertising in range")


def supervisor_run(link, monkeypatch, script) -> list[float]:
    """Run supervise() over a scripted list of attempts; return the gaps it left.

    Each entry is either an exception for connect() to raise, or "held" or
    "flapped" for a link that comes up and then drops - after long enough to
    count as a working connection, or far too soon to.
    """
    delays: list[float] = []
    steps = list(script)

    async def fake_sleep(seconds):
        delays.append(seconds)

    async def connect():
        if not steps:
            raise _ScriptDone
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        link._client = _Stub()
        link._up.set()
        link._up_since = time.monotonic() - (ble.STABLE_AFTER + 1 if step == "held" else 0.0)

        def die():
            link._client = None
            link._up.clear()
            link._wake.set()

        asyncio.get_running_loop().call_soon(die)

    monkeypatch.setattr(link, "connect", connect)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_ScriptDone):
        asyncio.run(link.supervise())
    return delays


def test_the_gap_between_attempts_doubles(link, monkeypatch):
    link.s.ble_retry_min, link.s.ble_retry_max = 2.0, 120.0
    delays = supervisor_run(link, monkeypatch,
                            [unreachable(), unreachable(), unreachable(), unreachable()])
    assert delays == [2.0, 4.0, 8.0, 16.0]


def test_the_gap_stops_doubling_at_the_configured_maximum(link, monkeypatch):
    link.s.ble_retry_min, link.s.ble_retry_max = 2.0, 8.0
    delays = supervisor_run(link, monkeypatch, [unreachable()] * 5)
    assert delays == [2.0, 4.0, 8.0, 8.0, 8.0]


def test_a_link_that_dies_at_once_does_not_reset_the_backoff(link, monkeypatch):
    """The flapping proxy. It accepts the GATT connection and drops it a
    second later, over and over.

    Counting that as a success put the retry gap back to its shortest on every
    cycle, so the bridge hammered the node at full speed for as long as the
    flapping lasted - which is the one situation the backoff exists for. The
    failure count went back to zero with it, so the recovery ladder never got
    past its first rung either.
    """
    link.s.ble_retry_min, link.s.ble_retry_max = 2.0, 120.0
    delays = supervisor_run(link, monkeypatch,
                            [unreachable(), unreachable(), "flapped",
                             unreachable(), unreachable()])
    assert delays == [2.0, 4.0, 8.0, 16.0]
    assert link._fails == 4


def test_a_link_that_held_does_reset_the_backoff(link, monkeypatch):
    """The other half of the same rule, and the reason it is a rule and not a
    ban: once a connection has carried a minute of traffic the conditions have
    demonstrably changed, and making the next hiccup wait two minutes would be
    punishing the bridge for a problem that went away."""
    link.s.ble_retry_min, link.s.ble_retry_max = 2.0, 120.0
    delays = supervisor_run(link, monkeypatch,
                            [unreachable(), unreachable(), "held",
                             unreachable(), unreachable()])
    assert delays == [2.0, 4.0, 2.0, 4.0]
    assert link._fails == 2


def test_a_scan_that_found_the_proxy_ends_the_long_scan_immediately(link, monkeypatch):
    """Unlike the backoff, this costs nothing if the link is short-lived: the
    next failed attempt puts the long scan straight back."""
    link._long_scan = True
    supervisor_run(link, monkeypatch, ["flapped"])
    assert link._long_scan is False


def test_the_supervisor_outlives_a_failure_it_cannot_report(link, monkeypatch, caplog):
    """The bookkeeping after a failed attempt is not safe code.

    It publishes a status that goes out over MQTT and it power-cycles the
    adapter over D-Bus. While it sat outside the loop's own protection, one
    throw from either ended the supervisor - and the bridge went on accepting
    commands, indefinitely, over a radio that would now never reconnect.
    """
    async def the_bus_went_away(_rung):
        raise RuntimeError("d-bus went away")

    monkeypatch.setattr(link, "_recover", the_bus_went_away)
    link.s.ble_retry_min, link.s.ble_retry_max = 2.0, 120.0
    with caplog.at_level("ERROR"):
        delays = supervisor_run(link, monkeypatch,
                                [unreachable(), unreachable(), unreachable()])
    assert delays == [2.0, 4.0, 8.0]
    assert "handling a failed attempt itself failed" in caplog.text


# --- choosing a radio, and saying which kind of "down" this is ----------------
#
# The failure this is all for: two adapters in one host, one of which cannot
# hear the mesh at all. The log said nothing but "connect, drop, connect" and
# the only way to find out was to scan from both by hand and compare.

class Dev:
    def __init__(self, address):
        self.address = address


class Adv:
    def __init__(self, rssi, net_id=NET_ID, service_type=0x00):
        self.rssi = rssi
        self.service_data = {PROXY_SERVICE: bytes([service_type]) + net_id}


@pytest.fixture
def radios(settings, monkeypatch):
    """A link whose adapters, and what each of them can hear, are ours to set."""
    settings.ble_scan_timeout = 0.0
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))
    lk = ProxyLink(KEYS, settings)
    lk.adapters = [Adapter("hci0", "AA:AA:AA:AA:AA:01"),
                   Adapter("hci1", "C4:7B:2F:91:0D:8E")]
    lk.hears = {}                 # adapter name -> {address: rssi}
    lk.scanned = []
    lk.powered = []

    async def list_adapters():
        if isinstance(lk.adapters, BaseException):
            raise lk.adapters
        return list(lk.adapters)

    async def discover(timeout=0.0, return_adv=False, adapter=None, **_kw):
        lk.scanned.append((adapter, timeout))
        return {addr: (Dev(addr), Adv(rssi))
                for addr, rssi in lk.hears.get(adapter, {}).items()}

    async def power_cycle(name, settle=2.0):
        lk.powered.append(name)

    monkeypatch.setattr(adapter_mod, "list_adapters", list_adapters)
    monkeypatch.setattr(adapter_mod, "power_cycle", power_cycle)
    monkeypatch.setattr(ble.BleakScanner, "discover", staticmethod(discover))
    # The BlueZ cache fallback needs a real bus; here it has nothing to add.
    monkeypatch.setattr(ProxyLink, "_find_cached", lambda self: _none())
    return lk


async def _none():
    return None


def test_a_host_with_no_radio_is_its_own_state(radios):
    """Not "connection failed". The dongle has been pulled, and no amount of
    scanning or reconnecting will help until someone plugs it back in."""
    radios.adapters = []
    with pytest.raises(ble.LinkDown) as caught:
        asyncio.run(radios.connect())
    assert caught.value.state == ble.STATE_NO_ADAPTER
    assert radios.scanned == []          # nothing to scan on, so nothing is tried


def test_radios_that_hear_nothing_are_their_own_state(radios):
    """The failure of 2026-11-19. The adapters are alive, the bridge is alive,
    and not one node of our network is audible."""
    with pytest.raises(ble.LinkDown) as caught:
        asyncio.run(radios.connect())
    assert caught.value.state == ble.STATE_NO_PROXY
    assert [a for a, _ in radios.scanned] == ["hci0", "hci1"]


def test_the_adapter_that_hears_the_mesh_wins(radios):
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"
    assert radios.last_rssi == -72


def test_the_strongest_of_several_candidates_wins(radios):
    radios.hears = {"hci0": {"AA:AA:AA:AA:AA:AA": -90},
                    "hci1": {"DA:DF:34:4C:35:B9": -72}}
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"


def test_a_configured_adapter_is_used_without_a_survey(radios):
    radios._wanted = "hci1"
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"
    assert radios.scanned == []


def test_a_configured_address_survives_a_renumbering(radios):
    """The reason BLE_ADAPTER takes a MAC. Same radio, different `hciN`."""
    radios._wanted = "C4:7B:2F:91:0D:8E"
    radios.adapters = [Adapter("hci0", "C4:7B:2F:91:0D:8E")]
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci0"


def test_an_adapter_that_is_not_there_is_reported_and_worked_around(radios, caplog):
    """Not fatal: `hciN` numbers are handed out at boot, so a name that was
    right yesterday can be wrong today through no fault of the configuration."""
    radios._wanted = "hci7"
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}
    with caplog.at_level("WARNING"):
        asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"
    assert "hci7" in caplog.text


def test_an_adapter_that_disappears_is_replaced(radios):
    radios.adapter = Adapter("hci1", "C4:7B:2F:91:0D:8E")
    radios.adapters = [Adapter("hci0", "AA:AA:AA:AA:AA:01")]
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci0"


def test_bluez_refusing_to_be_asked_is_not_fatal(radios, caplog):
    """bleak reaches BlueZ by its own route, so failing to enumerate costs the
    choice of adapter, not the link."""
    radios.adapters = BlueZUnavailable("no bus")
    radios._wanted = "hci1"
    with caplog.at_level("WARNING"):
        asyncio.run(radios._ensure_adapter())
    assert radios.adapter_name == "hci1"
    assert "cannot ask BlueZ" in caplog.text


def test_a_configured_address_that_matches_nothing_is_named_and_worked_around(radios, caplog):
    """The name form is covered above; the address form is the one in use.

    A MAC cannot be mistyped into another adapter's, so a MAC that matches
    nothing means the radio it names is not in the machine. The bridge has to
    say which value it could not honour, then carry on with a radio that works.
    """
    radios._wanted = "C4:7B:2F:91:0D:FF"
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}
    with caplog.at_level("WARNING"):
        asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"
    assert "C4:7B:2F:91:0D:FF" in caplog.text


def test_a_configured_address_is_not_claimed_when_bluez_cannot_be_asked(radios, caplog):
    """The quiet one, and the configuration the live bridge actually runs.

    bleak takes an `hciN` name or nothing at all, so an address means something
    only after BlueZ has listed the adapters - and here that is the step that
    failed. The setting does not apply; a line saying "carrying on with
    C4:7B:..." would state the opposite of what is happening, and nothing
    later in the log would ever correct it.
    """
    radios.adapters = BlueZUnavailable("no bus")
    radios._wanted = "C4:7B:2F:91:0D:8E"
    with caplog.at_level("WARNING"):
        asyncio.run(radios._ensure_adapter())
    assert radios.adapter_name is None           # nothing is handed to bleak
    assert "NOT with BLE_ADAPTER=C4:7B:2F:91:0D:8E" in caplog.text
    assert "hci" in caplog.text                  # and says which form would apply


def test_the_enumeration_warning_is_said_once_per_outage(radios, caplog):
    """It is on the path of every single attempt, and attempts are endless."""
    radios.adapters = BlueZUnavailable("no bus")
    with caplog.at_level("WARNING"):
        for _ in range(3):
            asyncio.run(radios._ensure_adapter())
    assert caplog.text.count("cannot ask BlueZ") == 1

    radios.adapters = [Adapter("hci0", "AA:AA:AA:AA:AA:01")]
    asyncio.run(radios._ensure_adapter())        # the outage ends
    radios.adapters = BlueZUnavailable("no bus")
    with caplog.at_level("WARNING"):
        asyncio.run(radios._ensure_adapter())    # a new one is worth saying again
    assert caplog.text.count("cannot ask BlueZ") == 2


def test_losing_every_radio_forgets_the_one_that_was_in_use(radios):
    """Otherwise the Home Assistant sensor goes on naming a radio that has been
    unplugged, sitting next to a status that says there is no adapter."""
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter is not None

    radios.adapters = []
    with pytest.raises(ble.LinkDown):
        asyncio.run(radios._ensure_adapter())
    assert radios.adapter is None
    d = radios.diagnostics()
    assert d["adapter_address"] is None and d["rssi"] is None


def test_a_survey_that_hears_nothing_keeps_the_configured_radio(radios):
    """An outage must not un-pin BLE_ADAPTER.

    Nothing was heard, so the survey has no evidence to prefer any radio - and
    whatever it settles on is then kept, without consulting BLE_ADAPTER again,
    until the next forced survey several failures later. Falling back to the
    first adapter would therefore park the bridge on the onboard radio for the
    whole of an ordinary outage, and leave it there when the mesh comes back.
    """
    radios._wanted = "C4:7B:2F:91:0D:8E"             # which is hci1 here
    radios.adapters = [Adapter("hci0", "AA:AA:AA:AA:AA:01"),
                       Adapter("hci1", "C4:7B:2F:91:0D:8E")]
    radios._force_survey = True
    with pytest.raises(ble.LinkDown) as caught:
        asyncio.run(radios._ensure_adapter())
    assert caught.value.state == ble.STATE_NO_PROXY
    assert radios.adapter.name == "hci1"

    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}   # the mesh comes back
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"


def test_an_unconfigured_survey_that_hears_nothing_still_has_a_radio(radios):
    """With no adapter at all the next attempt would have nothing to scan on,
    and this state exists to keep retrying while saying what is wrong."""
    with pytest.raises(ble.LinkDown):
        asyncio.run(radios._ensure_adapter())
    assert radios.adapter is not None


def test_the_only_adapter_is_taken_without_a_scan(radios):
    """A survey compares radios. With one radio there is nothing to compare,
    and the scan would only spend time before the attempt that matters."""
    radios.adapters = [Adapter("hci0", "AA:AA:AA:AA:AA:01")]
    radios._force_survey = True
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci0"
    assert radios.scanned == []


# --- what the messages tell the reader to do ---------------------------------

@pytest.mark.parametrize("raw, act_on", [
    ("org.freedesktop.DBus.Error.ServiceUnknown", "systemctl status bluetooth"),
    ("No powered Bluetooth adapter found", "bluetoothctl power on"),
    ("No powered Bluetooth adapter found", "rfkill unblock bluetooth"),
    ("Operation not permitted", "network_mode: host"),
    ("[Errno 111] Connection refused: /run/dbus/system_bus_socket", "/var/run/dbus"),
])
def test_a_bluez_failure_is_turned_into_something_to_do(raw, act_on):
    """These reach the reader as the whole explanation of why nothing works.

    BlueZ states what it would not do, which is never the thing to change: the
    remedy is a line of compose, a service that is not running, or a radio that
    is blocked, and none of them appear in the error. A message that only
    restates the failure leaves a reader with somewhere to search rather than
    something to try.
    """
    assert act_on in ble.explain_ble_error(RuntimeError(raw))


def test_an_unrecognised_failure_is_passed_through_whole():
    """Better the raw text than a guess: an explanation that does not fit the
    failure sends the reader somewhere there is nothing to find."""
    assert ble.explain_ble_error(RuntimeError("le-connection-abort-by-local")) \
        == "le-connection-abort-by-local"


# --- the recovery ladder, as the supervisor drives it -------------------------

def test_the_first_rung_scans_for_longer(radios):
    radios.adapter = Adapter("hci1", "C4:7B:2F:91:0D:8E")
    radios.s.ble_scan_timeout = 10.0
    radios._fails = 3
    asyncio.run(radios._recover(ble.recovery_rung(radios._fails)))
    asyncio.run(radios._scan("hci1"))
    assert radios.scanned == [("hci1", 10.0 * ble.LONG_SCAN_FACTOR)]


def test_the_second_rung_power_cycles_the_adapter(radios):
    radios.adapter = Adapter("hci1", "C4:7B:2F:91:0D:8E")
    radios._fails = 6
    asyncio.run(radios._recover(ble.recovery_rung(radios._fails)))
    assert radios.powered == ["hci1"]


def test_the_third_rung_looks_at_every_adapter_again(radios):
    """Even at an adapter that is present and configured: being there is not
    the same as being able to hear the network."""
    radios.adapter = Adapter("hci0", "AA:AA:AA:AA:AA:01")
    radios._wanted = "hci0"
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}
    radios._fails = 9
    asyncio.run(radios._recover(ble.recovery_rung(radios._fails)))
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"


def test_the_second_rung_says_why_it_could_not_run(radios, caplog):
    """BlueZ powers an adapter by name, and there is no name to use.

    Said rather than skipped. Every other rung logs the reason it ran, so a
    rung that returns in silence reads, in the log, exactly like one that ran
    and did not help - and the real problem is a step further back.
    """
    radios.adapter = None
    radios._wanted = ""
    radios._fails = 6
    with caplog.at_level("WARNING"):
        asyncio.run(radios._recover(2))
    assert radios.powered == []
    assert "nothing to power-cycle" in caplog.text
    assert "cannot ask BlueZ" in caplog.text      # points at the line to act on


def test_a_power_cycle_bluez_refuses_is_logged_not_raised(radios, monkeypatch):
    async def refuse(_name, settle=2.0):
        raise BlueZUnavailable("BlueZ would not power-cycle hci1")

    monkeypatch.setattr(adapter_mod, "power_cycle", refuse)
    radios.adapter = Adapter("hci1", "C4:7B:2F:91:0D:8E")
    radios._fails = 6
    asyncio.run(radios._recover(2))          # does not raise


# --- what Home Assistant is told ---------------------------------------------

def test_diagnostics_name_the_radio_and_the_state(radios):
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}
    asyncio.run(radios._ensure_adapter())
    d = radios.diagnostics()
    assert d["up"] is False
    assert d["adapter"] == "hci1 (C4:7B:2F:91:0D:8E)"
    assert d["adapter_address"] == "C4:7B:2F:91:0D:8E"
    assert d["rssi"] == -72
    assert d["drops"] == 0 and d["last_drop"] is None


def test_a_drop_is_counted_and_timestamped(link):
    connected(link)
    assert link.diagnostics()["up"] is True
    link._mark_down()
    d = link.diagnostics()
    assert d["up"] is False
    assert d["drops"] == 1
    assert d["status"] == ble.STATE_FAILING
    assert d["last_drop"] and d["last_drop"].endswith("+00:00")


def test_a_failed_attempt_is_not_counted_as_a_drop(link):
    """Different events. A connection that never came up did not drop, and
    counting it would make the number useless for spotting a flapping link."""
    with pytest.raises(RuntimeError):
        asyncio.run(link.connect())
    assert link.diagnostics()["drops"] == 0


def test_an_unchanged_answer_is_not_reported_again(radios):
    """Diagnostics are retained in MQTT, so republishing what is already there
    is pure noise. The survey reports (it learned both an adapter and an RSSI);
    asking again, with nothing changed, must say nothing at all."""
    seen = []
    radios.on_diagnostics = lambda: seen.append(radios.diagnostics()["adapter"])
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}

    asyncio.run(radios._ensure_adapter())
    assert set(seen) == {"hci1 (C4:7B:2F:91:0D:8E)"}

    settled = len(seen)
    asyncio.run(radios._ensure_adapter())    # same answer, nothing new to say
    assert len(seen) == settled


def test_moving_to_another_radio_is_reported_to_home_assistant(radios):
    """The one change the sensor exists for.

    A bridge that silently switched radios would leave the only outward sign of
    a dying dongle - the adapter sensor - still naming the dongle, so the move
    has to reach MQTT on the attempt that makes it.
    """
    seen = []
    radios.on_diagnostics = lambda: seen.append(radios.diagnostics()["adapter"])
    radios.hears = {"hci1": {"DA:DF:34:4C:35:B9": -72}}
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci1"

    assert radios.diagnostics()["rssi"] == -72

    radios.adapters = [Adapter("hci0", "AA:AA:AA:AA:AA:01")]    # the dongle is pulled
    asyncio.run(radios._ensure_adapter())
    assert radios.adapter.name == "hci0"
    assert seen[-1] == "hci0 (AA:AA:AA:AA:AA:01)"
    # And the signal reading goes with it: -72 dBm was measured on an antenna
    # that is no longer in the machine, so leaving it would read as a live
    # reading from the radio that replaced it.
    assert radios.diagnostics()["rssi"] is None
