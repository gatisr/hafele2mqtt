"""The optional `entities.yaml`: what the user wants an entity called.

Everything in this module is opt-in. The bridge's selling point is that you
drop a `.connect` file in and get working lights, so with no entities file
present nothing here changes a single byte of what goes on the wire.

What it is for is the half of naming the export cannot settle:

* the Connect app owns the names, so renaming a light there renames it in Home
  Assistant - pin `name:` and it stops following,
* `entity_id` is derived from the name, so `light.hafele_002e` is what you get
  for a light the app never named - set `entity_id:` and get `light.desk`,
* a mesh "room" is not a Home Assistant area and will not be in your language -
  set `area:` and the guess is out of the way,
* and `unique_id:` pins identity outright, for someone migrating from another
  bridge who needs the entities to land on ids that already exist.

Matching is by any of the things that identify an entity - its name, its id in
the export, its mesh address, or the unique_id it currently has - because which
of those a user knows depends entirely on how they got here.

`dump()` is the same thing backwards: the whole model written out as a file
that sets everything to what it already is. Nobody needs it to run the bridge;
it exists so that "what does the mesh look like today" is a text file you can
keep in version control and `diff` after the next export.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import ConfigError, Settings
from .model import Entity, MeshModel, Node, Scene, assign_areas, slug

log = logging.getLogger("overrides")

DEFAULT_NAME = "entities.yaml"
_FIELDS = {"name", "entity_id", "icon", "area", "unique_id", "enabled"}
_UNIQUE_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def locate(s: Settings) -> Path | None:
    """The entities file to use, or None if there is not one.

    An explicit ENTITIES_FILE that does not exist is an error, not a fallback:
    someone who named a file meant it, and starting anyway would silently
    publish every entity under the names they were trying to replace.
    """
    if s.entities_file:
        path = Path(s.entities_file)
        if not path.exists():
            raise ConfigError(
                f"ENTITIES_FILE={path} does not exist inside the container. Mount it "
                f"(e.g. `- ./{DEFAULT_NAME}:/config/{DEFAULT_NAME}:ro`), or unset "
                "ENTITIES_FILE to run without one.")
        return path
    found = Path(s.mesh_dir) / DEFAULT_NAME
    return found if found.is_file() else None


def apply(m: MeshModel, s: Settings, path: Path | None) -> int:
    """Apply `path` to the model in place. Returns how many entries matched."""
    if path is None:
        return 0
    doc = _read(path)
    used = 0
    for section, targets in (("lights", list(m.lights.values())), ("scenes", m.scenes)):
        for selector, spec in _section(doc, section, path).items():
            matches = [t for t in targets if _matches(t, selector, s)]
            if not matches:
                m.warnings.append(
                    f"{path}: {section}: {selector!r} matches nothing in the mesh file, so that "
                    "entry does nothing. Check the spelling, or remove it if the device is gone.")
                continue
            if len(matches) > 1:
                raise ConfigError(
                    f"{path}: {section}: {selector!r} matches "
                    f"{', '.join(sorted(_label(t) for t in matches))}. Use the id from the "
                    "export or the mesh address instead, so there is one obvious target.")
            _apply_one(m, matches[0], spec, selector, path, section)
            used += 1
    # An `area:` on a light is the one thing here that changes what a *device*
    # looks like, so the boxes have to be worked out again now rather than at
    # the end of parse().
    assign_areas(m)
    return used


def _require_yaml():
    try:
        import yaml
    except ImportError:                                 # pragma: no cover - dependency is pinned
        raise ConfigError(
            "PyYAML is not installed, so an entities file can be neither read nor written. "
            "Rebuild the image, or remove the file to run without one.") from None
    return yaml


def _read(path: Path) -> dict:
    yaml = _require_yaml()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as ex:
        raise ConfigError(f"{path} could not be read: {ex}.") from None
    except yaml.YAMLError as ex:
        raise ConfigError(f"{path} is not valid YAML: {ex}.") from None
    if doc is None:
        return {}                                       # an empty file is a fine way to say "none"
    if not isinstance(doc, dict):
        raise ConfigError(
            f"{path} contains a {type(doc).__name__} at the top level, expected `lights:` "
            "and/or `scenes:` mappings.")
    unknown = sorted(set(doc) - {"lights", "scenes"})
    if unknown:
        raise ConfigError(
            f"{path} has unknown top-level section(s): {', '.join(map(str, unknown))}. "
            "Only `lights:` and `scenes:` are read.")
    return doc


def _section(doc: dict, name: str, path: Path) -> dict:
    raw = doc.get(name)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: `{name}:` is a {type(raw).__name__}, expected a mapping of "
            "light/scene -> settings.")
    out = {}
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(
                f"{path}: {name}: {key!r} is a {type(spec).__name__}, expected a mapping like "
                "`name: Kitchen counter`.")
        extra = sorted(set(spec) - _FIELDS)
        if extra:
            raise ConfigError(
                f"{path}: {name}: {key!r} has unknown setting(s): {', '.join(map(str, extra))}. "
                f"Allowed: {', '.join(sorted(_FIELDS))}.")
        out[str(key)] = spec
    return out


def _matches(target: Entity | Scene, selector: str, s: Settings) -> bool:
    """True if `selector` names `target` by any of the handles it has.

    Case- and format-insensitive on purpose: `0x002E`, `002e` and `46` are the
    same address written by three different people, and nobody should have to
    guess which spelling this file wants.
    """
    want = selector.strip().casefold()
    handles = {target.name.casefold(), target.guid.casefold(), target.uid.casefold()}
    if isinstance(target, Entity):
        handles |= {f"0x{target.dst:04x}", f"{target.dst:04x}", str(target.dst)}
        handles.add(target.entity_id.casefold())
    else:
        handles |= {str(target.number), target.full_name(s.scene_name_prefix).casefold()}
        handles.add(target.entity_id(s.scene_name_prefix).casefold())
    handles.discard("")
    return want in handles


def _label(target: Entity | Scene) -> str:
    return f"{target.name!r} ({target.uid})"


def _apply_one(m: MeshModel, target: Entity | Scene, spec: dict, selector: str,
               path: Path, section: str) -> None:
    where = f"{path}: {section}: {selector!r}"

    if "enabled" in spec:
        if not isinstance(spec["enabled"], bool):
            raise ConfigError(f"{where}: enabled must be true or false, "
                              f"got {spec['enabled']!r}.")
        target.enabled = spec["enabled"]

    if "name" in spec:
        name = _text(spec["name"], f"{where}: name")
        target.name = name
        if isinstance(target, Scene):
            target.name_overridden = True

    if "icon" in spec:
        icon = _text(spec["icon"], f"{where}: icon")
        if ":" not in icon:
            raise ConfigError(
                f"{where}: icon={icon!r} has no prefix. Home Assistant wants a full icon name "
                f"like `mdi:{icon}`.")
        target.icon = icon

    if "area" in spec:
        area = _text(spec["area"], f"{where}: area")
        # An area belongs to a *device* in Home Assistant, never to a single
        # entity, and MQTT discovery has no way to say otherwise. A light sits
        # on a box of its own, so `area:` there is a real instruction; a group
        # or a scene sits on a bridge device shared with all the others, and
        # honouring it would drag every sibling into the same room. Say so
        # rather than writing the value somewhere it has no effect.
        if isinstance(target, Entity) and target.kind == "light":
            target.area = area
        else:
            m.warnings.append(
                f"{where}: area={area!r} is ignored. Home Assistant takes the area from the "
                f"device, and every {target.kind} shares one bridge device, so this would move "
                "all of them. Set the area on the entity in Home Assistant instead.")

    if "entity_id" in spec:
        raw = _text(spec["entity_id"], f"{where}: entity_id")
        domain = "light" if isinstance(target, Entity) else "scene"
        wanted = raw.split(".", 1)[1] if raw.startswith(f"{domain}.") else raw
        if "." in wanted:
            raise ConfigError(
                f"{where}: entity_id={raw!r} is in the wrong domain - a "
                f"{section[:-1]} can only be `{domain}.something`.")
        cleaned = slug(wanted)
        if not cleaned:
            raise ConfigError(
                f"{where}: entity_id={raw!r} has nothing usable in it. Home Assistant ids are "
                "lower-case letters, digits and underscores.")
        if cleaned != wanted:
            log.info("%s: entity_id %r -> %r (Home Assistant only accepts lower-case letters, "
                     "digits and underscores)", where, raw, f"{domain}.{cleaned}")
        target.object_id = cleaned

    if "unique_id" in spec:
        uid = _text(spec["unique_id"], f"{where}: unique_id")
        if not _UNIQUE_ID.match(uid):
            raise ConfigError(
                f"{where}: unique_id={uid!r} contains something other than letters, digits, "
                "underscores and hyphens. It ends up in an MQTT topic, so it has to stay simple.")
        target.uid = uid
        target.uid_pinned = True


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        shown = value if isinstance(value, str) else type(value).__name__
        raise ConfigError(f"{where} is {shown!r}, expected a non-empty string.")
    return value.strip()


# --- writing one out ---------------------------------------------------------

def dump(m: MeshModel, s: Settings) -> str:
    """The whole model written out as an entities file that changes nothing.

    Every entity, every setting, filled in with the value it has right now, so
    reading the result back produces exactly what is published today. That is
    also the only property worth testing about it.

    The output is deliberately boring - fixed key order, sorted by address, no
    timestamp and no run id - because the only reason to generate it twice is
    to compare the two.
    """
    out = _preamble(m)
    out.append("lights:")
    for e in sorted(m.singles, key=lambda x: x.dst):
        out.extend(_light_entry(e))
    for e in sorted(m.groups, key=lambda x: x.dst):
        out.extend(_group_entry(e))
    out.append("")
    out.append("scenes:")
    for sc in sorted(m.scenes, key=lambda x: x.number):
        out.extend(_scene_entry(sc, s))
    out.extend(_node_notes(m))
    return "\n".join(out) + "\n"


def _preamble(m: MeshModel) -> list[str]:
    return [
        "# hafele2mqtt entities file, written by `python -m app.main --dump-overrides`.",
        "#",
        "# Everything this bridge publishes, with every setting already filled in with",
        "# the value it has today. Reading it back changes nothing - it sets each entity",
        "# to what it already is. That is the point: keep it in version control, run the",
        "# command again after the next .connect export, and the diff is the list of what",
        "# moved in the mesh.",
        "#",
        "# Keeping it is entirely optional. The bridge runs the same with no entities",
        "# file at all, and identity across re-addressing is remembered in state.json",
        "# either way, not here.",
        "#",
        "# The key of each entry is the id from the export: the same anchor the bridge",
        "# remembers identity by, and the one handle that does not move when a device is",
        "# re-provisioned onto a new address.",
        "#",
        "# Comment lines inside an entry are NOT settings, and there is nowhere to put",
        "# them if they were. Mesh addresses, scene numbers, group multicast addresses,",
        "# group membership and node UUIDs all come from the export and are ignored as",
        "# input. They are printed because they are exactly what a re-export changes.",
        "#",
        "# Two things are written fuller than you would type them by hand:",
        "#  * scene names include SCENE_NAME_PREFIX, because a name written here replaces",
        "#    the whole name rather than being prefixed again,",
        "#  * `area:` appears on lights only. Home Assistant takes the area from the",
        "#    device, and groups and scenes share one, so the bridge refuses it there.",
        "#",
        f"# network: {m.mesh_name}",
        f"# all-lights group: 0x{m.all_dst:04X}",
        "",
    ]


def _light_entry(e: Entity) -> list[str]:
    note = f"{e.name} - light at mesh address 0x{e.dst:04X}"
    if e.node is not None:
        note += f", on node 0x{e.node.unicast:04X} ({e.node.name})"
    fields = [("name", _q(e.name)), ("entity_id", _q(e.entity_id))]
    if e.icon:
        fields.append(("icon", _q(e.icon)))
    if e.area:
        # Omitted rather than written empty when there is no area: an empty
        # string is refused on the way back in, and "no area" is the default
        # anyway. A room appearing in a later export shows up as an added line.
        fields.append(("area", _q(e.area)))
    fields += [("unique_id", _q(e.uid)), ("enabled", _bool(e.enabled))]
    return _entry(e, [note], fields)


def _group_entry(e: Entity) -> list[str]:
    members = ", ".join(sorted(x.name for x in e.members)) or "none"
    notes = [f"{e.name} - {e.group_type} group, multicast address 0x{e.dst:04X}",
             f"{len(e.members)} member(s): {members}"]
    fields = [("name", _q(e.name)), ("entity_id", _q(e.entity_id))]
    if e.icon:
        fields.append(("icon", _q(e.icon)))
    fields += [("unique_id", _q(e.uid)), ("enabled", _bool(e.enabled))]
    return _entry(e, notes, fields)


def _scene_entry(sc: Scene, s: Settings) -> list[str]:
    note = f"{sc.full_name(s.scene_name_prefix)} - scene number {sc.number}, kind {sc.kind}"
    fields = [("name", _q(sc.full_name(s.scene_name_prefix))),
              ("entity_id", _q(sc.entity_id(s.scene_name_prefix)))]
    if sc.icon:
        fields.append(("icon", _q(sc.icon)))
    fields += [("unique_id", _q(sc.uid)), ("enabled", _bool(sc.enabled))]
    return _entry(sc, [note], fields)


def _entry(target: Entity | Scene, notes: list[str], fields: list[tuple[str, str]]) -> list[str]:
    """One block: comments that only describe, then the settings that act."""
    key = target.guid
    if not key:
        # Nothing stable to key it by, so the unique_id has to do - and that
        # one follows the mesh address, which is the whole problem. Say so in
        # the file rather than leaving the next reader to work it out.
        key = target.uid
        notes = notes + ["no id in the export: keyed by unique_id, which follows the address"]
    out = [""]
    out.extend(f"  # {n}" for n in notes)
    out.append(f"  {_q(key)}:")
    out.extend(f"    {name}: {value}" for name, value in fields)
    return out


def _node_notes(m: MeshModel) -> list[str]:
    """The physical boxes, as comments.

    A node is a *device* in Home Assistant, not an entity, and there is no
    `nodes:` section to configure one - writing it would be an error. They are
    here because a box's UUID is what its lights are re-attached by after
    re-provisioning, so it is worth having in the diff.
    """
    out = ["",
           "# Physical boxes. Devices rather than entities: there is no `nodes:` section,",
           "# and adding one is an error. Listed because a box's UUID is what the bridge",
           "# re-attaches its lights by after it is re-provisioned onto a new address."]
    for n in sorted(m.nodes, key=lambda x: x.unicast):
        out.append(f"#   0x{n.unicast:04X}  {n.name}")
        out.append("#           " + "  ".join(_node_facts(n)))
    return out


def _node_facts(n: Node) -> list[str]:
    facts = [f"uuid={n.guid or '-'}", f"device_id={n.uid}", f"model={n.model}"]
    for label, value in (("fw", n.sw_version), ("hw", n.hw_version),
                         ("mac", n.mac), ("suggested_area", n.area)):
        if value:
            facts.append(f"{label}={value}")
    return facts


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _q(value: str) -> str:
    """A YAML scalar that comes back as the string that went in.

    Quoted unconditionally. Unquoted, a light called `No` is a boolean, `on` is
    a boolean, and `1.0` is a float - and the reader refuses all three as "not
    a string", on a file it wrote itself.
    """
    text = _require_yaml().safe_dump(value, default_style="'", allow_unicode=True,
                                     width=1 << 20).strip()
    return text.removesuffix("...").strip()
