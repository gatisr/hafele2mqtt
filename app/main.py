"""Hafele Connect Mesh <-> MQTT bridge with Home Assistant MQTT discovery.

Start-up order is deliberate: everything that can be checked without hardware
is checked first, so the common mistakes (no mesh file, bad key, wrong broker
password, no D-Bus) produce one clear line within a second of `docker up`
rather than a reconnect loop at 3 a.m.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from functools import partial

import paho.mqtt.client as mqtt

from . import __version__, broker, ble as ble_mod, mesh, overrides
from .ble import ProxyLink
from .config import ConfigError, Settings, load_mesh_document, plan_src_address, validate_mesh
from .model import Entity, MeshModel, apply_identity, describe, parse
from .state import FatalStateError, State, read_identity

log = logging.getLogger("bridge")

# ASCII digits only. str.isdigit() is also true for Arabic-Indic digits and for
# superscripts - and int() then refuses the superscripts, so the "safe" check
# let a crash through on exactly the input it was there to reject.
_ASCII_DIGITS = re.compile(r"[0-9]{1,5}")

# Start-up polling. The gaps between re-asking grow on purpose: a node that has
# missed two requests in a row is not busy, it is asleep, out of range or behind
# a relay that is dropping packets, and asking it faster fixes none of those
# while spending sequence numbers out of a space that is never refilled.
_STARTUP_POLL_DELAYS = (3.0, 8.0, 20.0, 45.0)
# Let the last round's answers arrive before naming anything as silent.
_STARTUP_SETTLE = 3.0
# The first poll waits for the radio rather than being thrown into a link that
# is still scanning. A cold BLE connect is a scan plus a GATT handshake, and the
# point of the start-up poll is precisely that it does not get skipped.
_STARTUP_LINK_WAIT = 120.0
# How often the heartbeat is touched when polling is switched off entirely. Well
# under health.FLOOR, so three of these fit inside the staleness limit and one
# late beat is not enough to make the container unhealthy.
_IDLE_HEARTBEAT = 60.0

# How long to wait for the proxy to answer our filter setup with a Filter Status.
# The two configuration PDUs go out Write Without Response, so the GATT stack
# reports success the moment they leave the phone's radio - the proxy agreeing is
# a separate message that comes back on the notify characteristic. Generous,
# because the answer shares the air with whatever else the mesh is doing, and the
# cost of waiting a moment longer is nothing next to the cost of being wrong.
_FILTER_CONFIRM_TIMEOUT = 5.0

# How long the cancelled background tasks get to actually unwind before the
# teardown carries on without them. Docker allows ten seconds between SIGTERM
# and SIGKILL, and the rest of the shutdown - releasing the proxy, telling the
# broker we are gone - needs most of them, so this is deliberately short. It
# exists because cancel() only schedules the CancelledError: without the wait,
# the supervisor can still be inside connect() holding the link's lock, and the
# disconnect that comes next would queue behind a scan with nothing to cut it
# short.
_SHUTDOWN_GRACE = 2.0
# And how long releasing the proxy may take after that. The same order as the
# wait for the retained `offline`, because the two run side by side and the
# shutdown is only as quick as the slower one.
_LINK_CLOSE_LIMIT = 6.0

# Everything that arrives from the broker is, as far as a log file is concerned,
# text written by whoever can publish to it. Control characters are the part
# that matters: a newline forges a second log line, and an ESC starts an ANSI
# sequence that can repaint or erase what a terminal has already shown. The log
# is what a person reads afterwards to work out what happened, so it has to say
# what actually arrived. C1 (\x80-\x9f) and the Unicode line separators are in
# here for the same reason as C0: some viewers act on them.
_LOG_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_LOG_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
# Long enough that a real Home Assistant command fits whole (they run to a few
# dozen characters), short enough that a megabyte of JSON cannot push the rest
# of the log out of the file.
_LOG_LIMIT = 200


def _escape(m: re.Match[str]) -> str:
    ch = m.group()
    if ch in _LOG_NAMED:
        return _LOG_NAMED[ch]
    # \x for a byte-sized codepoint, \u above it, so the escape is unambiguous
    # about which character it stands for rather than running the digits on.
    return f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}"


def sanitize(value: object, limit: int = _LOG_LIMIT) -> str:
    """Make untrusted text safe and short enough to put in a log line.

    Truncation happens first, so escaping only ever runs over `limit`
    characters and the result cannot exceed six times it however hostile the
    input - a bounded single line either way.

    A string is used as it stands, so a caller that wants it delimited quotes
    it in the format string. Anything else goes through repr(), which is the
    only form in which an arbitrary JSON value says what type it was.
    """
    text = value if isinstance(value, str) else repr(value)
    if len(text) > limit:
        text = text[:limit] + "..."
    return _LOG_UNSAFE.sub(_escape, text)


def scene_number(key: str) -> int | None:
    """The scene number in a `.../scene/<key>/set` topic, or None if there is none."""
    if not _ASCII_DIGITS.fullmatch(key):
        return None
    num = int(key)
    # A scene recall carries the number in two bytes; 0 means "no scene".
    return num if 1 <= num <= 0xFFFF else None


class CommandQueue:
    """Incoming commands, coalesced per topic and released at a bounded rate.

    Every command costs two sequence numbers out of a 24-bit space that is
    never refilled, and the bridge takes commands from a broker it does not
    own: anything able to publish can spend that space. Automatic rotation
    (see state.py) survives it, but only by burning through unicast addresses,
    so the counter is worth defending.

    Coalescing is the part that matters, and it is not a punishment: a topic
    that already has an unsent command gets that command *replaced*, because
    the last thing the user asked for is the only one that was ever
    interesting. Dragging a brightness slider publishes a position per pixel
    and all but the last are stale by the time they are read. The entry keeps
    its place in the queue when it is replaced, so one busy light cannot push
    itself ahead of a command for another.

    The rate limit underneath is insurance, not policy: the defaults sit an
    order of magnitude above what Home Assistant does, and the bucket holds a
    full queue's worth of tokens so a scene that switches everything at once
    goes out at full speed.
    """

    # One line per window when commands are being dropped or delayed, instead
    # of one line per command - the whole point is that this fires when
    # something is already flooding us.
    REPORT_EVERY = 10.0

    def __init__(self, maxsize: int, rate: float) -> None:
        self._items: dict[str, str] = {}
        self._maxsize = maxsize
        self._rate = rate
        # Full to begin with: a burst that arrives into an idle bridge is the
        # normal case (a scene, a restart, Home Assistant restoring states).
        self._tokens = float(maxsize)
        self._updated = time.monotonic()
        self._ready = asyncio.Event()
        self._closed = False
        self._dropped = 0
        self._delayed = 0
        self._report: asyncio.TimerHandle | None = None

    def __len__(self) -> int:
        return len(self._items)

    def offer(self, topic: str, payload: str) -> None:
        """Queue a command. Must run on the event loop thread."""
        if self._closed:
            return
        if topic in self._items:
            self._items[topic] = payload
        else:
            if len(self._items) >= self._maxsize:
                # The oldest goes, not the newest. A full queue means we are
                # behind, and being behind is exactly when the freshest
                # command is the one worth keeping.
                self._items.pop(next(iter(self._items)))
                self._note(dropped=1)
            self._items[topic] = payload
        self._ready.set()

    def close(self) -> None:
        """Wake a waiting get() for good; it returns the ("", "") sentinel."""
        self._closed = True
        if self._report is not None:
            self._report.cancel()
            self._report = None
        self._ready.set()

    async def get(self) -> tuple[str, str]:
        while True:
            if self._closed:
                return "", ""
            if not self._items:
                # Nothing awaits between the test and the clear, and offer()
                # only ever runs on this loop, so no command can slip through
                # the gap.
                self._ready.clear()
                await self._ready.wait()
                continue
            wait = self._take_token()
            if wait > 0:
                self._note(delayed=1)
                # Sleeping here rather than in offer() is deliberate: the
                # queue keeps coalescing while we wait, so a throttled slider
                # drag collapses into one command instead of a backlog.
                await asyncio.sleep(wait)
                continue
            topic = next(iter(self._items))
            return topic, self._items.pop(topic)

    def _take_token(self) -> float:
        """Spend one token, or return how long until there is one."""
        now = time.monotonic()
        self._tokens = min(float(self._maxsize),
                           self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self._rate

    def _note(self, dropped: int = 0, delayed: int = 0) -> None:
        self._dropped += dropped
        self._delayed += delayed
        if self._report is not None:
            return
        # First event of a window is logged at once, so the cause is visible
        # immediately; a timer folds the rest of the window into one summary.
        self._report = asyncio.get_running_loop().call_later(self.REPORT_EVERY, self._flush)
        self._log()

    def _log(self) -> None:
        log.warning("command rate limit: %d dropped, %d delayed (CMD_RATE_LIMIT=%g/s, "
                    "CMD_QUEUE_MAX=%d). Something is publishing faster than the mesh can "
                    "be driven; each command spends sequence numbers.",
                    self._dropped, self._delayed, self._rate, self._maxsize)
        self._dropped = 0
        self._delayed = 0

    def _flush(self) -> None:
        self._report = None
        if self._dropped or self._delayed:
            # Re-arms, so a flood that carries on keeps producing one line per
            # window rather than going quiet.
            self._note()


class Bridge:
    def __init__(self, s: Settings, model: MeshModel, state: State) -> None:
        self.s = s
        self.m = model
        self.state = state
        self.keys = mesh.Keys(model.net_key, model.app_key)
        # One TID counter per destination address. A transaction is identified
        # by source, destination and TID together, so a single bridge-wide
        # counter was not wrong - but it wrapped against the wrong thing. What
        # must not repeat inside the six-second transaction window is a TID to
        # the *same* destination, and a shared counter repeats after 256
        # commands to anything at all. At CMD_RATE_LIMIT's default of 10/s that
        # cannot be reached; raise it to 50/s and 256 commands in six seconds
        # can be, and the symptom would be one light ignoring a command because
        # it looked like a retransmission of the one before.
        self._tids: dict[int, int] = {}
        self.by_dst = {e.dst: e for e in model.singles}
        # Replay protection for incoming messages: source address -> the
        # (iv_index, seq) of the newest message accepted from it. See _is_fresh.
        self._seen: dict[int, tuple[int, int]] = {}
        self._replays = 0
        # Things worth saying exactly once rather than on every poll forever: a
        # node answering in a model we do not implement, a segmented message we
        # cannot reassemble, a network that never beacons.
        self._said: set[str] = set()
        # Set by the proxy's own Filter Status, waited for by _confirm_filter.
        self._filter_ok = asyncio.Event()
        self._filter_watch: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self.queue = CommandQueue(s.cmd_queue_max, s.cmd_rate_limit)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mesh_up = False
        self._stop = asyncio.Event()
        # Strong references to fire-and-forget tasks. Without them asyncio only
        # holds a weak one, so a task can be collected before it has run.
        self._tasks: set[asyncio.Task] = set()
        self.fatal: BaseException | None = None
        # Follow-up polls, coalesced. See _poll_soon().
        self._poll_pending: dict[str, Entity] = {}
        self._poll_everything = False
        self._poll_deadline = 0.0
        self._poll_waiting = False
        # The heartbeat says so once rather than once a poll. See _beat().
        self._beat_failed = False

        self.link = ProxyLink(self.keys, s,
                              on_iv_index=self._on_iv, on_pdu=self._on_pdu,
                              on_connected=self._setup_filter, on_state=self._on_link_state,
                              on_diagnostics=self._publish_diagnostics,
                              on_proxy_config=self._on_proxy_config)

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=s.mqtt_client_id)
        broker.configure(self.mqtt, s)
        # The will covers the process, not the mesh: it is the one thing the
        # broker can report on our behalf once we are gone. The lights' own
        # availability (below) additionally requires a reachable mesh, so a
        # radio problem takes the lights unavailable while the diagnostics
        # explaining it stay readable.
        self.mqtt.will_set(s.bridge_availability_topic, "offline", retain=True)
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=max(1, int(s.mqtt_reconnect_max)))
        self.mqtt.on_connect = self._on_mqtt_connect
        self.mqtt.on_disconnect = self._on_mqtt_disconnect
        self.mqtt.on_message = self._on_message

    # --- iv index / sequence numbers -------------------------------------
    @property
    def src(self) -> int:
        """The address we send from. Read from the state, never cached.

        It moves by itself when the counter for the current one runs low, and
        every use of it has to move with it in the same instant: the proxy
        filter, the `dst` a status message has to carry to be ours, the nonce
        of every PDU. A copy kept on the bridge is a copy that can disagree,
        and the symptom of disagreeing would be lights that stop reporting
        their state with nothing in any log.
        """
        return self.state.src_addr

    def _on_iv(self, iv: int, iv_update: bool = False) -> None:
        # The IV Update flag decides what the number in the beacon means. While
        # it is set the network is mid-update: the beacon carries the *new* IV
        # index, which nodes accept but do not transmit with yet (Mesh Profile
        # 3.10.5). Adopting it now would be the worst outcome this bridge has,
        # because every node still on the old index would discard everything we
        # send and set_iv() never walks the number back down - a transient
        # network event would need a human to undo. So we follow the spec:
        # transmit with the old index, accept both, and move up only when the
        # network says the update is over.
        self.state.set_iv_update(iv_update)
        transmit = max(0, iv - 1) if iv_update else iv
        # Authenticated by ble.py, but authentication only proves the beacon came
        # from something holding the network key, not that the number is sane. A
        # node that corrupted its own IV index would otherwise drag the bridge up
        # with it, permanently: set_iv never walks back down.
        ahead = transmit - self.state.iv_index
        if ahead > mesh.IV_INDEX_MAX_AHEAD:
            log.warning(
                "refusing IV index %d from a secure network beacon: that is %d ahead of our "
                "%d, and the spec allows one IV update per 96 hours. Adopting it would be "
                "permanent (the IV index is never lowered). Set IV_INDEX explicitly if the "
                "network really has moved this far.", transmit, ahead, self.state.iv_index)
            return
        try:
            self.state.set_iv(transmit, "secure network beacon")
        except FatalStateError as ex:
            # This runs inside bleak's notification callback, which swallows
            # whatever it raises. Without this the IV index would be adopted in
            # memory and never written: the bridge would look healthy, and the
            # next restart would come back on the old IV index and have every
            # message it sends discarded by the whole mesh, silently.
            self._fatal(ex)

    # --- failure handling --------------------------------------------------
    def _fatal(self, ex: BaseException) -> None:
        """State is gone; stop the bridge loudly instead of degrading quietly.

        A bridge that keeps running without a usable sequence number does not
        fail, it just stops being obeyed, and the logs blame the radio. Better to
        exit non-zero and let the restart policy and the logs say why.
        """
        if self.fatal is None:
            self.fatal = ex
            log.critical("%s", ex)
            log.critical("shutting down: continuing would silently stop the lights responding")
        self.request_stop()

    # --- mqtt -------------------------------------------------------------
    def _on_mqtt_connect(self, client, _u, _f, reason_code, _p=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.error("mqtt: broker refused the connection: %s", reason_code)
            return
        log.info("mqtt: up - %s:%d%s", self.s.mqtt_host, self.s.mqtt_port,
                 " (TLS)" if self.s.mqtt_tls else "")
        client.subscribe(f"{self.s.base_topic}/light/+/set")
        client.subscribe(f"{self.s.base_topic}/scene/+/set")
        # The process is up and talking; whether the mesh is reachable is a
        # separate question answered by _publish_availability().
        client.publish(self.s.bridge_availability_topic, "online", retain=True)
        self._publish_availability()
        try:
            self._discovery()
        except FatalStateError as ex:
            # This runs on paho's thread, where an exception is swallowed by the
            # client loop and the bridge would carry on looking healthy.
            self._fatal(ex)
            return
        self._republish_states()

    def _on_mqtt_disconnect(self, _c, _u, _f, reason_code=None, _p=None) -> None:
        if self._stop.is_set():
            # We are the ones who hung up. Promising a reconnect here would be
            # the last line in the log of every ordinary restart, and it would
            # be describing something that is never going to happen.
            log.info("mqtt: disconnected - %s", reason_code)
            return
        log.warning("mqtt: down - %s (reconnecting with backoff)", reason_code)

    def _on_message(self, _c, _u, msg) -> None:
        assert self.loop
        self.loop.call_soon_threadsafe(self.queue.offer, msg.topic,
                                       msg.payload.decode(errors="replace"))

    def _on_link_state(self, up: bool) -> None:
        self.mesh_up = up
        self._publish_availability()
        self._publish_diagnostics()

    def _publish_availability(self):
        payload = "online" if self.mesh_up else "offline"
        return self.mqtt.publish(self.s.availability_topic, payload, retain=True)

    def _publish_diagnostics(self):
        """Retained, so Home Assistant has the last known answer after a restart.

        Called from the BLE supervisor's thread of control and from paho's, and
        paho's publish() is the only shared thing it touches, which is
        thread-safe by design.
        """
        return self.mqtt.publish(self.s.diagnostics_topic,
                                 json.dumps(self.link.diagnostics()), retain=True)

    # --- discovery ---------------------------------------------------------
    def _bridge_device(self) -> dict:
        return {
            "identifiers": ["hafele2mqtt"],
            "name": f"Connect Mesh ({self.m.mesh_name})",
            "manufacturer": "Hafele",
            "model": "Hafele Connect Mesh bridge (BLE GATT proxy)",
            "sw_version": __version__,
        }

    def _scene_device(self) -> dict:
        """Scenes get a device of their own, reached through the bridge.

        A scene is not something the bridge controls, it is something the mesh
        remembers, and hanging six of them off the same device as the groups
        made the bridge's page one undifferentiated list. They stay a child of
        the bridge via `via_device`, so the tree still says how they are
        reached - they are just not mixed in with the controls any more.
        """
        return {
            "identifiers": ["hafele2mqtt_scenes"],
            "name": f"Connect Mesh scenes ({self.m.mesh_name})",
            "manufacturer": "Hafele",
            "model": "Hafele Connect Mesh scenes",
            "sw_version": __version__,
            "via_device": "hafele2mqtt",
        }

    def _device_for(self, e: Entity) -> dict:
        """Lights belong to their physical box; groups to the bridge itself.

        Groups deliberately stay on the bridge device rather than getting one
        each: a group is not a thing you can point at in a room, and six more
        devices named after them would be a longer list, not a tidier one.
        """
        if e.kind != "light" or e.node is None:
            return self._bridge_device()
        n = e.node
        model = n.model + (f" rev {n.hw_version}" if n.hw_version else "")
        dev = {
            "identifiers": [n.uid],
            "name": n.name,
            "manufacturer": "Hafele",
            "model": model,
            "via_device": "hafele2mqtt",
        }
        if n.sw_version:
            dev["sw_version"] = n.sw_version
        if n.area:
            # A suggestion, and only for a device Home Assistant has never seen:
            # an existing one keeps whatever area it was put in. See README.
            dev["suggested_area"] = n.area
        return dev

    def _availability(self, mesh: bool) -> list[dict]:
        """Availability entries: the process always, the mesh only when asked."""
        entries = [{"topic": self.s.bridge_availability_topic,
                    "payload_available": "online", "payload_not_available": "offline"}]
        if mesh:
            entries.append({"topic": self.s.availability_topic,
                            "payload_available": "online", "payload_not_available": "offline"})
        return entries

    def _diagnostic_configs(self) -> list[tuple[str, dict]]:
        """The entities that answer "why is the mesh unreachable?".

        They read one retained JSON document rather than a topic each, so the
        whole picture is written in a single publish and cannot be read
        half-updated. They hang off the process availability only - a
        diagnostic that disappears when the thing it diagnoses breaks is worse
        than no diagnostic at all.

        The `is not none` guard matters: a template that renders the string
        "None" would be published as a state, and an unknown RSSI would show up
        as the word None in the UI. An empty string is ignored by Home
        Assistant, which leaves the entity showing "unknown", which is true.
        """
        avail = self._availability(mesh=False)
        topic = self.s.diagnostics_topic
        common = {
            "availability": avail,
            "state_topic": topic,
            "entity_category": "diagnostic",
            "device": self._bridge_device(),
        }
        unknown = "{{{{ value_json.{0} if value_json.{0} is not none else '' }}}}"
        specs = [
            ("binary_sensor", "link", {
                "name": "Mesh proxy link",
                "device_class": "connectivity",
                "value_template": "{{ 'ON' if value_json.up else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
            }),
            ("sensor", "status", {
                "name": "Mesh proxy status",
                "icon": "mdi:bluetooth-audio",
                "value_template": "{{ value_json.status }}",
            }),
            ("sensor", "adapter", {
                "name": "Bluetooth adapter",
                "icon": "mdi:bluetooth",
                "value_template": unknown.format("adapter"),
            }),
            ("sensor", "rssi", {
                "name": "Mesh proxy signal",
                "device_class": "signal_strength",
                "state_class": "measurement",
                "unit_of_measurement": "dBm",
                "value_template": unknown.format("rssi"),
            }),
            ("sensor", "drops", {
                "name": "Mesh proxy drops",
                "icon": "mdi:lan-disconnect",
                # total_increasing, not a counter that can be relied on to
                # persist: it is reset by a restart, and that is exactly what
                # this class is for.
                "state_class": "total_increasing",
                "value_template": "{{ value_json.drops }}",
            }),
            ("sensor", "last_drop", {
                "name": "Mesh proxy last drop",
                "device_class": "timestamp",
                "value_template": unknown.format("last_drop"),
            }),
        ]
        out: list[tuple[str, dict]] = []
        for component, key, cfg in specs:
            uid = f"hafele2mqtt_{key}"
            out.append((f"{self.s.discovery_prefix}/{component}/{uid}/config",
                        {**common, **cfg, "unique_id": uid,
                         "default_entity_id": f"{component}.hafele_{key}"}))
        return out

    def _discovery(self) -> None:
        topics: set[str] = set()
        lights = [e for e in self.m.lights.values() if e.enabled]
        scenes = [sc for sc in self.m.scenes if sc.enabled]
        for e in lights:
            topic = f"{self.s.discovery_prefix}/light/{e.uid}/config"
            cfg = {
                "name": e.name,
                "unique_id": e.uid,
                # Only honoured when the entity is created. An entity that
                # already exists keeps the id it has, whatever we ask for.
                "default_entity_id": e.entity_id,
                "schema": "json",
                # Both, on purpose. Modern Home Assistant derives the colour
                # mode from `supported_color_modes` and only falls back to the
                # `brightness` flag when it is absent; there is no validator
                # rejecting the pair. Keeping the old key means a version old
                # enough to strip the new one still gets a dimmable light
                # rather than an on/off switch. `unique_id` is untouched by
                # this, so existing entities keep their identity.
                "brightness": True,
                "supported_color_modes": ["brightness"],
                "command_topic": f"{self.s.base_topic}/light/{e.uid}/set",
                "state_topic": f"{self.s.base_topic}/light/{e.uid}/state",
                # Both have to be true: the process alive *and* the mesh
                # reachable. Only the second one used to exist, which meant a
                # bridge killed outright left its lights showing a stale state
                # until the broker noticed the will.
                "availability": self._availability(mesh=True),
                "availability_mode": "all",
                "optimistic": True,
                "device": self._device_for(e),
            }
            if e.icon:
                cfg["icon"] = e.icon
            self.mqtt.publish(topic, json.dumps(cfg), retain=True)
            topics.add(topic)
            # Deliberately no state here. At this point the bridge has not asked
            # a single node anything, so the only state it could publish is the
            # dataclass default - every light off. Home Assistant would show
            # that as fact until the first poll a few seconds later.
        for sc in scenes:
            topic = f"{self.s.discovery_prefix}/scene/{sc.uid}/config"
            cfg = {
                "name": sc.full_name(self.s.scene_name_prefix),
                "unique_id": sc.uid,
                "default_entity_id": sc.entity_id(self.s.scene_name_prefix),
                "payload_on": "ON",
                "command_topic": f"{self.s.base_topic}/scene/{sc.number}/set",
                "availability": self._availability(mesh=True),
                "availability_mode": "all",
                "device": self._scene_device(),
            }
            if sc.icon:
                cfg["icon"] = sc.icon
            self.mqtt.publish(topic, json.dumps(cfg), retain=True)
            topics.add(topic)
        for topic, cfg in self._diagnostic_configs():
            self.mqtt.publish(topic, json.dumps(cfg), retain=True)
            topics.add(topic)
        # Whatever the link already knows. Without this the diagnostic entities
        # sit at "unknown" until the next state change, which on a healthy
        # bridge is never.
        self._publish_diagnostics()

        for e in self.m.lights.values():
            if not e.enabled:
                # Its config topic is retracted by the stale sweep below, but
                # the retained *state* is under BASE_TOPIC and would otherwise
                # sit in the broker forever, waiting to be adopted by whatever
                # comes back under that unique_id next.
                self.mqtt.publish(f"{self.s.base_topic}/light/{e.uid}/state", "", retain=True)

        self._clear_old_base_topic()
        stale = self.state.set_discovery(topics)
        for topic in sorted(stale):
            # Empty retained payload = "this entity is gone" to Home Assistant.
            self.mqtt.publish(topic, "", retain=True)
        if stale:
            log.info("removed %d discovery topic(s) that are no longer published", len(stale))
        log.info("published %d light/group and %d scene discovery topic(s)",
                 len(lights), len(scenes))

    def _clear_old_base_topic(self) -> None:
        """BASE_TOPIC changed: drop the retained state under the old prefix.

        The discovery topics themselves do not contain BASE_TOPIC, so the
        entities follow the change by themselves. What is left behind is the
        retained state payloads, which would otherwise sit in the broker
        forever.
        """
        old = self.state.base_topic
        if not old or old == self.s.base_topic:
            self.state.base_topic = self.s.base_topic
            return
        log.info("BASE_TOPIC changed %s -> %s, clearing retained state under the old prefix",
                 old, self.s.base_topic)
        self.mqtt.publish(f"{old}/status", "", retain=True)
        self.mqtt.publish(f"{old}/bridge/status", "", retain=True)
        self.mqtt.publish(f"{old}/bridge/diagnostics", "", retain=True)
        for e in self.m.lights.values():
            self.mqtt.publish(f"{old}/light/{e.uid}/state", "", retain=True)
        self.state.base_topic = self.s.base_topic

    def purge_discovery(self) -> tuple[int, object]:
        """Retract every discovery topic we ever published. Used by --purge-discovery.

        Returns the number of discovery topics retracted and the handle of the
        last publish, so the caller can wait for the queue to drain before
        disconnecting; otherwise the retractions never leave the process.
        """
        known = set(self.state.discovery)
        for e in self.m.lights.values():
            known.add(f"{self.s.discovery_prefix}/light/{e.uid}/config")
        for sc in self.m.scenes:
            known.add(f"{self.s.discovery_prefix}/scene/{sc.uid}/config")
        for topic, _cfg in self._diagnostic_configs():
            known.add(topic)
        for topic in sorted(known):
            self.mqtt.publish(topic, "", retain=True)
        for e in self.m.lights.values():
            self.mqtt.publish(f"{self.s.base_topic}/light/{e.uid}/state", "", retain=True)
        self.mqtt.publish(self.s.diagnostics_topic, "", retain=True)
        self.mqtt.publish(self.s.bridge_availability_topic, "", retain=True)
        last = self.mqtt.publish(self.s.availability_topic, "", retain=True)
        self.state.set_discovery(set())
        return len(known), last

    def _publish_state(self, e: Entity) -> None:
        if not e.enabled:
            # The entities file says this one is not exposed, so there is
            # nothing in Home Assistant listening and a retained payload here
            # would only be a mess to clean up later.
            return
        on = e.lightness > 0
        # A lightness of 1..128 rounds to brightness 0, and "ON at brightness 0"
        # is a state Home Assistant renders as a lit icon with the slider at the
        # bottom, which no user can make sense of. The lamp is on, so say 1.
        brightness = max(1, round(e.lightness / 65535 * 255)) if on else 0
        st = {"state": "ON" if on else "OFF", "brightness": brightness}
        self.mqtt.publish(f"{self.s.base_topic}/light/{e.uid}/state", json.dumps(st), retain=True)

    def _republish_states(self) -> None:
        """Re-send what we last knew, after an MQTT connection comes up.

        State is published only when it changes, so a publish that happens
        while the broker is unreachable is not retried, it is simply lost. The
        retained value then stays whatever it was before - possibly "ON" for a
        light that has been off for a day - and nothing corrects it until that
        same light happens to change again. Re-sending on connect turns that
        from permanent into a gap of seconds.

        Only entities we have actually heard from are published: `known` is
        false until a status message or a command has told us a real level, and
        publishing the dataclass default instead would be inventing a fact
        rather than restoring one.

        This is a floor, not a refresh. If BLE was down too, what we hold may
        itself be stale; the poll loop runs on its own schedule and corrects it
        as usual.
        """
        n = 0
        for e in self.m.lights.values():
            if e.enabled and e.known:
                self._publish_state(e)
                n += 1
        if n:
            log.info("mqtt: republished the last known state of %d entit%s",
                     n, "y" if n == 1 else "ies")

    # --- mesh send path ----------------------------------------------------
    def _tid(self, dst: int) -> int:
        """The next transaction identifier for messages sent to `dst`.

        Per destination, because that is what the specification makes a
        transaction out of; see the note on `_tids` in __init__.
        """
        tid = (self._tids.get(dst, -1) + 1) & 0xFF
        self._tids[dst] = tid
        return tid

    async def _setup_filter(self) -> None:
        """Ask the proxy to forward only messages addressed to us, and check it did.

        The two writes go out as Write Without Response over a link that can
        die between them, so sending them proves nothing. If the proxy did not
        take the filter, every status message is dropped one hop away and the
        bridge sees a mesh where no light ever answers - with nothing in any log
        to distinguish that from a radio problem. So we listen for the proxy's
        own Filter Status and say which of the two happened.

        The listening is a background task, not part of this call. Rotation
        runs this with send_lock held and the user's command waiting behind it,
        and the proxy's answer is worth a log line, never worth delaying a
        light by the length of a timeout.
        """
        self._filter_ok.clear()
        if self._filter_watch is not None:
            self._filter_watch.cancel()
            self._filter_watch = None
        # Read once, up front. self.src is a property backed by the state and
        # rotation moves it on its own schedule, while this call spans two
        # writes and the waits between them. Read fresh at each use, the three
        # uses can disagree: the address put in the whitelist is read as the
        # loop's tuple is built, the sender of the second write a fifth of a
        # second later, and the one _confirm_filter() reports on later still.
        # Nothing is dropped as a result - the proxy filter is per-connection,
        # and the whitelist content was always the earliest of the three - but
        # the bridge then announces "filter set and confirmed (whitelist
        # 0x....)" naming an address it did not whitelist. That line is the
        # first thing anyone reads when the lights stop reporting their state,
        # and it is the one place a wrong address costs an evening.
        src = self.src
        try:
            for op, params in (mesh.set_filter_type_whitelist(),
                               mesh.add_filter_addresses([src])):
                pdu = mesh.build_proxy_config_pdu(self.keys, self.state.iv_index,
                                                  self.state.next_seq(),
                                                  src, op, params)
                await self.link.send(pdu, ptype=2)
                await asyncio.sleep(0.2)
        except FatalStateError as ex:
            # Reconnecting cannot fix a spent counter, so let the supervisor stop
            # rather than reconnect forever against an error it cannot resolve.
            self._fatal(ex)
            return
        self._filter_watch = self._spawn(self._confirm_filter(src))

    async def _confirm_filter(self, src: int) -> None:
        """Wait for the proxy to answer the filter we just set, and say so either way.

        Takes the address as an argument rather than reading self.src, so a
        rotation that happens while this is waiting cannot make the log line
        name the wrong one.
        """
        try:
            await asyncio.wait_for(self._filter_ok.wait(), _FILTER_CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            # Not fatal, and not a reason to drop the link: an old proxy that
            # answers nothing still forwards everything, which is noisier than
            # we asked for but works. Worth a warning because the other reading
            # of this silence is a filter that was never applied.
            log.warning(
                "the proxy did not confirm the filter for 0x%04X within %.0fs. If the lights "
                "never report their state, this is the first thing to suspect: the bridge "
                "asked to be forwarded messages addressed to 0x%04X and has no confirmation "
                "that the proxy agreed.", src, _FILTER_CONFIRM_TIMEOUT, src)
            return
        log.info("proxy filter set and confirmed (whitelist 0x%04X)", src)

    def _on_proxy_config(self, pdu: bytes) -> None:
        """A proxy configuration message from the proxy; only Filter Status exists."""
        st = mesh.decode_proxy_config(self.keys, self.state.iv_index, pdu,
                                      accept_next=self.state.iv_update)
        if st is None:
            log.debug("ignoring an unreadable proxy configuration pdu: %s", pdu.hex())
            return
        log.debug("proxy 0x%04X filter: type=%d, %d address(es)",
                  st.src, st.filter_type, st.list_size)
        if st.filter_type == mesh.FILTER_WHITELIST and st.list_size >= 1:
            self._filter_ok.set()

    async def _send_access(self, dst: int, payload: bytes, repeats: int = 2) -> bool:
        """Send one access message. False means nothing reached the mesh."""
        async with self.send_lock:
            # Only while the radio is up. Rotating means changing the source
            # address, saving it, and then telling the proxy to forward to the
            # new one - and with the link down that last step cannot happen, so
            # we would be left committed to an address the proxy filters out,
            # with the user's command thrown away by the ConnectionError on the
            # way. Deferring is free: `needs_rotation` turns true with about a
            # million sequence numbers still in hand, which is weeks of
            # headroom, so the next connected send does it instead.
            if self.state.needs_rotation and self.link.connected:
                await self._rotate_src()
            return await self._send_locked(dst, payload, repeats)

    async def _rotate_src(self) -> None:
        """Adopt a fresh source address and make the proxy forward to it.

        Called with send_lock held, so no message is half-built when the
        address changes. The proxy filter is the one thing in the bridge that
        is bound to the old address rather than following it: the proxy was
        told to forward only what is addressed to 0x7FF7, so a status message
        coming back to the new address would be dropped before it reached us.
        Nothing would raise - the lights would simply stop reporting their
        state - which is why re-sending the filter is part of rotating and not
        a step someone has to remember.

        The caller only rotates while the link is up, but "up" can stop being
        true between that check and this send. If it does, the filter is not
        lost, only late: `_setup_filter` is registered as the on_connected hook
        and reads `self.src` out of the state each time, so the next reconnect
        sets it for the new address. What must not happen is the exception
        reaching the caller, because the user's command is still unsent behind
        it and would be thrown away for a step that repairs itself.
        """
        if self.state.rotate() is None:
            return
        try:
            await self._setup_filter()
        except Exception as ex:  # noqa: BLE001
            log.warning("could not set the proxy filter for the new address 0x%04X (%s); "
                        "the next reconnect will set it", self.src, ex)

    async def _send_locked(self, dst: int, payload: bytes, repeats: int) -> bool:
        for attempt in (1, 2):
            if not self.link.connected and not await self.link.wait_up(15.0):
                break
            try:
                # Unacknowledged model messages: send `repeats` times with a new
                # sequence number each (mandatory) but the same TID, so the node
                # treats them as one transaction.
                for _ in range(repeats):
                    pdu = mesh.build_access_pdu(self.keys, self.state.iv_index,
                                                self.state.next_seq(),
                                                self.src, dst, self.s.ttl, payload)
                    await self.link.send(pdu)
                    await asyncio.sleep(0.08)
                return True
            except FatalStateError:
                # Not a failed send: the sequence number is gone or cannot be
                # persisted. Retrying would burn the rest of the counter and log
                # "the proxy link is down", which is a lie about the radio.
                raise
            except Exception as ex:  # noqa: BLE001
                log.warning("send to 0x%04X failed (%s), attempt %d/2", dst, ex, attempt)
        log.error("command to 0x%04X dropped: mesh proxy link is down", dst)
        return False

    # --- mesh receive path -------------------------------------------------
    def _once(self, key: str) -> bool:
        """True the first time `key` is seen. For faults that repeat forever.

        A node that sends something the bridge cannot use sends it again on
        every change, forever. Logged unconditionally that is a log file made
        of one repeated line; logged never it is a silent fault. So: once per
        distinct cause, where the key carries whatever makes it distinct.
        """
        if key in self._said:
            return False
        self._said.add(key)
        return True

    def _is_fresh(self, src: int, iv_index: int, seq: int) -> bool:
        """Replay protection: has this source already sent us this, or newer?

        Every node in a mesh keeps one of these and so must we. The sequence
        number and IV index come out of the authenticated part of the PDU, so a
        replayer cannot raise them without the net key; what it *can* do is
        record a message off the air and send it again later, unchanged. For
        this bridge that means a Light Lightness Status, and the damage is a
        light reported at a brightness it had an hour ago - which Home Assistant
        then acts on in whatever automation watches that state.

        Kept in memory and only for addresses in the mesh file. In memory
        because persisting it would mean an fsync per status message, against a
        worst case of one stale brightness reading that the next poll corrects;
        and only for known addresses so that the map stays the size of the
        network rather than the size of whatever is being sent at us.
        """
        mark = (iv_index, seq)
        last = self._seen.get(src)
        if last is not None and mark <= last:
            self._replays += 1
            if self._once(f"replay:{src:04X}"):
                log.warning(
                    "ignoring a message from 0x%04X with sequence number %d at IV index %d: "
                    "that is not newer than the %d/%d already seen from it. Either it was "
                    "relayed to us twice, or someone is replaying recorded traffic. The state "
                    "it carries is stale either way.", src, seq, iv_index, last[1], last[0])
            return False
        self._seen[src] = mark
        return True

    def _on_pdu(self, pdu: bytes) -> None:
        # accept_next: while the network is mid-IV-update it carries messages
        # sent with iv_index + 1 as well as our own. Without this they decode to
        # nothing, which on the receive side looks exactly like the lights having
        # gone silent. See `_on_iv` for the transmit half.
        dec = mesh.decode_access(self.keys, self.state.iv_index, pdu,
                                 accept_next=self.state.iv_update)
        if not dec:
            return
        if dec.segmented:
            if self._once(f"segmented:{dec.src:04X}"):
                log.warning(
                    "0x%04X sent a segmented message, which this bridge can neither "
                    "reassemble nor acknowledge, so it is dropped and the sender will "
                    "retransmit it until it gives up. Nothing the bridge asks for needs "
                    "segmenting, so this is a node sending something unexpected rather "
                    "than an answer going missing.", dec.src)
            return
        log.debug("rx src=0x%04X dst=0x%04X seq=%d payload=%s",
                  dec.src, dec.dst, dec.seq, dec.payload.hex())
        if dec.dst != self.src:
            return
        src, payload = dec.src, dec.payload
        e = self.by_dst.get(src)
        if not e:
            return
        if not self._is_fresh(src, dec.iv_index, dec.seq):
            return
        if payload[:2] != mesh.OP_LIGHTNESS_STATUS or len(payload) < 4:
            # Addressed to us, by a node we know, in a model we do not drive.
            # Said once because it is the one clue that a light is answering
            # Generic OnOff (or anything else) while the bridge waits for a
            # Light Lightness Status that is never coming.
            if self._once(f"opcode:{src:04X}:{payload[:2].hex()}"):
                log.info(
                    "%s (0x%04X) answered with opcode %s, which this bridge does not "
                    "implement - it only drives Light Lightness. That light's state will "
                    "not follow what happens to it outside Home Assistant.",
                    e.name, src, payload[:2].hex())
            return
        present = int.from_bytes(payload[2:4], "little")
        if present != e.lightness or not e.known:
            log.info("status %s: %d -> %d", e.name, e.lightness, present)
            self._set_level(e, present)
            self._publish_state(e)
            self._refresh_groups()

    @staticmethod
    def _set_level(e: Entity, lightness: int) -> None:
        """Record what an entity is doing now, including what to restore on ON.

        One place rather than three. The group branch of _apply() used to set
        `lightness` and `known` but not `last_on`, so a group kept the 65535 it
        was constructed with and turning a group on always meant full
        brightness, whatever it had been dimmed to a moment earlier.
        """
        e.lightness = lightness
        e.known = True
        if lightness:
            e.last_on = lightness

    def _refresh_groups(self) -> None:
        for g in self.m.groups:
            reported = [x.lightness for x in g.members if x.known]
            if not reported:
                continue        # no member has answered yet; nothing to report
            v = max(reported)
            if v != g.lightness or not g.known:
                self._set_level(g, v)
                self._publish_state(g)

    # --- polling ------------------------------------------------------------
    async def poll(self, targets=None) -> None:
        # Sent with the same repeat count as a command, which is what the
        # default gives us. A status request is as unacknowledged as everything
        # else on this network: one copy lost to a collision is not retried by
        # anything, and the entity then waits out the whole poll interval before
        # it is asked again. The second copy costs one sequence number and
        # removes most of that.
        for e in targets or list(self.by_dst.values()):
            await self._send_access(e.dst, mesh.lightness_get())
            await asyncio.sleep(0.2)
        # A round that got all the way through its targets is the liveness
        # signal the container healthcheck reads. Here rather than before the
        # loop, so a send that hangs stops the heartbeat instead of covering
        # for itself.
        self._beat()

    def _beat(self) -> None:
        """Touch the heartbeat file the HEALTHCHECK reads. See app/health.py."""
        if not self.s.health_file:
            return
        try:
            # utime, not a write: after the file exists this updates an inode
            # timestamp and moves no data at all, which is the whole reason the
            # heartbeat is not simply `state.json` saved more often. On a
            # Raspberry Pi that difference is SD card wear.
            os.utime(self.s.health_file, None)
            return
        except OSError:
            pass
        try:
            with open(self.s.health_file, "ab"):
                pass
        except OSError as ex:
            if not self._beat_failed:
                # Once. A path that is not writable now will not be writable on
                # the next poll either, and the healthcheck already says so.
                self._beat_failed = True
                log.warning("cannot write the healthcheck heartbeat to %s: %s. The container "
                            "will report itself unhealthy until this is fixed; point "
                            "HEALTH_FILE at a writable path, or set it empty to turn the "
                            "healthcheck off.", self.s.health_file, ex)

    def _silent(self) -> list[Entity]:
        """The entities that have not reported a state yet."""
        return [e for e in self.by_dst.values() if not e.known]

    async def _startup_poll(self) -> None:
        """Poll everything once, then re-ask only whatever stayed silent.

        After a restart the bridge knows nothing, and nothing may be published
        for an entity it knows nothing about: the only level it holds is the
        dataclass default, and sending that would tell Home Assistant every
        light is off rather than that we have not asked yet. So between start-up
        and a node's first answer its entity has no state at all - not a wrong
        one, none - and the card in the UI is blank.

        What makes that gap long rather than brief is that mesh status messages
        are unacknowledged. A request that is lost is lost in silence: nothing
        retries it, and the entity waits a full poll interval to be asked again,
        and another one if it is unlucky twice. Re-asking just the entities that
        have not answered closes it, and *just* those - a second full poll would
        spend sequence numbers re-reading lamps that already replied.
        """
        if not self.link.connected and not await self.link.wait_up(_STARTUP_LINK_WAIT):
            log.info("start-up poll skipped: the mesh proxy link is not up yet. Home "
                     "Assistant shows the lights as unavailable until it is, and the "
                     "periodic poll takes over from there.")
            return
        await self.poll()
        rounds = 1
        for delay in _STARTUP_POLL_DELAYS:
            await asyncio.sleep(delay)
            silent = self._silent()
            if not silent or self._stop.is_set():
                break
            if not self.link.connected:
                # The radio went away mid-run. Not an attempt: there is nothing
                # to learn from shouting into a link that cannot carry it.
                continue
            rounds += 1
            log.info("start-up poll: re-asking %d light(s) that have not answered yet", len(silent))
            await self.poll(silent)
        silent = self._silent()
        if silent:
            await asyncio.sleep(_STARTUP_SETTLE)
            silent = self._silent()
        if not silent:
            log.info("start-up poll: all %d light(s) reported a state (%d round(s))",
                     len(self.by_dst), rounds)
            return
        # Named, because the alternative is finding out by noticing a blank card
        # in Home Assistant and then going looking for why.
        log.warning("start-up poll: %d light(s) never answered in %d round(s): %s. They have "
                    "no state in Home Assistant until they do; the periodic poll keeps asking.",
                    len(silent), rounds, ", ".join(e.name for e in silent))

    async def _poll_loop(self) -> None:
        # First beat before anything can block, so the healthcheck has a
        # timestamp to read from the moment the loop exists rather than only
        # after the start-up poll has finished waiting for a radio.
        self._beat()
        await asyncio.sleep(5)
        if self.s.poll_interval <= 0:
            # Polling is off by request, and a start-up poll is still polling.
            # The heartbeat is not, though, so it carries on by itself: with no
            # poll cycle to report, the healthcheck falls back to the weaker
            # claim that the event loop is still running, which beats declaring
            # the container unhealthy by configuration.
            while not self._stop.is_set():
                await asyncio.sleep(_IDLE_HEARTBEAT)
                self._beat()
            return
        try:
            await self._startup_poll()
        except FatalStateError as ex:
            self._fatal(ex)
            return
        except Exception:  # noqa: BLE001
            log.exception("the start-up poll failed")
        while not self._stop.is_set():
            # Beaten here as well as in poll(), and before the sleep, because
            # the poll below is skipped while the radio is down. A mesh the
            # bridge cannot reach is already reported on `hafele/status`;
            # making it an unhealthy container too would light the warning
            # through every ordinary BLE reconnect, and a warning that is
            # usually on is one nobody reads.
            self._beat()
            await asyncio.sleep(self.s.poll_interval)
            if self._stop.is_set():
                break
            # Do not burn sequence numbers (or log lines) polling into a dead link.
            if self.link.connected:
                self._check_beacons()
                try:
                    await self.poll()
                except FatalStateError as ex:
                    self._fatal(ex)
                    return
                except Exception:  # noqa: BLE001
                    log.exception("poll failed")

    def _check_beacons(self) -> None:
        """Say so if the link has been up a long time with no beacon on it.

        Secure Network beacons are how the bridge learns that the network's IV
        index has moved while it was down - `parse_secure_beacon` authenticates
        one with the beacon key regardless of what index the bridge believes in,
        so hearing one recovers from being arbitrarily far behind. Hearing none
        means that recovery path does not exist on this network, and nothing
        else in the bridge would ever mention it: everything keeps working,
        right up to the day the index moves and every message the bridge sends
        is silently rejected by nodes that have moved on without it.

        Three things look like this: beacons turned off network-wide, a net key
        that is not the network's, or a device that answers the proxy service
        without being a mesh proxy at all.
        """
        quiet = self.link.beacon_silence
        if quiet >= ble_mod.BEACON_SILENCE and self._once("no-beacons"):
            log.warning(
                "no authenticated network beacon in %.0fs on a link that is up. The bridge "
                "cannot learn a new IV index this way, so if the network's index moves while "
                "this one is at %d, messages will be sent that no node accepts. Check that "
                "the network has beacons enabled.", quiet, self.state.iv_index)

    def _poll_soon(self, targets, delay: float = 2.0) -> None:
        """Ask for one follow-up poll `delay` after the last command, not one per command.

        Dragging a brightness slider in Home Assistant sends a command per
        position, and each one used to start its own poll task: twenty
        positions meant twenty polls of the same lamp, on top of the twenty
        commands. Every one of those spends sequence numbers out of a 24-bit
        space that is never refilled and whose exhaustion costs a change of
        SRC_ADDR, so the waste is not free. The intermediate polls also say
        nothing - only the state after the last command is interesting.
        """
        if targets is None:
            self._poll_everything = True
        else:
            for e in targets:
                self._poll_pending[e.uid] = e
        # max(), not assignment: a light command arriving during a scene's
        # longer settle time must push the poll out, never pull it in.
        self._poll_deadline = max(self._poll_deadline, time.monotonic() + delay)
        if self._poll_waiting:
            return
        self._poll_waiting = True
        self._spawn(self._poll_when_quiet())

    async def _poll_when_quiet(self) -> None:
        try:
            while True:
                remaining = self._poll_deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            targets = None if self._poll_everything else list(self._poll_pending.values())
            self._poll_pending = {}
            self._poll_everything = False
        finally:
            # Cleared before the poll runs, so a command that lands while we
            # are polling starts a fresh wait instead of being dropped.
            self._poll_waiting = False
        if self.link.connected:
            await self.poll(targets)

    # --- fire-and-forget tasks ---------------------------------------------
    def _spawn(self, coro) -> asyncio.Task:
        """Run a coroutine in the background, holding on to it until it is done.

        asyncio only keeps a weak reference to a running task, so a bare
        create_task() can be garbage-collected mid-flight; and anything it raises
        would otherwise surface as a "task exception was never retrieved" line
        with no context.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        ex = task.exception()
        if isinstance(ex, FatalStateError):
            self._fatal(ex)
        elif ex is not None:
            log.error("background task failed: %s", ex, exc_info=ex)

    def _critical_ended(self, name: str, task: asyncio.Task) -> None:
        """Stop the bridge if one of the two tasks it cannot work without ends.

        The supervisor and the poller are supposed to run until cancelled. If
        either returns or raises on its own, the bridge keeps answering Home
        Assistant with a radio that will never reconnect or a mesh nobody ever
        asks for its state - healthy on the outside, doing half its job, with
        no log line saying when it stopped. Exiting hands the problem to the
        restart policy, which can actually fix it.
        """
        if task.cancelled() or self._stop.is_set():
            return  # the shutdown path cancels these on purpose
        ex = task.exception()
        if ex is None:
            ex = RuntimeError(f"the {name} task ended on its own, which it never should")
            log.critical("%s", ex)
        else:
            log.critical("the %s task died: %s", name, ex, exc_info=ex)
        self._fatal(ex)

    # --- command handling ---------------------------------------------------
    def _apply(self, e: Entity, lightness: int) -> None:
        """Record what we just told the mesh to do. Only call this after a send."""
        for t in (e.members or [e]):
            self._set_level(t, lightness)
            self._publish_state(t)
        if e.kind == "group":
            self._set_level(e, lightness)
            self._publish_state(e)
        self._refresh_groups()

    def _wanted_lightness(self, e: Entity, payload: str) -> int | None:
        """Turn an MQTT payload into a lightness, or None if it is not usable.

        Everything here arrives from the broker, so it is whatever anyone felt
        like publishing: `null`, a bare number, a brightness of "bright".
        """
        try:
            cmd = json.loads(payload)
        except ValueError:
            cmd = {"state": payload.strip().upper()}
        if not isinstance(cmd, dict):
            log.warning("ignoring command for %s: payload is JSON %s, expected an object "
                        "or a plain ON/OFF", e.name, type(cmd).__name__)
            return None
        state = cmd.get("state")
        # Case-folded: HA sends "OFF", but half the MQTT world sends "off", and
        # reading that as "not OFF" turned the light *on*.
        if isinstance(state, str) and state.strip().upper() == "OFF":
            return 0
        if "brightness" in cmd:
            try:
                brightness = int(cmd["brightness"])
            except (TypeError, ValueError, OverflowError):
                # OverflowError: Infinity and -Infinity are perfectly good JSON
                # numbers to Python's parser, and int() of one raises.
                log.warning("ignoring command for %s: brightness=%s is not a number",
                            e.name, sanitize(cmd["brightness"]))
                return None
            # Home Assistant sends 0-255. Anything outside that would overflow
            # the two-byte lightness field in the access message.
            return round(min(255, max(0, brightness)) / 255 * 65535)
        return e.last_on or 65535

    async def _handle(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        kind, key = parts[-3], parts[-2]
        if kind == "scene":
            num = scene_number(key)
            if num is None:
                log.warning("ignoring scene command: '%s' is not a scene number "
                            "(expected 1-65535)", sanitize(key))
                return
            if not await self._send_access(self.m.all_dst,
                                           mesh.scene_recall(num, self._tid(self.m.all_dst))):
                return
            log.info("scene %d recalled", num)
            self._poll_soon(None, 3.0)
            return
        e = self.m.lights.get(key)
        if not e or not e.enabled:
            return
        lightness = self._wanted_lightness(e, payload)
        if lightness is None:
            return
        if not await self._send_access(e.dst, mesh.lightness_set(lightness, self._tid(e.dst))):
            # Nothing reached the mesh, so the light did not change. Publishing
            # the requested value anyway would leave Home Assistant showing a
            # state that no node ever acknowledged.
            return
        self._apply(e, lightness)
        log.info("%s -> %d", e.name, lightness)
        self._poll_soon(e.members or [e])

    # --- lifecycle -----------------------------------------------------------
    def request_stop(self) -> None:
        """Ask the main loop to wind up. Safe to call from any thread."""
        loop = self.loop
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if loop is not None and here is not loop:
            # Neither Event.set() nor the command queue is thread-safe, and
            # this is reachable from paho's thread via _fatal().
            loop.call_soon_threadsafe(self._do_stop)
            return
        self._do_stop()

    def _do_stop(self) -> None:
        self._stop.set()
        self.queue.close()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.mqtt.connect_async(self.s.mqtt_host, self.s.mqtt_port, 30)
        self.mqtt.loop_start()
        log.info("bridge %s: %d light(s), %d group(s), %d scene(s), src=0x%04X iv=%d",
                 __version__, len(self.m.singles), len(self.m.groups), len(self.m.scenes),
                 self.src, self.state.iv_index)

        supervisor = asyncio.create_task(self.link.supervise())
        poller = asyncio.create_task(self._poll_loop())
        supervisor.add_done_callback(partial(self._critical_ended, "BLE supervisor"))
        poller.add_done_callback(partial(self._critical_ended, "poller"))
        try:
            while not self._stop.is_set():
                topic, payload = await self.queue.get()
                if self._stop.is_set():
                    break
                try:
                    await self._handle(topic, payload)
                except FatalStateError as ex:
                    self._fatal(ex)
                    break
                except Exception:  # noqa: BLE001
                    # Both halves come straight off the broker, so both are
                    # sanitized: see sanitize() for what a raw payload can do
                    # to the log this line is being written into.
                    log.exception("command failed: topic '%s', payload '%s'",
                                  sanitize(topic), sanitize(payload))
        finally:
            # Set first. Everything below is deliberate, and the callbacks that
            # would otherwise read it as a failure - _critical_ended, the mqtt
            # disconnect handler - fire while this block is running.
            self._stop.set()
            await self._stop_tasks(supervisor, poller)
            self.mesh_up = False
            # Together, not one after the other. Releasing the proxy and
            # telling the broker we are gone have nothing to do with each
            # other, and Docker allows ten seconds between SIGTERM and SIGKILL:
            # run in sequence the two timeouts add up to more than that, and
            # whichever went second would be the one that quietly never
            # happened. Both matter. A proxy left connected stops advertising,
            # so the bridge that restarts a second later cannot find the only
            # node it can reach; a retained `online` left behind leaves Home
            # Assistant offering lights that nothing is listening for.
            for outcome in await asyncio.gather(self._close_link(),
                                                self._announce_offline(),
                                                return_exceptions=True):
                if isinstance(outcome, BaseException):
                    log.error("a shutdown step failed: %s", outcome, exc_info=outcome)
            # disconnect() before loop_stop(), which is not interchangeable:
            # disconnect() only queues a DISCONNECT packet, and the network
            # thread is what writes it. Stopping the thread first leaves the
            # packet in the buffer, the socket closes unannounced, and the
            # broker treats every ordinary restart as a crash and fires the
            # last will.
            self.mqtt.disconnect()
            self.mqtt.loop_stop()
            try:
                self.state.save()
            except FatalStateError as ex:
                log.error("%s", ex)
            log.info("stopped cleanly")

    async def _stop_tasks(self, *critical: asyncio.Task) -> None:
        """Cancel the background tasks and wait for them to actually unwind.

        cancel() only schedules a CancelledError; it does not run anything. The
        supervisor in particular can be sitting inside connect(), holding the
        link's lock while a scan runs - and link.disconnect() wants that same
        lock, with no timeout of its own. Without this wait the teardown stops
        there until the scan feels like finishing, which on the shutdown path
        means until SIGKILL.
        """
        tasks = [t for t in (*critical, *self._tasks) if not t.done()]
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=_SHUTDOWN_GRACE)
        if pending:
            # Not fatal: the point of the wait is to let go of the locks, and a
            # task that is still going is one more reason to stop waiting.
            log.debug("%d background task(s) did not finish within %.0fs of being cancelled",
                      len(pending), _SHUTDOWN_GRACE)

    async def _close_link(self) -> None:
        """Release the proxy, with a limit on how long that is allowed to take.

        link.disconnect() takes the link's own lock, and a supervisor unwinding
        out of a cancelled connect() holds that lock for as long as its own
        teardown takes. That teardown is closing the very connection this call
        wants closed, so waiting all the way for the lock buys nothing while
        the clock to SIGKILL runs down.
        """
        try:
            await asyncio.wait_for(self.link.disconnect(), _LINK_CLOSE_LIMIT)
        except asyncio.TimeoutError:
            log.warning("ble: the proxy link did not close within %.0fs - the node may stay "
                        "connected, and so stay invisible to the next scan, until it times out "
                        "on its own", _LINK_CLOSE_LIMIT)
        except Exception as ex:  # noqa: BLE001 - a clean exit is worth more than this error
            log.debug("ble: closing the link on shutdown failed (%r)", ex)

    async def _announce_offline(self) -> None:
        """Publish the retained `offline` and make sure it actually left.

        publish() only queues; the disconnect follows immediately after.
        Without waiting, a clean shutdown could leave `online` retained in the
        broker and Home Assistant showing lights it cannot reach.
        """
        self._publish_availability()
        # Last, so the wait below covers both: the broker processes a single
        # client's publishes in order.
        info = self.mqtt.publish(self.s.bridge_availability_topic, "offline", retain=True)
        if info is None or not hasattr(info, "wait_for_publish"):
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(info.wait_for_publish, 5.0), 6.0)
            delivered = info.is_published()
        except Exception as ex:  # noqa: BLE001 - shutdown must not be derailed by this
            delivered = False
            log.debug("mqtt: waiting for the offline message failed (%r)", ex)
        if not delivered:
            log.warning("mqtt: the broker did not confirm the retained offline message; it may "
                        "keep showing the bridge as available until the last will fires")


# --- start-up ----------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="hafele2mqtt",
        description="Bridge a Hafele Connect Mesh network to MQTT / Home Assistant.")
    p.add_argument("--validate", "--dry-run", dest="validate", action="store_true",
                   help="read and check the .connect file, print what would be exposed, exit. "
                        "Touches neither Bluetooth nor MQTT.")
    p.add_argument("--dump-overrides", dest="dump", action="store_true",
                   help="print an entities file holding everything that is published right now, "
                        "then exit. Reading it back changes nothing; it is there to be kept in "
                        "version control and diffed after the next export. Touches neither "
                        "Bluetooth nor MQTT.")
    p.add_argument("--purge-discovery", dest="purge", action="store_true",
                   help="retract every Home Assistant discovery topic this bridge published "
                        "(use after changing DISCOVERY_PREFIX), then exit.")
    p.add_argument("--version", action="version", version=f"hafele2mqtt {__version__}")
    return p.parse_args(argv)


def _prepare(s: Settings) -> tuple[MeshModel, int, str, str, dict]:
    doc, origin = load_mesh_document(s)
    validate_mesh(doc, origin)
    model = parse(doc, s)
    entities = overrides.locate(s)
    if entities is not None:
        used = overrides.apply(model, s, entities)
        log.info("%s: applied %d override(s)", entities, used)
    plan = plan_src_address(doc, s.src_addr)
    if plan.collision:
        log.warning("SRC_ADDR 0x%04X collides with a real node in the mesh. Messages from the "
                    "bridge and from that node will be mixed up by the network's replay "
                    "protection. Unset SRC_ADDR to let the bridge pick a free address.", plan.src)
    # The document comes back out because rotation needs to ask it the same
    # question again later, with a different set of addresses to avoid.
    return model, plan.src, plan.reason, origin, doc


async def _run(s: Settings, model: MeshModel, state: State, purge: bool) -> int:
    # Synchronous paho, with a 15s CONNACK wait inside it. On the loop thread
    # that is 15s in which nothing else can be scheduled, including the signal
    # handlers installed further down.
    await asyncio.to_thread(broker.preflight, s)
    bridge = Bridge(s, model, state)

    if purge:
        # Without this the normal on_connect fires during connect() and
        # republishes every discovery topic we are here to retract, so the
        # command appeared to work and changed nothing. The LWT has to go too:
        # it would put `online` back under the availability topic on disconnect.
        bridge.mqtt.on_connect = None
        bridge.mqtt.will_clear()
        bridge.mqtt.connect(s.mqtt_host, s.mqtt_port, 30)
        bridge.mqtt.loop_start()
        count, info = bridge.purge_discovery()
        await asyncio.to_thread(info.wait_for_publish, 10.0)
        bridge.mqtt.loop_stop()
        bridge.mqtt.disconnect()
        print(f"retracted {count} discovery topic(s) under {s.discovery_prefix}/")
        return 0

    await ble_mod.preflight(s)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge.request_stop)
    await bridge.run()
    # Non-zero so the container restart policy and `docker ps` show the failure,
    # instead of a bridge that exited "successfully" after losing its state.
    return 1 if bridge.fatal else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level="INFO",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        s = Settings.from_env()
        logging.getLogger().setLevel(s.log_level)
        model, src, reason, origin, doc = _prepare(s)

        if args.validate:
            # Read-only: --validate must be safe to run against a bridge that
            # is up, so it reads the identity map out of the state file without
            # opening, locking or creating it. The unique_ids it prints are
            # then the ones that would really be published.
            apply_identity(model, read_identity(s.state_file))
            print(describe(model, s, src, reason, origin))
            return 0

        if args.dump:
            # Same read-only path as --validate, for the same reason: this has
            # to be runnable against a bridge that is up (and against one that
            # has never reached a light), so it reads the identity map without
            # opening, locking or creating the state file. Warnings go to the
            # log, so stdout is the file and nothing else.
            apply_identity(model, read_identity(s.state_file))
            for w in model.warnings:
                log.warning("%s", w)
            print(overrides.dump(model, s), end="")
            return 0

        state = State(s.state_file, s.seq_block, s.seq_recovery_jump,
                      auto_rotate=s.seq_auto_rotate,
                      # SRC_ADDR is deliberately not passed as the override here.
                      # It says where to start, and a bridge that started on a
                      # pinned address still has to be able to leave it; honouring
                      # it a second time would hand rotation back the one address
                      # it is trying to get away from.
                      pick_src=lambda avoid: plan_src_address(doc, None, avoid=avoid).src)
        # The counter belongs to the address we are about to send from, so load
        # it by that address instead of inheriting the previous SRC_ADDR's.
        state.open(src)
        # Identity before anything is published: the map is what decides which
        # unique_id each entity goes out under, and writing it back now means a
        # new entity is recorded even if the very first discovery never lands.
        state.set_identity(apply_identity(model, state.identity))
        for w in model.warnings:
            log.warning("%s", w)
        if s.iv_index is not None:
            # IV_INDEX is a floor, not a cap: secure network beacons may still
            # raise it, and set_iv() refuses to move it down.
            state.set_iv(s.iv_index, "IV_INDEX")
        return asyncio.run(_run(s, model, state, args.purge))
    except ConfigError as ex:
        log.error("%s", ex)
        return 1
    except FatalStateError as ex:
        # One readable line instead of a traceback: the message names the only
        # recovery there is (a different SRC_ADDR, or a writable volume).
        log.critical("%s", ex)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
