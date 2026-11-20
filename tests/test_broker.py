"""MQTT TLS settings and the start-up probe.

The TLS tests are not mocked. A throwaway listener with a self-signed
certificate is started on localhost and `preflight` really connects to it, so
what is under test is the handshake paho performs - which is the only part
that can be got wrong in a way a mock would never notice. Nothing here talks
to a broker that exists outside the test.
"""
from __future__ import annotations

import datetime
import gc
import socket
import ssl
import threading
import warnings

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import broker
from app.config import ConfigError, Settings

# The certificate is issued for this name only. The listener runs on
# 127.0.0.1, so connecting by address is a hostname mismatch - which is
# exactly the case MQTT_TLS_INSECURE exists for.
CERT_NAME = "hafele-test-broker"

# MQTT 3.1.1 CONNACK: session present 0, return code 0 (accepted).
CONNACK_OK = b"\x20\x02\x00\x00"


@pytest.fixture(scope="module")
def tls_cert(tmp_path_factory):
    """A self-signed certificate for CERT_NAME, as (cert_path, key_path).

    An EC key rather than RSA: the whole pair is generated per test session
    and an EC keypair costs nothing to make.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(CERT_NAME)]),
                           critical=False)
            .sign(key, hashes.SHA256()))

    directory = tmp_path_factory.mktemp("tls")
    cert_path = directory / "broker.crt"
    key_path = directory / "broker.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cert_path), str(key_path)


class FakeBroker:
    """Answers one CONNECT with a CONNACK, over TLS or in the clear.

    Enough of a broker for `preflight`, which only asks two questions: does
    the socket come up, and does a CONNACK arrive.
    """

    def __init__(self, cert: tuple[str, str] | None) -> None:
        self._listen = socket.socket()
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(5)
        self.port = self._listen.getsockname()[1]
        if cert is None:
            self._ctx = None
        else:
            self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._ctx.load_cert_chain(cert[0], cert[1])
        self.handshakes = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                raw, _ = self._listen.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(raw,), daemon=True).start()

    def _session(self, raw: socket.socket) -> None:
        conn = raw
        try:
            if self._ctx is not None:
                # Handshake separately so this thread holds the wrapped socket
                # even when the handshake fails. Letting wrap_socket() raise
                # leaves the SSLSocket to the garbage collector, which closes it
                # from under the interpreter at exit and trips the unraisable
                # hook - and pytest.ini turns warnings into errors.
                conn = self._ctx.wrap_socket(raw, server_side=True,
                                             do_handshake_on_connect=False)
                conn.settimeout(5)
                conn.do_handshake()
                self.handshakes += 1
            conn.settimeout(5)
            conn.recv(4096)          # CONNECT
            conn.sendall(CONNACK_OK)
            conn.recv(4096)          # DISCONNECT, or the client going away
        except OSError:
            # A client that walked away mid-handshake is one of the things
            # being tested, so it is not a failure here.
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._listen.close()


@pytest.fixture
def failed_handshake():
    """Dispose of the socket paho drops when a TLS handshake does not complete.

    `_ssl_wrap_socket` calls `do_handshake()` on the wrapped socket and lets it
    raise, at which point nothing but the traceback refers to that socket; it
    is collected later and complains that it was never closed. pytest.ini turns
    warnings into errors, and an unraisable one lands on whichever test happens
    to be running when the collection occurs, so it is swept up here instead.
    Nothing to fix in the bridge: the only caller is `preflight`, and a
    preflight that fails ends the process a moment later.
    """
    yield
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


def settings_for(monkeypatch, port: int, **env) -> Settings:
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", str(port))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env()


# --- what configure() does to a client --------------------------------------

class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def username_pw_set(self, user, password=None) -> None:
        self.calls.append(("username_pw_set", user, password))

    def tls_set(self, **kwargs) -> None:
        self.calls.append(("tls_set", kwargs))

    def tls_insecure_set(self, value) -> None:
        self.calls.append(("tls_insecure_set", value))


def test_configure_without_tls_touches_no_tls_setting(settings):
    client = FakeClient()
    broker.configure(client, settings)
    assert [c[0] for c in client.calls] == []


def test_configure_sets_credentials(monkeypatch, settings):
    monkeypatch.setenv("MQTT_USER", "u")
    monkeypatch.setenv("MQTT_PASS", "p")
    client = FakeClient()
    broker.configure(client, Settings.from_env())
    assert client.calls == [("username_pw_set", "u", "p")]


def test_configure_tls_uses_system_trust_store_without_a_ca(monkeypatch):
    monkeypatch.setenv("MQTT_TLS", "true")
    client = FakeClient()
    broker.configure(client, Settings.from_env())
    # ca_certs=None is how paho is told to load the system trust store.
    assert client.calls == [("tls_set", {"ca_certs": None})]


def test_configure_tls_passes_the_ca_and_insecure_flag(monkeypatch, tls_cert):
    monkeypatch.setenv("MQTT_TLS", "true")
    monkeypatch.setenv("MQTT_TLS_CA", tls_cert[0])
    monkeypatch.setenv("MQTT_TLS_INSECURE", "true")
    client = FakeClient()
    broker.configure(client, Settings.from_env())
    assert client.calls == [("tls_set", {"ca_certs": tls_cert[0]}),
                            ("tls_insecure_set", True)]


# --- the probe against a real listener ---------------------------------------

def test_preflight_plaintext(monkeypatch):
    fake = FakeBroker(None)
    try:
        broker.preflight(settings_for(monkeypatch, fake.port))
    finally:
        fake.close()


def test_preflight_tls_with_the_ca_and_a_matching_name(monkeypatch, tls_cert):
    """The whole TLS path, end to end, against a certificate we just made."""
    fake = FakeBroker(tls_cert)
    try:
        monkeypatch.setenv("MQTT_TLS", "true")
        monkeypatch.setenv("MQTT_TLS_CA", tls_cert[0])
        s = Settings.from_env()
        # The certificate names CERT_NAME, so the probe has to ask for that
        # name; the socket still goes to the loopback listener.
        s.mqtt_host = CERT_NAME
        s.mqtt_port = fake.port
        monkeypatch.setattr(broker.socket, "getaddrinfo",
                            lambda host, port, *a, **k: [
                                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                 ("127.0.0.1", port))])
        broker.preflight(s)
        assert fake.handshakes == 1
    finally:
        fake.close()


def test_preflight_tls_rejects_an_untrusted_certificate(monkeypatch, tls_cert, failed_handshake):
    """No CA given, so the self-signed certificate is not in any trust store."""
    fake = FakeBroker(tls_cert)
    try:
        s = settings_for(monkeypatch, fake.port, MQTT_TLS="true")
        with pytest.raises(ConfigError) as ex:
            broker.preflight(s)
        assert "TLS handshake" in str(ex.value)
        assert "MQTT_TLS_CA" in str(ex.value)
    finally:
        fake.close()


def test_preflight_tls_rejects_a_name_mismatch(monkeypatch, tls_cert, failed_handshake):
    """Trusted certificate, wrong name: this is what MQTT_TLS_INSECURE is for."""
    fake = FakeBroker(tls_cert)
    try:
        s = settings_for(monkeypatch, fake.port, MQTT_TLS="true", MQTT_TLS_CA=tls_cert[0])
        with pytest.raises(ConfigError) as ex:
            broker.preflight(s)
        assert "MQTT_TLS_INSECURE" in str(ex.value)
    finally:
        fake.close()


def test_preflight_tls_insecure_accepts_a_name_mismatch(monkeypatch, tls_cert):
    fake = FakeBroker(tls_cert)
    try:
        broker.preflight(settings_for(monkeypatch, fake.port, MQTT_TLS="true",
                                      MQTT_TLS_CA=tls_cert[0], MQTT_TLS_INSECURE="true"))
        assert fake.handshakes == 1
    finally:
        fake.close()


def test_preflight_tls_against_a_plaintext_listener(monkeypatch, failed_handshake):
    """TLS on where the broker speaks none: one line, naming the real cause.

    The two sides deadlock rather than fail, so this comes out of the connect
    timeout and not the TLS layer - which is why the timeout message has to
    know about MQTT_TLS.
    """
    fake = FakeBroker(None)
    try:
        s = settings_for(monkeypatch, fake.port, MQTT_TLS="true")
        with pytest.raises(ConfigError) as ex:
            broker.preflight(s)
        assert "MQTT_TLS=false" in str(ex.value)
    finally:
        fake.close()
