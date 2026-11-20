"""BLE GATT Mesh Proxy link (bleak), with start-up diagnostics and backoff.

Connection policy
-----------------
One supervisor task owns the link. Everything else just calls `send()`, which
waits for the link to be up. Failures are retried with exponential backoff
capped at BLE_RETRY_MAX, and the log gets ONE line per state change rather than
one line per attempt - a proxy that is out of range for an hour should not
produce an hour of identical warnings.

Proxy selection: when PROXY_ADDRESS is unset we scan for nodes advertising our
network ID and take the strongest RSSI *at connect time*. We deliberately do
not hop to a better proxy while connected; a working link is worth more than a
few dBm, and re-connecting drops the proxy filter and costs sequence numbers.

When the scan finds nothing we ask BlueZ once for a device it already knows.
A mesh proxy stops advertising for as long as a GATT client is connected to it,
so after an unclean exit (a container restart, say) the node the previous
process was using is invisible to every scan while the host still holds the
connection open. That looks exactly like "no proxy in range" and never resolves
by itself.

Why "down" is not one state
---------------------------
"The link is down" used to be a single condition, and the three things it can
actually mean need completely different actions:

- there is no adapter (the dongle was pulled, or BlueZ lost it);
- there is an adapter but it cannot hear a single advertisement from our
  network, which is what a deaf radio looks like - the onboard radio of a Pi
  standing a room away will do this while a USB dongle in the same slot sees
  the same node perfectly well;
- the proxy is there and the GATT link will not hold.

They are told apart here, they are named in the log, and they are published to
Home Assistant, because the middle one is invisible otherwise: the log fills up
with connect-and-drop cycles and nothing says the radio simply never heard the
network.

Repeated failure escalates: scan for longer, then power-cycle the adapter
through BlueZ, then re-check every adapter and move to one that can see the
network. Each step logs the one line that says why it happened.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from bleak import BleakClient, BleakScanner

from . import adapter as adapter_mod, mesh
from .adapter import Adapter, BlueZUnavailable
from .config import ConfigError, Settings

log = logging.getLogger("ble")

PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"
DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"
DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

DBUS_SOCKET = Path("/var/run/dbus/system_bus_socket")

_DBUS_HINT = (
    "Give the container the host bus and radio: `network_mode: host` plus "
    "`- /var/run/dbus:/var/run/dbus` in compose.yaml.")

# The link states, in the exact words that go to Home Assistant and to the log.
STATE_STARTING = "starting"
STATE_UP = "connected"
STATE_NO_ADAPTER = "no adapter"
STATE_NO_PROXY = "no proxy in range"
STATE_FAILING = "proxy seen, link failing"

# How many consecutive failures buy one rung of the recovery ladder. Low enough
# that a wedged radio is dealt with in a minute or two of backoff, high enough
# that an ordinary out-of-range moment is simply waited out.
ESCALATE_AFTER = 3
# What a "longer window" means once scanning normally has stopped working. A
# mesh proxy advertises every few hundred milliseconds, so a scan that finds
# nothing in ten seconds rarely finds something in thirty - but "rarely" is not
# "never", and this costs nothing but time we are spending anyway.
LONG_SCAN_FACTOR = 3.0

# Proxy PDU SAR field (Mesh Profile 6.3.1), the top two bits of every frame.
SAR_COMPLETE, SAR_FIRST, SAR_CONTINUE, SAR_LAST = 0, 1, 2, 3

# How much a reassembly is allowed to grow before it is abandoned. A network
# PDU is at most 29 octets and a proxy configuration PDU no larger, so this is
# an order of magnitude of headroom. There is a cap at all because the frames
# are fed by whatever is in radio range of the adapter: anyone can open this
# characteristic and send continuation segments that never end, and an
# unbounded bytearray on a Raspberry Pi is a way to take the bridge down
# without holding a single key.
MAX_PROXY_PDU = 384

# How long the link may be up without one authenticated Secure Network beacon
# before that silence is worth a line of its own. A proxy sends one as soon as
# a client connects, so this is generous by two orders of magnitude.
BEACON_SILENCE = 180.0

# How long a link has to hold before the connection that made it counts as a
# success. Below this the backoff is not reset, because a proxy that accepts
# the GATT connection and drops it a second later would otherwise reset it on
# every cycle: the bridge would reconnect at full speed for as long as the
# flapping lasted, which is the one case the backoff exists for. A link that
# has carried a minute of traffic has proved something; one that died during
# its first poll has not.
STABLE_AFTER = 60.0


def recovery_rung(fails: int, every: int = ESCALATE_AFTER) -> int:
    """Which recovery step a run of `fails` consecutive failures has earned.

    0 = none yet, 1 = scan for longer, 2 = power-cycle the adapter, 3 = look at
    every adapter again and move to whichever one can see the network.

    It stays on 3 rather than cycling back: rung 3 already contains a long scan
    on every radio in the machine, and it is the only rung that can change the
    answer to "which radio". Repeating the cheaper rungs forever would only add
    log lines.
    """
    if fails <= 0 or fails % every:
        return 0
    return min(3, fails // every)


def _plain(value):
    """dbus-fast hands every property back wrapped in a Variant."""
    return getattr(value, "value", value)


def cached_proxy_matches(objects: dict, network_id: bytes,
                         adapter: str | None) -> list[tuple[str, str, bool]]:
    """Pick the BlueZ device objects that carry our mesh network ID.

    `objects` is an ObjectManager.GetManagedObjects() reply. Returns
    (object path, address, connected), connected devices first: a connected one
    is precisely the device that is keeping itself out of our scan results.
    """
    want = b"\x00" + network_id
    prefix = f"/org/bluez/{adapter}/" if adapter else "/org/bluez/"
    out: list[tuple[str, str, bool]] = []
    for path, interfaces in objects.items():
        if not path.startswith(prefix):
            continue
        dev = interfaces.get("org.bluez.Device1")
        if not dev:
            continue
        data = _plain(_plain(dev.get("ServiceData")) or {}).get(PROXY_SERVICE)
        data = _plain(data)
        if not data or not bytes(data).startswith(want):
            continue
        address = _plain(dev.get("Address"))
        if not address:
            continue
        out.append((path, str(address), bool(_plain(dev.get("Connected")))))
    out.sort(key=lambda row: not row[2])
    return out


def explain_ble_error(ex: BaseException) -> str:
    """Map the usual BlueZ/D-Bus failures onto something a human can act on."""
    text = str(ex)
    low = text.lower()
    if "org.freedesktop.dbus.error.serviceunknown" in low or "org.bluez" in low and "unknown" in low:
        return ("BlueZ is not answering on the system D-Bus (org.bluez unknown). "
                "Is bluetooth running on the host (`systemctl status bluetooth`)? " + _DBUS_HINT)
    if "no powered bluetooth adapter" in low or "no bluetooth adapter" in low:
        return ("No powered Bluetooth adapter. Check `bluetoothctl list` on the host and power "
                "it on with `bluetoothctl power on`; if that is refused, the radio is blocked "
                "and `rfkill unblock bluetooth` is what clears it.")
    if "not permitted" in low or "accessdenied" in low or "permission denied" in low:
        return ("BlueZ refused access over D-Bus (permission denied). " + _DBUS_HINT)
    if "connection refused" in low and "dbus" in low:
        return ("Cannot reach the system D-Bus. " + _DBUS_HINT)
    return text


class LinkDown(ConnectionError):
    """The link is not up, and `state` says which of the three reasons it is.

    A ConnectionError so that everything which already treats a dead link as a
    connection failure keeps working; the extra field is only there for the
    log line and the Home Assistant sensor.
    """

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


async def preflight(s: Settings) -> None:
    """Fail fast if there is no way to reach a Bluetooth radio. Raises ConfigError."""
    if not DBUS_SOCKET.exists():
        raise ConfigError(
            f"No D-Bus socket at {DBUS_SOCKET}, so the container cannot talk to BlueZ. "
            + _DBUS_HINT)
    adapters: list[Adapter] = []
    try:
        adapters = await adapter_mod.list_adapters()
    except BlueZUnavailable as ex:
        # Not fatal here. bleak reaches BlueZ by itself, and the scan below is
        # the real test of whether Bluetooth works at all.
        log.debug("could not enumerate Bluetooth adapters (%s)", ex)
    chosen = adapter_mod.match(adapters, s.ble_adapter)
    if s.ble_adapter and adapters and chosen is None:
        # Deliberately not fatal: `hciN` numbers are handed out at boot, so a
        # name that was right yesterday can be wrong today through no fault of
        # the configuration. Saying so and carrying on beats refusing to start.
        log.warning("BLE_ADAPTER=%s is not one of the adapters BlueZ has (%s). The bridge "
                    "will pick whichever adapter can actually see the mesh.",
                    s.ble_adapter, ", ".join(a.label for a in adapters) or "none")
    try:
        await BleakScanner.discover(timeout=0.5, adapter=chosen.name if chosen else None)
    except ConfigError:
        raise
    except Exception as ex:  # noqa: BLE001 - bleak raises a wide family here
        raise ConfigError(f"Bluetooth is not usable: {explain_ble_error(ex)}") from None


class ProxyLink:
    def __init__(self, keys: mesh.Keys, s: Settings,
                 on_iv_index: Optional[Callable[[int, bool], None]] = None,
                 on_pdu: Optional[Callable[[bytes], None]] = None,
                 on_connected: Optional[Callable[[], Awaitable[None]]] = None,
                 on_state: Optional[Callable[[bool], None]] = None,
                 on_diagnostics: Optional[Callable[[], None]] = None,
                 on_proxy_config: Optional[Callable[[bytes], None]] = None) -> None:
        # The whole key set, not just the network ID: beacons have to be
        # authenticated here, and that needs the beacon key.
        self.keys = keys
        self.network_id = keys.network_id
        self.s = s
        self.address = s.proxy_address
        self.on_iv_index = on_iv_index
        self.on_pdu = on_pdu
        self.on_connected = on_connected
        self.on_state = on_state
        self.on_diagnostics = on_diagnostics
        self.on_proxy_config = on_proxy_config
        # Reassembly of a proxy PDU split across notifications. `_rx_type` is
        # both the message type of the reassembly and the flag that says one is
        # in progress; see _notify().
        self._rx = bytearray()
        self._rx_type: Optional[int] = None
        # Authenticated Secure Network beacons heard on this link, ever. Zero
        # for long enough is a fault all of its own: see `beacon_silence`.
        self.beacons = 0
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        # Writes to the proxy's DATA_IN characteristic, one message at a time.
        # Separate from _lock, which guards building and tearing down the link:
        # a send must not wait behind a scan, and holding _lock here would
        # deadlock connect() against its own on_connected hook.
        self._write = asyncio.Lock()
        self._up = asyncio.Event()
        self._wake = asyncio.Event()
        self._last_failure = ""
        self._down_since = time.monotonic()
        self._up_since = 0.0
        # What the configuration asked for, which is not necessarily what we
        # end up using: see _ensure_adapter().
        self._wanted = s.ble_adapter
        self.adapter: Optional[Adapter] = None
        self.status = STATE_STARTING
        self.last_rssi: Optional[int] = None
        self.drops = 0
        self.last_drop: Optional[str] = None
        self._fails = 0
        self._long_scan = False
        self._force_survey = False
        self._enumeration_warned = False
        self._hint: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def adapter_name(self) -> Optional[str]:
        """The adapter to hand bleak: whatever we settled on, else what was asked for."""
        if self.adapter is not None:
            return self.adapter.name
        # Before the first enumeration we have nothing better than the
        # configured value, and only the `hciN` form means anything to bleak.
        if self._wanted and adapter_mod.looks_like_adapter(self._wanted) \
                and self._wanted.lower().startswith("hci"):
            return self._wanted
        return None

    def _fallback_adapter_note(self) -> str:
        """What we are really about to scan on, said plainly.

        Worth a sentence of its own because of one case that is easy to report
        wrongly and impossible to spot afterwards: `BLE_ADAPTER` holding a MAC
        address. bleak takes an `hciN` name or nothing, so a MAC means something
        only once BlueZ has been asked which radio carries it - and this is the
        path where that question could not be asked. The setting then does not
        apply at all, which is the opposite of what a line saying "carrying on
        with C4:7B:..." would lead anyone to believe.
        """
        name = self.adapter_name
        if name:
            return f"carrying on with {name}"
        if self._wanted:
            return (f"carrying on with whichever adapter BlueZ picks, NOT with "
                    f"BLE_ADAPTER={self._wanted} - an adapter MAC can only be matched "
                    f"against the list that just failed to arrive, so set BLE_ADAPTER "
                    f"to the `hciN` name if you need it to apply regardless")
        return "carrying on with the default adapter"

    # --- diagnostics ------------------------------------------------------
    def diagnostics(self) -> dict:
        """What Home Assistant is told about the link. Plain JSON-able values."""
        return {
            "up": self._up.is_set(),
            "status": self.status,
            "adapter": self.adapter.label if self.adapter else (self._wanted or None),
            "adapter_name": self.adapter.name if self.adapter else None,
            "adapter_address": self.adapter.address if self.adapter else None,
            "rssi": self.last_rssi,
            "drops": self.drops,
            "last_drop": self.last_drop,
        }

    def _changed(self) -> None:
        if self.on_diagnostics:
            try:
                self.on_diagnostics()
            except Exception as ex:  # noqa: BLE001 - reporting must never break the link
                log.debug("ble: could not publish diagnostics (%r)", ex)

    def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            self._changed()

    def _adopt(self, chosen: Adapter) -> None:
        """Start using `chosen`, saying so once when it is a change."""
        if self.adapter is None or self.adapter.name != chosen.name \
                or self.adapter.address != chosen.address:
            log.info("ble: using adapter %s", chosen.label)
            if self.adapter is not None:
                self._forget_adapter()
            self.adapter = chosen
            self._changed()

    def _forget_adapter(self) -> None:
        """Stop using the current radio, and drop what only it could tell us.

        The RSSI goes with it. A signal reading only means anything next to the
        antenna that measured it, and carried across a change of adapter it
        would sit in Home Assistant looking like a live reading from the new
        one. On the path where that matters most - a host left with a single
        radio, which is adopted without a survey - nothing scans again to
        correct it, so the stale number would simply stay.
        """
        self.adapter = None
        self.last_rssi = None

    # --- discovery ------------------------------------------------------
    async def _ensure_adapter(self) -> None:
        """Decide which radio to scan and connect through.

        Called before every attempt, because the answer can change underneath
        us: a dongle can be unplugged, and `hciN` numbers move at boot. Raises
        LinkDown(STATE_NO_ADAPTER) when the host has no Bluetooth radio at all,
        which is a different problem from a radio that cannot hear anything.
        """
        try:
            adapters = await adapter_mod.list_adapters()
        except BlueZUnavailable as ex:
            # Never fatal: bleak has its own way to BlueZ, so a failure to
            # enumerate costs us the choice of adapter, not the link.
            if not self._enumeration_warned:
                log.warning("ble: cannot ask BlueZ which adapters exist (%s), %s. %s",
                            ex, self._fallback_adapter_note(), _DBUS_HINT)
                self._enumeration_warned = True
            return
        self._enumeration_warned = False
        if not adapters:
            # Forget whatever we were using. Keeping it would leave the
            # Home Assistant sensor naming a radio that is not in the machine
            # any more, next to a status that says there is no adapter.
            self._forget_adapter()
            raise LinkDown(STATE_NO_ADAPTER,
                           "BlueZ has no Bluetooth adapter at all - the radio is gone "
                           "(a USB dongle unplugged, or the host's bluetooth service "
                           "down). Check `bluetoothctl list` on the host, and "
                           "`rfkill unblock bluetooth` if it lists nothing")
        if self._force_survey:
            self._force_survey = False
            await self._survey(adapters)
            return
        if self.adapter is not None:
            # Keep what works, but re-read it: the same radio can come back
            # under a different `hciN` name.
            still = adapter_mod.match(adapters, self.adapter.address or self.adapter.name)
            if still is not None:
                self._adopt(still)
                return
            log.warning("ble: adapter %s is no longer there, looking for another one",
                        self.adapter.label)
            self._forget_adapter()
        chosen = adapter_mod.match(adapters, self._wanted)
        if chosen is not None:
            self._adopt(chosen)
            return
        if self._wanted:
            log.warning("ble: BLE_ADAPTER=%s is not among the adapters BlueZ has (%s), "
                        "choosing one that can see the mesh instead",
                        self._wanted, ", ".join(a.label for a in adapters))
        await self._survey(adapters)

    async def _survey(self, adapters: list[Adapter]) -> None:
        """Scan on every adapter and keep the one that hears our network best.

        This is the thing that had to be done by hand on the day the bridge
        went quiet: the two radios in the machine do not see the same network,
        and nothing short of scanning on both says which is which.
        """
        if len(adapters) == 1:
            self._adopt(adapters[0])
            return
        log.info("ble: checking which of %d adapters can see the mesh (%s)",
                 len(adapters), ", ".join(a.label for a in adapters))
        best: Optional[tuple[Adapter, str, int]] = None
        for a in adapters:
            found = await self._scan(a.name)
            if found is None:
                log.info("ble: %s hears nothing from our mesh", a.label)
                continue
            log.info("ble: %s hears the mesh at %s dBm", a.label, found[1])
            if best is None or found[1] > best[2]:
                best = (a, found[0], found[1])
        if best is None:
            # Adopt something anyway: with no adapter at all the next attempt
            # would have nothing to scan on, and the point of this state is to
            # keep retrying while saying plainly what is wrong.
            #
            # What it adopts matters more than it looks. The survey learned
            # nothing, so there is no evidence to prefer any radio - and
            # _ensure_adapter keeps whatever is adopted here without consulting
            # BLE_ADAPTER again until the next forced survey, nine failures
            # later. Falling back to whichever adapter is first would therefore
            # quietly un-pin the configured radio for the whole of an ordinary
            # outage: the mesh comes back, the configured dongle can hear it,
            # and the bridge is sitting on the onboard radio that cannot.
            self._adopt(adapter_mod.match(adapters, self._wanted) or adapters[0])
            raise LinkDown(
                STATE_NO_PROXY,
                f"not one of {len(adapters)} adapters can hear mesh network "
                f"{self.network_id.hex()} - the radios are alive but out of range of "
                "every node, or the network is down")
        self._adopt(best[0])
        # The survey just scanned; handing the address straight on saves
        # _find() from repeating the same scan seconds later.
        self._hint = best[1]
        self._note_rssi(best[2])

    def _note_rssi(self, rssi: int) -> None:
        if rssi != self.last_rssi:
            self.last_rssi = rssi
            self._changed()

    async def _scan(self, adapter_name: Optional[str]) -> Optional[tuple[str, int]]:
        """One scan on one adapter. Returns the strongest node on our network."""
        timeout = self.s.ble_scan_timeout
        if self._long_scan:
            timeout *= LONG_SCAN_FACTOR
        try:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True,
                                                adapter=adapter_name)
        except Exception as ex:  # noqa: BLE001 - one deaf adapter must not stop the survey
            log.debug("ble: scan on %s failed (%s)", adapter_name or "the default adapter", ex)
            return None
        best: Optional[tuple[str, int]] = None
        for dev, adv in found.values():
            data = adv.service_data.get(PROXY_SERVICE)
            # Mesh Proxy service data: type 0x00 = Network ID, then 8 bytes.
            if data and data[0] == 0x00 and data[1:9] == self.network_id:
                if best is None or adv.rssi > best[1]:
                    best = (dev.address, adv.rssi)
        return best

    async def _find(self) -> Optional[str]:
        if self.address:
            return self.address
        if self._hint:
            hint, self._hint = self._hint, None
            return hint
        best = await self._scan(self.adapter_name)
        if best:
            log.info("proxy candidate %s rssi=%s", *best)
            self._note_rssi(best[1])
            return best[0]
        return await self._find_cached()

    async def _find_cached(self) -> Optional[str]:
        """Second and last resort: a proxy BlueZ knows but that no longer advertises.

        Only a connected device is worth acting on here. We drop that stale
        connection and hand the address back, so the caller connects by address
        instead of waiting for an advertisement that will never come.
        """
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast.constants import BusType
        except ImportError:
            log.debug("dbus-fast is not installed, skipping the BlueZ cache lookup")
            return None
        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            root = bus.get_proxy_object("org.bluez", "/", await bus.introspect("org.bluez", "/"))
            manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
            matches = cached_proxy_matches(await manager.call_get_managed_objects(),
                                           self.network_id, self.adapter_name)
            for path, address, connected in matches:
                if not connected:
                    continue
                log.warning("ble: %s is still connected to our mesh at the host, so it has "
                            "stopped advertising and no scan can see it. Dropping that "
                            "connection and using it directly.", address)
                device = bus.get_proxy_object("org.bluez", path,
                                              await bus.introspect("org.bluez", path))
                await device.get_interface("org.bluez.Device1").call_disconnect()
                await asyncio.sleep(1.0)  # let BlueZ finish tearing the link down
                return address
            log.debug("BlueZ knows %d device(s) on our mesh, none of them connected",
                      len(matches))
            return None
        except Exception as ex:  # noqa: BLE001 - a failed fallback must not mask the real error
            log.debug("BlueZ cache lookup failed (%s)", ex)
            return None
        finally:
            if bus is not None:
                bus.disconnect()

    # --- notifications ---------------------------------------------------
    def _reset_rx(self) -> None:
        """Forget a half-received message.

        Called whenever a reassembly stops being meaningful: a new message
        starting, a malformed sequence, and every change of link state. That
        last one matters most. The buffer used to survive a disconnect, so the
        tail of a message cut short by a dropped link was still sitting there
        when the replacement link delivered the continuation of something else,
        and the two were spliced into one PDU that then failed to authenticate -
        a dropped message whose cause was invisible.
        """
        self._rx = bytearray()
        self._rx_type = None

    def _notify(self, _sender, data: bytearray) -> None:
        """One GATT notification: a whole proxy PDU, or a piece of one.

        Proxy PDU header: SAR (2 bits) | message type (6 bits), where type
        0 = network PDU, 1 = mesh beacon, 2 = proxy configuration.

        Every branch that is not "a complete, consistent message" throws the
        buffer away rather than carrying on with it. The bytes arriving here
        come from whatever is in radio range and have been authenticated by
        nothing yet, so a sequence that does not make sense is not something to
        reassemble as best we can - it is something to drop.
        """
        if not data:
            return
        sar, ptype = data[0] >> 6, data[0] & 0x3F
        if sar == SAR_COMPLETE:
            self._reset_rx()
            body = bytes(data[1:])
        elif sar == SAR_FIRST:
            self._rx = bytearray(data[1:])
            self._rx_type = ptype
            return
        else:
            if self._rx_type is None:
                # A continuation of nothing. We joined mid-message, or the
                # first segment was lost, or someone is writing noise at us.
                log.debug("ble: dropping a proxy segment that continues no message")
                return
            if ptype != self._rx_type:
                # Every segment of a message carries the same type. Two that
                # disagree are two different messages interleaved, and
                # concatenating them would produce a third thing that is
                # neither.
                log.debug("ble: dropping a proxy message whose segments disagree about "
                          "their type (%d then %d)", self._rx_type, ptype)
                self._reset_rx()
                return
            if len(self._rx) + len(data) - 1 > MAX_PROXY_PDU:
                log.warning("ble: a proxy message grew past %d bytes without ending; "
                            "dropping it", MAX_PROXY_PDU)
                self._reset_rx()
                return
            self._rx += data[1:]
            if sar == SAR_CONTINUE:
                return
            body, ptype = bytes(self._rx), self._rx_type
            self._reset_rx()
        self._dispatch(ptype, body)

    def _dispatch(self, ptype: int, body: bytes) -> None:
        if ptype == 1:
            beacon = mesh.parse_secure_beacon(self.keys, body)
            if beacon is None:
                # Anyone in radio range can send bytes down this characteristic,
                # and the IV index we take from them is written to disk and never
                # lowered. An unauthenticated beacon is therefore not noise, it is
                # a permanent outage, so it gets dropped rather than believed.
                log.debug("ignoring an unauthenticated mesh beacon: %s", body.hex())
            else:
                self.beacons += 1
                log.debug("secure network beacon, iv_index=%d iv_update=%s",
                          beacon.iv_index, beacon.iv_update)
                if self.on_iv_index:
                    self.on_iv_index(beacon.iv_index, beacon.iv_update)
        elif ptype == 0 and self.on_pdu:
            self.on_pdu(body)
        elif ptype == 2:
            log.debug("proxy config pdu %s", body.hex())
            if self.on_proxy_config:
                self.on_proxy_config(body)

    def _on_disconnect(self, client) -> None:
        # bleak reports the death of the client that died, which is not always
        # the one we are using: an abandoned link can announce itself long after
        # we built its replacement. Acting on that would tear down a healthy
        # connection and leave `_client` pointing at nothing.
        if self._client is not None and client is not self._client:
            log.debug("ble: ignoring a disconnect from a client we already replaced")
            return
        if self._up.is_set():
            log.warning("ble: proxy link dropped, reconnecting")
        self._mark_down(client)

    def _mark_down(self, client: object | None = None) -> None:
        """Record that the link is unusable. Deliberately does no I/O.

        This is reached from bleak's own disconnected callback, where the
        client is already being torn down and calling back into it can block
        or deadlock. Closing an abandoned-but-still-open link is therefore the
        job of `_close()`, which only the paths that know the link is still
        open call.

        `client` names the link the caller is giving up on, and nothing happens
        if that is not the link we are using any more. The paths that get here
        from a failure have already awaited something - a teardown, a write -
        and the supervisor can have built a replacement in that window.
        Clearing `_client` then would drop the only reference to a *working*
        connection while leaving it open, and a mesh proxy stops advertising
        for as long as anything is connected to it: the bridge would reconnect
        to a node that no scan can see any more. Passing nothing means
        unconditional, which is what shutdown and a failed attempt want.
        """
        if client is not None and self._client is not None and client is not self._client:
            log.debug("ble: not marking the link down for a client we already replaced")
            return
        self._reset_rx()
        if self._up.is_set():
            self._down_since = time.monotonic()
            self._up.clear()
            # Counted here rather than in the supervisor: this is the one place
            # every way of losing an established link goes through, and a drop
            # is a different event from a connection attempt that failed.
            self.drops += 1
            self.last_drop = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.status = STATE_FAILING
            if self.on_state:
                self.on_state(False)
            self._changed()
        self._client = None
        self._wake.set()

    async def _close(self, client) -> None:
        """Best-effort teardown of a link we have given up on.

        Bounded, because bleak's disconnect() waits on BlueZ and this runs on
        the path of a send that has already failed. Worth doing at all because
        a mesh proxy stops advertising for as long as a GATT client is
        connected to it: dropping the reference without closing the connection
        leaves the one node we can reach invisible to every scan, and getting
        back in then depends on the BlueZ cache fallback in _find_cached().
        """
        try:
            await asyncio.wait_for(client.disconnect(), 5.0)
        except Exception as ex:  # noqa: BLE001 - teardown must not mask the real error
            log.debug("ble: could not close the link we gave up on (%r)", ex)

    # --- connecting -------------------------------------------------------
    async def connect(self) -> None:
        """One connection attempt. Raises on failure."""
        async with self._lock:
            if self.connected:
                return
            await self._ensure_adapter()
            addr = await self._find()
            if not addr:
                raise LinkDown(
                    STATE_NO_PROXY,
                    f"no node is advertising mesh network {self.network_id.hex()} in range "
                    f"of {self.adapter.label if self.adapter else 'the adapter'}")
            client = BleakClient(addr, timeout=20.0, adapter=self.adapter_name,
                                 disconnected_callback=self._on_disconnect)
            try:
                # connect() is inside the cleanup, not in front of it. It does
                # not only open the socket: BlueZ discovers services inside it,
                # and a failure there raises with the ACL connection already
                # established. Left outside, every such attempt abandoned an
                # open GATT link - and the bridge retries every few seconds for
                # as long as the proxy misbehaves, so one bad node was enough to
                # accumulate them all evening.
                await client.connect()
                # Before start_notify, so the first frame of the new link cannot
                # land on top of whatever the old one left behind.
                self._reset_rx()
                await client.start_notify(DATA_OUT, self._notify)
                self._client = client
                down_for = time.monotonic() - self._down_since
                log.info("ble: up - proxy %s (mtu=%s) after %.0fs down",
                         addr, getattr(client, "mtu_size", "?"), down_for)
                self._last_failure = ""
                await asyncio.sleep(1.0)  # let the first secure network beacon arrive
                if self.on_connected:
                    await self.on_connected()
                if not client.is_connected:
                    # The link can die inside that sleep, or while the filter is
                    # being set up. Announcing "up" on the strength of a
                    # connect() that has since been undone would publish
                    # availability online over a link that is gone, and the
                    # supervisor would sit on _up waiting for a disconnect
                    # callback that already fired.
                    raise ConnectionError(
                        f"the link to {addr} dropped while it was being set up")
            except BaseException:
                # Everything here can fail, and an abandoned GATT link is the
                # worst possible leftover: the proxy node stops advertising
                # while it is connected, so the next scan would not find it and
                # the bridge would never come back.
                self._client = None
                # Bounded, via _close(). A bare disconnect() waits on BlueZ for
                # as long as BlueZ feels like taking, and this runs on the
                # shutdown path too - the supervisor is cancelled inside
                # connect(), unwinds through here, and everything else in the
                # teardown is queued behind it.
                await self._close(client)
                raise
            self._up_since = time.monotonic()
            self._up.set()
            self._set_status(STATE_UP)
            if self.on_state:
                self.on_state(True)

    @property
    def beacon_silence(self) -> float:
        """Seconds the link has been up without ever hearing an authenticated beacon.

        0.0 once one has been heard, or while the link is down. A proxy sends a
        Secure Network beacon as soon as a client connects, so a number that
        keeps climbing says one of three things: the network has beacons
        switched off, the net key in the mesh file is not the one in use, or the
        node we are attached to is not really a proxy for our network. All three
        leave the bridge transmitting on whatever IV index it last wrote down
        with no way to ever learn a new one - which works right up to the day
        the network moves two indices ahead, and then every message it sends is
        discarded in silence.
        """
        if self.beacons or not self._up.is_set():
            return 0.0
        return time.monotonic() - self._up_since

    async def supervise(self) -> None:
        """Keep the link up forever, with capped exponential backoff."""
        delay = self.s.ble_retry_min
        while True:
            # Clear before checking: _mark_down() clears _up and then sets
            # _wake, so clearing afterwards could swallow the wake-up.
            self._wake.clear()
            if self.connected and self._up.is_set():
                await self._wake.wait()
                # Only _mark_down() sets _wake, so the link has just gone
                # down. Whether the connection that made it counts as a
                # success is decided here rather than at connect() time,
                # because at connect() time nobody knows yet: see
                # STABLE_AFTER. A link that held is the proof that the
                # conditions are good again, and only then is it honest to
                # start the next failure from the shortest retry.
                if time.monotonic() - self._up_since >= STABLE_AFTER:
                    delay = self.s.ble_retry_min
                    self._fails = 0
                continue
            try:
                await self.connect()
            except Exception as ex:  # noqa: BLE001
                try:
                    await self._failed(ex, delay)
                except Exception:  # noqa: BLE001
                    # Nothing in the bookkeeping is worth the supervisor's
                    # life. It publishes a status that goes out over MQTT and
                    # it power-cycles the adapter over D-Bus, both of which
                    # can throw; an exception escaping this loop would end the
                    # one task that can put the bridge back on the mesh, and
                    # the bridge would carry on accepting commands forever
                    # with nothing to send them over. The traceback is the
                    # alarm, and the retry below still happens.
                    log.exception("ble: handling a failed attempt itself failed")
            else:
                # A scan that found the proxy proves the ordinary timeout is
                # long enough, so stop paying for the long one immediately.
                # Unlike the backoff, this costs nothing if the link turns out
                # to be short-lived: the next failure puts it straight back.
                self._long_scan = False
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.s.ble_retry_max)

    async def _failed(self, ex: BaseException, delay: float) -> None:
        """Record a failed connection attempt and try to make the next one work."""
        reason = explain_ble_error(ex)
        # A failure whose cause we could not name is, by elimination, one
        # where the proxy was there and the GATT link would not hold -
        # exactly the third state.
        state = getattr(ex, "state", STATE_FAILING)
        self._fails += 1
        # One line per distinct reason, then quiet. A proxy that is out of
        # range all evening must not fill the log with the same line.
        if reason != self._last_failure:
            log.warning("ble: down (%s) - %s (retrying, backoff up to %.0fs)",
                        state, reason, self.s.ble_retry_max)
            self._last_failure = reason
        else:
            log.debug("ble: still down - %s, retry in %.0fs", reason, delay)
        self._mark_down()
        self._set_status(state)
        await self._recover(recovery_rung(self._fails))

    async def _recover(self, rung: int) -> None:
        """Try harder, in the order that costs least first.

        Each rung logs the one line that says why it happened, because the
        point of the ladder is that the log explains the recovery as it is
        being attempted rather than after someone goes looking.
        """
        if rung == 1:
            log.warning("ble: %d attempts failed, scanning for %.0fx longer in case the "
                        "proxy only advertises rarely",
                        self._fails, LONG_SCAN_FACTOR)
            self._long_scan = True
        elif rung == 2:
            name = self.adapter_name
            if not name:
                # BlueZ is powered off and on by name, and there is no name:
                # the enumeration failed, so no adapter was resolved and bleak
                # is on whatever default BlueZ gave it. Said rather than
                # skipped, because every other rung logs the reason it ran and
                # a rung that returns in silence reads, in the log, exactly
                # like one that ran and did not help.
                log.warning("ble: %d attempts failed, but no adapter has been resolved - "
                            "BlueZ could not be asked which ones exist - so there is "
                            "nothing to power-cycle by name. The earlier `cannot ask "
                            "BlueZ` line is the one to act on.", self._fails)
                return
            log.warning("ble: %d attempts failed, power-cycling %s through BlueZ in case the "
                        "controller is wedged", self._fails,
                        self.adapter.label if self.adapter else name)
            try:
                await adapter_mod.power_cycle(name)
            except BlueZUnavailable as ex:
                log.warning("ble: the power-cycle did not happen (%s)", ex)
        elif rung == 3:
            log.warning("ble: %d attempts failed, re-checking every adapter to find one that "
                        "can actually hear the mesh", self._fails)
            self._force_survey = True

    async def wait_up(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._up.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def disconnect(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
            if client:
                await self._close(client)
        self._mark_down()

    # --- sending ----------------------------------------------------------
    async def send(self, network_pdu: bytes, ptype: int = 0) -> None:
        """Send one network PDU, applying GATT proxy SAR if it exceeds the MTU.

        Serialised. A message longer than the MTU goes out as several writes
        that the proxy reassembles by their SAR bits alone - there is no
        message id to tell two senders apart - so a second sender writing
        between them splices its own segments into the first message and the
        proxy throws both away. With a 23-byte MTU almost every message is
        segmented, and the bridge really does have two senders: commands hold
        the bridge's send lock, but the proxy filter set up by on_connected
        does not, and neither does the keepalive.
        """
        async with self._write:
            # Read inside the lock. A caller that waited here may have been
            # waiting on a write that failed and tore the link down, and
            # writing to the old client would raise against a connection that
            # is already gone.
            client = self._client
            if client is None or not client.is_connected:
                raise ConnectionError("mesh proxy link is down")
            chunk = max(20, int(getattr(client, "mtu_size", 23)) - 3)
            if 1 + len(network_pdu) <= chunk:
                parts = [bytes([ptype]) + network_pdu]  # SAR = complete message
            else:
                body = chunk - 1
                pieces = [network_pdu[i:i + body] for i in range(0, len(network_pdu), body)]
                parts = []
                for i, p in enumerate(pieces):
                    sar = 0x40 if i == 0 else (0xC0 if i == len(pieces) - 1 else 0x80)
                    parts.append(bytes([sar | ptype]) + p)
            try:
                for p in parts:
                    await client.write_gatt_char(DATA_IN, p, response=False)
            except Exception:
                # A write can fail with the GATT connection still open - and an
                # open connection is what stops the proxy node advertising, so
                # the supervisor's next scan would not find it. Close it before
                # letting go of the reference, then let the supervisor rebuild
                # the link.
                await self._close(client)
                # Named, so that a link the supervisor has already replaced
                # while we were closing this one is left alone.
                self._mark_down(client)
                raise
