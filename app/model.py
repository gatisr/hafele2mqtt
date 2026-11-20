"""Turn a .connect document into the entities the bridge exposes over MQTT.

Pure data: no BLE, no MQTT, no I/O. That is what makes `--validate` and the
unit tests possible without any hardware.

Identity
--------
A Home Assistant entity *is* its `unique_id`: change it and every automation,
script, dashboard card and group that named the old one is pointing at nothing.
The obvious identifier here - the mesh address - is the one thing in a Connect
network that is not stable. Re-provisioning a box or rebuilding a group hands
out new addresses, and the lights would come back as a second set of entities
with the first set orphaned.

So the address is still what a `unique_id` is *minted* from (that keeps the
zero-config start, and it is what the entities already in service use), but it
is not what identity is *kept* by. Every device, group, scene and node in a
Connect export carries a GUID that survives re-addressing, and `anchor()` turns
that into a key. `apply_identity()` looks each key up in a map the bridge
persists, and re-applies the `unique_id` that key was published under the first
time it was seen. The address underneath changes; the entity does not.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from .config import ConfigError, Settings, as_addr, as_int

log = logging.getLogger("model")

# Device types we can actually drive. Everything else in a Hafele export is
# either infrastructure (the box itself, a remote, the Hafele gateway) or a
# lamp type whose model we do not implement yet.
LIGHT_TYPES = {"com.haefele.led.white"}
INFRASTRUCTURE_PREFIXES = (
    "com.haefele.24v.", "com.haefele.230v.", "com.haefele.remote",
    "com.haefele.gateway", "com.haefele.sensor",
)

# Connect icon name -> Material Design Icons name, for scenes only. Lights are
# deliberately left alone: Home Assistant's own light icon follows the on/off
# state, and a fixed `icon` would replace that with one static glyph. Scenes
# have no such default (every scene is the same palette icon), so the export's
# icon is a real improvement there.
#
# Unmapped icons produce no `icon` at all rather than a guess. A wrong icon on
# every scene is worse than Home Assistant's own.
SCENE_ICONS = {
    "ui/sun": "mdi:white-balance-sunny",
    "ui/sunrise": "mdi:weather-sunset-up",
    "ui/sunset": "mdi:weather-sunset",
    "ui/moon": "mdi:weather-night",
    "ui/star": "mdi:star",
    "ui/grid": "mdi:view-grid",
    "scenes/standby": "mdi:power-standby",
    "scenes/routine": "mdi:calendar-clock",
    "scenes/night": "mdi:weather-night",
    "scenes/relax": "mdi:sofa",
    "scenes/party": "mdi:party-popper",
    "scenes/work": "mdi:desk-lamp",
    "scenes/dinner": "mdi:silverware-fork-knife",
}


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def anchor(kind: str, guid: str) -> str:
    """The key an entity's identity is remembered under.

    Namespaced by kind because the GUID spaces are not guaranteed to be
    disjoint - and because a group and a light must never be able to inherit
    each other's `unique_id` even if an export did reuse one.
    """
    return f"{kind}:{guid.strip().upper()}"


@dataclass
class Node:
    """A physical Hafele box/node; becomes one device in Home Assistant."""
    uid: str
    name: str
    unicast: int
    model: str
    sw_version: str = ""
    hw_version: str = ""
    guid: str = ""                  # nodes[].UUID - survives re-addressing
    mac: str = ""                   # tos_node.macBytes, for the --validate report
    area: str = ""                  # resolved from the lights on it; see device_area()

    @property
    def anchor(self) -> str:
        return anchor("node", self.guid)


@dataclass
class Entity:
    uid: str
    name: str
    dst: int
    kind: str                       # "light" | "group"
    node: Node | None = None
    group_type: str = ""            # tos_groups[].type for groups
    members: list["Entity"] = field(default_factory=list)
    lightness: int = 0
    last_on: int = 65535
    # False until a poll, a status message or a command tells us what the light
    # is actually doing. `lightness` is 0 before that, which is a guess, not a
    # reading, and publishing it would report every light as off on restart.
    known: bool = False
    guid: str = ""                  # tos_devices[].id / tos_groups[].id
    icon: str = ""                  # mdi: name, only ever from an override
    room: str = ""                  # Connect room name, resolved from roomId
    area: str = ""                  # what to suggest to HA; override beats room
    object_id: str = ""             # entity_id to ask for, without the domain
    enabled: bool = True            # entities.yaml can switch one off entirely
    uid_pinned: bool = False        # uid came from entities.yaml, not the address

    @property
    def anchor(self) -> str:
        return anchor(self.kind, self.guid)

    @property
    def entity_id(self) -> str:
        return f"light.{self.object_id or slug(self.name)}"


@dataclass
class Scene:
    number: int
    name: str
    kind: str = "scene"
    uid: str = ""
    guid: str = ""                  # tos_scenes[].id
    icon: str = ""
    object_id: str = ""
    enabled: bool = True
    uid_pinned: bool = False
    # SCENE_NAME_PREFIX exists to keep six scenes called "bright", "night" and
    # "cozy" from being six entities with no clue which system they belong to.
    # That argument does not survive someone writing the name out by hand, so
    # an override replaces the whole name rather than being prefixed again.
    name_overridden: bool = False

    @property
    def anchor(self) -> str:
        return anchor("scene", self.guid)

    def full_name(self, prefix: str) -> str:
        return self.name if self.name_overridden else f"{prefix} {self.name}".strip()

    def entity_id(self, prefix: str) -> str:
        return f"scene.{self.object_id or slug(self.full_name(prefix))}"


@dataclass
class MeshModel:
    mesh_name: str = "Connect Mesh"
    net_key: bytes = b""
    app_key: bytes = b""
    lights: dict[str, Entity] = field(default_factory=dict)
    scenes: list[Scene] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    all_dst: int = 0xC002
    warnings: list[str] = field(default_factory=list)
    # unique_ids this run restored from the identity map because the address
    # underneath them moved: {uid: "what it would have been called now"}.
    healed: dict[str, str] = field(default_factory=dict)

    @property
    def singles(self) -> list[Entity]:
        return [e for e in self.lights.values() if e.kind == "light"]

    @property
    def groups(self) -> list[Entity]:
        return [e for e in self.lights.values() if e.kind == "group"]


def _node_name(node: dict, unicast: int) -> str:
    """Name the physical box after its own tos_device entry.

    The node type and the device type are related but not equal: a remote whose
    `tos_node.type` is `com.haefele.remote` carries a device of type
    `com.haefele.remote.v2`. Hence a prefix match rather than equality, or the
    remote ends up called "Node 0x0008". Element names are empty strings in real
    exports, so that fallback almost never fires.
    """
    own_type = (node.get("tos_node") or {}).get("type") or ""
    for dev in node.get("tos_devices") or []:
        dtype = dev.get("type") or ""
        if dev.get("name") and own_type and (dtype.startswith(own_type) or own_type.startswith(dtype)):
            return dev["name"]
    for element in node.get("elements") or []:
        if element.get("name"):
            return element["name"]
    return f"Node 0x{unicast:04X}"


def parse(doc: dict, s: Settings) -> MeshModel:
    """Build the entity model. Raises ConfigError on an unusable network."""
    m = MeshModel(mesh_name=doc.get("meshName") or "Connect Mesh")
    # Index 0 of each list, always. A Hafele Connect export has exactly one of
    # each, so there is nothing to choose between; anything else is a network
    # this bridge has never been tried against, and silently taking the first
    # key would show up as lights that ignore every command.
    m.net_key = bytes.fromhex(doc["netKeys"][0]["key"])
    m.app_key = bytes.fromhex(doc["appKeys"][0]["key"])
    for field_name, label in (("netKeys", "net key"), ("appKeys", "app key")):
        count = len(doc.get(field_name) or [])
        if count > 1:
            m.warnings.append(
                f"The mesh file has {count} {label}s; the bridge uses {field_name}[0] "
                f"(index {(doc[field_name][0] or {}).get('index', 0)}). If the lights do not "
                f"respond, the nodes are probably bound to a different {label}.")

    tos_groups: dict[str, dict] = {}
    for g in doc.get("tos_groups", []):
        if not isinstance(g, dict) or "id" not in g:
            continue
        if g["id"] in tos_groups:
            raise ConfigError(
                f"Two groups share the id {g['id']} "
                f"({tos_groups[g['id']].get('name', '?')!r} and {g.get('name', '?')!r}). "
                "That id is what keeps the group's Home Assistant entity attached to it across "
                "re-addressing, so the bridge cannot tell which group owns which entity. "
                "Re-export the network from the Connect app.")
        tos_groups[g["id"]] = g
    members_by_group: dict[str, list[Entity]] = {}
    seen_dst: dict[int, str] = {}
    seen_guid: dict[str, str] = {}
    # The Connect app has no separate list of rooms: a room is a tos_group with
    # type "room", and a device's `roomId` points at one of them. That makes the
    # group list the only place a room *name* exists, which is worth saying out
    # loud because `tos_uiSettings` looks like it ought to hold it and does not
    # (it is only the favourites lists).
    rooms = {gid: (g.get("name") or "") for gid, g in tos_groups.items() if g.get("type") == "room"}

    for raw_node in doc.get("nodes", []):
        if not isinstance(raw_node, dict) or "unicastAddress" not in raw_node:
            continue
        unicast = as_addr(raw_node["unicastAddress"], "nodes[].unicastAddress")
        tos_node = raw_node.get("tos_node") or {}
        node = Node(
            uid=f"hafele_node_{unicast:04x}",
            name=_node_name(raw_node, unicast),
            unicast=unicast,
            model=tos_node.get("type") or "Connect Mesh node",
            sw_version=str(tos_node.get("firmwareVersion") or ""),
            hw_version=str(tos_node.get("hardwareVersion") or ""),
            guid=str(raw_node.get("UUID") or ""),
            mac=str(tos_node.get("macBytes") or ""),
        )
        m.nodes.append(node)

        for dev in raw_node.get("tos_devices") or []:
            dtype = dev.get("type") or ""
            if dtype not in LIGHT_TYPES:
                if dtype.startswith("com.haefele.led."):
                    m.warnings.append(
                        f"{dev.get('name', '?')!r} is a {dtype}, which this bridge cannot "
                        "drive yet (only plain white dimmable lights) - skipped.")
                elif not dtype.startswith(INFRASTRUCTURE_PREFIXES):
                    m.warnings.append(f"{dev.get('name', '?')!r} has unknown type {dtype} - skipped.")
                continue
            if "meshAddress" not in dev:
                m.warnings.append(f"{dev.get('name', '?')!r} has no meshAddress - skipped.")
                continue
            dst = as_addr(dev["meshAddress"], "tos_devices[].meshAddress")
            name = dev.get("name") or f"Light 0x{dst:04X}"
            if dst in seen_dst:
                raise ConfigError(
                    f"Two lights share mesh address 0x{dst:04X} ({seen_dst[dst]!r} and {name!r}). "
                    "The export is inconsistent - re-export it from the Connect app.")
            seen_dst[dst] = name
            guid = str(dev.get("id") or "")
            _claim_guid(seen_guid, guid, name, "lights")
            room = rooms.get(dev.get("roomId"), "")
            entity = Entity(uid=f"hafele_{dst:04x}", name=name, dst=dst, kind="light", node=node,
                            guid=guid, room=room, area=room)
            m.lights[entity.uid] = entity
            for gs in dev.get("groupSettings") or []:
                if isinstance(gs, dict) and "id" in gs:
                    members_by_group.setdefault(gs["id"], []).append(entity)

    if not m.lights:
        raise ConfigError(
            "The mesh file contains no controllable lights "
            f"(looked for tos_devices of type {'/'.join(sorted(LIGHT_TYPES))}). "
            "Nothing to expose to Home Assistant.")

    for gid, g in tos_groups.items():
        members = members_by_group.get(gid, [])
        if not members:
            continue
        if "multicastAddress" not in g:
            m.warnings.append(f"Group {g.get('name', gid)!r} has no multicastAddress - skipped.")
            continue
        dst = as_addr(g["multicastAddress"], "tos_groups[].multicastAddress")
        name = s.all_group_name if g.get("type") == "all" else (g.get("name") or f"Group 0x{dst:04X}")
        _claim_guid(seen_guid, gid, name, "groups")
        # A room group is the room, so it can suggest its own area. An ordinary
        # group spans whatever its members span, and "All lights" spans the
        # house, so neither gets one.
        room = g.get("name") or "" if g.get("type") == "room" else ""
        ge = Entity(uid=f"hafele_group_{dst:04x}", name=name, dst=dst, kind="group",
                    group_type=g.get("type") or "group", members=members,
                    guid=str(gid), room=room, area=room)
        m.lights[ge.uid] = ge

    m.all_dst = next((as_addr(g["multicastAddress"], "tos_groups[].multicastAddress")
                      for g in tos_groups.values()
                      if g.get("type") == "all" and "multicastAddress" in g), 0xC002)

    seen_scene: set[int] = set()
    for sc in doc.get("tos_scenes", []):
        if not isinstance(sc, dict) or "sceneNumber" not in sc:
            continue
        num = as_int(sc["sceneNumber"], "tos_scenes[].sceneNumber")
        if not 1 <= num <= 0xFFFF:
            # A scene recall carries the number in two bytes, so anything
            # outside that range would raise an OverflowError at the moment
            # someone pressed the button rather than here.
            m.warnings.append(
                f"Scene {sc.get('name', num)!r} has number {num}, which is not a mesh scene "
                "number (1-65535) - skipped.")
            continue
        if num in seen_scene:
            continue
        seen_scene.add(num)
        name = sc.get("name") or f"Scene {num}"
        guid = str(sc.get("id") or "")
        _claim_guid(seen_guid, guid, name, "scenes")
        m.scenes.append(Scene(number=num, name=name, kind=sc.get("kind") or "scene",
                              uid=f"hafele_scene_{num}", guid=guid,
                              icon=SCENE_ICONS.get(sc.get("icon") or "", "")))

    assign_areas(m)
    return m


def _claim_guid(seen: dict[str, str], guid: str, name: str, where: str) -> None:
    """Refuse an export that gives two things the same id.

    An id that names two entities is not something to pick a winner for: both
    would heal onto one `unique_id` and one of the two lights would answer to
    the other's entity. A GUID-less entry is let through - the identity map
    simply has no anchor for it and it falls back to its mesh address.
    """
    if not guid:
        return
    key = guid.strip().upper()
    if key in seen:
        raise ConfigError(
            f"Two entries in {where} share the id {guid} ({seen[key]!r} and {name!r}). "
            "That id is what keeps a Home Assistant entity attached to its device across "
            "re-addressing, so the bridge cannot tell which of the two owns which entity. "
            "Re-export the network from the Connect app.")
    seen[key] = name


def apply_identity(m: MeshModel, known: dict[str, str]) -> dict[str, str]:
    """Re-apply the `unique_id` each anchor was first published under.

    Returns the map to persist: everything `known` already held, plus an entry
    for every anchor seen in this export. Entries for anchors that are *not* in
    this export are kept on purpose - that is the whole memory. A box unplugged
    for a fortnight and re-provisioned on a new address has to come back as the
    entities it left as, and an export it is missing from is exactly when the
    only record of it is this map.

    Mutates the model in place: `m.lights` is re-keyed, and `uid` on every
    entity, node and scene is set to its remembered value.
    """
    mapping = dict(known)
    # Entities and scenes are separate MQTT components and nodes are devices,
    # so a name can repeat across the three without any ambiguity. Within one
    # of them it cannot.
    claimed: dict[str, set[str]] = {"entity": set(), "scene": set(), "node": set()}
    items: list[tuple[str, object, str]] = (
        [("entity", e, e.uid) for e in m.lights.values()]
        + [("scene", sc, sc.uid) for sc in m.scenes]
        + [("node", n, n.uid) for n in m.nodes]
    )

    # Three passes, and the order is the point. A pinned id is a sentence
    # someone wrote down, so it wins outright. Then an anchor the bridge has
    # already published under some id gets it back, before any newcomer is
    # allowed to mint one - so a new device moving into a freed address can
    # never evict the entity that used to live there.
    restored: set[int] = set()
    for index, (space, obj, natural) in enumerate(items):
        if not getattr(obj, "uid_pinned", False):
            continue
        if obj.uid in claimed[space]:
            raise ConfigError(
                f"Two entries in the entities file pin unique_id={obj.uid!r} (one of them is "
                f"{obj.name!r}). A unique_id names exactly one entity in Home Assistant, so "
                "one of the two has to be something else.")
        claimed[space].add(obj.uid)
        restored.add(index)
        if obj.guid:
            mapping[obj.anchor] = obj.uid

    for index, (space, obj, natural) in enumerate(items):
        if index in restored:
            continue
        remembered = mapping.get(obj.anchor) if obj.guid else None
        if remembered and remembered not in claimed[space]:
            claimed[space].add(remembered)
            restored.add(index)
            obj.uid = remembered
            if remembered != natural:
                m.healed[remembered] = natural

    for index, (space, obj, natural) in enumerate(items):
        if index in restored:
            continue
        uid = natural
        if uid in claimed[space]:
            # The address this would have been named after now belongs to an
            # entity that was here first. Fall back to something that cannot
            # collide and does not move: the anchor itself.
            uid = f"{natural}_{slug(obj.guid)[:8]}" if obj.guid else f"{natural}_x"
            m.warnings.append(
                f"{obj.name!r} would be published as {natural!r}, but that belongs to an entity "
                f"this bridge published earlier and whose mesh address has since moved. Using "
                f"{uid!r} instead.")
        claimed[space].add(uid)
        obj.uid = uid
        if obj.guid:
            mapping[obj.anchor] = uid

    m.lights = {e.uid: e for e in m.lights.values()}
    return mapping


def assign_areas(m: MeshModel) -> None:
    """Work out each box's `suggested_area` from the lights on it.

    Run at the end of `parse()` and again after the entities file has been
    applied, because an `area:` override on a light is exactly the thing that
    changes the answer. Cheap enough to do twice; wrong if done only once.
    """
    singles = m.singles
    for n in m.nodes:
        n.area = device_area(n, singles)


def device_area(node: Node, lights: list[Entity]) -> str:
    """The area to suggest for a physical box, or "" if there is no single answer.

    `suggested_area` is a property of the *device*, and a four-channel box can
    perfectly well drive lights in two rooms. Suggesting one of them would put
    the whole box - and so every light on it - in the wrong place, which is
    worse than suggesting nothing, so a box whose lights disagree gets nothing.
    """
    areas = {e.area for e in lights if e.node is node and e.area}
    if len(areas) != 1:
        return ""
    if any(e.node is node and not e.area for e in lights):
        return ""
    return areas.pop()


def describe(m: MeshModel, s: Settings, src: int, src_reason: str, origin: str) -> str:
    """The human report printed by --validate."""
    out: list[str] = []
    out.append(f"hafele2mqtt - mesh file: {origin}")
    out.append(f"network name:  {m.mesh_name}")
    out.append("net key / app key: present (NID and AID derived at runtime)")
    out.append(f"bridge address: 0x{src:04X}  ({src_reason})")
    out.append(f"all-nodes group: 0x{m.all_dst:04X}")
    out.append("")

    singles = sorted(m.singles, key=lambda e: e.dst)
    out.append(f"NODES ({len(m.nodes)})")
    for n in m.nodes:
        fw = f" fw {n.sw_version}" if n.sw_version else ""
        out.append(f"  0x{n.unicast:04X}  {n.name:<24} {n.model}{fw}")
        out.append(f"           id={n.uid}{_anchor_note(m, n)}"
                   f"{'  mac=' + n.mac if n.mac else ''}"
                   f"{'  suggested_area=' + n.area if n.area else ''}")
    out.append("")

    out.append(f"LIGHTS ({len(singles)})")
    for e in singles:
        out.append(f"  0x{e.dst:04X}  {e.name:<24} -> {e.entity_id}{_off_note(e)}")
        out.append(f"           unique_id={e.uid}{_anchor_note(m, e)}  "
                   f"cmd={s.base_topic}/light/{e.uid}/set")
    out.append("")

    groups = sorted(m.groups, key=lambda e: e.dst)
    out.append(f"GROUPS ({len(groups)})")
    for e in groups:
        names = ", ".join(sorted(x.name for x in e.members))
        out.append(f"  0x{e.dst:04X}  {e.name:<24} -> {e.entity_id}  "
                   f"[{e.group_type}]{_off_note(e)}")
        out.append(f"           unique_id={e.uid}{_anchor_note(m, e)}")
        out.append(f"           {len(e.members)} member(s): {names}")
    out.append("")

    out.append(f"SCENES ({len(m.scenes)})")
    for sc in sorted(m.scenes, key=lambda x: x.number):
        out.append(f"  #{sc.number:<5} {sc.full_name(s.scene_name_prefix):<24} -> "
                   f"{sc.entity_id(s.scene_name_prefix)}  [{sc.kind}]{_off_note(sc)}")
        out.append(f"           unique_id={sc.uid}{_anchor_note(m, sc)}"
                   f"{'  icon=' + sc.icon if sc.icon else ''}")
    out.append("")

    out.append("MQTT (not contacted in this mode)")
    out.append(f"  broker:          {s.mqtt_host}:{s.mqtt_port}"
               f"{' (authenticated)' if s.mqtt_user else ' (anonymous)'}")
    out.append(f"  base topic:      {s.base_topic}/")
    out.append(f"  availability:    {s.availability_topic} (mesh reachable)")
    out.append(f"                   {s.bridge_availability_topic} (process alive)")
    out.append(f"  diagnostics:     {s.diagnostics_topic}")
    out.append(f"  discovery prefix:{s.discovery_prefix}/")
    out.append("")
    out.append("The entity_id column is what Home Assistant is ASKED for "
               "(default_entity_id). It only takes effect when the entity is created; "
               "an entity that already exists keeps the id it has. Same for "
               "suggested_area and an existing device.")

    if m.healed:
        out.append("")
        out.append(f"RESTORED IDENTITY ({len(m.healed)})")
        out.append("  These moved to a new mesh address and kept the unique_id they were "
                   "published under, so nothing in Home Assistant had to change:")
        for uid, natural in sorted(m.healed.items()):
            out.append(f"  - {uid}  (a fresh install would have called it {natural})")

    if m.warnings:
        out.append("")
        out.append(f"WARNINGS ({len(m.warnings)})")
        out.extend(f"  - {w}" for w in m.warnings)
    return "\n".join(out)


def _anchor_note(m: MeshModel, obj) -> str:
    if obj.uid in m.healed:
        return f"  (kept; a fresh install would call it {m.healed[obj.uid]})"
    return "" if obj.guid else "  (no id in the export: identity follows the mesh address)"


def _off_note(obj) -> str:
    return "" if obj.enabled else "   NOT EXPOSED (entities file)"
