"""MQTT start-up probe.

The point of probing before the bridge starts is to tell the two failures that
look identical in a log apart: "I cannot reach your broker" and "your broker
told me to go away". Both normally show up as an endless reconnect loop, which
tells the user nothing.
"""
from __future__ import annotations

import socket
import ssl
import threading
from typing import Any

import paho.mqtt.client as mqtt

from .config import ConfigError, Settings

# CONNACK codes that mean "your credentials are wrong", across MQTT 3.1.1
# (4 = bad user name or password, 5 = not authorized) and MQTT 5
# (0x86 = bad user name or password, 0x87 = not authorized).
_AUTH_CODES = {4, 5, 0x86, 0x87}
_AUTH_WORDS = ("authoriz", "authentic", "user name", "password", "credential")


def _is_auth_failure(reason: Any) -> bool:
    value = getattr(reason, "value", reason)
    if isinstance(value, int) and value in _AUTH_CODES:
        return True
    text = str(reason).lower()
    return any(w in text for w in _AUTH_WORDS)


def _is_failure(reason: Any) -> bool:
    flag = getattr(reason, "is_failure", None)
    if isinstance(flag, bool):
        return flag
    value = getattr(reason, "value", reason)
    return not (isinstance(value, int) and value == 0)


def configure(client: mqtt.Client, s: Settings) -> None:
    """Apply credentials and TLS to a client. Used for every client we make.

    Both the probe below and the bridge's long-lived client go through here on
    purpose. A probe that connected differently from the real client would
    report on a connection nobody else makes: if the probe spoke plaintext to a
    TLS listener it would hang until the CONNACK timeout and blame the port,
    and if it spoke TLS where the bridge does not it would pass and leave the
    bridge in a reconnect loop.
    """
    if s.mqtt_user:
        client.username_pw_set(s.mqtt_user, s.mqtt_pass)
    if not s.mqtt_tls:
        return
    # ca_certs=None means paho loads the system trust store, which is what a
    # publicly-signed broker certificate needs; a private CA is named instead.
    client.tls_set(ca_certs=s.mqtt_tls_ca or None)
    if s.mqtt_tls_insecure:
        client.tls_insecure_set(True)


def preflight(s: Settings) -> None:
    """Connect once, synchronously. Raises ConfigError with one readable line."""
    result: dict[str, Any] = {}
    done = threading.Event()

    def on_connect(_client, _userdata, _flags, reason_code, _props=None) -> None:
        result["rc"] = reason_code
        done.set()

    probe = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{s.mqtt_client_id}-probe")
    configure(probe, s)
    probe.on_connect = on_connect

    where = f"{s.mqtt_host}:{s.mqtt_port}"
    try:
        probe.connect(s.mqtt_host, s.mqtt_port, keepalive=15)
    except socket.gaierror:
        raise ConfigError(
            f"MQTT_HOST={s.mqtt_host!r} does not resolve. Use an IP address, or the broker's "
            "hostname as the container sees it (with network_mode: host that is the host's "
            "own resolver).") from None
    except ConnectionRefusedError:
        raise ConfigError(
            f"Nothing is listening on {where}. Check MQTT_HOST/MQTT_PORT and that the broker "
            "is running.") from None
    except (TimeoutError, socket.timeout):
        if s.mqtt_tls:
            # A TLS client and a plain-MQTT listener deadlock: we wait for a
            # ServerHello, the broker waits for a CONNECT, and neither says
            # anything. Without this it reads as a firewall problem and the
            # user goes looking on the wrong machine.
            raise ConfigError(
                f"Timed out connecting to {where} with MQTT_TLS on. The host is up but sent "
                "nothing back, which is what a plain-MQTT listener does when it is handed a "
                "TLS handshake. Check that this port really is the TLS one (usually 8883), "
                "or set MQTT_TLS=false.") from None
        raise ConfigError(
            f"Timed out connecting to {where}. The host is up but not answering on that port - "
            "usually a firewall or the wrong port.") from None
    except ssl.SSLError as ex:
        # An OSError subclass, so this has to come first. Worth its own line:
        # a rejected certificate is a configuration answer, not a network one,
        # and the generic "cannot reach the broker" below reads as the latter.
        raise ConfigError(
            f"The TLS handshake with {where} failed: {ex}. "
            "If the broker uses a private CA, point MQTT_TLS_CA at its certificate; if it "
            "uses a self-signed certificate, MQTT_TLS_INSECURE=true skips the check (and "
            "with it the protection against an impostor). If that port is not a TLS "
            "listener at all, set MQTT_TLS=false.") from None
    except OSError as ex:
        raise ConfigError(f"Cannot reach the MQTT broker at {where}: {ex}.") from None

    probe.loop_start()
    answered = done.wait(15)
    try:
        probe.disconnect()
    finally:
        probe.loop_stop()

    if not answered:
        if s.mqtt_tls:
            raise ConfigError(
                f"Connected to {where} over TLS but the broker sent no CONNACK within 15s. "
                "The handshake succeeded, so the certificate is fine and the broker is simply "
                "not answering MQTT on that port.")
        raise ConfigError(
            f"Connected to {where} but the broker sent no CONNACK within 15s. "
            "Is that port really a plain-MQTT listener? A TLS listener behaves exactly like "
            "this, and the bridge is not using TLS - try MQTT_TLS=true (port 8883).")

    rc = result["rc"]
    if _is_auth_failure(rc):
        who = f"user {s.mqtt_user!r}" if s.mqtt_user else "an anonymous client"
        raise ConfigError(
            f"The MQTT broker at {where} rejected {who}: {rc}. "
            "Check MQTT_USER/MQTT_PASS, or allow anonymous access.")
    if _is_failure(rc):
        raise ConfigError(f"The MQTT broker at {where} refused the connection: {rc}.")
