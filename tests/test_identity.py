"""What keeps a Home Assistant entity the same entity when the mesh moves.

A `unique_id` is the entity. Re-provisioning a box or rebuilding a group hands
out new mesh addresses, and the address is what a `unique_id` is minted from,
so without the map tested here the lights come back as a second set of entities
and every automation that named the first set is pointing at nothing.
"""
from __future__ import annotations

import json

import pytest

from app.config import ConfigError
from app.model import anchor, apply_identity, parse
from app.state import State, read_identity

# Anchors in tests/fixtures/sample.connect. The values are deliberately
# readable rather than fetched at runtime: a test that derives the id from the
# same code it is checking cannot notice the id changing.
LIGHT1 = "00000000-0000-4000-8000-000000000101"
LIGHT4 = "00000000-0000-4000-8000-000000000104"
SHELF = "00000000-0000-4000-8000-00000000beef"
SCENE_ALL_ON = "00000000-0000-4000-8000-000000005ce1"
BOX1 = "00000000-0000-4000-8000-000000100000"


def readdress(doc: dict, guid: str, new: int) -> None:
    """Move whatever `guid` names to a different mesh address."""
    for node in doc["nodes"]:
        for dev in node.get("tos_devices", []):
            if dev.get("id") == guid:
                dev["meshAddress"] = new
                return
    for g in doc.get("tos_groups", []):
        if g["id"] == guid:
            g["multicastAddress"] = new
            return
    raise AssertionError(f"{guid} is not in the fixture")


# --- the point of the whole feature ------------------------------------------

def test_a_light_that_moves_address_keeps_its_entity(doc, settings):
    """Re-provisioning is the normal case, not the exotic one.

    A box taken off the wall and added back gets the next free address, which
    is almost never the one it had. Before the identity map that silently
    created `light.plaukts_apaksa_2` and left the original orphaned.
    """
    first = parse(doc, settings)
    mapping = apply_identity(first, {})
    assert "hafele_0010" in first.lights

    readdress(doc, LIGHT1, 0x0099)
    moved = parse(doc, settings)
    apply_identity(moved, mapping)

    assert "hafele_0010" in moved.lights, "the entity should have survived the move"
    assert moved.lights["hafele_0010"].dst == 0x0099, "pointing at the new address"
    assert "hafele_0099" not in moved.lights
    assert moved.healed == {"hafele_0010": "hafele_0099"}


def test_a_group_that_moves_address_keeps_its_entity(doc, settings):
    mapping = apply_identity(parse(doc, settings), {})
    readdress(doc, SHELF, 0xC0FF)
    moved = parse(doc, settings)
    apply_identity(moved, mapping)
    assert moved.lights["hafele_group_c00b"].dst == 0xC0FF


def test_a_node_that_moves_address_keeps_its_device(doc, settings):
    """The device carries the area and the name; losing it empties the page."""
    mapping = apply_identity(parse(doc, settings), {})
    doc["nodes"][0]["unicastAddress"] = "0088"
    moved = parse(doc, settings)
    apply_identity(moved, mapping)
    box = next(n for n in moved.nodes if n.unicast == 0x0088)
    assert box.uid == "hafele_node_0010"


def test_a_scene_keeps_its_entity_when_renumbered(doc, settings):
    mapping = apply_identity(parse(doc, settings), {})
    doc["tos_scenes"][0]["sceneNumber"] = 42
    moved = parse(doc, settings)
    apply_identity(moved, mapping)
    sc = next(s for s in moved.scenes if s.number == 42)
    assert sc.uid == "hafele_scene_1"


def test_nothing_is_healed_on_a_first_run(doc, settings):
    """With no memory the ids come straight off the addresses, as they always did."""
    m = parse(doc, settings)
    apply_identity(m, {})
    assert m.healed == {}
    assert "hafele_0010" in m.lights and "hafele_group_c002" in m.lights


def test_a_new_device_gets_a_new_entity(doc, settings):
    """Only things the bridge has seen before are restored; a genuinely new
    light has no anchor in the map and must mint its own id."""
    mapping = apply_identity(parse(doc, settings), {})
    doc["nodes"][1]["tos_devices"].append({
        "id": "00000000-0000-4000-8000-0000000001ff",
        "type": "com.haefele.led.white", "name": "New Light", "meshAddress": 0x0077,
        "groupSettings": [],
    })
    grown = parse(doc, settings)
    apply_identity(grown, mapping)
    assert "hafele_0077" in grown.lights
    assert grown.healed == {}


# --- the map itself ----------------------------------------------------------

def test_the_map_is_keyed_by_anchor_not_by_address(doc, settings):
    mapping = apply_identity(parse(doc, settings), {})
    assert mapping[anchor("light", LIGHT1)] == "hafele_0010"
    assert mapping[anchor("group", SHELF)] == "hafele_group_c00b"
    assert mapping[anchor("scene", SCENE_ALL_ON)] == "hafele_scene_1"
    assert mapping[anchor("node", BOX1)] == "hafele_node_0010"


def test_anchors_ignore_the_case_a_guid_is_written_in(doc, settings):
    """Exports have been seen with both; the same device must not get two keys."""
    assert anchor("light", LIGHT1.lower()) == anchor("light", LIGHT1.upper())
    assert anchor("light", f"  {LIGHT1}  ") == anchor("light", LIGHT1)


def test_an_anchor_missing_from_the_export_is_still_remembered(doc, settings):
    """A box unplugged for a fortnight is exactly when the map is the only record.

    Dropping its entry on the first export without it would mean the entity is
    lost at the moment it is most needed, so stale anchors are kept forever.
    """
    mapping = apply_identity(parse(doc, settings), {})
    doc["nodes"][1]["tos_devices"] = [d for d in doc["nodes"][1]["tos_devices"]
                                      if d.get("name") != "Light 4"]
    smaller = parse(doc, settings)
    after = apply_identity(smaller, mapping)
    assert after[anchor("light", LIGHT4)] == "hafele_0020"


def test_two_devices_with_one_guid_is_refused(doc, settings):
    """Silently picking one would attach a live entity to the wrong light."""
    doc["nodes"][1]["tos_devices"][1]["id"] = LIGHT1
    with pytest.raises(ConfigError, match="share the id"):
        parse(doc, settings)


def test_two_groups_with_one_guid_is_refused(doc, settings):
    doc["tos_groups"][2]["id"] = doc["tos_groups"][1]["id"]
    with pytest.raises(ConfigError, match="share the id"):
        parse(doc, settings)


def test_a_newcomer_cannot_evict_the_entity_that_moved_off_its_address(doc, settings):
    """The hard case: Light 1 moves away and a new light takes 0x0010.

    Minting ids address-first would hand `hafele_0010` to the newcomer and the
    entity nine automations refer to would start switching a different lamp.
    Restoring remembered anchors before anyone mints is what prevents it.
    """
    mapping = apply_identity(parse(doc, settings), {})
    readdress(doc, LIGHT1, 0x0099)
    doc["nodes"][1]["tos_devices"].append({
        "id": "00000000-0000-4000-8000-0000000001ff",
        "type": "com.haefele.led.white", "name": "Newcomer", "meshAddress": 0x0010,
        "groupSettings": [],
    })
    m = parse(doc, settings)
    apply_identity(m, mapping)

    assert m.lights["hafele_0010"].name == "Light 1"
    assert m.lights["hafele_0010"].dst == 0x0099
    newcomer = next(e for e in m.lights.values() if e.name == "Newcomer")
    assert newcomer.uid != "hafele_0010"
    assert newcomer.uid.startswith("hafele_0010_")
    assert any("belongs to an entity" in w for w in m.warnings)


def test_ids_stay_unique_even_in_the_collision_case(doc, settings):
    mapping = apply_identity(parse(doc, settings), {})
    readdress(doc, LIGHT1, 0x0099)
    doc["nodes"][1]["tos_devices"].append({
        "id": "00000000-0000-4000-8000-0000000001ff",
        "type": "com.haefele.led.white", "name": "Newcomer", "meshAddress": 0x0010,
        "groupSettings": [],
    })
    m = parse(doc, settings)
    apply_identity(m, mapping)
    all_uids = [e.uid for e in m.lights.values()]
    assert len(set(all_uids)) == len(all_uids)
    assert list(m.lights) == all_uids, "the dict must be re-keyed to the final ids"


def test_lights_scenes_and_nodes_do_not_compete_for_the_same_id(doc, settings):
    """They are three different MQTT components, so a name can repeat."""
    m = parse(doc, settings)
    apply_identity(m, {"light:" + LIGHT1.upper(): "hafele_x",
                       "scene:" + SCENE_ALL_ON.upper(): "hafele_x",
                       "node:" + BOX1.upper(): "hafele_x"})
    assert "hafele_x" in m.lights
    assert any(sc.uid == "hafele_x" for sc in m.scenes)
    assert any(n.uid == "hafele_x" for n in m.nodes)


# --- persistence -------------------------------------------------------------

def test_the_map_survives_a_restart(doc, settings, tmp_path):
    path = tmp_path / "state.json"
    first = State(path, seq_block=10)
    first.open(0x7FF7)
    first.set_identity(apply_identity(parse(doc, settings), first.identity))

    second = State(path, seq_block=10)
    second.open(0x7FF7)
    assert second.identity[anchor("light", LIGHT1)] == "hafele_0010"

    readdress(doc, LIGHT1, 0x0099)
    moved = parse(doc, settings)
    apply_identity(moved, second.identity)
    assert moved.lights["hafele_0010"].dst == 0x0099


def test_read_identity_does_not_create_or_touch_the_state_file(tmp_path):
    """--validate has to be safe to run against a bridge that is up."""
    path = tmp_path / "state.json"
    assert read_identity(path) == {}
    assert not path.exists()


def test_read_identity_merges_the_backup_under_the_primary(tmp_path):
    """_load() does, so a report that did not would name entities the running
    bridge does not."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"identity": {"entity:A": "new", "entity:B": "b"}}))
    (tmp_path / "state.json.bak").write_text(
        json.dumps({"identity": {"entity:A": "old", "entity:C": "c"}}))
    assert read_identity(path) == {"entity:A": "new", "entity:B": "b", "entity:C": "c"}


def test_a_state_file_with_a_broken_identity_block_still_opens(tmp_path):
    """The seq counter in this file is what stops replayed commands; losing it
    over a bad name map would be a far worse trade."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "identity": ["not", "a", "map"]}))
    s = State(path, seq_block=10)
    s.open(0x7FF7)
    assert s.identity == {}
