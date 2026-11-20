"""Which Bluetooth radio the bridge talks through, and how to find it again.

A host can have more than one adapter, and on a Raspberry Pi with a USB dongle
the two are not interchangeable: the onboard radio can be deaf to a mesh that
the dongle hears perfectly well, with nothing in any log to say so. The bridge
therefore has to be able to name an adapter, notice when the one it was told to
use is not there, and pick one that can actually see the network.

`hciN` is not a stable name. The numbers are handed out in probe order at boot,
so unplugging a dongle, or a reboot that enumerates USB in a different order,
silently moves `hci1` onto another radio. An adapter's MAC address does not
move, so `BLE_ADAPTER` accepts either form and this module resolves it against
what BlueZ reports now.

Everything that talks to D-Bus lives here; everything that parses its answers is
a plain function, so the parsing is tested without a bus.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("ble")

ADAPTER_IFACE = "org.bluez.Adapter1"

_HCI_NAME = re.compile(r"\Ahci\d+\Z", re.IGNORECASE)
_MAC = re.compile(r"\A[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\Z")


class BlueZUnavailable(RuntimeError):
    """BlueZ could not be asked at all (no bus, no service, no permission)."""


@dataclass(frozen=True)
class Adapter:
    name: str                 # "hci1"
    address: str              # "AA:BB:CC:DD:EE:FF", stable across reboots
    powered: bool = True

    @property
    def label(self) -> str:
        """What goes in a log line and in the Home Assistant sensor."""
        return f"{self.name} ({self.address})" if self.address else self.name


def looks_like_adapter(value: str) -> bool:
    """True for the two forms BLE_ADAPTER accepts: `hciN` or a MAC address."""
    return bool(_HCI_NAME.match(value) or _MAC.match(value))


def normalise(value: str) -> str:
    """Canonical spelling of a BLE_ADAPTER value: `hci1`, `AA:BB:CC:DD:EE:FF`."""
    value = value.strip()
    return value.lower() if _HCI_NAME.match(value) else value.upper()


def _plain(value):
    """dbus-fast hands every property back wrapped in a Variant."""
    return getattr(value, "value", value)


def _sort_key(name: str) -> tuple[int, str]:
    """hci2 before hci10, which plain string order gets wrong."""
    digits = name[3:]
    return (int(digits), "") if digits.isdigit() else (1 << 30, name)


def adapters_from_objects(objects: dict) -> list[Adapter]:
    """The adapters in an ObjectManager.GetManagedObjects() reply, in hci order."""
    found: list[Adapter] = []
    for path, interfaces in objects.items():
        props = interfaces.get(ADAPTER_IFACE)
        if props is None:
            continue
        name = path.rsplit("/", 1)[-1]
        if not name:
            continue
        found.append(Adapter(name=name,
                             address=str(_plain(props.get("Address")) or ""),
                             powered=bool(_plain(props.get("Powered")))))
    found.sort(key=lambda a: _sort_key(a.name))
    return found


def match(adapters: list[Adapter], wanted: str | None) -> Adapter | None:
    """Find `wanted` - an `hciN` name or a MAC - among `adapters`.

    Matching on both forms is the whole point: the configuration may name the
    radio by the number it had last boot or by the address it will always have,
    and only one of those two is still true after a reboot.
    """
    if not wanted:
        return None
    want = normalise(wanted)
    for a in adapters:
        if a.name.lower() == want.lower() or (a.address and a.address.upper() == want):
            return a
    return None


# --- D-Bus ------------------------------------------------------------------

async def _system_bus():
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType
    except ImportError:  # pragma: no cover - dbus-fast is a hard dependency
        raise BlueZUnavailable("dbus-fast is not installed, so BlueZ cannot be asked "
                               "which Bluetooth adapters exist.") from None
    try:
        return await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as ex:  # noqa: BLE001 - the bus fails in a wide family of ways
        raise BlueZUnavailable(f"cannot reach the system D-Bus ({ex})") from None


async def list_adapters() -> list[Adapter]:
    """Ask BlueZ for every adapter it has. Raises BlueZUnavailable if it cannot be asked.

    An empty list is a real answer, and a different one from "the bus is not
    there": it means the host has no Bluetooth radio at all right now, which is
    exactly what happens when the USB dongle has been pulled.
    """
    bus = await _system_bus()
    try:
        root = bus.get_proxy_object("org.bluez", "/", await bus.introspect("org.bluez", "/"))
        manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
        return adapters_from_objects(await manager.call_get_managed_objects())
    except BlueZUnavailable:
        raise
    except Exception as ex:  # noqa: BLE001
        raise BlueZUnavailable(f"BlueZ did not answer on the system D-Bus ({ex})") from None
    finally:
        bus.disconnect()


async def power_cycle(name: str, settle: float = 2.0) -> None:
    """Turn an adapter off and on again through BlueZ's `Powered` property.

    This is the software equivalent of re-plugging the dongle: it makes BlueZ
    tear the HCI state down and bring it back up, which clears the kind of
    wedged controller that answers D-Bus calls but reports no advertisements.
    Raises if BlueZ refuses, so the caller can say so rather than assume it
    worked.
    """
    bus = await _system_bus()
    try:
        path = f"/org/bluez/{name}"
        obj = bus.get_proxy_object("org.bluez", path, await bus.introspect("org.bluez", path))
        iface = obj.get_interface(ADAPTER_IFACE)
        await iface.set_powered(False)
        await asyncio.sleep(settle)
        await iface.set_powered(True)
        await asyncio.sleep(settle)
    except Exception as ex:  # noqa: BLE001
        raise BlueZUnavailable(f"BlueZ would not power-cycle {name} ({ex})") from None
    finally:
        bus.disconnect()
