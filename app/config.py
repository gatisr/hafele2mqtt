"""Settings, mesh-document loading and start-up validation.

Everything in here runs before BLE or MQTT are touched, so `--validate` can
exercise the whole path on a laptop with no Bluetooth and no broker.

The guiding rule for this module: a misconfigured container must die
immediately with ONE readable line, never with a traceback and never by
spinning in a retry loop. Every raise below is a `ConfigError`, and
`app.main` prints `str(exc)` and exits 1.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import adapter

log = logging.getLogger("config")


class ConfigError(Exception):
    """A fatal, user-fixable configuration problem. Message must be one line."""


# Written out rather than taken from logging's own table: that table is
# mutable, accepts a numeric string as a level, and answers "Level 7" for a
# typo instead of refusing it. The point here is to list what works.
_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


# --- small env helpers ------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw, 0)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number.") from None


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number.") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (use true/false).")


def as_addr(value: Any, what: str) -> int:
    """Mesh addresses appear as both ints and hex strings in a .connect file."""
    if isinstance(value, bool):
        raise ConfigError(f"{what} is a boolean, expected a mesh address.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            raise ConfigError(f"{what}={value!r} is not a hex mesh address.") from None
    raise ConfigError(f"{what} is {type(value).__name__}, expected a mesh address.")


def as_int(value: Any, what: str) -> int:
    """A plain integer out of a .connect file, or one readable line saying why not.

    The rule for this module is that a bad export dies with a sentence, not a
    traceback, and a bare int() in the middle of parsing breaks that rule for
    exactly the fields a hand-edited export is most likely to mangle.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{what} is {type(value).__name__}, expected a number.")
    try:
        return int(value)
    except (ValueError, OverflowError):
        # OverflowError: Infinity is a number json.loads is happy to produce.
        raise ConfigError(f"{what}={value!r} is not a number.") from None


@dataclass
class Settings:
    # mesh source
    mesh_file: str = ""
    mesh_dir: str = "/config"
    # Optional naming overrides. Empty means "use <mesh_dir>/entities.yaml if
    # there is one", which is how a user gets the feature without setting
    # anything; see overrides.locate().
    entities_file: str = ""
    # mesh parameters
    src_addr: int | None = None        # None -> derive from the .connect file
    iv_index: int | None = None        # None -> take from state file / beacons
    ttl: int = 5
    seq_block: int = 500
    seq_recovery_jump: int = 100_000
    seq_auto_rotate: bool = True
    poll_interval: float = 30.0
    # ble
    proxy_address: str | None = None
    # An `hciN` name or an adapter MAC address. Empty means "work it out": see
    # app/adapter.py for why neither form can simply be trusted.
    ble_adapter: str | None = None
    ble_scan_timeout: float = 10.0
    ble_retry_min: float = 2.0
    ble_retry_max: float = 120.0
    # mqtt
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str | None = None
    mqtt_pass: str | None = None
    mqtt_client_id: str = "hafele2mqtt"
    # TLS is off by default, which is the behaviour every existing deployment
    # already has. Turning it on is a choice about the network the broker is
    # on, not something that can be guessed from the port.
    mqtt_tls: bool = False
    # Empty means the system trust store, which is what a broker with a
    # publicly-signed certificate needs. A private CA goes here.
    mqtt_tls_ca: str = ""
    # Skips hostname verification. For self-signed certificates only, and it
    # gives up the protection TLS was turned on for; see the README.
    mqtt_tls_insecure: bool = False
    # Separate from BLE_RETRY_MAX on purpose: the two failures have nothing in
    # common. A proxy out of radio range is worth backing off hard; a broker
    # that went away for a restart should be picked up again promptly.
    mqtt_reconnect_max: float = 60.0
    base_topic: str = "hafele"
    discovery_prefix: str = "homeassistant"
    # incoming commands
    # Insurance against a broker nobody is watching, not a policy: both
    # defaults sit an order of magnitude above anything Home Assistant does.
    # See CommandQueue in main.py.
    cmd_rate_limit: float = 10.0
    cmd_queue_max: int = 64
    # paths
    state_file: str = "/data/state.json"
    # healthcheck
    # The bridge touches this on every turn of its poll loop, and the
    # Dockerfile's HEALTHCHECK reads its age; see app/health.py. It lives under
    # /tmp rather than beside the state file because a heartbeat is not state:
    # nothing needs it to survive a restart, and /tmp can be a tmpfs, which
    # keeps a once-a-poll timestamp off a Raspberry Pi's SD card entirely.
    # Empty switches the healthcheck off.
    health_file: str = "/tmp/health"
    # 0 means "three poll intervals, and never less than health.FLOOR".
    health_max_age: float = 0.0
    # display
    all_group_name: str = "All lights"
    scene_name_prefix: str = "Scene"
    # misc
    log_level: str = "INFO"

    @property
    def availability_topic(self) -> str:
        """Says the mesh is reachable. What the lights and scenes hang off."""
        return f"{self.base_topic}/status"

    @property
    def bridge_availability_topic(self) -> str:
        """Says the bridge process is running, which is a weaker and separate claim.

        The diagnostics exist to explain a mesh that is unreachable, so they
        cannot share an availability topic with the lights: they would go
        unavailable at exactly the moment they have something to say.
        """
        return f"{self.base_topic}/bridge/status"

    @property
    def diagnostics_topic(self) -> str:
        """One retained JSON document; every diagnostic entity reads a field of it."""
        return f"{self.base_topic}/bridge/diagnostics"

    @staticmethod
    def from_env() -> "Settings":
        src_raw = _env("SRC_ADDR", "auto").lower()
        iv_raw = _env("IV_INDEX", "auto").lower()
        s = Settings(
            mesh_file=_env("MESH_FILE"),
            mesh_dir=_env("MESH_DIR", "/config"),
            entities_file=_env("ENTITIES_FILE"),
            src_addr=None if src_raw in ("", "auto") else _env_int("SRC_ADDR", 0),
            iv_index=None if iv_raw in ("", "auto") else _env_int("IV_INDEX", 0),
            ttl=_env_int("TTL", 5),
            seq_block=_env_int("SEQ_BLOCK", 500),
            seq_recovery_jump=_env_int("SEQ_RECOVERY_JUMP", 100_000),
            seq_auto_rotate=_env_bool("SEQ_AUTO_ROTATE", True),
            poll_interval=_env_float("POLL_INTERVAL", 30.0),
            proxy_address=_env("PROXY_ADDRESS") or None,
            ble_adapter=_env("BLE_ADAPTER") or None,
            ble_scan_timeout=_env_float("BLE_SCAN_TIMEOUT", 10.0),
            ble_retry_min=_env_float("BLE_RETRY_MIN", 2.0),
            ble_retry_max=_env_float("BLE_RETRY_MAX", 120.0),
            mqtt_host=_env("MQTT_HOST", "localhost"),
            mqtt_port=_env_int("MQTT_PORT", 1883),
            mqtt_user=_env("MQTT_USER") or None,
            mqtt_pass=os.environ.get("MQTT_PASS") or None,
            mqtt_client_id=_env("MQTT_CLIENT_ID", "hafele2mqtt"),
            mqtt_tls=_env_bool("MQTT_TLS", False),
            mqtt_tls_ca=_env("MQTT_TLS_CA"),
            mqtt_tls_insecure=_env_bool("MQTT_TLS_INSECURE", False),
            mqtt_reconnect_max=_env_float("MQTT_RECONNECT_MAX", 60.0),
            base_topic=_env("BASE_TOPIC", "hafele").strip("/"),
            discovery_prefix=_env("DISCOVERY_PREFIX", "homeassistant").strip("/"),
            cmd_rate_limit=_env_float("CMD_RATE_LIMIT", 10.0),
            cmd_queue_max=_env_int("CMD_QUEUE_MAX", 64),
            state_file=_env("STATE_FILE", "/data/state.json"),
            health_file=_env("HEALTH_FILE", "/tmp/health"),
            health_max_age=_env_float("HEALTH_MAX_AGE", 0.0),
            all_group_name=_env("ALL_GROUP_NAME", "All lights"),
            scene_name_prefix=_env("SCENE_NAME_PREFIX", "Scene"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )
        if not 0 <= s.ttl <= 127:
            raise ConfigError(f"TTL={s.ttl} is out of range (0-127).")
        if s.ttl < 2:
            # TTL counts relays, and both 0 and 1 mean "do not relay this": a
            # message leaves the proxy and is acted on only by nodes that hear
            # the proxy's own radio. In a one-room network that is fine and
            # saves the mesh a great deal of needless repeating. In a house it
            # is the explanation for the lights at the far end never responding
            # while the near ones are perfect - a symptom that looks like weak
            # hardware and is actually this line of configuration.
            log.warning(
                "TTL=%d means messages are not relayed, so only nodes within direct radio "
                "range of the proxy will act on them. That is deliberate in a single-room "
                "network; if distant lights do not respond, raise TTL to at least the "
                "number of hops between the proxy and the furthest node.", s.ttl)
        if s.seq_block < 1:
            raise ConfigError(f"SEQ_BLOCK={s.seq_block} must be at least 1.")
        if s.seq_recovery_jump < 0:
            raise ConfigError(f"SEQ_RECOVERY_JUMP={s.seq_recovery_jump} cannot be negative.")
        if s.mqtt_reconnect_max < 1:
            raise ConfigError(
                f"MQTT_RECONNECT_MAX={s.mqtt_reconnect_max} must be at least 1 second.")
        if s.src_addr is not None and not 0x0001 <= s.src_addr <= 0x7FFF:
            raise ConfigError(
                f"SRC_ADDR=0x{s.src_addr:04X} is not a unicast address "
                "(must be 0x0001-0x7FFF). Leave it unset to derive it from the mesh file.")
        if s.iv_index is not None and not 0 <= s.iv_index <= 0xFFFFFFFF:
            # It goes into a 4-byte nonce on every single message, so an
            # out-of-range value is not a bad setting that degrades the bridge,
            # it is an OverflowError on every send - thrown from inside a BLE
            # callback, where it is swallowed. One line here instead.
            raise ConfigError(
                f"IV_INDEX={s.iv_index} is out of range (0-4294967295). "
                "Leave it unset to take it from the state file and mesh beacons.")
        if s.poll_interval < 0:
            raise ConfigError(
                f"POLL_INTERVAL={s.poll_interval} cannot be negative. "
                "Use 0 to disable polling.")
        if s.health_max_age < 0:
            raise ConfigError(
                f"HEALTH_MAX_AGE={s.health_max_age} cannot be negative. "
                "Use 0 to derive it from POLL_INTERVAL.")
        if s.ble_scan_timeout <= 0:
            raise ConfigError(f"BLE_SCAN_TIMEOUT={s.ble_scan_timeout} must be greater than 0.")
        if s.ble_retry_min <= 0:
            # Zero is the dangerous one: the reconnect backoff starts at this
            # value, so the supervisor would retry with no pause at all and
            # spin a core against a radio that is not coming back.
            raise ConfigError(
                f"BLE_RETRY_MIN={s.ble_retry_min} must be greater than 0, "
                "otherwise reconnecting becomes a busy loop.")
        if s.ble_retry_max < s.ble_retry_min:
            raise ConfigError(
                f"BLE_RETRY_MAX={s.ble_retry_max} is below BLE_RETRY_MIN={s.ble_retry_min}.")
        if not 1 <= s.mqtt_port <= 65535:
            raise ConfigError(f"MQTT_PORT={s.mqtt_port} is out of range (1-65535).")
        if s.mqtt_tls_ca:
            # Checked here rather than left to paho: a missing CA file surfaces
            # from inside the TLS handshake as a FileNotFoundError several
            # frames deep, which is the one failure mode this module exists to
            # prevent. The path is the container's, so a bad bind mount is the
            # usual cause.
            ca = Path(s.mqtt_tls_ca)
            if not ca.exists():
                raise ConfigError(
                    f"MQTT_TLS_CA={s.mqtt_tls_ca} does not exist inside the container. "
                    "Mount the CA certificate in (e.g. `- ./ca.crt:/config/ca.crt:ro`), "
                    "or leave MQTT_TLS_CA unset to use the system trust store.")
            if not ca.is_file():
                raise ConfigError(
                    f"MQTT_TLS_CA={s.mqtt_tls_ca} is a directory, expected a CA certificate file.")
        if s.mqtt_tls and s.mqtt_port == 1883:
            # A warning and not an error: a broker really can be told to serve
            # TLS on 1883, and silently "fixing" the port would connect the
            # bridge somewhere the user never named.
            log.warning(
                "MQTT_TLS is on but MQTT_PORT is still 1883, which is the plain-MQTT port "
                "(TLS is normally 8883). Set MQTT_PORT=8883 unless your broker really "
                "serves TLS on 1883.")
        if not s.mqtt_tls and (s.mqtt_tls_ca or s.mqtt_tls_insecure):
            # Both settings do nothing without MQTT_TLS, and a user who set one
            # of them believes the connection is encrypted. It is not.
            log.warning(
                "MQTT_TLS_CA/MQTT_TLS_INSECURE are set but MQTT_TLS is off, so they are "
                "ignored and the connection to the broker is in the clear. Set MQTT_TLS=true.")
        if s.cmd_rate_limit <= 0:
            raise ConfigError(
                f"CMD_RATE_LIMIT={s.cmd_rate_limit} must be greater than 0, "
                "otherwise no command is ever executed.")
        if s.cmd_queue_max < 1:
            raise ConfigError(f"CMD_QUEUE_MAX={s.cmd_queue_max} must be at least 1.")
        if s.log_level not in _LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL={s.log_level!r} is not a log level "
                f"(use one of: {', '.join(_LOG_LEVELS)}).")
        if not s.base_topic:
            raise ConfigError("BASE_TOPIC is empty.")
        if s.proxy_address and not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", s.proxy_address):
            raise ConfigError(
                f"PROXY_ADDRESS={s.proxy_address!r} is not a BLE address like AA:BB:CC:DD:EE:FF.")
        if s.ble_adapter:
            # Checked here, not at connect time: a typo must be one line at
            # start-up rather than an adapter the bridge quietly never finds.
            # An adapter that is *absent* is a different matter and is handled
            # at runtime, because that is a thing that happens to a correct
            # configuration.
            if not adapter.looks_like_adapter(s.ble_adapter):
                raise ConfigError(
                    f"BLE_ADAPTER={s.ble_adapter!r} is neither an adapter name like hci0 "
                    "nor an adapter MAC address like AA:BB:CC:DD:EE:FF.")
            s.ble_adapter = adapter.normalise(s.ble_adapter)
        return s


# --- loading the .connect document -----------------------------------------

_MOUNT_HINT = ("Mount your Hafele Connect export into /config "
               "(e.g. `- ./mesh.connect:/config/mesh.connect:ro`), "
               "or pass it as MESH_JSON / MESH_JSON_BASE64.")


def load_mesh_document(s: Settings) -> tuple[dict, str]:
    """Return (document, human description of where it came from).

    Priority: MESH_FILE -> MESH_JSON -> MESH_JSON_BASE64 -> autodiscovery in
    MESH_DIR. An explicitly set source is never silently skipped: if MESH_FILE
    points at nothing, that is an error rather than a fallback to scanning.
    """
    if s.mesh_file:
        path = Path(s.mesh_file)
        if path.is_dir():
            return _load_from_dir(path)
        if not path.exists():
            raise ConfigError(f"MESH_FILE={path} does not exist inside the container. {_MOUNT_HINT}")
        return _parse_json(_read_text(path), str(path))

    raw = os.environ.get("MESH_JSON", "").strip()
    if raw:
        return _parse_json(raw, "MESH_JSON")

    b64 = os.environ.get("MESH_JSON_BASE64", "").strip()
    if b64:
        try:
            decoded = base64.b64decode(b64, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as ex:
            raise ConfigError(
                f"MESH_JSON_BASE64 is not valid base64-encoded UTF-8 ({ex}). "
                "Produce it with: base64 -w0 mesh.connect") from None
        return _parse_json(decoded, "MESH_JSON_BASE64")

    return _load_from_dir(Path(s.mesh_dir))


def _load_from_dir(directory: Path) -> tuple[dict, str]:
    if not directory.is_dir():
        raise ConfigError(f"{directory} is not a directory inside the container. {_MOUNT_HINT}")
    found = sorted(p for p in directory.iterdir() if p.suffix == ".connect" and p.is_file())
    if not found:
        listing = ", ".join(sorted(p.name for p in directory.iterdir())) or "(empty)"
        raise ConfigError(
            f"No *.connect file in {directory} (it contains: {listing}). {_MOUNT_HINT}")
    if len(found) > 1:
        names = ", ".join(p.name for p in found)
        raise ConfigError(
            f"{len(found)} *.connect files in {directory} ({names}) - "
            f"set MESH_FILE=/config/<name>.connect to pick one.")
    return _parse_json(_read_text(found[0]), str(found[0]))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError:
        raise ConfigError(f"{path} is not readable (permission denied).") from None
    except UnicodeDecodeError:
        raise ConfigError(
            f"{path} is not UTF-8 text, so it is not a Hafele Connect export.") from None
    except OSError as ex:
        raise ConfigError(f"{path} could not be read: {ex}.") from None


def _parse_json(text: str, origin: str) -> tuple[dict, str]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as ex:
        raise ConfigError(
            f"{origin} is not valid JSON: {ex.msg} at line {ex.lineno} column {ex.colno}. "
            "Re-export the network from the Hafele Connect app.") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{origin} contains JSON {type(doc).__name__}, expected an object.")
    return doc, origin


# --- validating the .connect document --------------------------------------

_HEX32 = re.compile(r"\A[0-9a-fA-F]{32}\Z")


def validate_mesh(doc: dict, origin: str) -> None:
    """Raise ConfigError if `doc` is not a usable Hafele Connect export."""
    missing = [k for k in ("tos_network", "netKeys", "nodes") if k not in doc]
    if missing:
        raise ConfigError(
            f"{origin} is not a Hafele Connect export: missing {', '.join(missing)}. "
            "Export the network from the Connect app (Settings -> Share network), "
            "not a Home Assistant or nRF Mesh backup.")

    if not any(n.get("tos_devices") for n in doc.get("nodes", []) if isinstance(n, dict)):
        raise ConfigError(
            f"{origin} has no tos_devices on any node, so it is not a Hafele Connect export "
            "(or the export was taken before any device was added).")

    net_key = _first_key(doc, "netKeys", origin, "net key")
    app_key = _first_key(doc, "appKeys", origin, "app key")
    _check_key(net_key, "netKeys[0].key", origin)
    _check_key(app_key, "appKeys[0].key", origin)


def _first_key(doc: dict, field_name: str, origin: str, label: str) -> Any:
    keys = doc.get(field_name)
    if not isinstance(keys, list) or not keys:
        raise ConfigError(f"{origin} has no {label} ({field_name} is empty). The export is incomplete.")
    entry = keys[0]
    if not isinstance(entry, dict) or "key" not in entry:
        raise ConfigError(f"{origin}: {field_name}[0] has no 'key' field. The export is incomplete.")
    return entry["key"]


def _check_key(value: Any, where: str, origin: str) -> None:
    if not isinstance(value, str) or not _HEX32.match(value):
        shown = value if isinstance(value, str) else type(value).__name__
        raise ConfigError(
            f"{origin}: {where} must be 32 hex characters (128-bit key), got {shown!r}. "
            "The export is corrupt - re-export it from the Connect app.")


# --- deriving the bridge's own unicast address ------------------------------

@dataclass
class AddressPlan:
    src: int
    reason: str
    occupied: set[int] = field(default_factory=set)
    collision: bool = False


def occupied_unicasts(doc: dict) -> set[int]:
    """Every unicast address already claimed by a node, including its elements.

    A node owns `unicastAddress .. unicastAddress + elementCount - 1`. Only
    looking at `unicastAddress` would happily hand us the second element of a
    six-element box, which is a silent, intermittent breakage.
    """
    used: set[int] = set()
    for node in doc.get("nodes", []):
        if not isinstance(node, dict) or "unicastAddress" not in node:
            continue
        base = as_addr(node["unicastAddress"], "nodes[].unicastAddress")
        count = len(node.get("elements") or []) or 1
        declared = (node.get("tos_node") or {}).get("elementCount")
        if declared not in (None, ""):
            count = max(count, as_int(declared, "nodes[].tos_node.elementCount") or 1)
        used.update(range(base, base + count))
    for prov in doc.get("provisioners", []):
        addr = (prov or {}).get("unicastAddress")
        if addr is not None:
            used.add(as_addr(addr, "provisioners[].unicastAddress"))
    return used


def plan_src_address(doc: dict, override: int | None,
                     avoid: set[int] | frozenset[int] = frozenset()) -> AddressPlan:
    """Choose the unicast address this bridge sends from.

    The Connect app reserves a high address for itself (`provisionerMeshAddress`,
    typically 0x7FF6) and allocates nodes from a low range, so the addresses just
    above the provisioner are the safest place to sit. We still check the whole
    occupied set, because an imported or merged network can break that pattern.

    `avoid` is for the one caller that cannot take just any free address: the
    automatic rotation in state.py, which must never land back on an address
    whose 24-bit counter it has already spent. Those addresses are free as far
    as the mesh is concerned, so they are excluded from the search without being
    reported as occupied.
    """
    used = occupied_unicasts(doc)

    if override is not None:
        plan = AddressPlan(override, "SRC_ADDR override", used, override in used)
        return plan

    taken = used | set(avoid)
    prov_raw = (doc.get("tos_network") or {}).get("provisionerMeshAddress")
    if prov_raw is not None:
        prov = as_addr(prov_raw, "tos_network.provisionerMeshAddress")
        for candidate in range(prov + 1, min(prov + 64, 0x8000)):
            if candidate not in taken:
                return AddressPlan(candidate, f"provisioner 0x{prov:04X} + {candidate - prov}", used)

    for candidate in range(0x7FFF, 0x0000, -1):
        if candidate not in taken:
            return AddressPlan(candidate, "highest free unicast address", used)

    raise ConfigError(
        "No free unicast address in the mesh - every address 0x0001-0x7FFF is taken. "
        "Set SRC_ADDR explicitly.")
