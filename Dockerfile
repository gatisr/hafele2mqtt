FROM python:3.12-slim-bookworm

# bleak talks to BlueZ over D-Bus, so the container needs the host's system bus
# socket bind-mounted at /var/run/dbus (see compose.yaml). No BlueZ userland
# tools are needed inside the image.

# MESH_FILE is deliberately NOT set: leaving it empty lets the bridge pick up
# whatever single *.connect file is mounted into MESH_DIR, so the export keeps
# the name the Connect app gave it. Setting it here would turn that into a hard
# requirement to rename the file to mesh.connect.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MESH_DIR=/config \
    STATE_FILE=/data/state.json

WORKDIR /srv

# Installed at build time, not on every container start.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runs as root on purpose: D-Bus access to BlueZ requires either root or a
# polkit/dbus policy granting org.bluez to an unprivileged uid. With
# network_mode: host and a bind-mounted system bus there is no uid mapping that
# reliably works across hosts, so dropping privileges here would only break BLE.
# The process holds no listening socket of its own.

VOLUME ["/data"]

# The bridge touches HEALTH_FILE on every turn of its poll loop, and this reads
# how long ago that was. STATE_FILE could not be used for it: it is rewritten
# once every SEQ_BLOCK messages, so its mtime answers a different question, and
# touching it more often would mean more fsyncs onto a Raspberry Pi's SD card.
# The heartbeat is an inode timestamp under /tmp instead, which writes no data
# at all and need not survive a restart.
#
# What this does NOT check is whether the mesh is reachable. A radio out of
# range or a reconnect in progress keeps the loop turning, and is reported on
# `hafele/status` where Home Assistant already reads it; a container that goes
# red through every ordinary BLE reconnect is a warning nobody reads, and
# restarting it would not bring a radio back anyway.
#
# The numbers are deliberately slack. The staleness limit lives in app/health.py
# (three poll intervals, never under 180s, HEALTH_MAX_AGE to override), so an
# unhealthy verdict needs that limit to pass and then three checks in a row to
# fail. start-period covers a cold BLE connect, which is a scan plus a GATT
# handshake and can take a couple of minutes on a distant proxy.
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "-m", "app.health"]

CMD ["python", "-m", "app.main"]
