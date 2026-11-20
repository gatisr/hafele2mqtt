"""Mesh file loading, validation error paths and source address planning."""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest

from app.config import (ConfigError, Settings, as_addr, load_mesh_document,
                        occupied_unicasts, plan_src_address, validate_mesh)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.connect"


def write(tmp_path, name: str, doc) -> str:
    p = tmp_path / name
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return str(p)


# --- source selection: all three ways of handing over the file --------------

def test_mesh_file_explicit(monkeypatch):
    monkeypatch.setenv("MESH_FILE", str(FIXTURE))
    doc, origin = load_mesh_document(Settings.from_env())
    assert doc["meshName"] == "Test Network"
    assert origin == str(FIXTURE)


def test_mesh_json_env(monkeypatch):
    monkeypatch.setenv("MESH_JSON", FIXTURE.read_text())
    doc, origin = load_mesh_document(Settings.from_env())
    assert doc["meshName"] == "Test Network"
    assert origin == "MESH_JSON"


def test_mesh_json_base64_env(monkeypatch):
    monkeypatch.setenv("MESH_JSON_BASE64",
                       base64.b64encode(FIXTURE.read_bytes()).decode())
    doc, origin = load_mesh_document(Settings.from_env())
    assert doc["meshName"] == "Test Network"
    assert origin == "MESH_JSON_BASE64"


def test_mesh_json_base64_rejects_garbage(monkeypatch):
    monkeypatch.setenv("MESH_JSON_BASE64", "this is not base64!!")
    with pytest.raises(ConfigError, match="not valid base64"):
        load_mesh_document(Settings.from_env())


def test_autodiscovery_finds_any_connect_name(monkeypatch, tmp_path):
    """The user must not have to rename their export to mesh.connect."""
    (tmp_path / "kitchen-2026.connect").write_text(FIXTURE.read_text())
    (tmp_path / "README.txt").write_text("ignore me")
    monkeypatch.setenv("MESH_DIR", str(tmp_path))
    doc, origin = load_mesh_document(Settings.from_env())
    assert doc["meshName"] == "Test Network"
    assert origin.endswith("kitchen-2026.connect")


def test_autodiscovery_refuses_to_guess_between_two(monkeypatch, tmp_path):
    (tmp_path / "a.connect").write_text("{}")
    (tmp_path / "b.connect").write_text("{}")
    monkeypatch.setenv("MESH_DIR", str(tmp_path))
    with pytest.raises(ConfigError) as ex:
        load_mesh_document(Settings.from_env())
    assert "a.connect" in str(ex.value) and "b.connect" in str(ex.value)
    assert "MESH_FILE" in str(ex.value)


def test_mesh_file_may_be_a_directory(monkeypatch, tmp_path):
    (tmp_path / "only.connect").write_text(FIXTURE.read_text())
    monkeypatch.setenv("MESH_FILE", str(tmp_path))
    doc, _ = load_mesh_document(Settings.from_env())
    assert doc["meshName"] == "Test Network"


def test_empty_config_dir_explains_the_mount(monkeypatch, tmp_path):
    monkeypatch.setenv("MESH_DIR", str(tmp_path))
    with pytest.raises(ConfigError) as ex:
        load_mesh_document(Settings.from_env())
    assert "No *.connect file" in str(ex.value)
    assert "/config" in str(ex.value)


def test_missing_mesh_file_is_not_silently_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("MESH_FILE", str(tmp_path / "nope.connect"))
    monkeypatch.setenv("MESH_JSON", FIXTURE.read_text())
    with pytest.raises(ConfigError, match="does not exist"):
        load_mesh_document(Settings.from_env())


def test_broken_json_reports_position(monkeypatch, tmp_path):
    monkeypatch.setenv("MESH_FILE", write(tmp_path, "x.connect", '{"a": 1,,}'))
    with pytest.raises(ConfigError) as ex:
        load_mesh_document(Settings.from_env())
    assert "not valid JSON" in str(ex.value) and "line 1" in str(ex.value)


def test_json_array_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MESH_FILE", write(tmp_path, "x.connect", "[1,2,3]"))
    with pytest.raises(ConfigError, match="expected an object"):
        load_mesh_document(Settings.from_env())


# --- document validation ----------------------------------------------------

def test_valid_document_passes(doc):
    validate_mesh(doc, "fixture")


def test_rejects_foreign_backup():
    with pytest.raises(ConfigError) as ex:
        validate_mesh({"version": "1.0.0", "nodes": []}, "backup.json")
    assert "not a Hafele Connect export" in str(ex.value)
    assert "tos_network" in str(ex.value)


def test_rejects_export_without_devices(doc):
    for node in doc["nodes"]:
        node["tos_devices"] = []
    with pytest.raises(ConfigError, match="no tos_devices"):
        validate_mesh(doc, "fixture")


def test_rejects_missing_app_key(doc):
    doc["appKeys"] = []
    with pytest.raises(ConfigError, match="no app key"):
        validate_mesh(doc, "fixture")


@pytest.mark.parametrize("bad", ["", "deadbeef", "0" * 31, "0" * 33, "zz" * 16, 12345])
def test_rejects_malformed_net_key(doc, bad):
    doc["netKeys"][0]["key"] = bad
    with pytest.raises(ConfigError, match="32 hex characters"):
        validate_mesh(doc, "fixture")


def test_rejects_malformed_app_key(doc):
    doc["appKeys"][0]["key"] = "not-a-key"
    with pytest.raises(ConfigError) as ex:
        validate_mesh(doc, "fixture")
    assert "appKeys[0].key" in str(ex.value)


# --- source address planning -------------------------------------------------

def test_occupied_covers_every_element_not_just_the_base(doc):
    used = occupied_unicasts(doc)
    # Box 1 is a four-element node at 0x0010.
    assert {0x10, 0x11, 0x12, 0x13} <= used
    # The eight-element remote at 0x0030 owns 0x30..0x37.
    assert {0x30, 0x37} <= used
    assert 0x38 not in used


def test_auto_src_sits_just_above_the_provisioner(doc):
    plan = plan_src_address(doc, None)
    assert plan.src == 0x7FF7            # provisionerMeshAddress 0x7FF6 + 1
    assert plan.collision is False
    assert "provisioner" in plan.reason


def test_auto_src_skips_an_occupied_candidate(doc):
    doc["tos_network"]["provisionerMeshAddress"] = "000F"
    plan = plan_src_address(doc, None)
    # 0x0010..0x0013 belong to Box 1, so the first free one is 0x0014.
    assert plan.src == 0x14
    assert plan.collision is False


def test_auto_src_without_a_provisioner_takes_the_top(doc):
    del doc["tos_network"]["provisionerMeshAddress"]
    plan = plan_src_address(doc, None)
    assert plan.src == 0x7FFF


def test_addresses_to_avoid_are_skipped_without_being_called_occupied(doc):
    """Rotation's extra ban list: free in the mesh, but burnt for this bridge.

    An address whose counter this bridge has already spent is not occupied -
    nothing is provisioned there - so it must not be reported as such, or the
    collision warning would start lying. It still cannot be handed out.
    """
    plan = plan_src_address(doc, None, avoid={0x7FF7, 0x7FF8})
    assert plan.src == 0x7FF9
    assert 0x7FF7 not in plan.occupied
    assert plan.collision is False


def test_a_mesh_with_nothing_left_to_offer_says_so_once(doc):
    """The error rotation relies on, and the one a human reads when it is stuck."""
    with pytest.raises(ConfigError, match="No free unicast address in the mesh"):
        plan_src_address(doc, None, avoid=set(range(0x0001, 0x8000)))


def test_an_explicit_address_ignores_the_avoid_list(doc):
    # Rotation never passes an override, so this only covers a human insisting.
    assert plan_src_address(doc, 0x7FF7, avoid={0x7FF7}).src == 0x7FF7


def test_override_is_honoured_and_collision_is_flagged(doc):
    assert plan_src_address(doc, 0x0100).collision is False
    # 0x0011 is Light 2: using it would make the mesh mix us up with a real node.
    assert plan_src_address(doc, 0x0011).collision is True


def test_src_addr_env_must_be_unicast(monkeypatch):
    monkeypatch.setenv("SRC_ADDR", "0xC002")
    with pytest.raises(ConfigError, match="not a unicast address"):
        Settings.from_env()


# --- environment parsing ------------------------------------------------------

def test_defaults_are_the_documented_ones(settings):
    assert settings.mesh_dir == "/config"
    assert settings.src_addr is None and settings.iv_index is None
    assert settings.ttl == 5 and settings.seq_block == 500
    assert settings.base_topic == "hafele"
    assert settings.availability_topic == "hafele/status"
    assert settings.discovery_prefix == "homeassistant"
    assert settings.state_file == "/data/state.json"


def test_src_and_iv_accept_auto(monkeypatch):
    monkeypatch.setenv("SRC_ADDR", "auto")
    monkeypatch.setenv("IV_INDEX", "AUTO")
    s = Settings.from_env()
    assert s.src_addr is None and s.iv_index is None


def test_numeric_env_errors_name_the_variable(monkeypatch):
    monkeypatch.setenv("TTL", "loud")
    with pytest.raises(ConfigError, match="TTL='loud'"):
        Settings.from_env()


def test_a_bad_recovery_jump_is_a_config_error_not_a_traceback(monkeypatch):
    # It used to be read at import time, so a typo here crashed the process
    # before any of the friendly configuration checks had run.
    monkeypatch.setenv("SEQ_RECOVERY_JUMP", "lots")
    with pytest.raises(ConfigError, match="SEQ_RECOVERY_JUMP='lots'"):
        Settings.from_env()
    monkeypatch.setenv("SEQ_RECOVERY_JUMP", "-1")
    with pytest.raises(ConfigError, match="cannot be negative"):
        Settings.from_env()
    monkeypatch.setenv("SEQ_RECOVERY_JUMP", "250000")
    assert Settings.from_env().seq_recovery_jump == 250000


def test_proxy_address_must_look_like_a_ble_address(monkeypatch):
    monkeypatch.setenv("PROXY_ADDRESS", "hci0")
    with pytest.raises(ConfigError, match="AA:BB:CC:DD:EE:FF"):
        Settings.from_env()
    monkeypatch.setenv("PROXY_ADDRESS", "AA:BB:CC:DD:EE:FF")
    assert Settings.from_env().proxy_address == "AA:BB:CC:DD:EE:FF"


def test_base_topic_slashes_are_trimmed(monkeypatch):
    monkeypatch.setenv("BASE_TOPIC", "/hafele/")
    assert Settings.from_env().base_topic == "hafele"


def test_as_addr_handles_both_encodings():
    assert as_addr("7FF6", "x") == 0x7FF6
    assert as_addr(0x7FF6, "x") == 0x7FF6
    with pytest.raises(ConfigError):
        as_addr("zz", "x")


def test_the_mqtt_reconnect_ceiling_is_its_own_setting(monkeypatch):
    """It used to be BLE_RETRY_MAX, which is about a radio, not a broker.

    The two failures have nothing in common: a proxy out of range is worth
    backing off hard, while a broker that went away for a restart should be
    picked up again promptly. Tuning one silently moved the other.
    """
    monkeypatch.setenv("BLE_RETRY_MAX", "300")
    s = Settings.from_env()
    assert s.ble_retry_max == 300
    assert s.mqtt_reconnect_max == 60.0

    monkeypatch.setenv("MQTT_RECONNECT_MAX", "5")
    s = Settings.from_env()
    assert s.mqtt_reconnect_max == 5
    assert s.ble_retry_max == 300


def test_a_reconnect_ceiling_below_a_second_is_rejected(monkeypatch):
    monkeypatch.setenv("MQTT_RECONNECT_MAX", "0")
    with pytest.raises(ConfigError, match="MQTT_RECONNECT_MAX"):
        Settings.from_env()
    monkeypatch.setenv("MQTT_RECONNECT_MAX", "often")
    with pytest.raises(ConfigError, match="MQTT_RECONNECT_MAX"):
        Settings.from_env()


def test_an_out_of_range_iv_index_is_refused_at_start(monkeypatch):
    """Otherwise it is an OverflowError on every send, from inside a callback.

    The IV index goes into a four-byte field of every nonce the bridge builds,
    so a number that does not fit breaks each message individually rather than
    the start-up once - and it breaks it where bleak swallows the exception.
    """
    monkeypatch.setenv("IV_INDEX", "-1")
    with pytest.raises(ConfigError, match="IV_INDEX"):
        Settings.from_env()
    monkeypatch.setenv("IV_INDEX", "4294967296")
    with pytest.raises(ConfigError, match="out of range"):
        Settings.from_env()
    monkeypatch.setenv("IV_INDEX", "4294967295")
    assert Settings.from_env().iv_index == 0xFFFFFFFF
    monkeypatch.setenv("IV_INDEX", "0")
    assert Settings.from_env().iv_index == 0


def test_poll_interval_zero_disables_polling_and_negative_is_refused(monkeypatch):
    # 0 is the documented way to turn polling off, so it has to stay valid; a
    # negative value is the typo that would otherwise mean the same thing by
    # accident and never be mentioned.
    monkeypatch.setenv("POLL_INTERVAL", "0")
    assert Settings.from_env().poll_interval == 0
    monkeypatch.setenv("POLL_INTERVAL", "-5")
    with pytest.raises(ConfigError, match="POLL_INTERVAL"):
        Settings.from_env()


def test_a_zero_ble_retry_floor_is_refused(monkeypatch):
    # The reconnect backoff starts at this value, so zero is a busy loop
    # against a radio that is not coming back, not a fast retry.
    monkeypatch.setenv("BLE_RETRY_MIN", "0")
    with pytest.raises(ConfigError, match="busy loop"):
        Settings.from_env()
    monkeypatch.setenv("BLE_RETRY_MIN", "0.5")
    assert Settings.from_env().ble_retry_min == 0.5


def test_a_retry_ceiling_below_the_floor_is_refused(monkeypatch):
    monkeypatch.setenv("BLE_RETRY_MIN", "30")
    monkeypatch.setenv("BLE_RETRY_MAX", "10")
    with pytest.raises(ConfigError, match="BLE_RETRY_MAX"):
        Settings.from_env()


def test_a_zero_scan_timeout_is_refused(monkeypatch):
    monkeypatch.setenv("BLE_SCAN_TIMEOUT", "0")
    with pytest.raises(ConfigError, match="BLE_SCAN_TIMEOUT"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["hci", "hcione", "0", "usb0", "hci0:hci1",
                                   "AA:BB:CC:DD:EE", "AA-BB-CC-DD-EE-FF",
                                   "AA:BB:CC:DD:EE:GG"])
def test_a_bad_ble_adapter_is_refused_with_both_forms_spelled_out(monkeypatch, value):
    """The setting most likely to be typed from memory, so the one most likely wrong.

    It is the escape hatch for a host with two radios, reached for by someone
    who has just worked out that the onboard adapter is deaf to the mesh.
    A value the bridge cannot match has to stop it at start-up, showing both
    accepted forms; the alternative is a bridge that silently ignores the
    setting and runs on the radio the setting existed to avoid.
    """
    monkeypatch.setenv("BLE_ADAPTER", value)
    with pytest.raises(ConfigError) as ex:
        Settings.from_env()
    assert "BLE_ADAPTER" in str(ex.value)
    assert "hci0" in str(ex.value) and "AA:BB:CC:DD:EE:FF" in str(ex.value)


@pytest.mark.parametrize("given, stored", [
    ("hci0", "hci0"),
    ("HCI1", "hci1"),
    ("hci10", "hci10"),
    (" hci0 ", "hci0"),
    ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
    ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"),
])
def test_both_adapter_forms_are_accepted_and_stored_one_way(monkeypatch, given, stored):
    """Adapters are matched by string, so the spelling has to be settled here.

    BlueZ reports names lower-case and addresses upper-case. A value typed the
    other way round would match no adapter and be indistinguishable, in every
    log line that follows, from a dongle someone had unplugged.
    """
    monkeypatch.setenv("BLE_ADAPTER", given)
    assert Settings.from_env().ble_adapter == stored


def test_an_unset_ble_adapter_leaves_the_choice_to_the_bridge(settings):
    """The default has to stay falsy: it is what every survey branches on."""
    assert not settings.ble_adapter


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_an_impossible_mqtt_port_is_refused(monkeypatch, port):
    monkeypatch.setenv("MQTT_PORT", port)
    with pytest.raises(ConfigError, match="MQTT_PORT"):
        Settings.from_env()


def test_a_bad_log_level_is_one_line_listing_the_real_ones(monkeypatch):
    """It used to reach logging.setLevel(), which raises a bare ValueError.

    That escaped the ConfigError handler in main() and came out as a traceback
    saying "Unknown level", with no hint of which variable or what to put in it.
    """
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    with pytest.raises(ConfigError) as ex:
        Settings.from_env()
    assert "LOG_LEVEL" in str(ex.value)
    assert "DEBUG" in str(ex.value) and "WARNING" in str(ex.value)


def test_log_levels_are_accepted_in_any_case(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings.from_env().log_level == "DEBUG"


@pytest.mark.parametrize("declared", ["many", None, [4], "", "0x04"])
def test_an_unreadable_element_count_is_one_line_not_a_traceback(doc, declared):
    """A hand-edited export should not end in a stack trace.

    The rest of the loader turns bad input into a single ConfigError naming the
    field; int() here bypassed all of that.
    """
    doc["nodes"][0]["tos_node"]["elementCount"] = declared
    if declared in (None, ""):
        occupied_unicasts(doc)                      # absent, not wrong: fall back
        return
    with pytest.raises(ConfigError, match="elementCount"):
        occupied_unicasts(doc)


# --- MQTT TLS ----------------------------------------------------------------

def test_tls_is_off_and_invisible_by_default(settings):
    """Every deployment that exists today has to keep working untouched."""
    assert settings.mqtt_tls is False
    assert settings.mqtt_tls_ca == ""
    assert settings.mqtt_tls_insecure is False


def test_tls_settings_are_read(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("not parsed here, only opened by paho later")
    monkeypatch.setenv("MQTT_TLS", "true")
    monkeypatch.setenv("MQTT_TLS_CA", str(ca))
    monkeypatch.setenv("MQTT_TLS_INSECURE", "yes")
    monkeypatch.setenv("MQTT_PORT", "8883")
    s = Settings.from_env()
    assert s.mqtt_tls is True
    assert s.mqtt_tls_ca == str(ca)
    assert s.mqtt_tls_insecure is True


def test_a_missing_ca_file_is_caught_here_not_inside_paho(monkeypatch, tmp_path):
    """Otherwise it surfaces as a FileNotFoundError from ssl, with no variable named."""
    monkeypatch.setenv("MQTT_TLS", "true")
    monkeypatch.setenv("MQTT_TLS_CA", str(tmp_path / "absent.crt"))
    with pytest.raises(ConfigError) as ex:
        Settings.from_env()
    assert "MQTT_TLS_CA" in str(ex.value)
    assert "does not exist" in str(ex.value)


def test_a_ca_that_is_a_directory_is_refused(monkeypatch, tmp_path):
    """The usual mistake: the mount point was given instead of the file in it."""
    monkeypatch.setenv("MQTT_TLS", "true")
    monkeypatch.setenv("MQTT_TLS_CA", str(tmp_path))
    with pytest.raises(ConfigError, match="directory"):
        Settings.from_env()


def test_tls_on_the_plaintext_port_warns_but_starts(monkeypatch, caplog):
    """A warning, not an error: a broker may genuinely serve TLS on 1883."""
    monkeypatch.setenv("MQTT_TLS", "true")
    monkeypatch.setenv("MQTT_PORT", "1883")
    with caplog.at_level(logging.WARNING, logger="config"):
        s = Settings.from_env()
    assert s.mqtt_port == 1883
    assert "8883" in caplog.text


def test_tls_options_without_tls_warn_that_they_do_nothing(monkeypatch, tmp_path, caplog):
    """Set the CA, forget MQTT_TLS, and the link is in the clear with no sign of it."""
    ca = tmp_path / "ca.crt"
    ca.write_text("x")
    monkeypatch.setenv("MQTT_TLS_CA", str(ca))
    with caplog.at_level(logging.WARNING, logger="config"):
        Settings.from_env()
    assert "MQTT_TLS" in caplog.text
    assert "in the clear" in caplog.text


# --- the command queue -------------------------------------------------------

def test_command_limits_default_well_above_normal_use(settings):
    assert settings.cmd_rate_limit == 10.0
    assert settings.cmd_queue_max == 64


def test_command_limits_are_read(monkeypatch):
    monkeypatch.setenv("CMD_RATE_LIMIT", "2.5")
    monkeypatch.setenv("CMD_QUEUE_MAX", "8")
    s = Settings.from_env()
    assert s.cmd_rate_limit == 2.5
    assert s.cmd_queue_max == 8


@pytest.mark.parametrize("rate", ["0", "-1"])
def test_a_rate_limit_of_zero_is_refused(monkeypatch, rate):
    # Not a way of disabling commands: it is a bridge that accepts them and
    # never sends one, which looks exactly like broken hardware.
    monkeypatch.setenv("CMD_RATE_LIMIT", rate)
    with pytest.raises(ConfigError, match="CMD_RATE_LIMIT"):
        Settings.from_env()


@pytest.mark.parametrize("size", ["0", "-5"])
def test_an_empty_command_queue_is_refused(monkeypatch, size):
    monkeypatch.setenv("CMD_QUEUE_MAX", size)
    with pytest.raises(ConfigError, match="CMD_QUEUE_MAX"):
        Settings.from_env()
