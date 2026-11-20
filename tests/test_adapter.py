"""Which Bluetooth radio the bridge uses, and how it is named.

The point of these is the one thing that is easy to get wrong and impossible to
notice: `hciN` is a boot-order label, not an identity. A configuration naming
`hci1` can silently end up pointing at a different radio after a reboot, and the
only symptom is a bridge that hears nothing.
"""
from __future__ import annotations

import pytest

from app import adapter as adapter_mod
from app.adapter import Adapter, adapters_from_objects, looks_like_adapter, match, normalise
from app.ble import ESCALATE_AFTER, recovery_rung


class Variant:
    """Stands in for dbus-fast's wrapper, which every property arrives in."""

    def __init__(self, value):
        self.value = value


def objects(*entries: tuple[str, str, bool]) -> dict:
    out: dict = {"/org/bluez/hci0/dev_AA_BB": {"org.bluez.Device1": {}}}
    for name, address, powered in entries:
        out[f"/org/bluez/{name}"] = {adapter_mod.ADAPTER_IFACE: {
            "Address": Variant(address), "Powered": Variant(powered)}}
    return out


# --- the two forms BLE_ADAPTER accepts ---------------------------------------

@pytest.mark.parametrize("value", ["hci0", "hci12", "HCI1", "aa:bb:cc:dd:ee:ff",
                                   "C4:7B:2F:91:0D:8E"])
def test_both_forms_are_accepted(value):
    assert looks_like_adapter(value)


@pytest.mark.parametrize("value", ["", "hci", "hci0x", "usb0", "AA:BB:CC:DD:EE",
                                   "AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:GG"])
def test_anything_else_is_rejected(value):
    assert not looks_like_adapter(value)


def test_names_are_lowercased_and_addresses_upper():
    assert normalise(" HCI1 ") == "hci1"
    assert normalise("c4:7b:2f:91:0d:8e") == "C4:7B:2F:91:0D:8E"


# --- reading BlueZ's answer ---------------------------------------------------

def test_adapters_are_read_out_of_an_objectmanager_reply():
    found = adapters_from_objects(objects(("hci0", "AA:AA:AA:AA:AA:01", True)))
    assert found == [Adapter("hci0", "AA:AA:AA:AA:AA:01", True)]


def test_hci2_sorts_before_hci10():
    """Plain string order puts hci10 first, which would change which adapter a
    host with many radios falls back to."""
    found = adapters_from_objects(objects(("hci10", "AA:AA:AA:AA:AA:10", True),
                                          ("hci2", "AA:AA:AA:AA:AA:02", True)))
    assert [a.name for a in found] == ["hci2", "hci10"]


def test_a_host_with_no_radio_gives_an_empty_list():
    assert adapters_from_objects(objects()) == []


# --- matching -----------------------------------------------------------------

ADAPTERS = [Adapter("hci0", "AA:AA:AA:AA:AA:01"), Adapter("hci1", "C4:7B:2F:91:0D:8E")]


def test_an_adapter_is_found_by_name():
    assert match(ADAPTERS, "hci1").address == "C4:7B:2F:91:0D:8E"


def test_an_adapter_is_found_by_address():
    """The whole reason BLE_ADAPTER takes a MAC: this still works after a
    reboot that renumbered the radios."""
    assert match(ADAPTERS, "c4:7b:2f:91:0d:8e").name == "hci1"


def test_an_adapter_that_is_not_there_is_not_invented():
    assert match(ADAPTERS, "hci7") is None
    assert match(ADAPTERS, "FF:FF:FF:FF:FF:FF") is None
    assert match(ADAPTERS, None) is None
    assert match([], "hci0") is None


def test_the_label_names_the_radio_as_well_as_the_slot():
    assert Adapter("hci1", "C4:7B:2F:91:0D:8E").label == "hci1 (C4:7B:2F:91:0D:8E)"
    assert Adapter("hci1", "").label == "hci1"


# --- the recovery ladder ------------------------------------------------------

def test_nothing_happens_until_failures_add_up():
    assert [recovery_rung(n) for n in range(0, ESCALATE_AFTER)] == [0] * ESCALATE_AFTER


def test_each_block_of_failures_buys_the_next_rung():
    assert recovery_rung(3, every=3) == 1       # scan for longer
    assert recovery_rung(6, every=3) == 2       # power-cycle the adapter
    assert recovery_rung(9, every=3) == 3       # look at every adapter again


def test_the_ladder_stops_at_the_top_rather_than_cycling():
    """Rung 3 already re-scans every radio, so repeating the cheaper rungs
    forever would only add log lines."""
    assert [recovery_rung(n, every=3) for n in (12, 15, 300)] == [3, 3, 3]


def test_only_the_failure_that_completes_a_block_counts():
    assert recovery_rung(4, every=3) == 0
    assert recovery_rung(5, every=3) == 0
