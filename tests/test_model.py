"""Parsing a .connect document into the entities exposed over MQTT."""
from __future__ import annotations

import pytest

from app.config import ConfigError, Settings
from app.model import describe, parse, slug


@pytest.fixture
def model(doc, settings):
    return parse(doc, settings)


def test_lights_are_only_the_drivable_devices(model):
    assert sorted(e.name for e in model.singles) == ["Light 1", "Light 2", "Light 3", "Light 4"]
    # The box itself, the remote and the gateway must not become entities.
    assert not any("Box" in e.name or "Remote" in e.name for e in model.lights.values())


def test_light_addresses_and_unique_ids(model):
    by_name = {e.name: e for e in model.singles}
    assert by_name["Light 1"].dst == 0x10
    assert by_name["Light 1"].uid == "hafele_0010"
    assert by_name["Light 4"].uid == "hafele_0020"
    assert len({e.uid for e in model.lights.values()}) == len(model.lights)


def test_unsupported_lamp_type_warns_but_does_not_crash(model):
    assert any("Colour Light" in w and "cannot drive" in w for w in model.warnings)
    assert "hafele_0021" not in model.lights


def test_groups_get_their_members(model):
    by_name = {e.name: e for e in model.groups}
    assert set(by_name) == {"All lights", "Room", "Shelf"}
    assert by_name["Shelf"].dst == 0xC00B
    assert sorted(m.name for m in by_name["Shelf"].members) == ["Light 1", "Light 2"]
    assert len(by_name["All lights"].members) == 4
    assert by_name["All lights"].group_type == "all"


def test_empty_group_is_skipped(model):
    assert "hafele_group_c010" not in model.lights


def test_all_group_name_is_configurable(doc, monkeypatch):
    monkeypatch.setenv("ALL_GROUP_NAME", "Everything")
    m = parse(doc, Settings.from_env())
    assert any(e.name == "Everything" for e in m.groups)


def test_all_destination_comes_from_the_all_group(model):
    assert model.all_dst == 0xC002


def test_scenes_are_deduplicated_by_number(model):
    assert [(s.number, s.name) for s in model.scenes] == [
        (1, "All on"), (2, "All off"), (3, "Dimmed")]
    assert model.scenes[2].kind == "preset"
    assert model.scenes[0].uid == "hafele_scene_1"


def test_nodes_carry_home_assistant_device_metadata(model):
    by_name = {n.name: n for n in model.nodes}
    assert set(by_name) == {"Box 1", "Box 2", "Remote 1"}
    assert by_name["Box 1"].sw_version == "6.0.5"
    assert by_name["Box 1"].model == "com.haefele.24v.blebox.v2"
    assert by_name["Box 1"].uid == "hafele_node_0010"


def test_remote_is_named_despite_its_versioned_device_type(model):
    # tos_node.type is "com.haefele.remote" but the device is ".remote.v2".
    assert any(n.name == "Remote 1" for n in model.nodes)


def test_a_node_with_no_nameable_device_falls_back_to_its_address(doc, settings):
    for dev in doc["nodes"][2]["tos_devices"]:
        dev["name"] = ""
    m = parse(doc, settings)
    assert any(n.name == "Node 0x0030" for n in m.nodes)


def test_lights_point_at_their_box(model):
    by_name = {e.name: e for e in model.singles}
    assert by_name["Light 1"].node.name == "Box 1"
    assert by_name["Light 4"].node.name == "Box 2"


def test_planned_entity_ids(model):
    assert {e.entity_id for e in model.singles} == {
        "light.light_1", "light.light_2", "light.light_3", "light.light_4"}


def test_duplicate_mesh_address_is_a_hard_error(doc, settings):
    doc["nodes"][1]["tos_devices"][1]["meshAddress"] = 0x10
    with pytest.raises(ConfigError, match="share mesh address"):
        parse(doc, settings)


def test_network_without_lights_is_a_hard_error(doc, settings):
    for node in doc["nodes"]:
        node["tos_devices"] = [d for d in node["tos_devices"]
                               if d["type"] != "com.haefele.led.white"]
    with pytest.raises(ConfigError, match="no controllable lights"):
        parse(doc, settings)


def test_more_than_one_key_is_called_out(doc, settings):
    """Taking [0] silently would show up as lights that ignore every command.

    A node bound to the second app key decrypts nothing we send, and mesh gives
    no feedback about it, so the only clue would be this warning.
    """
    assert not [w for w in parse(doc, settings).warnings if "key" in w]
    doc["appKeys"].append({"index": 1, "key": "0" * 32, "name": "Second"})
    warnings = [w for w in parse(doc, settings).warnings if "app key" in w]
    assert len(warnings) == 1
    assert "appKeys[0]" in warnings[0]


def test_slug_strips_diacritics_and_punctuation():
    # The Connect app happily stores accented names and parenthesised suffixes,
    # neither of which may reach an entity id.
    assert slug("Küchenschrank oben") == "kuchenschrank_oben"
    assert slug("Shelf 2 (Control)") == "shelf_2_control"
    assert slug("  --Mixed__Case--  ") == "mixed_case"


def test_describe_lists_everything_without_touching_the_network(model, settings):
    text = describe(model, settings, 0x7FF7, "provisioner 0x7FF6 + 1", "sample.connect")
    for expected in ("LIGHTS (4)", "GROUPS (3)", "SCENES (3)", "NODES (3)",
                     "0x7FF7", "light.light_1", "hafele/light/hafele_0010/set",
                     "hafele/status", "WARNINGS (1)"):
        assert expected in text


@pytest.mark.parametrize("number", ["three", None, [3], {"n": 3}, "Infinity"])
def test_an_unreadable_scene_number_is_one_line_not_a_traceback(doc, settings, number):
    """int() on whatever the export held crashed the container on start-up.

    Everything else in this file turns bad input into a single ConfigError that
    names the field; a scene number had no reason to be the exception.
    """
    doc["tos_scenes"][0]["sceneNumber"] = number
    with pytest.raises(ConfigError, match="sceneNumber"):
        parse(doc, settings)


@pytest.mark.parametrize("number", [0, -1, 65536, 1000000])
def test_a_scene_number_the_mesh_cannot_carry_is_skipped_with_a_warning(doc, settings, number):
    """A scene recall puts the number in two bytes.

    So an out-of-range one did not fail here, it failed with an OverflowError
    at the instant somebody pressed the button - in the MQTT handler, hours
    later, with nothing on screen tying it back to the export.
    """
    doc["tos_scenes"][0]["sceneNumber"] = number
    m = parse(doc, settings)
    assert number not in [s.number for s in m.scenes]
    assert any("not a mesh scene number" in w for w in m.warnings)


@pytest.mark.parametrize("number", [1, 65535])
def test_the_scene_numbers_the_mesh_can_carry_survive(doc, settings, number):
    doc["tos_scenes"][0]["sceneNumber"] = number
    assert number in [s.number for s in parse(doc, settings).scenes]
