"""MQTT command handling, with the mesh send path stubbed out.

Nothing here touches Bluetooth: `_send_access` is replaced by a recorder, so the
tests assert which address would be driven with which payload.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import pytest

from app import main as main_mod
from app.main import Bridge
from app.mesh import OP_LIGHTNESS_SET_UNACK, OP_SCENE_RECALL_UNACK
from app.model import parse
from app.state import SEQ_WARN_AT, FatalStateError, State

from test_discovery import FakeMqtt
from test_pdu import filter_status
from test_state import FakePlanner


@pytest.fixture
def bridge(doc, settings, tmp_path, monkeypatch):
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    b = Bridge(settings, parse(doc, settings), state)
    b.mqtt = FakeMqtt()
    b.sent = []

    async def record(dst, payload, repeats=2):
        b.sent.append((dst, payload))
        return b.send_succeeds

    b.send_succeeds = True
    monkeypatch.setattr(b, "_send_access", record)
    # _handle schedules a follow-up poll; there is no link to poll over here.
    # A plain assignment, not monkeypatch: unpatch_spawn() removes it from the
    # instance by hand, and pytest 9's monkeypatch teardown then fails trying
    # to undo an attribute that is already gone. The bridge is per-test anyway.
    b._spawn = lambda coro: coro.close()
    return b


def send(bridge, topic, payload):
    asyncio.run(bridge._handle(topic, payload))
    return bridge.sent


def lightness(payload: bytes) -> int:
    assert payload[:2] == OP_LIGHTNESS_SET_UNACK
    return int.from_bytes(payload[2:4], "little")


def test_on_without_brightness_restores_the_last_level(bridge):
    bridge.m.lights["hafele_0010"].last_on = 30000
    dst, payload = send(bridge, "hafele/light/hafele_0010/set", '{"state":"ON"}')[0]
    assert dst == 0x10
    assert lightness(payload) == 30000


def test_a_never_used_light_turns_on_at_full(bridge):
    bridge.m.lights["hafele_0010"].last_on = 0
    _, payload = send(bridge, "hafele/light/hafele_0010/set", '{"state":"ON"}')[0]
    assert lightness(payload) == 65535


def test_off_sends_zero(bridge):
    _, payload = send(bridge, "hafele/light/hafele_0010/set", '{"state":"OFF"}')[0]
    assert lightness(payload) == 0


@pytest.mark.parametrize("brightness,expected", [(0, 0), (1, 257), (128, 32896), (255, 65535)])
def test_brightness_scales_across_the_full_range(bridge, brightness, expected):
    _, payload = send(bridge, "hafele/light/hafele_0010/set",
                      json.dumps({"state": "ON", "brightness": brightness}))[0]
    assert lightness(payload) == expected


def test_a_bare_payload_is_accepted_as_well_as_json(bridge):
    _, payload = send(bridge, "hafele/light/hafele_0010/set", "off")[0]
    assert lightness(payload) == 0


def test_a_group_command_goes_to_the_multicast_address_once(bridge):
    sent = send(bridge, "hafele/light/hafele_group_c00b/set", '{"state":"ON"}')
    assert len(sent) == 1
    assert sent[0][0] == 0xC00B


def test_a_group_command_updates_its_members_optimistically(bridge):
    send(bridge, "hafele/light/hafele_group_c00b/set", '{"state":"ON","brightness":255}')
    states = {json.loads(p)["state"] for t, p, _ in bridge.mqtt.sent
              if t in ("hafele/light/hafele_0010/state", "hafele/light/hafele_0011/state")}
    assert states == {"ON"}
    # Light 3 is not in the Shelf group and must not have moved.
    assert bridge.m.lights["hafele_0012"].lightness == 0


def test_a_group_reflects_the_brightest_member(bridge):
    send(bridge, "hafele/light/hafele_0010/set", '{"state":"ON","brightness":255}')
    assert bridge.m.lights["hafele_group_c002"].lightness == 65535


def test_scene_recall_targets_the_all_group(bridge):
    dst, payload = send(bridge, "hafele/scene/3/set", "ON")[0]
    assert dst == 0xC002
    assert payload[:2] == OP_SCENE_RECALL_UNACK
    assert int.from_bytes(payload[2:4], "little") == 3


def test_an_unknown_entity_is_ignored_rather_than_crashing(bridge):
    assert send(bridge, "hafele/light/hafele_dead/set", '{"state":"ON"}') == []


def test_transaction_ids_advance_and_wrap(bridge):
    bridge._tids[0x0010] = 0xFF
    assert [bridge._tid(0x0010) for _ in range(3)] == [0, 1, 2]


def test_each_destination_counts_its_own_transactions(bridge):
    """Two lights commanded in the same breath must not share a TID counter.

    A transaction is (source, destination, TID), so what must not repeat inside
    the six-second window is a TID to the same destination. A single counter for
    the whole bridge wrapped against total commands sent instead, which is the
    wrong thing to count: it made 256 commands to anything collide rather than
    256 commands to one light.
    """
    assert bridge._tid(0x0010) == bridge._tid(0x0011) == 0
    assert bridge._tid(0x0010) == 1
    assert bridge._tid(0x0011) == 1
    # Exhaust one destination's space; the other is untouched by it.
    bridge._tids[0x0010] = 0xFF
    assert bridge._tid(0x0010) == 0
    assert bridge._tid(0x0011) == 2


# --- commands that never reached the mesh ------------------------------------

def states(bridge) -> list[str]:
    return [p for t, p, _ in bridge.mqtt.sent if t.endswith("/state")]


def test_a_dropped_command_does_not_publish_a_state_nobody_acknowledged(bridge):
    """A send that failed means the light did not move.

    The old code logged "command dropped" and then published the requested
    brightness anyway, so Home Assistant showed a light at a level no node ever
    agreed to, and kept showing it until the next poll.
    """
    bridge.send_succeeds = False
    send(bridge, "hafele/light/hafele_0010/set", '{"state":"ON","brightness":255}')
    assert states(bridge) == []
    assert bridge.m.lights["hafele_0010"].lightness == 0
    assert bridge.m.lights["hafele_0010"].known is False


def test_a_dropped_group_command_leaves_the_members_alone(bridge):
    bridge.send_succeeds = False
    send(bridge, "hafele/light/hafele_group_c00b/set", '{"state":"ON"}')
    assert states(bridge) == []
    assert all(e.lightness == 0 for e in bridge.m.lights["hafele_group_c00b"].members)


def test_a_dropped_scene_recall_is_not_announced(bridge):
    bridge.send_succeeds = False
    assert len(send(bridge, "hafele/scene/3/set", "ON")) == 1   # attempted once
    assert states(bridge) == []


# --- payloads from whoever happens to publish on the topic -------------------

@pytest.mark.parametrize("payload", [
    "1",                                     # valid JSON, not an object
    "null",
    "[1, 2, 3]",
    '{"state":"ON","brightness":"bright"}',
    '{"state":"ON","brightness":null}',
    '{"state":"ON","brightness":[255]}',
])
def test_an_unusable_payload_is_ignored_rather_than_crashing(bridge, payload):
    # json.loads("1") is an int, so cmd.get() used to raise AttributeError
    # inside the MQTT callback, where nothing was there to catch it.
    assert send(bridge, "hafele/light/hafele_0010/set", payload) == []
    assert states(bridge) == []


@pytest.mark.parametrize("brightness,expected", [(-5, 0), (999, 65535)])
def test_an_out_of_range_brightness_is_clamped_not_overflowed(bridge, brightness, expected):
    # The lightness field is two bytes, so 999 scaled up would not fit and the
    # message would be rejected by the mesh rather than by us.
    _, payload = send(bridge, "hafele/light/hafele_0010/set",
                      json.dumps({"state": "ON", "brightness": brightness}))[0]
    assert lightness(payload) == expected


@pytest.mark.parametrize("key", [
    "kitchen", "", " 3", "3 ", "+3", "-1", "3.0", "0x10",
    "0",                                     # 0 means "no scene" in a recall
    "65536", "99999",                        # the field is two bytes wide
    "\u0663",                                # Arabic-Indic 3: isdigit(), int() == 3
    "\u00b3",                                # superscript 3: isdigit(), int() raises
])
def test_a_scene_topic_that_is_not_a_plain_number_is_ignored(bridge, key):
    """`str.isdigit()` is not the same question as "will int() take this".

    It is true for Arabic-Indic digits, which int() happily converts - so
    `hafele/scene/٣/set` recalled scene 3 from a topic nobody subscribed under.
    It is also true for superscripts, which int() refuses, so the check that was
    there to prevent a crash let one through on exactly its own input.
    """
    assert send(bridge, f"hafele/scene/{key}/set", "ON") == []


@pytest.mark.parametrize("key, num", [("1", 1), ("3", 3), ("65535", 65535)])
def test_the_scene_numbers_the_mesh_accepts_still_work(bridge, key, num):
    _, payload = send(bridge, f"hafele/scene/{key}/set", "ON")[0]
    assert int.from_bytes(payload[2:4], "little") == num


@pytest.mark.parametrize("state", ["off", "Off", " off "])
def test_a_lowercase_off_turns_the_light_off(bridge, state):
    """`{"state":"off"}` used to turn the light *on*.

    Home Assistant sends "OFF", but plenty of scripts and Node-RED flows send
    "off", and comparing against "OFF" exactly read that as "not off" and fell
    through to the restore-last-level branch.
    """
    _, payload = send(bridge, "hafele/light/hafele_0010/set",
                      json.dumps({"state": state}))[0]
    assert lightness(payload) == 0


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
def test_a_non_finite_brightness_is_ignored_rather_than_crashing(bridge, literal):
    # Python's json accepts these as floats, and int(float("inf")) raises
    # OverflowError, int(float("nan")) raises ValueError - both inside the
    # command handler where only the generic logger would have seen them.
    assert send(bridge, "hafele/light/hafele_0010/set",
                '{"state":"ON","brightness":%s}' % literal) == []
    assert states(bridge) == []


@pytest.mark.parametrize("level", [1, 64, 128])
def test_a_barely_lit_light_is_not_published_as_brightness_zero(bridge, level):
    """A lamp that is on is never at brightness 0.

    Lightness 1..128 scales to 0.0..0.5 and rounds to 0, so Home Assistant was
    shown "ON, brightness 0": a lit icon with the slider on the floor, and
    dragging it up by one step sent a value the lamp was already at.
    """
    bridge.m.lights["hafele_0010"].lightness = level
    bridge._publish_state(bridge.m.lights["hafele_0010"])
    assert json.loads(states(bridge)[-1]) == {"state": "ON", "brightness": 1}


def test_a_light_that_is_off_still_reports_brightness_zero(bridge):
    bridge.m.lights["hafele_0010"].lightness = 0
    bridge._publish_state(bridge.m.lights["hafele_0010"])
    assert json.loads(states(bridge)[-1]) == {"state": "OFF", "brightness": 0}


# --- a spent counter is not a radio problem ----------------------------------

def test_an_exhausted_counter_is_not_reported_as_a_dead_radio(doc, settings, tmp_path):
    """`except Exception` caught this and called it a dropped command.

    So the bridge logged "mesh proxy link is down", blamed the radio, retried
    (burning what was left of the counter) and carried on running while nothing
    it sent was being accepted. It has to come out of the send path intact.
    """
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    state.seq = 0xFFFFFF
    state.reserved = 0xFFFFFF + 10

    b = Bridge(settings, parse(doc, settings), state)
    b.mqtt = FakeMqtt()

    class LinkThatIsFine:
        connected = True

        async def send(self, *_a, **_kw):
            raise AssertionError("the PDU should never have been built")

    b.link = LinkThatIsFine()
    with pytest.raises(FatalStateError, match="sequence number space"):
        asyncio.run(b._send_access(0x0010, OP_LIGHTNESS_SET_UNACK + b"\x00\x00\x00"))


# --- background tasks --------------------------------------------------------

def unpatch_spawn(bridge) -> None:
    """Put the real _spawn back; the fixture stubs it out for every other test."""
    bridge.__dict__.pop("_spawn", None)


def test_a_spawned_task_is_held_until_it_finishes(bridge):
    """asyncio only keeps a weak reference, so a task can vanish mid-flight.

    Both of the fire-and-forget polls here are the only thing that corrects
    Home Assistant after a group command, so losing one leaves the UI wrong
    until the next poll cycle.
    """
    unpatch_spawn(bridge)

    seen = []

    async def body():
        await asyncio.sleep(0)
        seen.append("ran")

    async def main():
        task = bridge._spawn(body())
        assert task in bridge._tasks
        await task
        assert bridge._tasks == set()

    asyncio.run(main())
    assert seen == ["ran"]


def test_a_failing_background_task_is_logged_not_swallowed(bridge, caplog):
    unpatch_spawn(bridge)

    async def body():
        raise RuntimeError("the poll fell over")

    async def main():
        with caplog.at_level("ERROR"):
            await asyncio.gather(bridge._spawn(body()), return_exceptions=True)

    asyncio.run(main())
    assert "the poll fell over" in caplog.text


def test_a_background_task_that_loses_the_state_file_stops_the_bridge(bridge):
    unpatch_spawn(bridge)

    async def body():
        raise FatalStateError("cannot persist mesh state")

    async def main():
        bridge.loop = asyncio.get_running_loop()
        await asyncio.gather(bridge._spawn(body()), return_exceptions=True)

    asyncio.run(main())
    assert isinstance(bridge.fatal, FatalStateError)


# --- a group remembers how bright it was -------------------------------------

def test_turning_a_group_back_on_restores_the_level_it_was_dimmed_to(bridge):
    """ON without a brightness means "back to what you were", for groups too.

    The group branch recorded `lightness` but never `last_on`, so a group kept
    the 65535 it was constructed with. Dimming the kitchen to a quarter,
    switching it off and switching it on again put it back to full every time -
    which reads as the bridge ignoring the dim, not as a bookkeeping slip.
    """
    group = "hafele/light/hafele_group_c00b/set"
    send(bridge, group, '{"state":"ON","brightness":64}')
    dimmed = lightness(bridge.sent[-1][1])
    assert dimmed < 65535

    send(bridge, group, '{"state":"OFF"}')
    assert lightness(bridge.sent[-1][1]) == 0

    send(bridge, group, '{"state":"ON"}')
    assert lightness(bridge.sent[-1][1]) == dimmed


def test_a_group_that_has_never_been_used_still_turns_on_at_full(bridge):
    send(bridge, "hafele/light/hafele_group_c00b/set", '{"state":"ON"}')
    assert lightness(bridge.sent[-1][1]) == 65535


# --- an IV index we cannot write down ----------------------------------------

def test_an_iv_index_that_cannot_be_persisted_stops_the_bridge(bridge, monkeypatch):
    """_on_iv runs inside bleak's notification callback, which eats exceptions.

    So losing the volume here left the new IV index in memory and nowhere else:
    the bridge kept running and looking healthy, and the next restart came back
    on the old IV index with every message it sent discarded by the entire
    mesh, silently.
    """
    def gone(*_a, **_kw):
        raise FatalStateError("cannot persist mesh state to /data/state.json")

    monkeypatch.setattr(bridge.state, "set_iv", gone)
    bridge._on_iv(bridge.state.iv_index + 1)       # must not raise out of here
    assert isinstance(bridge.fatal, FatalStateError)


# --- follow-up polls, coalesced ----------------------------------------------

class ConnectedLink:
    connected = True


@pytest.fixture
def polling(bridge, monkeypatch):
    """A bridge whose real _spawn runs, recording polls instead of sending."""
    unpatch_spawn(bridge)
    bridge.polls = []

    async def record(targets=None):
        bridge.polls.append(None if targets is None else sorted(e.uid for e in targets))

    monkeypatch.setattr(bridge, "poll", record)
    monkeypatch.setattr(bridge, "link", ConnectedLink())
    return bridge


def test_twenty_slider_steps_start_one_waiter_not_twenty(bridge):
    """Each command used to start its own poll task.

    Dragging a brightness slider sends a command per position, so twenty
    positions meant twenty polls of the same lamp on top of the twenty
    commands. Those polls spend sequence numbers out of a 24-bit space that is
    never refilled, and they say nothing: only the state after the last command
    is interesting.
    """
    unpatch_spawn(bridge)

    async def main():
        for step in range(1, 21):
            await bridge._handle("hafele/light/hafele_0010/set",
                                 json.dumps({"state": "ON", "brightness": step * 12}))
        assert len(bridge._tasks) == 1
        for task in list(bridge._tasks):
            task.cancel()

    asyncio.run(main())


def test_a_burst_of_commands_is_answered_by_a_single_poll(polling):
    async def main():
        for uid in ("hafele_0010", "hafele_0011", "hafele_0010"):
            polling._poll_soon([polling.m.lights[uid]], 0.02)
        await asyncio.sleep(0.12)

    asyncio.run(main())
    assert polling.polls == [["hafele_0010", "hafele_0011"]]


def test_the_poll_waits_for_the_commands_to_stop_rather_than_firing_mid_drag(polling):
    async def main():
        polling._poll_soon([polling.m.lights["hafele_0010"]], 0.06)
        await asyncio.sleep(0.04)
        polling._poll_soon([polling.m.lights["hafele_0010"]], 0.06)
        await asyncio.sleep(0.04)
        assert polling.polls == [], "polled while commands were still arriving"
        await asyncio.sleep(0.08)

    asyncio.run(main())
    assert len(polling.polls) == 1


def test_a_light_command_cannot_pull_in_a_scene_settle(polling):
    """A scene asks for a longer wait because more lamps have to move.

    The deadline is a max(), not an assignment, so a light command landing
    straight after a scene pushes the poll out and never forward.
    """
    async def main():
        polling._poll_soon(None, 0.15)                              # scene
        polling._poll_soon([polling.m.lights["hafele_0010"]], 0.01)  # light
        await asyncio.sleep(0.08)
        assert polling.polls == []
        await asyncio.sleep(0.12)

    asyncio.run(main())
    assert polling.polls == [None]       # the scene's "poll everything" won


def test_a_command_arriving_after_the_poll_gets_its_own(polling):
    async def main():
        polling._poll_soon([polling.m.lights["hafele_0010"]], 0.02)
        await asyncio.sleep(0.1)
        polling._poll_soon([polling.m.lights["hafele_0011"]], 0.02)
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert polling.polls == [["hafele_0010"], ["hafele_0011"]]


# --- the start-up poll --------------------------------------------------------

class DeadLink:
    connected = False

    def __init__(self, comes_up: bool = False) -> None:
        self.comes_up = comes_up
        self.waited = []

    async def wait_up(self, timeout):
        self.waited.append(timeout)
        self.connected = self.comes_up
        return self.comes_up


def test_a_poll_is_repeated_as_many_times_as_a_command(bridge, monkeypatch):
    """A poll used to go out once where a command goes out twice.

    Nothing on this network is acknowledged, so the single copy was the whole
    attempt: lose it to a collision and the lamp is simply not asked again
    until the next interval, with nothing anywhere saying a request went
    missing.
    """
    seen = []

    async def record(dst, payload, repeats=2):
        seen.append(repeats)
        return True

    monkeypatch.setattr(bridge, "_send_access", record)
    asyncio.run(bridge.poll([bridge.m.lights["hafele_0010"]]))

    command = []

    async def record_command(dst, payload, repeats=2):
        command.append(repeats)
        return True

    monkeypatch.setattr(bridge, "_send_access", record_command)
    send(bridge, "hafele/light/hafele_0010/set", '{"state":"ON"}')
    assert seen == command == [2]


@pytest.fixture
def startup(bridge, monkeypatch):
    """A bridge whose start-up poll records its targets instead of sending.

    `answers` is the set of uids that reply when polled; everything else stays
    silent, which is what the real gap looks like.
    """
    unpatch_spawn(bridge)
    bridge.polls = []
    bridge.answers = set()

    async def record(targets=None):
        asked = list(targets) if targets is not None else list(bridge.by_dst.values())
        bridge.polls.append(sorted(e.uid for e in asked))
        for e in asked:
            if e.uid in bridge.answers:
                e.known = True

    monkeypatch.setattr(bridge, "poll", record)
    monkeypatch.setattr(bridge, "link", ConnectedLink())
    monkeypatch.setattr(main_mod, "_STARTUP_POLL_DELAYS", (0.01, 0.01, 0.01))
    monkeypatch.setattr(main_mod, "_STARTUP_SETTLE", 0.01)
    return bridge


def test_the_start_up_poll_re_asks_only_the_lights_that_stayed_silent(startup):
    """The point of the retry is the gap, not a second full sweep.

    Re-reading lamps that already answered would spend sequence numbers out of
    a space that is never refilled, to learn what we were just told.
    """
    startup.answers = {"hafele_0010", "hafele_0011"}
    asyncio.run(startup._startup_poll())

    assert startup.polls[0] == ["hafele_0010", "hafele_0011", "hafele_0012", "hafele_0020"]
    for later in startup.polls[1:]:
        assert later == ["hafele_0012", "hafele_0020"]


def test_the_start_up_poll_stops_once_everything_has_answered(startup):
    startup.answers = {e.uid for e in startup.by_dst.values()}
    asyncio.run(startup._startup_poll())
    assert len(startup.polls) == 1


def test_a_light_that_stays_silent_is_named_in_the_log(startup, caplog):
    """Otherwise the symptom is a blank card in Home Assistant and no clue why."""
    startup.answers = {"hafele_0010", "hafele_0011", "hafele_0020"}
    with caplog.at_level("WARNING"):
        asyncio.run(startup._startup_poll())
    assert "Light 3" in caplog.text
    assert "Light 1" not in caplog.text
    # The number of rounds is there too, so the next reader knows whether it
    # was asked once or five times before giving up.
    assert "4 round(s)" in caplog.text


def test_everything_answering_is_logged_too(startup, caplog):
    startup.answers = {e.uid for e in startup.by_dst.values()}
    with caplog.at_level("INFO"):
        asyncio.run(startup._startup_poll())
    assert "all 4 light(s) reported a state" in caplog.text


def test_the_start_up_poll_waits_for_the_radio_rather_than_skipping(startup, monkeypatch):
    """The link is rarely up five seconds in: a cold connect is a scan first.

    Skipping the poll because of that is how the gap got long - the next
    attempt was then a whole poll interval away.
    """
    link = DeadLink(comes_up=True)
    monkeypatch.setattr(startup, "link", link)
    startup.answers = {e.uid for e in startup.by_dst.values()}
    asyncio.run(startup._startup_poll())
    assert link.waited == [main_mod._STARTUP_LINK_WAIT]
    assert len(startup.polls) == 1


def test_a_radio_that_never_comes_up_is_not_polled_into(startup, monkeypatch):
    monkeypatch.setattr(startup, "link", DeadLink(comes_up=False))
    asyncio.run(startup._startup_poll())
    assert startup.polls == []


def test_each_round_asks_only_what_is_still_missing(startup, monkeypatch):
    """Answers trickle in between rounds, and the target set has to follow.

    Nothing is acknowledged, so a round is a guess at what was lost. Computing
    it once and re-using it would re-read lamps that answered in the meantime,
    spending sequence numbers out of a space that is never refilled.
    """
    answering = ["hafele_0010", "hafele_0011", "hafele_0012"]
    recorded = startup.poll

    async def poll(targets=None):
        await recorded(targets)
        if answering:                       # one more lamp replies after each round
            startup.m.lights[answering.pop(0)].known = True

    monkeypatch.setattr(startup, "poll", poll)
    asyncio.run(startup._startup_poll())
    assert [len(p) for p in startup.polls] == [4, 3, 2, 1]
    assert startup.polls[-1] == ["hafele_0020"]


def test_a_link_that_drops_mid_run_is_neither_shouted_into_nor_counted(startup, monkeypatch, caplog):
    """A radio can go away between rounds, and sending into it teaches nothing.

    Counting those rounds would be worse than wasteful: the line that says how
    many times a lamp was asked is the one a reader uses to decide whether the
    lamp is deaf or the bridge never really asked, and rounds that went nowhere
    would make it say the opposite of what happened.
    """
    link = startup.link
    recorded = startup.poll
    naps = []

    async def poll(targets=None):
        await recorded(targets)
        link.connected = False              # the radio goes away after the first round

    real_sleep = asyncio.sleep

    async def nap(_d):
        naps.append(_d)
        if len(naps) == 3:                  # and comes back before the last one
            link.connected = True
        await real_sleep(0)

    monkeypatch.setattr(startup, "poll", poll)
    monkeypatch.setattr(asyncio, "sleep", nap)
    with caplog.at_level("WARNING"):
        asyncio.run(startup._startup_poll())

    assert len(startup.polls) == 2          # the first, and one after the radio returns
    assert startup.polls[1] == ["hafele_0010", "hafele_0011", "hafele_0012", "hafele_0020"]
    assert "2 round(s)" in caplog.text      # not four


def test_an_answer_arriving_while_settling_is_not_called_silent(startup, monkeypatch, caplog):
    """The settle pause exists because the last round's replies are still in flight.

    Mesh answers arrive as unsolicited status messages some time after the
    request. Reading the list the instant the last poll returns would name
    lamps that were about to answer, which is a false alarm in the one log
    line that is meant to be worth acting on.
    """
    startup.answers = {"hafele_0010", "hafele_0011", "hafele_0020"}
    late = startup.m.lights["hafele_0012"]
    monkeypatch.setattr(main_mod, "_STARTUP_SETTLE", 0.123)   # distinct from the delays
    real_sleep = asyncio.sleep

    async def nap(d):
        if d == 0.123:
            late.known = True
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", nap)
    with caplog.at_level("INFO"):
        asyncio.run(startup._startup_poll())
    assert "never answered" not in caplog.text
    assert "Light 3" not in caplog.text
    assert "all 4 light(s) reported a state" in caplog.text


def test_a_shutdown_during_the_start_up_poll_ends_it(startup, monkeypatch):
    """It runs for the best part of a minute when lamps are missing, and a
    container being stopped must not wait out the rest of the rounds."""
    recorded = startup.poll

    async def poll(targets=None):
        await recorded(targets)
        startup._stop.set()

    monkeypatch.setattr(startup, "poll", poll)
    asyncio.run(startup._startup_poll())
    assert len(startup.polls) == 1


def test_polling_turned_off_means_no_start_up_poll_either(startup, monkeypatch):
    """POLL_INTERVAL=0 is documented as "no polling", so it has to mean that.

    The heartbeat is not polling, so it carries on regardless: see the next
    test for why the loop cannot simply return.
    """
    startup.s.poll_interval = 0
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _d: real_sleep(0))
    monkeypatch.setattr(startup, "_beat", startup._stop.set)
    asyncio.run(startup._poll_loop())
    assert startup.polls == []


def test_the_heartbeat_outlives_polling_being_turned_off(startup, monkeypatch):
    """POLL_INTERVAL=0 used to end the loop, which would now end the heartbeat.

    A container whose healthcheck can never pass again is a red light nobody
    reads, and it would come on for a setting the README offers.
    """
    startup.s.poll_interval = 0
    beats = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _d: real_sleep(0))

    def beat():
        beats.append(1)
        if len(beats) == 3:
            startup._stop.set()

    monkeypatch.setattr(startup, "_beat", beat)
    asyncio.run(startup._poll_loop())
    assert len(beats) == 3


# --- the healthcheck heartbeat ------------------------------------------------

def test_the_heartbeat_creates_the_file_and_then_only_touches_it(bridge, tmp_path):
    """The second beat must not be a write.

    Touching an inode is what makes this cheap enough to do once a poll on a
    Raspberry Pi's SD card, and is the reason the heartbeat is not state.json
    saved more often.
    """
    path = tmp_path / "beat"
    bridge.s.health_file = str(path)
    bridge._beat()
    assert path.exists()

    before = path.stat().st_mtime
    os.utime(path, (before - 100, before - 100))
    bridge._beat()
    assert path.stat().st_mtime > before - 100
    assert path.stat().st_size == 0


def test_a_poll_that_completes_is_what_the_heartbeat_means(bridge, tmp_path):
    path = tmp_path / "beat"
    bridge.s.health_file = str(path)
    asyncio.run(bridge.poll())
    assert path.exists()


def test_an_empty_health_file_means_no_heartbeat_at_all(bridge, tmp_path, monkeypatch):
    """Opting out has to opt out of the writing too, not just the reading."""
    bridge.s.health_file = ""
    monkeypatch.setattr(main_mod.os, "utime", lambda *a, **kw: pytest.fail("touched"))
    bridge._beat()


def test_an_unwritable_heartbeat_is_reported_once_rather_than_once_a_poll(
        bridge, tmp_path, caplog):
    """It will not be writable on the next poll either, and the healthcheck
    already says so. A line a poll would bury the log it is written into."""
    bridge.s.health_file = str(tmp_path / "nope" / "beat")
    with caplog.at_level(logging.WARNING, logger="bridge"):
        for _ in range(5):
            bridge._beat()
    said = [r for r in caplog.records if "heartbeat" in r.message]
    assert len(said) == 1
    assert "HEALTH_FILE" in said[0].getMessage()


# --- reconnect backoff --------------------------------------------------------

def test_the_reconnect_ceiling_is_wired_to_its_own_setting(doc, settings, tmp_path, monkeypatch):
    """The MQTT backoff used to be capped by BLE_RETRY_MAX.

    Raising the Bluetooth retry ceiling because the proxy is far away also made
    the bridge wait that long before noticing the broker had come back, and
    nothing in either name suggested the two were connected.
    """
    seen = {}

    class Recorder:
        def __init__(self, *_a, **_kw):
            pass

        def reconnect_delay_set(self, min_delay, max_delay):
            seen["min"], seen["max"] = min_delay, max_delay

        def __getattr__(self, _name):
            return lambda *_a, **_kw: None

    monkeypatch.setattr(main_mod.mqtt, "Client", Recorder)
    settings.ble_retry_max = 300
    settings.mqtt_reconnect_max = 7
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    Bridge(settings, parse(doc, settings), state)
    assert seen["max"] == 7


# --- source address rotation --------------------------------------------------

class FakeLink:
    """Just enough ProxyLink to let the real send path run."""

    connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, int]] = []

    async def send(self, pdu: bytes, ptype: int = 0) -> None:
        self.sent.append((pdu, ptype))

    async def wait_up(self, _timeout: float) -> bool:
        return True


def rotating_bridge(doc, settings, tmp_path, *free):
    state = State(tmp_path / "state.json", seq_block=10, pick_src=FakePlanner(*free))
    state.open(0x7FF7)
    b = Bridge(settings, parse(doc, settings), state)
    b.mqtt = FakeMqtt()
    b.link = FakeLink()
    return b


def test_rotating_re_whitelists_the_new_address_on_the_proxy(doc, settings, tmp_path,
                                                             monkeypatch):
    """The proxy filter is the one thing in the bridge bound to the old address.

    Everything else follows by itself: a status message comes back to whatever
    src the request carried, and the Home Assistant ids are built from the
    *device* address. The proxy is different - it was told to forward only what
    is addressed to 0x7FF7, so after a rotation every reply would be dropped one
    hop short of us. Nothing raises. The lights would simply stop reporting
    their state, which is why re-sending the filter is part of rotating rather
    than a step someone has to remember.
    """
    whitelisted: list[list[int]] = []
    real = main_mod.mesh.add_filter_addresses
    monkeypatch.setattr(main_mod.mesh, "add_filter_addresses",
                        lambda addrs: (whitelisted.append(list(addrs)), real(addrs))[1])

    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    b.state.seq = SEQ_WARN_AT + 1
    assert asyncio.run(b._send_access(0x10, OP_LIGHTNESS_SET_UNACK + b"\x00\x00\x01")) is True

    assert b.src == 0x7FF8
    assert whitelisted == [[0x7FF8]], "the proxy was left forwarding to the old address"


def test_an_ordinary_send_does_not_touch_the_proxy_filter(doc, settings, tmp_path):
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    before = b.src
    assert asyncio.run(b._send_access(0x10, OP_LIGHTNESS_SET_UNACK + b"\x00\x00\x01")) is True
    assert b.src == before
    # Two repeats of one access message, and nothing of proxy type 2.
    assert [ptype for _pdu, ptype in b.link.sent] == [0, 0]


def test_the_address_the_bridge_sends_from_is_never_a_stale_copy(doc, settings, tmp_path):
    """A cached self.src would keep working right up to the first rotation."""
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    b.state.seq = SEQ_WARN_AT + 1
    b.state.rotate()
    assert b.src == b.state.src_addr == 0x7FF8


def test_rotation_waits_for_the_radio_rather_than_committing_without_it(doc, settings,
                                                                        tmp_path):
    """Rotating with the link down would leave the bridge unreachable.

    The move is persisted immediately, but telling the proxy to forward to the
    new address needs the radio. With the link down the bridge would be
    committed to an address the proxy filters out, and the command that
    triggered it thrown away by the ConnectionError on the way out. Deferring
    costs nothing: `needs_rotation` turns true with about a million sequence
    numbers still in hand.
    """
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    b.link.connected = False
    b.state.seq = SEQ_WARN_AT + 1

    assert asyncio.run(b._send_access(0x10, OP_LIGHTNESS_SET_UNACK + b"\x00\x00\x01")) is True
    assert b.src == 0x7FF7, "rotated onto an address the proxy does not forward to"
    assert b.state.needs_rotation, "the need to rotate was dropped, not deferred"
    # And the user's command still went out, on the old address.
    assert [ptype for _pdu, ptype in b.link.sent] == [0, 0]


def test_the_deferred_rotation_happens_on_the_next_connected_send(doc, settings, tmp_path):
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    b.link.connected = False
    b.state.seq = SEQ_WARN_AT + 1
    asyncio.run(b._send_access(0x10, OP_LIGHTNESS_SET_UNACK + b"\x00\x00\x01"))
    assert b.src == 0x7FF7

    b.link.connected = True
    asyncio.run(b._send_access(0x10, OP_LIGHTNESS_SET_UNACK + b"\x00\x00\x02"))
    assert b.src == 0x7FF8
    assert not b.state.needs_rotation


def test_a_filter_that_could_not_be_sent_does_not_eat_the_command(doc, settings, tmp_path,
                                                                   monkeypatch):
    """The link can go down between the check above and the filter send.

    The rotation is already on disk at that point, so there is nothing to undo
    and nothing is lost: `_setup_filter` is the on_connected hook and reads the
    address out of the state, so the next reconnect sets it. Letting the
    exception out would throw away the user's command, which is still unsent
    behind it, for a step that repairs itself.
    """
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    b.state.seq = SEQ_WARN_AT + 1

    async def no_radio():
        raise ConnectionError("mesh proxy link is down")

    monkeypatch.setattr(b, "_setup_filter", no_radio)
    assert asyncio.run(b._send_access(0x10, OP_LIGHTNESS_SET_UNACK + b"\x00\x00\x01")) is True
    assert b.src == 0x7FF8
    assert [ptype for _pdu, ptype in b.link.sent] == [0, 0]


# --- proxy filter confirmation -------------------------------------------------

def answer(bridge, **kw) -> None:
    """Hand the bridge the Filter Status its proxy would have sent back."""
    bridge._on_proxy_config(filter_status(bridge.keys, iv_index=bridge.state.iv_index, **kw))


def test_setting_the_filter_does_not_wait_for_the_proxy(doc, settings, tmp_path, monkeypatch):
    """Rotation runs _setup_filter with send_lock held and a command queued behind it.

    If the confirmation were awaited inline, a proxy that answers nothing
    would hold that lock for the whole timeout and the light would sit there,
    unlit, for a log line. The wait belongs in the background.
    """
    monkeypatch.setattr(main_mod, "_FILTER_CONFIRM_TIMEOUT", 30.0)
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)

    async def go():
        await asyncio.wait_for(b._setup_filter(), 2.0)
        assert b._filter_watch is not None and not b._filter_watch.done()
        b._filter_watch.cancel()

    asyncio.run(go())
    assert [ptype for _pdu, ptype in b.link.sent] == [2, 2]


def test_a_proxy_that_confirms_the_filter_is_believed(doc, settings, tmp_path, caplog):
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)

    async def go():
        with caplog.at_level(logging.INFO, logger="bridge"):
            await b._setup_filter()
            answer(b, list_size=1)
            await b._filter_watch

    asyncio.run(go())
    assert any("filter set and confirmed" in r.message for r in caplog.records)


def test_a_proxy_that_never_answers_is_warned_about(doc, settings, tmp_path, caplog,
                                                     monkeypatch):
    """The silence is not fatal - an old proxy still forwards - but it is the
    one explanation for a mesh where no light ever reports its state, so it
    must not be indistinguishable from a radio problem."""
    monkeypatch.setattr(main_mod, "_FILTER_CONFIRM_TIMEOUT", 0.05)
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)

    async def go():
        with caplog.at_level(logging.WARNING, logger="bridge"):
            await b._setup_filter()
            await b._filter_watch

    asyncio.run(go())
    assert any("did not confirm the filter" in r.message for r in caplog.records)
    assert b.link.connected, "an unconfirmed filter is not a reason to drop the link"


def test_a_blacklist_is_not_taken_as_the_whitelist_we_asked_for(doc, settings, tmp_path,
                                                                 monkeypatch):
    """A proxy that answered with the opposite filter would forward everything
    *except* us, which is the worst outcome of the three and the one most
    worth not reading as success."""
    monkeypatch.setattr(main_mod, "_FILTER_CONFIRM_TIMEOUT", 0.05)
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)

    async def go():
        await b._setup_filter()
        answer(b, filter_type=0x01, list_size=1)
        await b._filter_watch

    asyncio.run(go())
    assert not b._filter_ok.is_set()


def test_an_empty_whitelist_is_not_a_confirmation(doc, settings, tmp_path, monkeypatch):
    """Filter type accepted, our address not actually in the list."""
    monkeypatch.setattr(main_mod, "_FILTER_CONFIRM_TIMEOUT", 0.05)
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)

    async def go():
        await b._setup_filter()
        answer(b, list_size=0)
        await b._filter_watch

    asyncio.run(go())
    assert not b._filter_ok.is_set()


def test_a_reply_to_the_previous_filter_does_not_confirm_this_one(doc, settings, tmp_path):
    """Reconnects and rotations both re-send the filter, so a late answer to
    the one before must not be mistaken for an answer to this one."""
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)

    async def go():
        await b._setup_filter()
        answer(b, list_size=1)
        assert b._filter_ok.is_set()
        first = b._filter_watch
        await b._setup_filter()                 # a reconnect, say
        assert not b._filter_ok.is_set()
        assert first.cancelled() or first.done()
        b._filter_watch.cancel()

    asyncio.run(go())


def test_an_unreadable_proxy_message_is_ignored_rather_than_believed(doc, settings, tmp_path):
    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    b._on_proxy_config(b"\x00" * 21)
    b._on_proxy_config(b"")
    assert not b._filter_ok.is_set()


# --- startup ------------------------------------------------------------------

def test_the_broker_preflight_does_not_freeze_the_event_loop(settings, monkeypatch):
    """preflight() is synchronous paho with a 15 s CONNACK wait inside it.

    On the loop thread that is 15 s in which nothing else can be scheduled,
    including the signal handlers installed a few lines further down - so a
    container pointed at an unreachable broker ignored SIGTERM and took the
    full `docker stop` timeout to die.
    """
    from app import broker
    from app.config import ConfigError
    from app.main import _run

    ticks = []

    def slow_and_doomed(_s):
        time.sleep(0.2)
        raise ConfigError("broker unreachable")      # stops _run right here

    monkeypatch.setattr(broker, "preflight", slow_and_doomed)

    async def ticker():
        while True:
            await asyncio.sleep(0.01)
            ticks.append(1)

    async def main():
        beat = asyncio.create_task(ticker())
        with pytest.raises(ConfigError):
            await _run(settings, None, None, purge=False)
        beat.cancel()

    asyncio.run(main())
    assert len(ticks) > 5, "the loop was frozen for the whole preflight"


# --- shutdown -----------------------------------------------------------------

class TeardownMqtt(FakeMqtt):
    """FakeMqtt that also records the order of the client-level calls.

    The order is the whole point of two of the tests below, and `sent` only
    shows publishes, so disconnect() and loop_stop() have to be visible too.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def connect_async(self, *_a, **_kw) -> None:
        self.calls.append("connect_async")

    def loop_start(self) -> None:
        self.calls.append("loop_start")

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    def loop_stop(self) -> None:
        self.calls.append("loop_stop")

    def publish(self, topic, payload="", retain=False, **kw):
        self.calls.append(f"publish {topic}")
        return super().publish(topic, payload, retain, **kw)


class TeardownLink(FakeLink):
    """A link whose supervisor and close can both be scripted."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.events: list[str] = []
        self.supervise_body = None     # coroutine function, or None to idle
        self.close_body = None

    async def supervise(self) -> None:
        self.events.append("supervise started")
        try:
            if self.supervise_body is not None:
                await self.supervise_body()
            else:
                await asyncio.Event().wait()      # forever, like the real one
        except asyncio.CancelledError:
            self.events.append("supervise cancelled")
            raise

    async def disconnect(self) -> None:
        self.events.append("disconnect")
        if self.close_body is not None:
            await self.close_body()
        self.closed = True


def stopping_bridge(doc, settings, tmp_path, monkeypatch):
    """A bridge whose run() reaches the teardown as soon as it is started.

    The poller is replaced wholesale: the real one sleeps five seconds before
    it does anything, which is five seconds of nothing in every test here.
    """
    state = State(tmp_path / "state.json", seq_block=10)
    state.open(0x7FF7)
    b = Bridge(settings, parse(doc, settings), state)
    b.mqtt = TeardownMqtt()
    b.link = TeardownLink()

    async def idle_poller():
        await asyncio.Event().wait()

    monkeypatch.setattr(b, "_poll_loop", idle_poller)
    return b


def run_until_stopped(bridge, after=None):
    """Run the bridge and ask it to stop, optionally doing something first."""
    async def go():
        task = asyncio.create_task(bridge.run())
        await asyncio.sleep(0)          # let run() get as far as the queue
        if after is not None:
            await after()
        else:
            bridge.request_stop()
        await asyncio.wait_for(task, 10.0)

    asyncio.run(go())


def test_the_broker_is_told_we_are_leaving_before_its_thread_is_stopped(doc, settings,
                                                                        tmp_path, monkeypatch):
    """disconnect() queues a packet; loop_stop() stops the thread that writes it.

    Done the other way round the DISCONNECT never leaves the buffer, the socket
    just closes, and the broker publishes the last will - so every ordinary
    `docker restart` looked to Home Assistant like the bridge had crashed, and
    the retained `offline` this shutdown just published was overwritten by the
    will's own.
    """
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)
    run_until_stopped(b)
    assert b.mqtt.calls.index("disconnect") < b.mqtt.calls.index("loop_stop")


def test_a_clean_shutdown_leaves_offline_retained(doc, settings, tmp_path, monkeypatch):
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)
    run_until_stopped(b)
    last = [(p, r) for t, p, r in b.mqtt.sent if t == b.s.bridge_availability_topic][-1]
    assert last == ("offline", True)


def test_the_proxy_is_released_on_the_way_out(doc, settings, tmp_path, monkeypatch):
    """A mesh proxy stops advertising while anything is connected to it, so a
    link left open makes the one node the bridge can reach invisible to the
    scan the next container start runs."""
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)
    run_until_stopped(b)
    assert b.link.closed is True


def test_the_shutdown_waits_for_the_supervisor_to_unwind(doc, settings, tmp_path,
                                                          monkeypatch):
    """cancel() only schedules the CancelledError - it runs nothing.

    The supervisor can be inside connect() holding the link's lock, and
    link.disconnect() wants that same lock with no timeout of its own. Closing
    the link before the supervisor has unwound means queueing behind a scan
    with nothing to cut it short.
    """
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)

    async def holds_on():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Standing in for the teardown inside a cancelled connect().
            await asyncio.sleep(0.05)
            b.link.events.append("supervisor let go")
            raise

    b.link.supervise_body = holds_on
    run_until_stopped(b)
    assert b.link.events.index("supervisor let go") < b.link.events.index("disconnect")


def test_a_task_that_will_not_stop_does_not_hold_the_shutdown_open(doc, settings, tmp_path,
                                                                    monkeypatch):
    """The wait is a grace period, not a promise.

    Something that shields itself from cancellation would otherwise keep the
    container alive until SIGKILL, and the steps after the wait - releasing the
    proxy, the retained `offline` - are exactly the ones that would then never
    run.
    """
    monkeypatch.setattr(main_mod, "_SHUTDOWN_GRACE", 0.05)
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)

    async def ignores_cancellation():
        await asyncio.shield(asyncio.sleep(30))

    b.link.supervise_body = ignores_cancellation
    run_until_stopped(b)
    assert b.link.closed is True
    assert "disconnect" in b.mqtt.calls


def test_a_link_that_will_not_close_does_not_swallow_the_offline_message(doc, settings,
                                                                         tmp_path, monkeypatch):
    """The two teardown steps run together, not one behind the other.

    They have nothing to do with each other, and Docker allows ten seconds
    between SIGTERM and SIGKILL: in sequence their timeouts add up to more than
    that, so whichever went second was the one that quietly never happened.
    """
    monkeypatch.setattr(main_mod, "_LINK_CLOSE_LIMIT", 0.05)
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)

    async def never_closes():
        await asyncio.sleep(30)

    b.link.close_body = never_closes
    run_until_stopped(b)
    assert b.link.closed is False, "the close really did have to be abandoned"
    assert b.mqtt.by_topic()[b.s.bridge_availability_topic] == "offline"
    assert b.mqtt.calls.index("disconnect") < b.mqtt.calls.index("loop_stop")


def test_the_shutdown_disconnect_is_not_reported_as_a_reconnect(doc, settings, tmp_path,
                                                                 monkeypatch, caplog):
    """We are the ones hanging up, so "reconnecting with backoff" would be a
    promise nothing intends to keep - and the last line in the log of every
    ordinary restart, which is the worst place to put an untrue warning."""
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)
    b._stop.set()
    with caplog.at_level(logging.DEBUG, logger="bridge"):
        b._on_mqtt_disconnect(None, None, None, 0)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_ordinary_disconnect_is_still_warned_about(doc, settings, tmp_path, monkeypatch,
                                                       caplog):
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="bridge"):
        b._on_mqtt_disconnect(None, None, None, 7)
    assert any("reconnecting" in r.message for r in caplog.records)


# --- background tasks that stop when they should not --------------------------

def test_the_bridge_stops_when_the_supervisor_dies(doc, settings, tmp_path, monkeypatch,
                                                    caplog):
    """A dead supervisor means nothing will ever reconnect the radio again.

    The bridge carried on perfectly happily: it took commands off the broker,
    failed to send every one of them, and reported itself healthy throughout.
    The traceback went nowhere, because nothing ever looked at the task again.
    """
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)

    async def dies():
        raise RuntimeError("bluez went away")

    b.link.supervise_body = dies
    with caplog.at_level(logging.CRITICAL, logger="bridge"):
        run_until_stopped(b, after=lambda: asyncio.sleep(0.05))
    assert isinstance(b.fatal, RuntimeError)
    assert any("bluez went away" in r.message for r in caplog.records)


def test_the_bridge_stops_when_the_poller_returns_on_its_own(doc, settings, tmp_path,
                                                              monkeypatch, caplog):
    """No exception, no log line, and the lights simply stop being polled.

    Worth treating as fatal rather than ignoring: the poll loop is written to
    run forever, so reaching the end of it means an assumption somewhere is
    wrong, and the symptom - states that go stale and never recover - is one
    the user would blame on the mesh.
    """
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)

    async def just_returns():
        return

    monkeypatch.setattr(b, "_poll_loop", just_returns)
    with caplog.at_level(logging.CRITICAL, logger="bridge"):
        run_until_stopped(b, after=lambda: asyncio.sleep(0.05))
    assert b.fatal is not None
    assert any("ended on its own" in r.message for r in caplog.records)


def test_cancelling_those_tasks_at_shutdown_is_not_a_failure(doc, settings, tmp_path,
                                                              monkeypatch, caplog):
    """The shutdown cancels both on purpose, and the done callbacks fire while
    it is running. Reading that as the task dying would make every clean exit
    end in a critical log line and a non-zero status."""
    b = stopping_bridge(doc, settings, tmp_path, monkeypatch)
    with caplog.at_level(logging.CRITICAL, logger="bridge"):
        run_until_stopped(b)
    assert b.fatal is None
    assert not caplog.records


# --- the proxy filter and a rotation in the middle of it ----------------------

def test_the_filter_is_built_out_of_one_address_even_if_it_rotates(doc, settings, tmp_path,
                                                                    monkeypatch, caplog):
    """Two writes with waits between them, and self.src can move in between.

    Read fresh at each use, the address put in the whitelist and the address
    the bridge then says it whitelisted are read at different moments, so a
    rotation in the gap makes the confirmation name an address nobody asked
    the proxy to forward to. Nothing is dropped - the whitelist itself was
    still built from the earlier address - but "filter set and confirmed
    (whitelist 0x7FF8)" for a filter containing 0x7FF7 is a lie told in the
    one log line someone reads when no light reports its state.
    """
    addrs: list[list[int]] = []
    srcs: list[int] = []
    real_add = main_mod.mesh.add_filter_addresses
    real_build = main_mod.mesh.build_proxy_config_pdu
    monkeypatch.setattr(main_mod.mesh, "add_filter_addresses",
                        lambda a: (addrs.append(list(a)), real_add(a))[1])
    monkeypatch.setattr(main_mod.mesh, "build_proxy_config_pdu",
                        lambda keys, iv, seq, src, op, params:
                        (srcs.append(src), real_build(keys, iv, seq, src, op, params))[1])

    b = rotating_bridge(doc, settings, tmp_path, 0x7FF8)
    started_at = b.src

    async def rotate_under_it(_pdu, ptype=0):
        # The rotation lands between the two writes, which is the only window
        # there is: both are issued from inside this one call.
        b.state.src_addr = 0x7FF8

    monkeypatch.setattr(b.link, "send", rotate_under_it)

    async def go():
        with caplog.at_level(logging.INFO, logger="bridge"):
            await b._setup_filter()
            answer(b, list_size=1)
            await b._filter_watch

    asyncio.run(go())
    assert b.src == 0x7FF8, "the rotation really did happen in the gap"
    # All three agree on the address this run was started for.
    assert addrs == [[started_at]]
    # Sliced: the proxy's own answer is built through the same function.
    assert srcs[:2] == [started_at, started_at]
    assert any(f"whitelist 0x{started_at:04X}" in r.message for r in caplog.records)
