"""Home Assistant discovery payloads, checked without a broker.

The bridge's MQTT client is swapped for a recorder, so these assert exactly what
would go on the wire: stable unique_ids, a device block per physical box, and
the retraction of topics that are no longer in the mesh file.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from app import main as main_mod
from app.config import Settings
from app.main import Bridge
from app.model import parse
from app.state import State

from test_pdu import segmented_access


class PublishInfo:
    """Stands in for paho's MQTTMessageInfo, which only reports after the fact."""

    def __init__(self, published: bool = True):
        self.published = published
        self.waited: float | None = None

    def wait_for_publish(self, timeout=None):
        # paho returns silently when the timeout expires rather than raising,
        # so is_published() is the only thing that says whether it went.
        self.waited = timeout

    def is_published(self) -> bool:
        return self.published


class FakeMqtt:
    """Records publishes instead of sending them."""

    def __init__(self, published: bool = True):
        self.sent: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []
        self.published = published
        self.last_info = PublishInfo()

    def subscribe(self, topic, **_kw):
        self.subscribed.append(topic)

    def publish(self, topic, payload="", retain=False, **_kw):
        self.sent.append((topic, payload, retain))
        self.last_info = PublishInfo(self.published)
        return self.last_info

    def by_topic(self) -> dict[str, str]:
        return {t: p for t, p, _ in self.sent}


@pytest.fixture
def bridge(doc, settings, tmp_path):
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    b = Bridge(settings, parse(doc, settings), state)
    b.mqtt = FakeMqtt()
    return b


def configs(bridge) -> dict[str, dict]:
    """The controllable entities: lights, groups and scenes, without diagnostics."""
    return {t: cfg for t, cfg in all_configs(bridge).items()
            if not cfg["unique_id"].startswith("hafele2mqtt_")}


def all_configs(bridge) -> dict[str, dict]:
    return {t: json.loads(p) for t, p, _ in bridge.mqtt.sent
            if t.endswith("/config") and p}


def test_every_entity_gets_a_retained_config(bridge):
    bridge._discovery()
    cfgs = configs(bridge)
    assert len(cfgs) == 7 + 3           # 4 lights + 3 groups + 3 scenes
    assert all(retain for t, _, retain in bridge.mqtt.sent if t.endswith("/config"))


def test_discovery_publishes_no_state_it_has_not_measured(bridge):
    """Announcing entities is not the same as knowing what they are doing.

    At this point no node has been asked anything, so the only state available
    is the dataclass default, every light off. Publishing that retained made
    Home Assistant show the whole house as off on every restart, until the
    first poll a few seconds later corrected it.
    """
    bridge._discovery()
    assert [t for t, _, _ in bridge.mqtt.sent if t.endswith("/state")] == []


def test_unique_ids_are_unique_and_stable(bridge):
    bridge._discovery()
    uids = [c["unique_id"] for c in configs(bridge).values()]
    assert len(set(uids)) == len(uids)
    assert "hafele_0010" in uids and "hafele_group_c00b" in uids and "hafele_scene_1" in uids


def test_a_light_is_attached_to_its_physical_box(bridge):
    bridge._discovery()
    cfg = configs(bridge)["homeassistant/light/hafele_0010/config"]
    assert cfg["device"]["identifiers"] == ["hafele_node_0010"]
    assert cfg["device"]["name"] == "Box 1"
    assert cfg["device"]["manufacturer"] == "Hafele"
    assert cfg["device"]["sw_version"] == "6.0.5"
    assert cfg["device"]["via_device"] == "hafele2mqtt"


def test_groups_belong_to_the_bridge_device(bridge):
    bridge._discovery()
    cfg = configs(bridge)["homeassistant/light/hafele_group_c002/config"]
    assert cfg["device"]["identifiers"] == ["hafele2mqtt"]
    # The hub must keep entities of its own, or `via_device` on every box and
    # on the scene device would point at a device with nothing on it.
    assert "via_device" not in cfg["device"]


def test_scenes_have_their_own_device_under_the_bridge(bridge):
    bridge._discovery()
    dev = configs(bridge)["homeassistant/scene/hafele_scene_1/config"]["device"]
    assert dev["identifiers"] == ["hafele2mqtt_scenes"]
    assert dev["via_device"] == "hafele2mqtt"
    assert dev["name"] == "Connect Mesh scenes (Test Network)"


def test_a_box_suggests_the_area_its_lights_agree_on(bridge):
    bridge._discovery()
    cfg = configs(bridge)["homeassistant/light/hafele_0010/config"]
    assert cfg["device"]["suggested_area"] == "Room"


def test_a_box_whose_lights_name_no_room_suggests_nothing(bridge):
    """An invented area is worse than none: it shows up in the dashboard as a
    room that does not exist and has to be deleted by hand."""
    bridge._discovery()
    cfg = configs(bridge)["homeassistant/light/hafele_0020/config"]
    assert "suggested_area" not in cfg["device"]


def test_entities_ask_for_a_readable_entity_id(bridge):
    """`default_entity_id`, not the legacy `object_id`, which Home Assistant
    dropped from MQTT discovery in 2026.4. Honoured at creation only."""
    bridge._discovery()
    cfgs = configs(bridge)
    assert cfgs["homeassistant/light/hafele_0010/config"]["default_entity_id"] == "light.light_1"
    assert cfgs["homeassistant/light/hafele_group_c00b/config"]["default_entity_id"] == \
        "light.shelf"
    assert cfgs["homeassistant/scene/hafele_scene_1/config"]["default_entity_id"] == \
        "scene.scene_all_on"
    assert not any("object_id" in c for c in cfgs.values())


def test_a_scene_carries_the_icon_from_the_export(bridge):
    bridge._discovery()
    cfg = configs(bridge)["homeassistant/scene/hafele_scene_1/config"]
    assert cfg["icon"] == "mdi:white-balance-sunny"


def test_an_unmapped_icon_produces_none_rather_than_a_guess(bridge):
    """Scene 3's icon is `ui/somethingNew`. A wrong glyph on every scene is
    worse than Home Assistant's own default."""
    bridge._discovery()
    assert "icon" not in configs(bridge)["homeassistant/scene/hafele_scene_3/config"]


def test_lights_get_no_icon_so_they_keep_the_on_off_one(bridge):
    """Home Assistant's light icon follows the state; a fixed one replaces that
    with a single static glyph."""
    bridge._discovery()
    assert "icon" not in configs(bridge)["homeassistant/light/hafele_0010/config"]


def test_entities_need_both_the_process_and_the_mesh(bridge):
    bridge._discovery()
    for cfg in configs(bridge).values():
        assert cfg["availability_mode"] == "all"
        assert [a["topic"] for a in cfg["availability"]] == [
            "hafele/bridge/status", "hafele/status"]
        assert all(a["payload_not_available"] == "offline" for a in cfg["availability"])


def test_scene_commands_address_the_scene_number(bridge):
    bridge._discovery()
    cfg = configs(bridge)["homeassistant/scene/hafele_scene_1/config"]
    assert cfg["command_topic"] == "hafele/scene/1/set"
    assert cfg["name"] == "Scene All on"


def test_availability_follows_the_mesh_link_not_the_broker(bridge):
    bridge._on_link_state(True)
    bridge._on_link_state(False)
    assert [(t, p) for t, p, _ in bridge.mqtt.sent if t == "hafele/status"] == [
        ("hafele/status", "online"), ("hafele/status", "offline")]


# --- entities turned off in entities.yaml ------------------------------------

def test_a_disabled_light_is_never_announced(bridge):
    bridge.m.lights["hafele_0010"].enabled = False
    bridge._discovery()
    assert "homeassistant/light/hafele_0010/config" not in configs(bridge)
    assert "homeassistant/light/hafele_0011/config" in configs(bridge)


def test_disabling_a_light_clears_what_it_left_in_the_broker(bridge):
    """Its config topic goes through the stale sweep, but the retained *state*
    lives under BASE_TOPIC and would otherwise sit there forever."""
    bridge._discovery()
    bridge.m.lights["hafele_0010"].enabled = False
    bridge.mqtt = FakeMqtt()
    bridge._discovery()
    cleared = [t for t, p, retain in bridge.mqtt.sent if p == "" and retain]
    assert "homeassistant/light/hafele_0010/config" in cleared
    assert "hafele/light/hafele_0010/state" in cleared


def test_a_disabled_light_publishes_no_state(bridge):
    e = bridge.m.lights["hafele_0010"]
    e.enabled = False
    bridge._publish_state(e)
    assert bridge.mqtt.sent == []


def test_a_disabled_scene_is_never_announced(bridge):
    bridge.m.scenes[0].enabled = False
    bridge._discovery()
    assert "homeassistant/scene/hafele_scene_1/config" not in configs(bridge)


def test_removing_a_light_retracts_its_discovery_topic(bridge, doc, settings):
    bridge._discovery()
    assert "homeassistant/light/hafele_0020/config" in bridge.state.discovery

    # Re-export the network without Light 4, as if it had been deleted.
    doc["nodes"][1]["tos_devices"] = [d for d in doc["nodes"][1]["tos_devices"]
                                      if d.get("name") != "Light 4"]
    smaller = Bridge(settings, parse(doc, settings), bridge.state)
    smaller.mqtt = FakeMqtt()
    smaller._discovery()

    retracted = [t for t, p, retain in smaller.mqtt.sent if p == "" and retain]
    assert "homeassistant/light/hafele_0020/config" in retracted
    assert "homeassistant/light/hafele_0010/config" not in retracted


def test_changing_base_topic_clears_the_old_retained_state(doc, settings, tmp_path, monkeypatch):
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    first = Bridge(settings, parse(doc, settings), state)
    first.mqtt = FakeMqtt()
    first._discovery()
    assert state.base_topic == "hafele"

    monkeypatch.setenv("BASE_TOPIC", "mesh")
    moved = Settings.from_env()
    second = Bridge(moved, parse(doc, moved), state)
    second.mqtt = FakeMqtt()
    second._discovery()

    cleared = [t for t, p, retain in second.mqtt.sent if p == "" and retain]
    assert "hafele/status" in cleared
    assert "hafele/light/hafele_0010/state" in cleared
    assert state.base_topic == "mesh"
    # The discovery topics do not contain BASE_TOPIC, so no entity is orphaned.
    assert not [t for t in cleared if t.endswith("/config")]


def test_purge_retracts_everything_it_ever_published(bridge):
    bridge._discovery()
    bridge.mqtt = FakeMqtt()
    count, _ = bridge.purge_discovery()
    assert count == 10 + 6              # 7 lights/groups + 3 scenes + 6 diagnostics
    cleared = bridge.mqtt.by_topic()
    assert all(v == "" for v in cleared.values())
    assert "homeassistant/light/hafele_0010/config" in cleared
    assert "homeassistant/binary_sensor/hafele2mqtt_link/config" in cleared
    assert "hafele/status" in cleared
    assert "hafele/bridge/status" in cleared
    assert "hafele/bridge/diagnostics" in cleared
    assert bridge.state.discovery == set()


class StubPaho:
    """A paho client that behaves like a broker: connecting fires on_connect."""

    def __init__(self, *_a, **_kw):
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.sent: list[tuple[str, str, bool]] = []
        self.will: tuple | None = None

    def username_pw_set(self, *_a, **_kw): pass
    def reconnect_delay_set(self, **_kw): pass
    def subscribe(self, _topic): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass

    def will_set(self, *a, **_kw): self.will = a
    def will_clear(self): self.will = None

    def connect(self, *_a, **_kw):
        if self.on_connect:
            self.on_connect(self, None, None, 0)
        return 0

    def publish(self, topic, payload="", retain=False, **_kw):
        self.sent.append((topic, payload, retain))
        return PublishInfo()


def test_purge_discovery_leaves_the_topics_retracted(doc, settings, tmp_path, monkeypatch):
    """--purge-discovery used to retract everything and then put it straight back.

    The normal on_connect handler fires from paho's thread the moment connect()
    succeeds, so it republished every config topic the purge had just cleared
    and rewrote state.discovery with them. The command printed a count and
    changed nothing.
    """
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    model = parse(doc, settings)

    made: list[StubPaho] = []
    monkeypatch.setattr(main_mod.broker, "preflight", lambda _s: None)
    monkeypatch.setattr(main_mod.mqtt, "Client", lambda *a, **kw: made[-1])

    # A normal start first, so there is something retained to purge.
    made.append(StubPaho())
    normal = Bridge(settings, model, state)
    normal.mqtt.connect(settings.mqtt_host, settings.mqtt_port, 30)
    assert state.discovery

    made.append(StubPaho())
    assert asyncio.run(main_mod._run(settings, model, state, purge=True)) == 0

    sent = made[-1].sent
    assert sent, "the purge published nothing at all"
    assert all(payload == "" and retain for _, payload, retain in sent)
    assert "homeassistant/light/hafele_0010/config" in [t for t, _, _ in sent]
    assert "homeassistant/scene/hafele_scene_1/config" in [t for t, _, _ in sent]
    assert state.discovery == set()
    # The last will would republish `online` under the availability topic the
    # moment this client disconnects, undoing half the purge.
    assert made[-1].will is None


# --- shutdown ----------------------------------------------------------------

def test_shutdown_waits_for_the_retained_offline_to_leave(bridge):
    """publish() only queues, and loop_stop() follows immediately after.

    Without the wait a clean shutdown could leave `online` retained in the
    broker, so Home Assistant kept nine lights available over a bridge that had
    exited, until something else triggered the last will.
    """
    asyncio.run(bridge._announce_offline())
    assert bridge.mqtt.by_topic()["hafele/status"] == "offline"
    assert bridge.mqtt.last_info.waited == 5.0


def test_shutdown_says_so_when_the_broker_never_confirmed(bridge, caplog):
    # paho's wait_for_publish returns silently on timeout, so a silent wait
    # looks exactly like a successful one.
    bridge.mqtt.published = False
    with caplog.at_level("WARNING"):
        asyncio.run(bridge._announce_offline())
    assert "did not confirm" in caplog.text


# --- iv index ----------------------------------------------------------------

def test_an_iv_index_inside_the_spec_window_is_adopted(bridge):
    bridge.state.set_iv(5, "test")
    bridge._on_iv(5 + main_mod.mesh.IV_INDEX_MAX_AHEAD)
    assert bridge.state.iv_index == 5 + main_mod.mesh.IV_INDEX_MAX_AHEAD


def test_a_wild_iv_index_jump_is_refused(bridge, caplog):
    """Authentication proves who sent it, not that the number is sane.

    The IV index is written to disk and never lowered, so adopting a huge one
    is a permanent outage that survives every restart. The spec allows one IV
    update per 96 hours, which is where the ceiling of 42 comes from.
    """
    bridge.state.set_iv(5, "test")
    with caplog.at_level("WARNING"):
        bridge._on_iv(5 + main_mod.mesh.IV_INDEX_MAX_AHEAD + 1)
    assert bridge.state.iv_index == 5
    assert "refusing IV index" in caplog.text


# --- iv update in progress ----------------------------------------------------

def test_an_iv_update_in_progress_is_not_adopted(bridge):
    """The number in the beacon is the *next* index, not the one in use.

    Nodes accept it but keep transmitting with the old one until the update
    finishes (Mesh Profile 3.10.5). Taking it now would mean every message the
    bridge sends is discarded by the whole network - and `set_iv` never lowers
    the index, so no restart would undo it.
    """
    bridge.state.set_iv(5, "test")
    bridge._on_iv(6, iv_update=True)
    assert bridge.state.iv_index == 5
    assert bridge.state.iv_update is True


def test_the_new_index_is_adopted_once_the_update_is_over(bridge):
    bridge.state.set_iv(5, "test")
    bridge._on_iv(6, iv_update=True)
    assert bridge.state.iv_index == 5
    bridge._on_iv(6, iv_update=False)
    assert bridge.state.iv_index == 6
    assert bridge.state.iv_update is False


def test_repeated_update_beacons_do_not_creep_the_index_upward(bridge):
    # A beacon arrives every ten seconds for as long as the update runs, so
    # anything that moved the index per beacon rather than per transition would
    # walk it a long way up over the 96 hours the procedure can take.
    bridge.state.set_iv(5, "test")
    for _ in range(20):
        bridge._on_iv(6, iv_update=True)
    assert bridge.state.iv_index == 5


def test_an_update_beacon_at_index_zero_does_not_go_negative(bridge):
    # iv - 1 on a fresh network would be -1, which is an OverflowError the
    # moment it reaches to_bytes() on the send path.
    assert bridge.state.iv_index == 0
    bridge._on_iv(0, iv_update=True)
    assert bridge.state.iv_index == 0


def test_a_received_message_on_the_new_index_is_still_decoded(bridge):
    """The other half of an IV update: what nodes send while it runs.

    The IVI bit in the header is only the bottom bit of the index, so a PDU
    sent with iv+1 looks like one sent with iv-1 and decodes to nothing. The
    lights would appear to have stopped answering, with nothing logged.
    """
    keys = bridge.keys
    payload = main_mod.mesh.OP_LIGHTNESS_STATUS + (40000).to_bytes(2, "little")
    pdu = main_mod.mesh.build_access_pdu(keys, 6, 1, 0x0010, bridge.src, 5, payload)

    bridge.state.set_iv(5, "test")
    assert main_mod.mesh.decode_access(keys, 5, pdu) is None
    dec = main_mod.mesh.decode_access(keys, 5, pdu, accept_next=True)
    assert dec.src == 0x0010 and dec.payload == payload
    # The index a message actually decoded under is what replay protection has
    # to compare against, so it comes back out rather than being inferred.
    assert dec.iv_index == 6 and dec.seq == 1


def test_the_old_index_is_still_decoded_during_an_update(bridge):
    # Accepting the next index must not cost us the previous one: during an
    # update the network carries both, and most nodes are still on the old.
    keys = bridge.keys
    payload = main_mod.mesh.OP_LIGHTNESS_STATUS + (1).to_bytes(2, "little")
    pdu = main_mod.mesh.build_access_pdu(keys, 4, 1, 0x0010, bridge.src, 5, payload)
    dec = main_mod.mesh.decode_access(keys, 5, pdu, accept_next=True)
    assert dec.src == 0x0010 and dec.payload == payload
    assert dec.iv_index == 4


def test_an_ivi_bit_set_at_index_zero_is_dropped_not_crashed(bridge):
    # There is no index below 0 to fall back to. The naive candidate list has
    # -1 in it, and to_bytes() raises OverflowError out of a BLE callback.
    pdu = bytes([0x80 | bridge.keys.nid]) + bytes(20)
    assert main_mod.mesh.decode_access(bridge.keys, 0, pdu) is None
    assert main_mod.mesh.decode_access(bridge.keys, 0, pdu, accept_next=True) is None


# --- the receive path: what the bridge accepts from the mesh ------------------

def status_pdu(bridge, lightness: int, *, src: int = 0x0010, seq: int = 1,
               iv_index: int | None = None, dst: int | None = None,
               opcode: bytes | None = None) -> bytes:
    """A Light Lightness Status as a light would send it."""
    op = main_mod.mesh.OP_LIGHTNESS_STATUS if opcode is None else opcode
    payload = op + lightness.to_bytes(2, "little")
    return main_mod.mesh.build_access_pdu(
        bridge.keys, bridge.state.iv_index if iv_index is None else iv_index,
        seq, src, bridge.src if dst is None else dst, 5, payload)


def test_a_status_message_updates_the_light_it_came_from(bridge):
    bridge._on_pdu(status_pdu(bridge, 30000, seq=1))
    assert bridge.by_dst[0x0010].lightness == 30000


def test_the_same_message_twice_is_only_acted_on_once(bridge):
    """A replayed Light Lightness Status reports a brightness from the past.

    The sequence number is inside the authenticated header, so a replayer
    cannot raise it - but it does not have to. Recording a message off the air
    and sending it again later is enough to make Home Assistant believe a light
    is at a level it left an hour ago, and act on it.
    """
    pdu = status_pdu(bridge, 30000, seq=5)
    bridge._on_pdu(pdu)
    bridge.by_dst[0x0010].lightness = 100        # something changed it since
    bridge._on_pdu(pdu)
    assert bridge.by_dst[0x0010].lightness == 100
    assert bridge._replays == 1


def test_an_older_sequence_number_is_refused_even_if_it_is_new_to_us(bridge):
    # Not just exact repeats: anything at or below the high-water mark is
    # either a duplicate off a second relay path or a recording.
    bridge._on_pdu(status_pdu(bridge, 30000, seq=10))
    bridge._on_pdu(status_pdu(bridge, 999, seq=9))
    assert bridge.by_dst[0x0010].lightness == 30000
    assert bridge._replays == 1


def test_the_sequence_number_only_has_to_beat_that_one_source(bridge):
    """Each node counts for itself. One high-water mark for the whole mesh
    would let the busiest node mute every other one."""
    bridge._on_pdu(status_pdu(bridge, 30000, src=0x0010, seq=500))
    bridge._on_pdu(status_pdu(bridge, 40000, src=0x0011, seq=2))
    assert bridge.by_dst[0x0010].lightness == 30000
    assert bridge.by_dst[0x0011].lightness == 40000
    assert bridge._replays == 0


def test_a_new_iv_index_outranks_any_sequence_number(bridge):
    """Sequence numbers restart when the IV index moves, by design.

    So freshness is the pair, not the number: a seq of 1 at index 1 is newer
    than a seq of a million at index 0. Comparing sequence numbers alone would
    mute every node for as long as it took them to count back up.
    """
    bridge._on_pdu(status_pdu(bridge, 30000, seq=0xFFFFFF, iv_index=0))
    bridge.state.set_iv(1, "test")
    bridge._on_pdu(status_pdu(bridge, 40000, seq=1, iv_index=1))
    assert bridge.by_dst[0x0010].lightness == 40000
    assert bridge._replays == 0


def test_an_old_iv_index_is_refused_whatever_its_sequence_number(bridge):
    # The other direction of the same rule, which is what a recording made
    # before an IV update looks like.
    bridge.state.set_iv(1, "test")
    bridge._on_pdu(status_pdu(bridge, 30000, seq=1, iv_index=1))
    bridge._on_pdu(status_pdu(bridge, 999, seq=0xFFFFFF, iv_index=0))
    assert bridge.by_dst[0x0010].lightness == 30000
    assert bridge._replays == 1


def test_replay_protection_remembers_only_nodes_in_the_mesh_file(bridge):
    """The map must not be growable by whoever is transmitting.

    Addresses are filtered before `_seen` is touched, so it stays the size of
    the network rather than the size of the traffic.
    """
    bridge._on_pdu(status_pdu(bridge, 30000, src=0x0010))
    bridge._on_pdu(status_pdu(bridge, 30000, src=0x7001))   # not in the file
    assert set(bridge._seen) == {0x0010}


def test_a_message_addressed_to_somebody_else_is_not_remembered(bridge):
    # Mesh traffic that happens to reach us is not ours to track, and counting
    # it would raise the high-water mark past messages that really are.
    bridge._on_pdu(status_pdu(bridge, 30000, seq=50, dst=0x0011))
    assert bridge._seen == {}
    bridge._on_pdu(status_pdu(bridge, 40000, seq=2))
    assert bridge.by_dst[0x0010].lightness == 40000


def test_an_unhandled_opcode_from_a_known_light_is_named_once(bridge, caplog):
    """The one clue that a node speaks a model the bridge does not.

    Without it, a light answering Generic OnOff is indistinguishable from a
    light that never answers, and the two have nothing in common to fix.
    """
    with caplog.at_level(logging.INFO, logger="bridge"):
        for _ in range(5):
            bridge._on_pdu(status_pdu(bridge, 1, opcode=b"\x82\x04"))
    said = [r for r in caplog.records if "does not" in r.getMessage()]
    assert len(said) == 1
    assert "8204" in said[0].getMessage()


def test_two_different_unhandled_opcodes_are_both_named(bridge, caplog):
    with caplog.at_level(logging.INFO, logger="bridge"):
        bridge._on_pdu(status_pdu(bridge, 1, seq=1, opcode=b"\x82\x04"))
        bridge._on_pdu(status_pdu(bridge, 1, seq=2, opcode=b"\x82\x08"))
    said = [r for r in caplog.records if "does not" in r.getMessage()]
    assert len(said) == 2


def test_a_segmented_message_is_reported_once_rather_than_dropped(bridge, caplog):
    """The bridge can neither reassemble nor acknowledge one.

    So it is lost either way, and the sender will keep retransmitting it. What
    changes is that the log says which of "never answered" and "answered in a
    form I threw away" actually happened.
    """
    pdu = segmented_access(bridge.keys, bridge.state.iv_index, 1, 0x0010, bridge.src)
    with caplog.at_level(logging.WARNING, logger="bridge"):
        for _ in range(5):
            bridge._on_pdu(pdu)
    said = [r for r in caplog.records if "segmented" in r.getMessage()]
    assert len(said) == 1
    assert bridge.by_dst[0x0010].known is False


# --- colour mode --------------------------------------------------------------

def test_lights_declare_the_brightness_colour_mode(bridge):
    """Newer Home Assistant reads the mode from `supported_color_modes`.

    Without it a JSON-schema light is treated as on/off only, or raises a
    repair notice, and the brightness slider the mesh supports disappears.
    """
    bridge._discovery()
    for topic, cfg in configs(bridge).items():
        if "/light/" not in topic:
            continue
        assert cfg["supported_color_modes"] == ["brightness"]
        # Kept alongside it on purpose: an older Home Assistant drops the key
        # it does not know and would otherwise fall back to an on/off switch.
        assert cfg["brightness"] is True


def test_the_colour_mode_change_did_not_move_any_unique_id(bridge):
    """The one thing a discovery change must never do.

    `unique_id` is the entity's identity: change it and Home Assistant creates
    a new entity, orphaning the old one along with its history, its name, its
    area and every automation pointing at it.
    """
    bridge._discovery()
    uids = {c["unique_id"] for c in configs(bridge).values()}
    assert uids == {
        "hafele_0010", "hafele_0011", "hafele_0012", "hafele_0020",
        "hafele_group_c002", "hafele_group_c006", "hafele_group_c00b",
        "hafele_scene_1", "hafele_scene_2", "hafele_scene_3",
    }
    for topic, cfg in configs(bridge).items():
        assert topic.rsplit("/", 2)[1] == cfg["unique_id"]


# --- republishing state when MQTT comes back ----------------------------------

class ReasonOk:
    """paho hands the callback a reason code object, not an int."""
    is_failure = False


def connect(bridge) -> None:
    """Drive on_connect the way paho's network thread would."""
    bridge._on_mqtt_connect(bridge.mqtt, None, None, ReasonOk())


def live_states(bridge) -> dict[str, dict]:
    return {t: json.loads(p) for t, p, _ in bridge.mqtt.sent
            if t.endswith("/state") and p}


def test_nothing_known_means_nothing_republished(bridge):
    """A first connection must not invent a state for anything.

    `known` is false until a node has told us a level, and publishing the
    dataclass default instead would put "every light off" into the broker as a
    retained fact.
    """
    connect(bridge)
    assert live_states(bridge) == {}


def test_what_we_last_knew_is_republished_on_reconnect(bridge):
    """State is published on change only, so a lost publish is lost for good.

    The retained value in the broker then stays whatever it was - possibly ON
    for a light that has been off since - and nothing corrects it until that
    same light happens to change again.
    """
    e = bridge.m.lights["hafele_0010"]
    e.lightness, e.known = 65535, True
    connect(bridge)
    states = live_states(bridge)
    assert states["hafele/light/hafele_0010/state"] == {"state": "ON", "brightness": 255}
    assert "hafele/light/hafele_0011/state" not in states


def test_a_disabled_entity_is_not_republished(bridge):
    e = bridge.m.lights["hafele_0010"]
    e.lightness, e.known, e.enabled = 65535, True, False
    connect(bridge)
    assert live_states(bridge) == {}


def test_the_republish_comes_after_discovery(bridge):
    """Order matters: a state for an entity that does not exist yet is dropped.

    Home Assistant only keeps what arrives on a topic it already has a config
    for, so publishing state first would leave the retained value in the broker
    and the entity showing nothing.
    """
    e = bridge.m.lights["hafele_0010"]
    e.lightness, e.known = 30000, True
    connect(bridge)
    topics = [t for t, p, _ in bridge.mqtt.sent if p]
    assert topics.index("homeassistant/light/hafele_0010/config") < \
        topics.index("hafele/light/hafele_0010/state")


def test_a_refused_connection_publishes_nothing(bridge):
    class Refused:
        is_failure = True

    bridge._on_mqtt_connect(bridge.mqtt, None, None, Refused())
    assert bridge.mqtt.sent == [] and bridge.mqtt.subscribed == []


def test_discovery_prefix_is_honoured(doc, tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_PREFIX", "ha")
    s = Settings.from_env()
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    b = Bridge(s, parse(doc, s), state)
    b.mqtt = FakeMqtt()
    b._discovery()
    assert all(t.startswith("ha/") for t in configs(b))
