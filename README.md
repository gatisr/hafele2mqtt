# hafele2mqtt

Häfele Connect Mesh → MQTT bridge with Home Assistant discovery.

It talks Bluetooth Mesh directly to a Häfele light over a GATT proxy connection, using the keys from your Häfele Connect app export. **No Häfele gateway, no cloud account.**

## Contents

[What you get](#what-you-get) · [Requirements](#requirements) · [Quick start](#quick-start) · [Configuration](#configuration) · [Security](#security) · [More documentation](#more-documentation) · [Development](#development)

## What you get

| In Häfele Connect | In Home Assistant |
| --- | --- |
| Light | `light.*` (on/off, brightness) |
| Group | `light.*`, so existing groups keep working |
| Scene | `scene.*` |
| (the bridge itself) | Diagnostic sensors: link state, adapter, signal strength, drops |

- Entities appear by themselves through MQTT discovery. Nothing goes into `configuration.yaml`.
- State stays correct when a light is changed from a wall switch or the Häfele app (polling).
- Entities keep their `unique_id` when you re-export or re-provision the mesh, so dashboards and automations survive.

## Requirements

- A Linux host with **BlueZ** and a Bluetooth adapter in range of the lights.
- An **MQTT broker**.
- A **`.connect` export** from the Häfele Connect app (Settings → Share network).
- Docker with Compose.

## Quick start

#### 1. Get the code

```sh
git clone https://github.com/gatisr/hafele2mqtt.git
cd hafele2mqtt
```

#### 2. Add your mesh file and MQTT settings

```sh
mkdir -p config
cp /path/to/Kitchen.connect config/      # any file name is fine
cp .env.example .env                     # set MQTT_HOST, MQTT_USER, MQTT_PASS
```

#### 3. Check it, then start it

```sh
docker compose run --rm hafele2mqtt python -m app.main --validate   # no Bluetooth or MQTT needed
docker compose up -d --build
```

`--validate` prints every light, group, scene and topic the bridge would publish, then exits.

### Prefer a ready-made image?

CI publishes a multi-arch image (`linux/amd64`, `linux/arm64`) to GitHub Container Registry. Skip the clone and use a `compose.yaml` like this:

```yaml
services:
  hafele2mqtt:
    image: ghcr.io/gatisr/hafele2mqtt:latest
    container_name: hafele2mqtt
    restart: unless-stopped
    network_mode: host                 # required for Bluetooth
    env_file: .env
    volumes:
      - /var/run/dbus:/var/run/dbus
      - ./config:/config:ro            # your .connect file
      - ./data:/data                   # must be writable: holds the mesh counter
    tmpfs:
      - /tmp
```

Then `docker compose up -d`. You still need `config/` and `.env` from step 2.

## Configuration

Only the three `MQTT_*` values are required. Everything else is derived from the export or has a working default; the full list is in [.env.example](.env.example).

| Setting | What it does |
| --- | --- |
| `MQTT_HOST` `MQTT_USER` `MQTT_PASS` | **Required.** Your broker |
| `MQTT_TLS` | Encrypt the broker connection. See [MQTT over TLS](docs/mqtt.md#mqtt-over-tls) |
| `BLE_ADAPTER` | Pin one Bluetooth adapter. Prefer its MAC over `hci0`. Empty = pick whichever hears the mesh. See [radio selection](docs/bluetooth.md#which-radio-the-bridge-uses) |
| `PROXY_ADDRESS` | Pin one proxy light instead of auto-discovering |
| `POLL_INTERVAL` | Seconds between state polls (default `30`). Higher = counters last longer. `0` disables polling |
| `TTL` | Relay hops a command may take (default `5`). Raise it if distant lights do not respond |
| `BASE_TOPIC`, `DISCOVERY_PREFIX` | MQTT topic prefixes. Changing `DISCOVERY_PREFIX` needs [a purge first](docs/home-assistant.md#cleaning-up-orphaned-entities) |
| `CMD_RATE_LIMIT`, `CMD_QUEUE_MAX` | Bound what the broker can send. See [limits](docs/mqtt.md#limits-on-incoming-commands) |
| `LOG_LEVEL` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` or `DEBUG` |

Leave `SRC_ADDR` and `IV_INDEX` empty. Setting them wrongly breaks the bridge silently; why is explained in [mesh internals](docs/mesh-internals.md).

### Ways to give the bridge the mesh file

The first one that is set wins.

| Variable | Use when |
| --- | --- |
| `MESH_FILE` | `/config` holds several exports and you want to name one |
| `MESH_DIR` (default `/config`) | Normal case: the single `*.connect` in there is used |
| `MESH_JSON` | A secret manager injects the file contents as a string |
| `MESH_JSON_BASE64` | Same, base64-encoded (Portainer, Kubernetes secrets) |

### Renaming entities

Optional. Put an `entities.yaml` next to the `.connect` file to rename entities, pin ids, set icons or hide things. `--dump-overrides` writes a complete starting file for you. See [Home Assistant docs](docs/home-assistant.md#overriding-names-entitiesyaml).

## Controlling lights over MQTT

```sh
mosquitto_pub -t 'hafele/light/hafele_0010/set' -m '{"state":"ON","brightness":128}'
mosquitto_pub -t 'hafele/light/hafele_0010/set' -m '{"state":"OFF"}'
mosquitto_pub -t 'hafele/scene/3/set'           -m 'ON'
```

Run `--validate` for the exact ids of your own lights. The full topic list is in [MQTT docs](docs/mqtt.md).

## Security

- **Your `.connect` file is a key to your lights.** Anyone holding it has full control. Keep it out of git (it is already in `.gitignore`), and treat `config/` like a password file. It is mounted read-only.
- **MQTT is plaintext by default.** The password and every light state cross the network unencrypted. Fine on a LAN you control; turn on `MQTT_TLS` anywhere else.
- **The broker decides who may command the lights.** Anything that can publish to `hafele/…/set` can switch lights and recall scenes. Use broker ACLs. The bridge also limits how fast commands are accepted.
- **The container runs as root** and mounts the host's D-Bus socket. BlueZ access needs this, and there is no portable way around it. A process that escaped the container would be talking to the host's services as root, with your mesh keys beside it. Run it on a machine you trust, not a shared box. Details and the polkit alternative: [design notes](docs/design.md#deliberate-omissions).
- It opens no listening port.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## More documentation

| Topic | File |
| --- | --- |
| Something does not work | [Troubleshooting](docs/troubleshooting.md) |
| Topics, TLS, command limits | [MQTT](docs/mqtt.md) |
| Entities, naming, `entities.yaml`, cleanup | [Home Assistant](docs/home-assistant.md) |
| Radio choice and reconnect behaviour | [Bluetooth](docs/bluetooth.md) |
| IV index, sequence numbers, why state is kept on disk | [Mesh internals](docs/mesh-internals.md) |
| Build and deploy with `scripts/deploy.sh` | [Deploying](docs/deploying.md) |
| Code layout, supported platforms, what is left out on purpose | [Design](docs/design.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Release history | [CHANGELOG.md](CHANGELOG.md) |

## Development

```sh
pip install -r requirements-dev.txt
python -m pytest
```

The tests need no Bluetooth, broker or hardware. `tests/fixtures/sample.connect` is fictional; never commit a real network export.

## Licence

MIT, see [LICENSE](LICENSE).

Häfele is a trademark of its respective owner. This is an independent, unofficial implementation written against the published Bluetooth Mesh Profile specification. It is not affiliated with, endorsed by or supported by Häfele.
