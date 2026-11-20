# MQTT: topics, TLS and command limits

Back to the [README](../README.md).

## MQTT topics

Everything below `hafele/` moves with `BASE_TOPIC`; everything below `homeassistant/` moves with `DISCOVERY_PREFIX`. `<uid>` is `hafele_<addr>` for a single light, `hafele_group_<addr>` for a group, with `<addr>` the four-digit lowercase hex mesh address. Scenes are commanded by scene number, not by uid.

| Topic | Direction | Retained | Payload |
| --- | --- | --- | --- |
| `hafele/status` | out | yes | `online` or `offline`, following the **mesh** link |
| `hafele/bridge/status` | out | yes | `online` or `offline`, following the **process**. Also the MQTT last will |
| `hafele/bridge/diagnostics` | out | yes | One JSON document describing the BLE link, see below |
| `hafele/light/<uid>/state` | out | yes | `{"state": "ON", "brightness": 0-255}` |
| `hafele/light/<uid>/set` | in | no | JSON command, see below |
| `hafele/scene/<number>/set` | in | no | `ON` |
| `homeassistant/light/<uid>/config` | out | yes | Discovery payload, empty to retract |
| `homeassistant/scene/hafele_scene_<n>/config` | out | yes | Discovery payload, empty to retract |

Light commands accept the Home Assistant JSON light schema:

```sh
mosquitto_pub -t 'hafele/light/hafele_0010/set' -m '{"state":"ON","brightness":128}'
mosquitto_pub -t 'hafele/light/hafele_0010/set' -m '{"state":"OFF"}'
mosquitto_pub -t 'hafele/light/hafele_group_c002/set' -m 'ON'
mosquitto_pub -t 'hafele/scene/3/set' -m 'ON'
```

Those are the real uids of `tests/fixtures/sample.connect`, so you can check the shape against the fixture without any hardware.

A bare `ON`, or `{"state":"ON"}` with no brightness, restores the level the light was last on at, falling back to full. That applies to groups as well as to single lights: dim a group to a quarter, turn it off, turn it back on, and it comes back at a quarter rather than at full. A group command is addressed to the group once and applied to every member, and a group's own state follows the brightest member.

Scene topics take a plain decimal scene number, `1` to `65535`. Anything else - a name, a leading `+`, a non-ASCII digit - is ignored with one log line rather than guessed at, because recalling the wrong scene is worse than recalling none.

Run `--validate` to print the exact topics for your own export rather than guessing the addresses.

The diagnostics document is written whole, on every change, so it can never be read half-updated:

```json
{
  "up": true,
  "status": "connected",
  "adapter": "hci1 (AA:BB:CC:DD:EE:FF)",
  "adapter_name": "hci1",
  "adapter_address": "AA:BB:CC:DD:EE:FF",
  "rssi": -72,
  "drops": 3,
  "last_drop": "2026-01-01T00:00:00+00:00"
}
```

`status` is one of `starting`, `connected`, `no adapter`, `no proxy in range` or `proxy seen, link failing`. The last three are the distinction worth having: no radio at all, a radio that cannot hear this network, and a radio that hears it but cannot hold a connection to it. `rssi` and `last_drop` are `null` until there is something to say.

`rssi` goes back to `null` whenever the bridge changes adapter, and the adapter fields go `null` when the host has no radio at all. A signal reading only means anything next to the antenna that measured it, so carrying one across a change of radio would leave Home Assistant showing a live-looking number from hardware that is no longer in use, with nothing to correct it.

### MQTT over TLS

`MQTT_TLS=false` is the default, and with it the MQTT password and every light state cross the network in the clear. On a LAN you control that is a reasonable place to be; on anything shared it is not.

Switching it on is three settings and usually a port change:

```sh
MQTT_TLS=true
MQTT_PORT=8883
MQTT_TLS_CA=/config/ca.crt      # only for a private CA
```

`MQTT_TLS_CA` names the certificate that signed the broker's own, as the container sees it, so mount it in alongside the mesh export (`- ./ca.crt:/config/ca.crt:ro`). Leave it empty if the broker's certificate was issued by a public CA: the system trust store already has what it needs. A path that does not exist is refused at start-up with one line naming the variable, rather than surfacing later as a `FileNotFoundError` from inside the TLS library.

`MQTT_TLS_INSECURE=true` skips the check that the certificate matches `MQTT_HOST`, and nothing else. It does **not** skip verifying the signature, so a self-signed certificate still has to be named in `MQTT_TLS_CA` whether this is set or not. What it gives up is the part of TLS that tells your broker apart from anything else holding that certificate, which is most of the reason to have turned TLS on. It is for a certificate issued for a name you cannot make the bridge use, and reissuing the certificate is the better answer every time.

Both MQTT clients the bridge creates, the start-up probe and the long-lived one, are configured from the same place, so the probe can never test a connection the bridge does not then make. That matters most in the two directions this is easy to get wrong: TLS pointed at a plain listener hangs rather than fails, and plain MQTT pointed at a TLS listener does the same, so both end in a start-up message that names `MQTT_TLS` instead of blaming the port or the firewall.

Enabling TLS with `MQTT_PORT` still at 1883 is a warning, not an error. 8883 is the usual TLS port and 1883 the plaintext one, but a broker is free to serve TLS wherever it likes, so the bridge says so once and carries on. Setting `MQTT_TLS_CA` or `MQTT_TLS_INSECURE` while `MQTT_TLS` is off warns too: on their own they do nothing, and the connection they look like they are protecting is in the clear.

### Limits on incoming commands

The bridge takes commands from a broker it does not own, and every command spends two sequence numbers out of a space that is never refilled, see [The 24-bit horizon](mesh-internals.md#the-24-bit-horizon). A misconfigured automation, or anything else with write access to the broker, can burn through unicast addresses that way without doing anything the lights would even show. Two settings bound it, and the defaults are chosen so that nothing a normal installation does ever reaches them.

- **`CMD_RATE_LIMIT`** (default `10`) - commands per second, sustained.
- **`CMD_QUEUE_MAX`** (default `64`) - how many commands are held at once, and also the burst allowed from idle.

The burst is what keeps the limit out of the way. The bucket starts full and holds `CMD_QUEUE_MAX` tokens, so a scene switching 64 lights at once goes out at full speed, and only a publisher that keeps that rate up afterwards is slowed. Home Assistant turning a light on is one command, and the busiest thing it does unprompted is a transition, which is a handful.

Coalescing does more than the limit does, and it is not a restriction. When a command arrives for an entity that already has one queued and unsent, the queued one is **replaced**: dragging a brightness slider publishes a position per pixel, and all but the last are already wrong by the time they could be sent. The replacement keeps its place in the queue, so one busy light cannot push ahead of a command for another. Coalescing carries on while the rate limit is waiting, which is what turns a flood into one command per entity rather than a backlog behind the limit.

When the queue is full, the **oldest** command is dropped. Full means the bridge is behind, and being behind is exactly when the freshest instruction is the one worth keeping.

Dropping or delaying is logged once per ten second window rather than once per command, with a count of each:

```
WARNING bridge: command rate limit: 184 dropped, 12 delayed (CMD_RATE_LIMIT=10/s, CMD_QUEUE_MAX=64).
```

Seeing that line at all means something is publishing faster than the mesh can be driven, which is worth finding rather than tuning around. Both values must be above zero: `CMD_RATE_LIMIT=0` would be a bridge that accepts commands and never sends one, which is indistinguishable from broken hardware.
