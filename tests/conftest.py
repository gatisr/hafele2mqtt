from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURE = Path(__file__).parent / "fixtures" / "sample.connect"

# Every environment variable the bridge reads, so a stray value in the shell
# cannot change a test result.
BRIDGE_ENV = [
    "MESH_FILE", "MESH_DIR", "MESH_JSON", "MESH_JSON_BASE64", "ENTITIES_FILE",
    "SRC_ADDR", "IV_INDEX", "TTL", "SEQ_BLOCK", "SEQ_RECOVERY_JUMP", "SEQ_AUTO_ROTATE",
    "POLL_INTERVAL",
    "PROXY_ADDRESS", "BLE_ADAPTER", "BLE_SCAN_TIMEOUT", "BLE_RETRY_MIN", "BLE_RETRY_MAX",
    "MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASS", "MQTT_CLIENT_ID",
    "MQTT_RECONNECT_MAX", "MQTT_TLS", "MQTT_TLS_CA", "MQTT_TLS_INSECURE",
    "CMD_RATE_LIMIT", "CMD_QUEUE_MAX",
    "BASE_TOPIC", "DISCOVERY_PREFIX", "STATE_FILE",
    "HEALTH_FILE", "HEALTH_MAX_AGE",
    "ALL_GROUP_NAME", "SCENE_NAME_PREFIX", "LOG_LEVEL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in BRIDGE_ENV:
        monkeypatch.delenv(name, raising=False)
    # HEALTH_FILE defaults to /tmp/health, which is a real path on whoever's
    # machine the tests are running on. Point it inside the test's own
    # directory, so a run touches nothing it does not own.
    monkeypatch.setenv("HEALTH_FILE", str(tmp_path / "health"))


@pytest.fixture
def doc() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def settings():
    from app.config import Settings
    return Settings.from_env()
