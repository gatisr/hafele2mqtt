"""The optional entities.yaml: what it changes, and what it refuses to pretend.

Every test here starts from a model that already works without the file, because
that is the contract: the file is opt-in, and with none present nothing in this
module touches a single byte of what goes on the wire.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app import overrides
from app.config import ConfigError, Settings
from app.main import Bridge, main
from app.model import apply_identity, parse
from app.state import State

LIGHT1 = "00000000-0000-4000-8000-000000000101"


@pytest.fixture
def model(doc, settings):
    return parse(doc, settings)


def write(tmp_path, text: str):
    path = tmp_path / "entities.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def apply(model, settings, tmp_path, text: str) -> int:
    return overrides.apply(model, settings, write(tmp_path, text))


# --- opt-in ------------------------------------------------------------------

def test_no_file_means_no_change(model, settings):
    before = {uid: e.name for uid, e in model.lights.items()}
    assert overrides.apply(model, settings, None) == 0
    assert {uid: e.name for uid, e in model.lights.items()} == before


def test_an_empty_file_is_a_fine_way_to_say_none(model, settings, tmp_path):
    assert apply(model, settings, tmp_path, "") == 0


def test_locate_finds_entities_yaml_beside_the_mesh_file(tmp_path, monkeypatch):
    (tmp_path / "entities.yaml").write_text("lights: {}")
    monkeypatch.setenv("MESH_DIR", str(tmp_path))
    assert overrides.locate(Settings.from_env()) == tmp_path / "entities.yaml"


def test_locate_returns_nothing_when_there_is_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_DIR", str(tmp_path))
    assert overrides.locate(Settings.from_env()) is None


def test_a_named_entities_file_that_is_not_there_is_an_error(tmp_path, monkeypatch):
    """Starting anyway would publish every entity under the names the user was
    trying to replace, and the only clue would be a line in the log."""
    monkeypatch.setenv("ENTITIES_FILE", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="does not exist"):
        overrides.locate(Settings.from_env())


# --- naming ------------------------------------------------------------------

def test_a_pinned_name_survives_a_rename_in_the_connect_app(model, settings, tmp_path):
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    name: Kitchen counter\n")
    assert model.lights["hafele_0010"].name == "Kitchen counter"


def test_entity_id_is_asked_for_without_the_domain(model, settings, tmp_path):
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    entity_id: light.desk\n")
    assert model.lights["hafele_0010"].entity_id == "light.desk"


def test_entity_id_may_be_written_bare(model, settings, tmp_path):
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    entity_id: desk\n")
    assert model.lights["hafele_0010"].entity_id == "light.desk"


def test_entity_id_is_cleaned_up_rather_than_rejected(model, settings, tmp_path):
    """Home Assistant only accepts lower-case, digits and underscores, and a
    hard error over a capital letter helps nobody."""
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    entity_id: Study Desk\n")
    assert model.lights["hafele_0010"].entity_id == "light.study_desk"


def test_entity_id_in_the_wrong_domain_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="wrong domain"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n    entity_id: switch.desk\n")


def test_an_entity_id_with_nothing_usable_in_it_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="nothing usable"):
        apply(model, settings, tmp_path, 'lights:\n  Light 1:\n    entity_id: "!!!"\n')


def test_a_scene_name_override_replaces_the_prefix_too(model, settings, tmp_path):
    """Without this the user asks for "Good morning" and gets "Scene Good morning"."""
    apply(model, settings, tmp_path, "scenes:\n  1:\n    name: Good morning\n")
    sc = next(s for s in model.scenes if s.number == 1)
    assert sc.full_name(settings.scene_name_prefix) == "Good morning"
    assert sc.entity_id(settings.scene_name_prefix) == "scene.good_morning"


# --- icons -------------------------------------------------------------------

def test_an_icon_is_taken_as_written(model, settings, tmp_path):
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    icon: mdi:lamp\n")
    assert model.lights["hafele_0010"].icon == "mdi:lamp"


def test_an_icon_without_a_prefix_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="no prefix"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n    icon: lamp\n")


# --- areas -------------------------------------------------------------------

def test_an_area_on_a_light_moves_its_box(model, settings, tmp_path):
    """Home Assistant takes the area from the device, so the only way an `area:`
    on a light can mean anything is through the box it sits on."""
    text = "lights:\n" + "".join(
        f"  {n}:\n    area: Study\n" for n in ("Light 1", "Light 2", "Light 3"))
    apply(model, settings, tmp_path, text)
    box1 = next(n for n in model.nodes if n.unicast == 0x0010)
    assert box1.area == "Study"


def test_a_box_with_lights_in_two_areas_suggests_neither(model, settings, tmp_path):
    """Guessing one of them would put two thirds of the box in the wrong room."""
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    area: Study\n")
    box1 = next(n for n in model.nodes if n.unicast == 0x0010)
    assert box1.area == ""


def test_an_area_on_a_group_is_ignored_and_says_so(model, settings, tmp_path):
    """Groups share one bridge device, so honouring it would drag every sibling
    into the same room. Saying nothing would look like it had worked."""
    apply(model, settings, tmp_path, "lights:\n  Shelf:\n    area: Study\n")
    assert any("is ignored" in w and "Shelf" in w for w in model.warnings)


def test_an_area_on_a_scene_is_ignored_and_says_so(model, settings, tmp_path):
    apply(model, settings, tmp_path, "scenes:\n  1:\n    area: Study\n")
    assert any("is ignored" in w for w in model.warnings)


# --- identity ----------------------------------------------------------------

def test_a_pinned_unique_id_is_what_gets_published(model, settings, tmp_path):
    """For someone migrating off another bridge, whose entities already exist."""
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    unique_id: old_bridge_lamp\n")
    apply_identity(model, {})
    assert "old_bridge_lamp" in model.lights
    assert "hafele_0010" not in model.lights


def test_a_pinned_unique_id_beats_the_remembered_one(model, doc, settings, tmp_path):
    """The map is the bridge's memory; the file is the user's instruction."""
    mapping = apply_identity(parse(doc, settings), {})
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    unique_id: old_bridge_lamp\n")
    after = apply_identity(model, mapping)
    assert "old_bridge_lamp" in model.lights
    assert after[f"light:{LIGHT1.upper()}"] == "old_bridge_lamp"


def test_two_entries_pinning_the_same_unique_id_are_refused(model, settings, tmp_path):
    apply(model, settings, tmp_path,
          "lights:\n  Light 1:\n    unique_id: same\n  Light 2:\n    unique_id: same\n")
    with pytest.raises(ConfigError, match="pin unique_id"):
        apply_identity(model, {})


def test_a_unique_id_that_cannot_go_in_a_topic_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="other than letters"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n    unique_id: has/a/slash\n")


# --- hiding ------------------------------------------------------------------

def test_a_disabled_light_is_still_parsed_but_marked(model, settings, tmp_path):
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    enabled: false\n")
    assert model.lights["hafele_0010"].enabled is False
    assert model.lights["hafele_0011"].enabled is True


def test_enabled_has_to_be_a_boolean(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="true or false"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n    enabled: maybe\n")


# --- matching ----------------------------------------------------------------

@pytest.mark.parametrize("selector", [
    "Light 1", "light 1",                       # the name, as the app shows it
    "0x0010", "0010", "16",                     # the mesh address, three ways
    LIGHT1, LIGHT1.lower(),                     # the id from the export
    "hafele_0010",                              # the unique_id it has now
    "light.light_1",                            # the entity_id in Home Assistant
])
def test_a_light_can_be_named_by_any_handle_it_has(model, settings, tmp_path, selector):
    """Which of these a user knows depends entirely on how they got here."""
    apply(model, settings, tmp_path, f"lights:\n  {selector!r}:\n    name: Found\n")
    assert model.lights["hafele_0010"].name == "Found"


@pytest.mark.parametrize("selector", ["1", "All on", "Scene All on", "hafele_scene_1",
                                      "scene.scene_all_on"])
def test_a_scene_can_be_named_by_any_handle_it_has(model, settings, tmp_path, selector):
    apply(model, settings, tmp_path, f"scenes:\n  {selector!r}:\n    name: Found\n")
    assert next(s for s in model.scenes if s.number == 1).name == "Found"


def test_a_selector_that_matches_nothing_warns_rather_than_stops(model, settings, tmp_path):
    """A typo should not take the lights down; it should be visible in the log."""
    assert apply(model, settings, tmp_path, "lights:\n  Lihgt 1:\n    name: Oops\n") == 0
    assert any("matches nothing" in w for w in model.warnings)


def test_an_ambiguous_selector_is_refused(model, settings, tmp_path):
    """Picking one of two silently renames the wrong lamp."""
    model.lights["hafele_0011"].name = "Light 1"
    with pytest.raises(ConfigError, match="matches"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n    name: Which one\n")


# --- the file itself ---------------------------------------------------------

def test_an_unknown_setting_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="unknown setting"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n    colour: red\n")


def test_an_unknown_section_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="unknown top-level section"):
        apply(model, settings, tmp_path, "groups:\n  Shelf:\n    name: Nope\n")


def test_a_list_where_a_mapping_belongs_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="expected a mapping"):
        apply(model, settings, tmp_path, "lights:\n  - Light 1\n")


def test_broken_yaml_is_refused_with_the_parser_s_complaint(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n   name: [unclosed\n")


def test_a_value_that_is_not_text_is_refused(model, settings, tmp_path):
    with pytest.raises(ConfigError, match="expected a non-empty string"):
        apply(model, settings, tmp_path, "lights:\n  Light 1:\n    name: 7\n")


def test_groups_are_configured_in_the_lights_section(model, settings, tmp_path):
    """A group is a light entity in Home Assistant, so it lives there too."""
    apply(model, settings, tmp_path, "lights:\n  Shelf:\n    name: Bookcase\n")
    assert model.lights["hafele_group_c00b"].name == "Bookcase"


# --- writing one out ---------------------------------------------------------
#
# The dump exists to be diffed, so the only thing that makes it worth anything
# is that reading it back is a no-op. Everything below is that one property,
# approached from a few directions.

class Recorder:
    """Stands in for the MQTT client: what was published is what we compare."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def publish(self, topic, payload="", retain=False, **_kw):
        self.sent.append((topic, payload))

    def subscribe(self, *_a, **_kw):
        pass


def published(model, settings, tmp_path, tag: str) -> dict[str, dict]:
    """Every discovery payload this model would put on the wire."""
    state = State(tmp_path / f"state-{tag}.json", seq_block=10)
    state.open(0x7FF7)
    bridge = Bridge(settings, model, state)
    bridge.mqtt = Recorder()
    bridge._discovery()
    return {t: json.loads(p) for t, p in bridge.mqtt.sent if t.endswith("/config") and p}


def reread(text: str, doc, settings, tmp_path):
    """A fresh model with `text` applied, as a restart with the file in place."""
    fresh = parse(doc, settings)
    overrides.apply(fresh, settings, write(tmp_path, text))
    apply_identity(fresh, {})
    return fresh


def test_reading_the_dump_back_changes_nothing(model, doc, settings, tmp_path):
    """The whole point. Save the output, hand it straight back to the bridge,
    and Home Assistant must not be able to tell the difference."""
    apply_identity(model, {})
    before = published(model, settings, tmp_path, "before")
    after = reread(overrides.dump(model, settings), doc, settings, tmp_path)
    assert published(after, settings, tmp_path, "after") == before


def test_reading_the_dump_back_produces_the_same_dump(model, doc, settings, tmp_path):
    """Otherwise the second diff is against a moving target and every run of
    the command is a changed file."""
    apply_identity(model, {})
    text = overrides.dump(model, settings)
    assert overrides.dump(reread(text, doc, settings, tmp_path), settings) == text


def test_the_dump_adds_no_warning_of_its_own(model, doc, settings, tmp_path):
    """A file that warns about itself would teach everyone to ignore warnings.
    The export's own complaints are still there; the file adds none."""
    apply_identity(model, {})
    baseline = list(model.warnings)
    after = reread(overrides.dump(model, settings), doc, settings, tmp_path)
    assert after.warnings == baseline


def test_the_dump_writes_out_every_entity(model, settings):
    """Not just the interesting ones: a diff is only useful if it is complete."""
    apply_identity(model, {})
    doc_back = yaml.safe_load(overrides.dump(model, settings))
    assert set(doc_back) == {"lights", "scenes"}
    assert len(doc_back["lights"]) == len(model.lights)      # 4 lights + 3 groups
    assert len(doc_back["scenes"]) == len(model.scenes)


def test_every_entry_is_keyed_by_the_anchor_identity_is_remembered_by(model, settings):
    """The mesh address moves and the name follows the Connect app. The id from
    the export is the one handle that stays put, so it is what the file is
    organised by - the diff then shows a device changing, not disappearing."""
    doc_back = yaml.safe_load(overrides.dump(model, settings))
    assert set(doc_back["lights"]) == {e.guid for e in model.lights.values()}
    assert set(doc_back["scenes"]) == {sc.guid for sc in model.scenes}


def test_the_values_that_cannot_be_set_are_there_as_comments(model, settings):
    """Mesh addresses, scene numbers and node UUIDs are export-side facts the
    file cannot change - and exactly what a re-export moves, so they are in the
    output as comments rather than left out."""
    text = overrides.dump(model, settings)
    assert "mesh address 0x0010" in text                 # a light
    assert "multicast address 0xC00B" in text            # a group
    assert "scene number 3" in text                      # a scene
    assert "uuid=00000000-0000-4000-8000-000000100000" in text      # a box
    assert "3 member(s): Light 1, Light 2, Light 3" in text


def test_a_name_that_yaml_would_read_as_something_else_survives(model, doc, settings, tmp_path):
    """Unquoted, a light called `No` comes back as a boolean and the reader
    refuses it as "not a string" - on a file the bridge wrote itself."""
    model.lights["hafele_0010"].name = "No"
    apply_identity(model, {})
    after = reread(overrides.dump(model, settings), doc, settings, tmp_path)
    assert after.lights["hafele_0010"].name == "No"


def test_a_scene_name_is_written_with_its_prefix(model, settings, tmp_path, doc):
    """A name in this file replaces the whole name, prefix and all. Writing the
    bare one would turn `Scene All on` into `All on` on the way back in."""
    apply_identity(model, {})
    after = reread(overrides.dump(model, settings), doc, settings, tmp_path)
    sc = next(x for x in after.scenes if x.number == 1)
    assert sc.full_name(settings.scene_name_prefix) == "Scene All on"
    assert sc.entity_id(settings.scene_name_prefix) == "scene.scene_all_on"


def test_a_disabled_entity_stays_disabled_through_the_dump(model, doc, settings, tmp_path):
    apply(model, settings, tmp_path, "lights:\n  Light 1:\n    enabled: false\n")
    apply_identity(model, {})
    after = reread(overrides.dump(model, settings), doc, settings, tmp_path)
    assert after.lights["hafele_0010"].enabled is False


def test_the_dump_carries_no_timestamp_or_run_id(model, settings):
    """Two runs an hour apart have to produce the same bytes, or the diff is
    noise with the answer buried in it."""
    apply_identity(model, {})
    assert overrides.dump(model, settings) == overrides.dump(model, settings)


def test_the_command_prints_the_file_without_claiming_the_state_file(tmp_path, monkeypatch,
                                                                     capsys):
    """It has to be safe to run against a bridge that is up, and to work on a
    machine where one has never started - so it reads the state file rather
    than opening, locking or creating it."""
    monkeypatch.setenv("MESH_DIR", str(Path(__file__).parent / "fixtures"))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    assert main(["--dump-overrides"]) == 0
    assert set(yaml.safe_load(capsys.readouterr().out)) == {"lights", "scenes"}
    assert not (tmp_path / "state.json").exists()


def test_an_entity_with_no_id_in_the_export_is_keyed_by_its_unique_id(model, settings, doc,
                                                                      tmp_path):
    """Falling back silently would leave a key nobody can explain."""
    model.lights["hafele_0010"].guid = ""
    apply_identity(model, {})
    text = overrides.dump(model, settings)
    assert "no id in the export" in text
    assert "'hafele_0010':" in text
