#!/usr/bin/env python3
"""Generate tests/fixtures/sample.connect.

The fixture is written from scratch here rather than derived from anyone's real
export: the keys are obviously fake, the names are neutral, the MAC bytes are
made up, and the UUIDs are constant. Nothing in tests/ may ever contain a real mesh key - a net key plus an
app key is full control of someone's lights.

Run after changing the expected shape:  python tests/make_fixture.py
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "sample.connect"

# Obviously-fake 128-bit keys. Never replace these with anything real.
NET_KEY = "000102030405060708090a0b0c0d0e0f"
APP_KEY = "0f0e0d0c0b0a09080706050403020100"

G_ALL = "00000000-0000-4000-8000-00000000a11a"
G_ROOM = "00000000-0000-4000-8000-00000000f00d"
G_SUB = "00000000-0000-4000-8000-00000000beef"
S_ON = "00000000-0000-4000-8000-000000005ce1"
S_OFF = "00000000-0000-4000-8000-000000005ce2"
S_DIM = "00000000-0000-4000-8000-000000005ce3"


def light(uid: str, name: str, addr: int, groups: list[str], room: str | None = None) -> dict:
    return {
        "id": uid,
        "type": "com.haefele.led.white",
        "icon": "devices/bulb",
        "name": name,
        "meshAddress": addr,
        "value": [0],
        "lastValue": [65535],
        "ports": [0],
        "scheduleSettings": [],
        "sceneSettings": [{"id": S_ON, "sceneValue": [65535], "status": "completed"}],
        "groupSettings": [{"id": g, "status": "completed"} for g in groups],
        # Real exports point this at a tos_group whose type is "room"; that
        # group is the only place the room's name exists.
        "roomId": room,
    }


def node(unicast: str, elements: int, ntype: str, devices: list[dict], fw: str = "6.0.5",
         mac: str = "") -> dict:
    return {
        "UUID": f"00000000-0000-4000-8000-0000{unicast.lower()}0000",
        "unicastAddress": unicast,
        "security": "secure",
        "deviceKey": "ffffffffffffffffffffffffffffffff",
        "cid": "07E9",
        "vid": "0001",
        "pid": "0001",
        # Real exports leave element names empty; keep that, so the tests do not
        # lean on a fallback that never fires in the field.
        "elements": [{"index": i, "location": "0000", "name": "", "models": []}
                     for i in range(elements)],
        "configComplete": True,
        "netKeys": [{"index": 0, "updated": False}],
        "appKeys": [{"index": 0, "updated": False}],
        "excluded": False,
        "tos_node": {
            "elementCount": elements,
            "type": ntype,
            "macBytes": mac,
            "firmwareVersion": fw,
            "bootloaderVersion": "1.0.0",
            "hardwareVersion": "1.0.0",
            "networkTransmitCount": 2,
            "networkTransmitInterval": 20,
            "relayState": "enabled",
            "relayCount": 2,
            "relayInterval": 20,
        },
        "tos_devices": devices,
    }


def build() -> dict:
    # Mirrors the shape of a real export: the box's own device entry shares its
    # mesh address with the first light on it, and the remote's device type is a
    # versioned variant of its node type.
    # Box 1's three lights are all in the same room, so the box has one
    # unambiguous suggested_area. Box 2's single light is in no room at all,
    # which is the other half of that logic and just as common in the field.
    box1 = node("0010", 4, "com.haefele.24v.blebox.v2", [
        {"id": "00000000-0000-4000-8000-000000000b01",
         "type": "com.haefele.24v.blebox.v2", "icon": "devices/blebox6", "name": "Box 1",
         "meshAddress": 0x10, "value": [0], "lastValue": [0]},
        light("00000000-0000-4000-8000-000000000101", "Light 1", 0x10,
              [G_ALL, G_ROOM, G_SUB], room=G_ROOM),
        light("00000000-0000-4000-8000-000000000102", "Light 2", 0x11,
              [G_ALL, G_ROOM, G_SUB], room=G_ROOM),
        light("00000000-0000-4000-8000-000000000103", "Light 3", 0x12,
              [G_ALL, G_ROOM], room=G_ROOM),
    ], mac="00AA01")
    box2 = node("0020", 2, "com.haefele.24v.blebox.v2", [
        {"id": "00000000-0000-4000-8000-000000000b02",
         "type": "com.haefele.24v.blebox.v2", "icon": "devices/blebox6", "name": "Box 2",
         "meshAddress": 0x20, "value": [0], "lastValue": [0]},
        light("00000000-0000-4000-8000-000000000104", "Light 4", 0x20, [G_ALL]),
        # A colour lamp we cannot drive yet: must produce a warning, not a crash.
        {"id": "00000000-0000-4000-8000-000000000105",
         "type": "com.haefele.led.rgb", "icon": "devices/bulb", "name": "Colour Light",
         "meshAddress": 0x21, "value": [0], "lastValue": [0], "groupSettings": []},
    ], mac="00AA02")
    remote = node("0030", 8, "com.haefele.remote", [
        {"id": "00000000-0000-4000-8000-000000000901",
         "type": "com.haefele.remote.v2", "icon": "devices/bleRemote2", "name": "Remote 1",
         "meshAddress": 0x30, "modules": []},
    ], mac="00AA03")

    return {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "id": "http://www.bluetooth.com/specifications/assigned-numbers/mesh-profile/cdb-schema.json#",
        "version": "1.0.1",
        "meshName": "Test Network",
        "meshUUID": "00000000-0000-4000-8000-00000000e511",
        "timestamp": "2026-01-01T00:00:00Z",
        "partial": False,
        "tos_network": {
            "version": 3,
            "appVersion": "2.4.4",
            "platform": "test",
            "provisionerMeshAddress": "7FF6",
            "nextUnicastAddress": 0x31,
            "defaultNetworkTransmissionCount": 2,
            "defaultRelayCount": 2,
        },
        "tos_schedules": [],
        "tos_scenes": [
            {"id": S_ON, "kind": "scene", "sceneNumber": 1, "name": "All on",
             "icon": "ui/sun", "color": "#ffffff", "groupId": None},
            {"id": S_OFF, "kind": "scene", "sceneNumber": 2, "name": "All off",
             "icon": "scenes/standby", "color": "#000000", "groupId": None},
            # An icon the mapping has never heard of: must produce no icon at
            # all rather than a guess.
            {"id": S_DIM, "kind": "preset", "sceneNumber": 3, "name": "Dimmed",
             "icon": "ui/somethingNew", "color": "#888888", "groupId": None},
            # Duplicate scene number: the parser must keep only the first.
            {"id": "00000000-0000-4000-8000-000000005ce4", "kind": "preset",
             "sceneNumber": 3, "name": "Dimmed copy", "icon": "ui/somethingNew",
             "color": "#888888", "groupId": None},
        ],
        "tos_groups": [
            {"id": G_ALL, "type": "all", "name": "TOS_Internal_All", "icon": "ui/grid",
             "multicastAddress": 0xC002, "multicastAddress2": 0xC003,
             "multicastAddress3": 0xC004, "multicastAddress4": 0xC005},
            # type "room" is the only place a room name exists; roomId on a
            # device points here.
            {"id": G_ROOM, "type": "room", "name": "Room", "icon": "rooms/kitchen",
             "multicastAddress": 0xC006, "multicastAddress2": 0xC007,
             "multicastAddress3": 0xC008, "multicastAddress4": 0xC009},
            {"id": G_SUB, "type": "group", "name": "Shelf", "icon": "ui/grid",
             "multicastAddress": 0xC00B, "multicastAddress2": 0xC00C,
             "multicastAddress3": 0xC00D, "multicastAddress4": 0xC00E},
            # A group nobody is a member of: must be skipped silently.
            {"id": "00000000-0000-4000-8000-00000000dead", "type": "group",
             "name": "Empty", "icon": "ui/grid", "multicastAddress": 0xC010,
             "multicastAddress2": 0xC011, "multicastAddress3": 0xC012,
             "multicastAddress4": 0xC013},
        ],
        "tos_presets": {"color_presets": [], "temperature_presets": []},
        "tos_uiSettings": {"scenes": {"favorites": []}, "groups": {"favorites": []},
                           "devices": {"favorites": []}, "rooms": {"favorites": []}},
        "nodes": [box1, box2, remote],
        "netKeys": [{"name": "NetKey", "index": 0, "key": NET_KEY,
                     "minSecurity": "secure", "phase": 0,
                     "timestamp": "2026-01-01T00:00:00Z"}],
        "appKeys": [{"name": "App Key", "index": 0, "boundNetKey": 0, "key": APP_KEY}],
        "provisioners": [{
            "UUID": "00000000-0000-4000-8000-00000000d00d",
            "provisionerName": "Provisioner",
            "allocatedUnicastRange": [{"lowAddress": "0001", "highAddress": "1000"}],
            "allocatedGroupRange": [{"lowAddress": "C000", "highAddress": "CFFF"}],
            "allocatedSceneRange": [{"firstScene": "0001", "lastScene": "0fff"}],
        }],
        "groups": [
            {"name": "TOS_Internal_All (Control)", "address": "C002", "parentAddress": "0000"},
            {"name": "Room (Control)", "address": "C006", "parentAddress": "0000"},
            {"name": "Shelf (Control)", "address": "C00B", "parentAddress": "0000"},
        ],
        "scenes": [
            {"name": "All on", "number": "0001", "addresses": ["0010", "0011", "0012", "0020"]},
            {"name": "All off", "number": "0002", "addresses": ["0010", "0011", "0012", "0020"]},
            {"name": "Dimmed", "number": "0003", "addresses": ["0010", "0011", "0012", "0020"]},
        ],
        "networkExclusions": [],
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT}")
