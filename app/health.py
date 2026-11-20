"""The container healthcheck: has the bridge's poll loop gone round recently?

Run from the Dockerfile as `python -m app.health`. It reads one timestamp and
exits, so it costs an interpreter start and nothing else.

What it asserts is deliberately narrow. The bridge touches `HEALTH_FILE` on
every turn of its poll loop (see `Bridge._beat`), so a stale file means that
loop has stopped going round: the event loop is wedged, the task died, or a
send is hung. It does **not** mean the mesh is unreachable. A radio out of
range, a proxy node that went away, a reconnect in progress - all of those keep
the loop turning and are reported where they belong, on `hafele/status`, which
Home Assistant already reads. Wiring them to the container's health as well
would light a red warning through every ordinary BLE reconnect, and a warning
that is usually on is a warning nobody reads.

Restarting the container does not fix a radio either, which is the other half
of the argument: an orchestrator acting on this signal should only ever be
acting on a process that has stopped working.
"""
from __future__ import annotations

import os
import sys
import time

from .config import ConfigError, Settings

# The floor under the staleness limit. The limit is three poll intervals, and
# at a short POLL_INTERVAL three of them is a window narrow enough that one
# slow round trips it. Nothing here is time-critical: the healthcheck's job is
# to tell a stopped bridge from a running one, not to measure it.
FLOOR = 180.0


def limit_for(poll_interval: float, configured: float = 0.0) -> float:
    """How old the heartbeat may be before the bridge counts as stopped."""
    if configured > 0:
        return configured
    return max(FLOOR, poll_interval * 3)


def check(path: str, limit: float, now: float | None = None) -> str:
    """Return an empty string if the heartbeat is fresh, else why it is not."""
    if not path:
        # HEALTH_FILE deliberately emptied: there is no heartbeat to judge, so
        # the only honest answer is that the container is fine.
        return ""
    try:
        age = (time.time() if now is None else now) - os.stat(path).st_mtime
    except FileNotFoundError:
        return (f"{path} does not exist yet, so the bridge has not completed a poll cycle. "
                "This is normal during start-up and a stopped poll loop after that.")
    except OSError as ex:
        return f"cannot read {path}: {ex}"
    if age > limit:
        return (f"the last poll cycle finished {age:.0f}s ago, which is past the {limit:.0f}s "
                "limit. The bridge process is up but its poll loop has stopped going round.")
    return ""


def main() -> int:
    try:
        s = Settings.from_env()
    except ConfigError as ex:
        # The bridge refuses to start on exactly these, so a container holding
        # one is not a healthy container whatever its heartbeat says.
        print(str(ex), file=sys.stderr)
        return 1
    problem = check(s.health_file, limit_for(s.poll_interval, s.health_max_age))
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
