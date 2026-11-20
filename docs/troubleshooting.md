# Troubleshooting

Back to the [README](../README.md).

## Diagnosing a failed start

Every foreseeable misconfiguration exits immediately with one line, not a traceback and not a retry loop. Checks run in order of cost: mesh file, then state directory, then MQTT, then Bluetooth, so you get the cheapest answer first.

| Symptom | Means |
| --- | --- |
| `... is not a Hafele Connect export` | Wrong file (an nRF Mesh or HA backup, say) |
| `2 *.connect files in /config` | Set `MESH_FILE` to pick one |
| `... is not valid JSON` | Truncated or corrupted export; re-export it |
| `netKeys[0].key must be 32 hex characters` | Partial export |
| `tos_scenes[].sceneNumber=... is not a number` | Hand-edited export; the field has to be an integer |
| `nodes[].tos_node.elementCount is str, expected a number` | Same, and this one decides address ranges |
| `State directory /data is not writable` | Mounted `:ro`, or missing |
| `MQTT_HOST=... does not resolve` | Name the container cannot see; use an IP |
| `Nothing is listening on host:port` | Broker down, or wrong port |
| `... rejected user 'x'` | Wrong `MQTT_USER` / `MQTT_PASS` |
| `MQTT_TLS_CA=... does not exist inside the container` | Right path on the host, not mounted into the image |
| `The TLS handshake with host:port failed` | Broker certificate not signed by `MQTT_TLS_CA`, or issued for another name |
| `Timed out connecting ... with MQTT_TLS on` | That port is a plain-MQTT listener; usually `MQTT_PORT=8883` |
| `... sent no CONNACK within 15s` | The opposite: a TLS listener being spoken to in the clear |
| `No D-Bus socket at /var/run/dbus/...` | Missing `network_mode: host` + dbus mount |
| `No powered Bluetooth adapter` | `bluetoothctl power on`, then `rfkill unblock bluetooth` if that is refused |
| `BlueZ has no Bluetooth adapter at all` | The radio is gone: a dongle unplugged, or the host's bluetooth service down |
| `BLE_ADAPTER=... is neither an adapter name like hci0 nor an adapter MAC address` | Typo; it takes one form or the other |
| `cannot ask BlueZ which adapters exist` | The dbus mount or `network_mode: host`; the bridge carries on, but cannot honour a `BLE_ADAPTER` given as a MAC |

### When it fails after a clean start

The link not coming up is not one problem, so it is not reported as one. Every `ble: down` line names the state, and the same word is the value of the `Mesh proxy status` sensor:

| State | What it means | Where to look |
| --- | --- | --- |
| `no adapter` | BlueZ reports no Bluetooth adapter at all | The dongle, `rfkill`, the container's `network_mode: host` and dbus mount |
| `no proxy in range` | The radio works, but no node is advertising our network | Range, aerial, a proxy node that is powered down, or proxy disabled on every node |
| `proxy seen, link failing` | A node is advertising and the GATT connection will not hold | Interference, a node at the edge of range, a wedged controller |

The second one is the easiest to misread, because a radio that hears nothing looks exactly like a network that is switched off. If the host has more than one adapter, the bridge checks them all before reporting it, and the log says what each of them heard; see [Which radio the bridge uses](bluetooth.md#which-radio-the-bridge-uses).

One specific case is worth knowing about: `ble: down (no proxy in range) - no node is advertising mesh network ...`, repeating, with every entity unavailable, on a host that was working minutes ago.
A mesh proxy stops advertising while a GATT client is connected to it, so if the previous process died without closing its link, BlueZ on the host still holds it open and no scan can see that node.
The bridge handles this itself: when a scan finds nothing it asks BlueZ for a device it already knows that carries our network ID, drops that stale connection and connects by address.
If you hit it on a host where `dbus-fast` is unavailable, the manual equivalent is `bluetoothctl disconnect <address>`.

The opposite shape of failure is a link that is up and entities that never get a state, with nothing failing anywhere.
Two log lines explain almost every case of it, and both are warnings rather than errors because the bridge keeps working either way.

`the proxy did not confirm the filter for 0x....` means the bridge asked the proxy to forward messages addressed to it and the proxy never said it agreed.
Those requests go out as Write Without Response, so sending them proves nothing, and a proxy that did not take the filter drops every reply one hop short of the bridge.
Some proxies simply answer nothing and forward everything anyway, in which case the lights do report their state and the line can be ignored; if they do not, this is the first thing to suspect.

`no authenticated network beacon in ...s on a link that is up` means the network is not beaconing, or not where this radio can hear it.
Beacons are how the bridge learns the IV index, and the only way it can catch up if the network's index moved while the bridge was down.
Nothing breaks the moment this appears, and everything breaks eventually, so check that secure network beacons are enabled on the network.
