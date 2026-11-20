# Home Assistant: entities, naming and overrides

Back to the [README](../README.md).

## Home Assistant

Discovery config topics are retained, so entities survive a broker restart.

- Each light carries a stable `unique_id` and belongs to a device representing its physical Häfele box, with `manufacturer`, `model` and the box's firmware as `sw_version`.
- Lights and scenes require **both** availability topics (`availability_mode: all`): the process has to be running *and* the mesh has to be reachable. The mesh half is driven by the state of the BLE proxy link rather than by MQTT being connected, so a dead link shows up as unavailable entities instead of a stale state that quietly lies.
- The diagnostic entities require only the process topic, for the obvious reason: a diagnostic that goes unavailable at the exact moment it has something to report is worse than none.
- When a light disappears from a re-exported `.connect` file, its discovery topic is retracted automatically (the bridge remembers what it published).

### What you end up with

There is nothing to put in `configuration.yaml`. Provided the MQTT integration is set up and pointed at the same broker, the entities appear by themselves within a few seconds of the first start.

- One `light.*` per Häfele light, grouped under a device carrying its box's model and firmware.
- One `light.*` per Connect group, so the groups you already made in the app keep working instead of having to be rebuilt as HA light groups.
- One `scene.*` per Connect scene, named `Scene <name>` (change the prefix with `SCENE_NAME_PREFIX`) and carrying the icon the Connect app shows for it where that icon has an obvious Material Design equivalent.
- Six diagnostic entities on the bridge device, folded away under "Diagnostic" rather than cluttering the dashboard: the proxy link as a `connectivity` binary sensor, the state in words, which adapter is in use, the last signal strength, how many times the link has dropped since start-up, and when it last did. Together they answer "is it the radio, the range or the node?" without an SSH session, and the signal one is a sensible thing to put on a graph: a link that is about to start failing usually gets quieter first.

Three devices' worth of structure, not one:

| Device | Holds |
| --- | --- |
| `<box name>` (one per physical box) | the lights wired to that box |
| `Connect Mesh (<network name>)` | the groups, and it is the bridge itself |
| `Connect Mesh scenes (<network name>)` | the scenes, reached through the bridge via `via_device` |

Scenes used to hang off the bridge device next to the groups, which made its page one undifferentiated list of a dozen entities. They are a separate device now. Groups deliberately stay on the bridge: every box and the scene device point at it with `via_device`, and a device with no entities of its own is a dead end in the UI.

### Where the state comes from after a restart

A restarted bridge knows nothing. It holds no level for any light, and it deliberately publishes nothing for a light it has not asked yet: the only value it could send is its own default, and sending that would be telling Home Assistant that every light is off rather than that nobody has looked. So between start-up and a node's first answer, that entity has no state at all - the card is blank rather than wrong.

Filling that in is the job of the start-up poll, which is not the same thing as the periodic one:

- It waits for the Bluetooth link instead of being skipped when the radio is still scanning. A cold connect is a scan plus a GATT handshake, so at the moment the first poll would otherwise fire there is usually nothing to send into. The wait is bounded at two minutes; if the link is not up by then the poll is skipped with a line saying so, and the periodic poll takes over. That is the right outcome rather than a failure, because a radio that has not come up in two minutes has a problem of its own, and the link section above is already logging what it is.
- Whatever has not answered is asked again, on a widening gap, several times. Only those: lights that already replied are not re-read, because every read spends sequence numbers out of a space that is never refilled. The list is recomputed before each round, so answers that arrive between rounds take those lights out of the next one.
- If the link drops partway through, the remaining rounds wait rather than sending into a radio that cannot carry them, and those rounds are not counted as attempts. The count in the log is what tells you whether a light was really asked or never reached.
- When something never answers, it is named in the log at `WARNING`, with the number of attempts. A light with no state in Home Assistant and no line in the log is a thing you find out about by noticing a blank card days later. There is a short pause before that line is written, because the answers to the last round are still in flight and naming a light that was about to reply is a false alarm in the one message meant to be worth acting on.

`POLL_INTERVAL=0` turns this off with everything else; it means no polling, and a start-up poll is polling.

### Entities keep their identity when the mesh moves

A `unique_id` *is* the entity as far as Home Assistant is concerned. Change it and every automation, script, dashboard card and group that named the old one is pointing at nothing.

The obvious identifier here, the mesh address, is the one thing in a Connect network that is not stable. Re-provisioning a box or rebuilding a group hands out new addresses, and the lights would come back as a second set of entities with the first set orphaned.

So the address is still what a `unique_id` is *minted* from, but it is not what identity is *kept* by. Every device, group, scene and node in a Connect export carries a GUID that survives re-addressing. The bridge records `GUID -> unique_id` in its state file the first time it sees each one, and re-applies it on every start. The address underneath changes; the entity does not.

There is nothing to configure and nothing to migrate. The ids are the same ones the bridge has always published, so an existing install keeps every entity it has.

What happens in each case:

| In the export | What the bridge does |
| --- | --- |
| A known GUID on a new address | Keeps the old `unique_id`, retargets it. Logged as `RESTORED IDENTITY` by `--validate` |
| A GUID never seen before | Mints a new `unique_id` from the address, as it always did |
| A known GUID that has vanished | Keeps the mapping forever. A box unplugged for a fortnight is exactly when the map is the only record of it |
| A new device on an address a known GUID has left | The old entity keeps the id; the newcomer gets a non-colliding one derived from its GUID, and a warning says so |
| Two devices sharing one GUID | Refuses to start. Picking one would attach a live entity to the wrong lamp |

### Naming: what Home Assistant will and will not do

This is where it is easy to promise more than MQTT discovery can deliver, so plainly:

- **`default_entity_id`** is what the bridge asks for, so a light the Connect app never named lands on `light.desk_strip` rather than `light.hafele_0010`. Home Assistant honours it **only when it creates the entity.** An entity that already exists keeps the `entity_id` it has, and nothing the bridge publishes will rename it. (This replaced the older `object_id`, which Home Assistant removed from MQTT discovery in 2026.4; `default_entity_id` arrived in 2025.10. Older versions ignore unknown discovery keys silently, so there is nothing to configure either way.)
- **`suggested_area`** is likewise consulted **only when the device is created.** A box Home Assistant already knows keeps whatever area you put it in. The bridge only suggests one at all when every light on a box agrees on the same Connect room, because guessing otherwise would put two thirds of a box in the wrong place. Note that the room name comes from the Connect app, so if your HA areas are in another language you will get a new area in the app's language rather than a match.

To rename entities that already exist, rename them in the Home Assistant UI, or set `enabled: false` in `entities.yaml`, restart, and turn them back on with the ids you want. Renames in the UI are safe: the `unique_id` does not change when you rename, and it does not change when you re-export the `.connect` file either, so renames and dashboards survive both.

### Overriding names: `entities.yaml`

Entirely optional. Drop an `entities.yaml` next to the `.connect` file (or point `ENTITIES_FILE` at one) and the bridge reads it; with no file present nothing changes.

```yaml
lights:
  Desk strip:                   # whatever the Connect app calls it
    name: Desk lighting         # stops following renames in the app
    entity_id: light.desk       # asked for at creation; see above
    area: Study                 # see the caveat below
  "0x0010":                     # or address it by mesh address
    enabled: false              # do not expose this one at all
  Shelf:                        # groups live in `lights:` too
    name: Bookcase
scenes:
  3:                            # or by scene number
    name: Bright
    icon: mdi:white-balance-sunny
    unique_id: old_bridge_scene # for migrating off another bridge
```

Settings: `name`, `entity_id`, `icon`, `area`, `unique_id`, `enabled`. Anything else is an error rather than a line in the log, because a typo in a naming file is otherwise invisible.

The key on the left can be any handle the thing has: its name as the Connect app shows it, the id from the export, the mesh address (`0x0010`, `0010` and `16` all work), its current `unique_id`, or its Home Assistant `entity_id`. Which of those you know depends entirely on how you got here. A key that matches nothing is a warning, not a failure; a key that matches two things is a failure, because picking one of them silently renames the wrong lamp.

Two caveats worth knowing before you write the file:

- **`area:` works on lights only.** Home Assistant takes the area from the *device*, and MQTT discovery has no way to say otherwise. A light sits on a box of its own, so an `area:` there becomes that box's `suggested_area` (and only when every light on the box agrees). A group or a scene shares one device with all the others, so honouring it would drag every sibling into the same room. The bridge warns and ignores it rather than writing it somewhere it has no effect.
- **`unique_id:` is a loaded gun.** It pins identity outright, which is what you want when migrating from another bridge onto ids that already exist, and is how you orphan every entity you have if you use it for anything else.

Run `--validate` after editing: it prints the `unique_id` and `entity_id` each entity would get, with the file applied.
You do not have to write the file from scratch either: `--dump-overrides` prints a complete one with every current value already in it, which is also useful on its own as a record of what the mesh looks like.

They are ordinary MQTT lights from there on:

```yaml
automation:
  - alias: Kitchen lights at dusk
    triggers:
      - trigger: sun
        event: sunset
    actions:
      - action: light.turn_on
        target:
          entity_id: light.connect_mesh_room
        data:
          brightness_pct: 60
```

Because an entity's availability follows the state of the BLE link, add `condition: state` on the entity being `unavailable` if you have automations that must not fire while the mesh is unreachable.

### Keeping track of what the mesh looks like: `--dump-overrides`

```sh
docker compose run --rm hafele2mqtt python -m app.main --dump-overrides > entities.yaml
```

That writes a complete entities file: every light, every group and every scene, with every setting already filled in with the value it has right now.
Reading it back changes nothing, because it sets each entity to what it already is.
`entities.yaml.example` in this repository is what the output looks like.

It is worth keeping because the Connect app owns the other half of this.
Rooms get renamed, a box is re-provisioned onto a new address, a scene is rebuilt with a different number, and from the Home Assistant side none of that announces itself.
Keep the dump beside your `.connect` file in version control, run the command again after the next export, and `diff` is the answer:

```diff
-  # Desk strip - light at mesh address 0x0010, on node 0x0010 (Study box)
+  # Desk lamp - light at mesh address 0x0014, on node 0x0010 (Study box)
   '00000000-0000-4000-8000-000000000101':
-    name: 'Desk strip'
+    name: 'Desk lamp'
     entity_id: 'light.desk_strip'
     area: 'Study'
     unique_id: 'hafele_0010'
     enabled: true
```

The entry stays where it is because it is keyed by the id from the export, which is the same anchor the bridge remembers identity by, and `unique_id` is unchanged because the entity in Home Assistant did not move.
The light was renamed and re-addressed, and the diff says so in two lines instead of leaving you to find out from a dashboard that stopped working.

Four things are worth knowing about it:

- **Nothing has to be mounted.** The bridge does not read the file unless you put it where it looks (beside the `.connect` file, or at `ENTITIES_FILE`), and it behaves identically with no file at all. Identity across re-addressing is kept in `state.json` either way, not here.
- **Mounting it turns a report into instructions.** Every `name`, `entity_id`, `area` and `unique_id` in the file is then pinned, and the next dump shows you the file's values rather than the app's. For tracking, keep it out of the container. For pinning, mount it read-only and expect to maintain it by hand from then on.
- **It runs with the bridge down.** The command reads the export and the identity map and touches neither Bluetooth nor MQTT, so it works on a machine that has never been in range of a light. Warnings go to the log, so the file itself is the whole of standard output.
- **The output is meant for `diff`.** Fixed key order, sorted by address, no timestamp and no run id, so two runs an hour apart are the same bytes unless something really changed.

Values the file cannot set are printed as comments: mesh addresses, scene numbers, group multicast addresses, group membership and node UUIDs.
They are read from the export and ignored as input, and they are in the output precisely because they are what a re-export moves.

### Cleaning up orphaned entities

- **Changing `BASE_TOPIC`** does *not* orphan entities: the discovery topics do not contain it, so the entities just follow. The retained *state* payloads under the old prefix would linger, so the bridge clears those itself.
- **Changing `DISCOVERY_PREFIX`** *does* orphan them, because the old config topics are still retained under the old prefix. Retract them first, with the old value still in place:

  ```sh
  docker compose run --rm hafele2mqtt python -m app.main --purge-discovery
  ```

  Then change the prefix and start normally.
