"""Durable bridge state: sequence numbers, IV index, published discovery topics.

Why this file matters more than its size suggests
--------------------------------------------------
Bluetooth Mesh nodes keep a replay-protection list: for every source address
they remember the highest sequence number seen and *silently drop* anything
lower or equal. So a sequence number that goes backwards does not produce an
error anywhere - the lights simply stop responding until the counter has
climbed back past the old high-water mark, which at the bridge's normal poll
rate can take the better part of a day. It is the same rake as a Zigbee frame
counter reset.

The IV index has the same failure mode: send with the wrong one and every node
discards the message without complaint.

Hence: persist both, never move either backwards, and refuse to start at all if
the state directory is not writable rather than quietly degrade.

The counter is 24 bits wide and *per source address*, which gives it a finite
lifetime rather than a wrap-around: see `next_seq()` and docs/mesh-internals.md
(sequence numbers). That is why the high-water mark is kept per source address -
moving SRC_ADDR is the only recovery this bridge can carry out on its own, and
it only works if the new address starts its own counter.

Moving it is therefore done automatically, while there is still a million
numbers left rather than at the moment the last one is handed out: see
`needs_rotation` and `rotate()`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import ConfigError

log = logging.getLogger("state")


class FatalStateError(RuntimeError):
    """State was lost or cannot be persisted: carrying on would corrupt the mesh.

    Separate from the ordinary errors a send can raise, because those deserve a
    retry and this one deserves a shutdown. It stays a RuntimeError so existing
    callers that catch that keep working.
    """


SEQ_MAX = 0xFFFFFF
# Warn - and, with SEQ_AUTO_ROTATE on, move to a fresh source address - with
# this much of the space left. At the floor rate of this bridge (nine lights
# polled twice a minute, about 26 000 numbers a day) a million is over a month
# of notice. The margin is what makes the move invisible: it happens during an
# ordinary send, with the counter still working, rather than at the moment
# next_seq() has nothing left to hand out and commands have already started
# failing.
SEQ_WARN_AT = SEQ_MAX - 1_000_000
# Seconds at 2026-01-01T00:00:00Z. Only used to derive a cold-start floor.
_EPOCH = 1767225600
# Sequence numbers to skip after losing persisted state; see _load().
RECOVERY_JUMP = 100_000


def _clock_floor() -> int:
    """A monotonically rising lower bound for the sequence number.

    Minutes since 2026-01-01. Not an estimate of real usage (the bridge burns
    sequence numbers far faster than one per minute), just a floor that rises by
    itself while the bridge is not running.
    """
    return max(1, int((time.time() - _EPOCH) / 60))


def _as_int(value: object, default: int | None) -> int | None:
    """An int out of whatever JSON actually contained, or `default`.

    Booleans are rejected on purpose: `true` is an int to Python, and silently
    reading a corrupt `"iv_index": true` as 1 is how a state file ends up
    looking fine and sending undecryptable messages.
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)                    # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_marks(raw: object) -> dict[int, int]:
    marks: dict[int, int] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            addr, mark = _as_int(key, None), _as_int(value, None)
            if addr is not None and mark is not None:
                marks[addr] = mark
    return marks


def _parse_identity(raw: object) -> dict[str, str]:
    """The anchor -> unique_id map, with anything that is not a pair of strings dropped.

    Lenient on purpose. Unlike the sequence number, a lost entry here costs one
    entity its name at worst, which is not worth refusing to start over.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and k and v}


def read_identity(path: str | Path) -> dict[str, str]:
    """The identity map in a state file, without opening or locking anything.

    For `--validate`, which has to report the unique_ids the bridge would
    actually publish but must not create, write to or claim the state file.

    Merges the backup under the primary exactly as _load() does. A report that
    worked off the primary alone would name entities the running bridge does
    not, which is the one thing --validate must not do.
    """
    out: dict[str, str] = {}
    for candidate in (Path(f"{path}.bak"), Path(path)):
        try:
            data = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.update(_parse_identity(data.get("identity")))
    return out


def _fsync_dir(directory: Path) -> None:
    """Make a rename durable.

    fsync on the file only promises the *contents* are on disk. The directory
    entry that gives those contents the name `state.json` is a separate write,
    so without this a power cut can leave the old file back in place with the
    new one fsynced into an inode nothing points at.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as ex:
        # A few filesystems (some network ones) refuse fsync on a directory.
        # The payload is already durable; only the rename is left to chance.
        log.debug("cannot fsync %s (%s); the rename may not be on disk yet", directory, ex)
    finally:
        os.close(fd)


class State:
    def __init__(self, path: str | Path, seq_block: int = 500,
                 recovery_jump: int = RECOVERY_JUMP,
                 auto_rotate: bool = True,
                 pick_src: Callable[[set[int]], int] | None = None) -> None:
        self.path = Path(path)
        self.bak = self.path.with_suffix(self.path.suffix + ".bak")
        self.seq_block = seq_block
        self.recovery_jump = recovery_jump
        # `pick_src` is handed the addresses rotation must not land on and
        # returns a free unicast address; app.main supplies one backed by the
        # mesh file. Without it there is nothing to rotate to, so rotation is
        # simply off - which is what keeps it out of the way in tests.
        self.auto_rotate = auto_rotate
        self.pick_src = pick_src
        self._rotation_impossible = False
        self.iv_index = 0
        # True while the network is mid-IV-update. Deliberately not persisted:
        # it is re-learned from the next beacon within seconds, and a stale
        # `true` read back off disk would make the bridge accept an IV index
        # the network has since stopped using. See `set_iv_update`.
        self.iv_update = False
        self.src_addr = 0
        self.base_topic = ""
        self.discovery: set[str] = set()
        # Anchor (a GUID out of the .connect file) -> the unique_id the entity
        # behind it was published under. This is what survives a device being
        # re-provisioned onto a different mesh address; see model.apply_identity.
        self.identity: dict[str, str] = {}
        self.seq_by_src: dict[int, int] = {}
        self.reserved = 0
        self.seq = 0
        self._warned_low = False
        # Two threads reach this object: the asyncio loop (sends, shutdown) and
        # paho's network thread (on_connect -> discovery bookkeeping). They wrote
        # the same .tmp path concurrently, which can interleave into a file that
        # loads with a *lower* reserved value, and a sequence number that goes
        # backwards stops the whole mesh obeying us without raising anything.
        # Reentrant because _reserve/set_iv/set_discovery all call save().
        self._lock = threading.RLock()

    # --- lifecycle ------------------------------------------------------
    def open(self, src: int = 0) -> None:
        """Load state for the address `src` will send from.

        Passing the address matters: the counter belongs to it, so a different
        address must not inherit the old one's position.
        """
        self._preflight()
        self._load(src)

    def _preflight(self) -> None:
        """Fail now, loudly, rather than at message 500 in the middle of the night."""
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            raise ConfigError(
                f"State directory {directory} cannot be created ({ex}). "
                "Mount a writable volume there, e.g. `- ./data:/data` in compose.yaml.") from None
        probe = directory / ".write-test"
        try:
            probe.write_text("x")
            probe.unlink()
        except OSError as ex:
            raise ConfigError(
                f"State directory {directory} is not writable ({ex}). The bridge would lose its "
                "mesh sequence number on every restart and the lights would stop responding. "
                "Mount it read-write, e.g. `- ./data:/data` (no `:ro`).") from None

    def _read(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as ex:
            log.warning("state file %s is unreadable (%s), trying the backup", path, ex)
            return None
        if not isinstance(data, dict):
            log.warning("state file %s contains JSON %s, expected an object; trying the backup",
                        path, type(data).__name__)
            return None
        for name in ("iv_index", "src_addr", "reserved"):
            if name in data and _as_int(data[name], None) is None:
                # A `null` or a string here used to reach int() in _load and
                # take the process down on every start, without the backup ever
                # being tried. A file we cannot believe is a file we cannot
                # read, so it gets the same treatment.
                log.warning("state file %s has a corrupt %s (%r), treating the whole file as "
                            "unreadable and trying the backup", path, name, data[name])
                return None
        return data

    @staticmethod
    def _mark_in(data: dict, src: int) -> int:
        """The highest sequence number `data` records for `src`; 0 if it records none."""
        mark = _parse_marks(data.get("seq_by_src")).get(src, 0)
        if src == _as_int(data.get("src_addr"), 0):
            # State written before per-source marks existed: the bare
            # `reserved` belongs to whatever address was in use then.
            mark = max(mark, _as_int(data.get("reserved"), 0) or 0)
        return mark

    def _load(self, src: int) -> None:
        primary = self._read(self.path)
        backup = self._read(self.bak)
        data = primary if primary is not None else backup
        floor = _clock_floor()
        self.src_addr = src

        if data is None:
            siblings = [p for p in self.path.parent.iterdir() if not p.name.startswith(".")]
            if siblings:
                self.reserved = floor + self.recovery_jump
                log.warning(
                    "no usable state file at %s but %s is not empty: persisted state was lost. "
                    "Skipping %d sequence numbers so the mesh does not reject our messages "
                    "(replay protection). Starting at seq %d. If the lights ignore the bridge "
                    "for the next few hours, this is why: raise SEQ_RECOVERY_JUMP or set a "
                    "different SRC_ADDR to get a counter the nodes have no record of.",
                    self.path, self.path.parent, self.recovery_jump, self.reserved)
            else:
                self.reserved = floor
                log.info("fresh install, starting at sequence number %d", self.reserved)
        else:
            self.iv_index = max(0, _as_int(data.get("iv_index"), 0) or 0)
            self.base_topic = str(data.get("base_topic") or "")
            self.discovery = set(data.get("discovery") or [])
            self.identity = _parse_identity(data.get("identity"))
            self.seq_by_src = _parse_marks(data.get("seq_by_src"))
            if backup is not None and backup is not data:
                # The backup is older by construction, so it can only ever raise
                # the high-water mark, never lower it. Merging costs nothing and
                # covers a primary file that parses but has lost an address.
                # Done before the address is settled, because which addresses are
                # spent decides whether the requested one can be used at all.
                for addr, value in _parse_marks(backup.get("seq_by_src")).items():
                    if value > self.seq_by_src.get(addr, 0):
                        self.seq_by_src[addr] = value
                # Same argument for the identity map: an anchor the primary has
                # lost but the backup still holds would mean an entity silently
                # re-registering under a new unique_id. The primary wins any
                # anchor they both have.
                self.identity = {**_parse_identity(backup.get("identity")), **self.identity}
            previous = _as_int(data.get("src_addr"), 0) or 0
            if not src:
                self.src_addr = previous
            elif src != previous and src in self.spent_srcs() and self._rotation_available:
                # SRC_ADDR names the address the bridge started on, and that
                # address has since spent its counter and been rotated off.
                # Honouring it again would hand the bridge a position the nodes
                # have already seen past, so the first send would trip rotation
                # and burn a fresh address - on every single restart. Replay
                # lists are one entry per source address and not free. The
                # persisted address is the one that still works.
                log.warning(
                    "SRC_ADDR 0x%04X has already spent its 24-bit sequence number space, so the "
                    "bridge stays on 0x%04X, which it moved to by itself. Point SRC_ADDR at that "
                    "address or unset it to silence this; sending from a spent address cannot be "
                    "made to work.", src, previous)
                self.src_addr = previous
            mark = self._mark_in(data, self.src_addr)
            if backup is not None and backup is not data:
                mark = max(mark, self._mark_in(backup, self.src_addr))
                self.iv_index = max(self.iv_index,
                                    max(0, _as_int(backup.get("iv_index"), 0) or 0))
            self.reserved = max(mark, floor)
            if primary is None:
                # Falling back to .bak is exactly the case the recovery jump
                # exists for: the backup is a snapshot of an *earlier* save, so
                # its reserved block has already been handed out and the nodes
                # have seen those numbers. The clock floor is no protection -
                # it rises by 1440 a day while the bridge spends tens of
                # thousands, so the gap between floor and reserved only grows,
                # and resuming from the backup would replay more numbers every
                # month the bridge stays up.
                self.reserved += self.recovery_jump
                log.warning(
                    "state file %s was unusable, so the bridge resumed from the backup %s and "
                    "skipped %d sequence numbers on top (the backup is by definition behind "
                    "what was already sent, and the mesh silently ignores a sequence number it "
                    "has seen). Starting at seq %d for src 0x%04X.",
                    self.path, self.bak, self.recovery_jump, self.reserved, self.src_addr)
            if previous and self.src_addr != previous:
                log.warning(
                    "source address changed 0x%04X -> 0x%04X, so the counter starts over at "
                    "%d. The nodes keep replay records per source address and have none for "
                    "0x%04X, which is exactly what makes this the way to recover an exhausted "
                    "counter. Going back to 0x%04X would resume at %d.",
                    previous, self.src_addr, self.reserved, self.src_addr,
                    previous, self.seq_by_src.get(previous, 0))
            log.info("state loaded: seq %d for src 0x%04X, iv_index %d, %d discovery topic(s)",
                     self.reserved, self.src_addr, self.iv_index, len(self.discovery))

        self.seq = self.reserved
        self._reserve()

    # --- persistence ----------------------------------------------------
    def _snapshot(self) -> None:
        """Point .bak at the current state.json without unlinking it first.

        A hard link is the cheap way to get a second name for the file that is
        there right now; the os.replace() that follows rebinds state.json to a
        new inode and leaves .bak holding the old one. Filesystems that refuse
        links (and the odd bind mount) fall back to a copy.
        """
        staging = self.path.with_suffix(self.path.suffix + ".bak.tmp")
        staging.unlink(missing_ok=True)
        try:
            os.link(self.path, staging)
        except OSError:
            shutil.copyfile(self.path, staging)
        os.replace(staging, self.bak)

    def save(self) -> None:
        """Write the state out, synchronously, fsync included.

        Synchronous on purpose, and it has to stay that way. The whole design
        rests on the sequence number being on disk *before* the frame that uses
        it leaves the radio; hand the write to a thread and a crash in the gap
        brings the bridge back on a number the nodes have already recorded,
        which is precisely the silent replay failure the file exists to
        prevent.

        The cost is bounded by `_reserve()`, which writes once per `seq_block`
        (500) sequence numbers rather than once per message, so this fsync is
        amortised over about 500 sends. `set_iv()` and `set_discovery()` write
        far more rarely still. One fsync per 500 messages on the event loop is
        not worth trading that guarantee for.
        """
        with self._lock:
            payload = json.dumps({
                "version": 1,
                "reserved": self.reserved,
                "iv_index": self.iv_index,
                "src_addr": self.src_addr,
                "base_topic": self.base_topic,
                "discovery": sorted(self.discovery),
                "identity": dict(sorted(self.identity.items())),
                # Per source address, so that changing SRC_ADDR really does hand
                # the bridge a counter the nodes have never seen, and changing
                # back resumes where that address left off instead of replaying.
                "seq_by_src": {str(k): v for k, v in sorted(self.seq_by_src.items())},
            })
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                # 1. The new state, complete and fsynced, under a name nothing
                #    reads yet.
                with open(tmp, "w") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                # 2. Only now take the backup. Moving state.json aside first
                #    (the previous order) left a window in which state.json did
                #    not exist at all and .bak had already been overwritten with
                #    its contents, so dying in that window cost a whole
                #    generation of state for no reason.
                if self.path.exists():
                    self._snapshot()
                # 3. One rename: a reader sees the old file or the new one,
                #    never neither. The fsync is what makes that rename outlive
                #    a power cut.
                os.replace(tmp, self.path)
                _fsync_dir(self.path.parent)
            except OSError as ex:
                # Writability was checked at start-up, so this means the volume
                # went away underneath us. Continuing would risk a silent seq
                # rollback.
                raise FatalStateError(
                    f"cannot persist mesh state to {self.path} ({ex}); refusing to continue "
                    "because a lost sequence number makes the lights stop responding") from None

    # --- sequence numbers -----------------------------------------------
    def _reserve(self) -> None:
        with self._lock:
            self.reserved = self.seq + self.seq_block
            if self.src_addr:
                self.seq_by_src[self.src_addr] = self.reserved
            self.save()

    def next_seq(self) -> int:
        """The next sequence number to send with. Raises once the space is gone.

        The 24-bit space is spent, not wrapped: there is no legal way back to a
        number a node has already recorded. The spec's answer is an IV update,
        which only the network as a whole can perform and which this bridge
        cannot initiate (it is a guest in someone else's network, not its
        provisioner). What it *can* do is move to a fresh source address, which
        the nodes have no replay record for, so that is what the message says.
        """
        with self._lock:
            if self.seq >= self.reserved - 1:
                self._reserve()
            self.seq += 1
            if self.seq > SEQ_MAX:
                raise FatalStateError(
                    "mesh sequence number space exhausted for source address "
                    f"0x{self.src_addr:04X}. Set SRC_ADDR to a different free unicast address "
                    "and restart: the bridge then starts a fresh counter for it, which the "
                    "nodes will accept. See docs/mesh-internals.md.")
            if self.seq > SEQ_WARN_AT and not self._warned_low:
                self._warned_low = True
                log.warning(
                    "sequence number %d of %d used for source address 0x%04X. %s",
                    self.seq, SEQ_MAX, self.src_addr,
                    "Moving to a fresh source address on the next send."
                    if self._rotation_available else
                    "When the space runs out the bridge stops; move SRC_ADDR to another free "
                    "unicast address before then. See docs/mesh-internals.md.")
            return self.seq

    # --- source address rotation -----------------------------------------
    @property
    def _rotation_available(self) -> bool:
        return self.auto_rotate and self.pick_src is not None and not self._rotation_impossible

    @property
    def needs_rotation(self) -> bool:
        """True while the counter is past the threshold and a move is still possible.

        Deliberately not acted on inside next_seq(): the address is half of
        every PDU the caller is building, and changing it between reserving a
        sequence number and stamping it into a message would produce exactly
        the kind of mismatch the nodes drop in silence. The caller asks at a
        point of its own choosing, where no message is half-built.
        """
        return self._rotation_available and self.seq > SEQ_WARN_AT

    def spent_srcs(self) -> set[int]:
        """Addresses whose counter is past the threshold, so there is no way back to them.

        Self-consistent on purpose: an address the bridge would rotate *off*
        the moment it adopted it is not a candidate to rotate *onto*. The marks
        are kept forever in seq_by_src, so this survives restarts and the
        bridge cannot work its way around a ring of burnt addresses.
        """
        return {addr for addr, mark in self.seq_by_src.items() if mark > SEQ_WARN_AT}

    def rotate(self) -> int | None:
        """Move to a source address whose counter the nodes have no record of.

        Returns the new address, or None if there was none to move to - in
        which case the bridge keeps running on the old one until the space is
        genuinely gone and next_seq() raises. That is a worse outcome, but it
        is the honest one: rotation that cannot find an address is not a reason
        to stop a bridge that still has a million sequence numbers left.
        """
        with self._lock:
            if not self._rotation_available:
                return None
            old, spent_to = self.src_addr, max(self.reserved, self.seq)
            # Record where the old address actually got to before leaving it.
            # This is what puts it in spent_srcs(), so it has to happen first,
            # or the very next rotation could pick it straight back up.
            if old:
                self.seq_by_src[old] = max(self.seq_by_src.get(old, 0), spent_to)
            unavailable = self.spent_srcs() | {old}
            try:
                new = self.pick_src(unavailable)        # type: ignore[misc]
            except ConfigError as ex:
                self._rotation_impossible = True
                log.error(
                    "cannot move source address 0x%04X on: %s The bridge keeps running on it "
                    "and will stop when its sequence number space is gone.", old, ex)
                return None
            if new in unavailable:
                self._rotation_impossible = True
                log.error(
                    "refusing to move source address 0x%04X to 0x%04X: that address has already "
                    "spent its sequence number space, and the nodes would ignore every message "
                    "sent from it. The bridge keeps running on 0x%04X.", old, new, old)
                return None
            self.src_addr = new
            # Same floor as a fresh install: a rising lower bound costs a few
            # per cent of the new address's space and covers the case where
            # /data was wiped at some point and the nodes remember more than
            # the state file does.
            self.seq = max(self.seq_by_src.get(new, 0), _clock_floor())
            self._warned_low = False
            self._reserve()
            log.warning(
                "source address 0x%04X -> 0x%04X: it had %d of its 24-bit sequence number space "
                "left, and a fresh address is the only counter reset this bridge can perform "
                "(an IV update is the network's job, not a guest's). The new counter starts at "
                "%d. Nodes keep one replay record per source address, so this costs each of them "
                "an entry.",
                old, new, SEQ_MAX - spent_to, self.seq)
            return new

    # --- iv index --------------------------------------------------------
    def set_iv(self, iv: int, source: str = "beacon") -> bool:
        """Adopt an IV index. Returns True if it changed.

        The IV index only ever increases in a healthy network, so a lower value
        is either a stale beacon or a node that never completed an IV update.
        Following it down would make our messages undecryptable network-wide.
        """
        with self._lock:
            if iv == self.iv_index:
                return False
            if iv < self.iv_index:
                log.warning("ignoring IV index %d from %s: lower than the current %d",
                            iv, source, self.iv_index)
                return False
            log.warning("IV index %d -> %d (from %s)", self.iv_index, iv, source)
            self.iv_index = iv
            self.save()
            return True

    def set_iv_update(self, in_progress: bool) -> None:
        """Record whether the network is mid-IV-update. Logs the transitions only.

        Beacons arrive every ten seconds, so this is called constantly; only a
        change is worth a line. Nothing is written to disk - see the note on
        `self.iv_update` in __init__ for why a persisted flag would be worse
        than no flag at all.
        """
        with self._lock:
            if in_progress == self.iv_update:
                return
            self.iv_update = in_progress
            if in_progress:
                log.warning(
                    "the mesh has started an IV update (current IV index %d). The bridge keeps "
                    "transmitting with %d, as the spec requires, and accepts messages on both "
                    "that and %d until the network says the update is over.",
                    self.iv_index, self.iv_index, self.iv_index + 1)
            else:
                log.info("the mesh IV update is over; back to normal operation on IV index %d",
                         self.iv_index)

    # --- discovery bookkeeping -------------------------------------------
    def set_discovery(self, topics: set[str]) -> set[str]:
        """Record the discovery topics now in use; return the ones that went stale."""
        with self._lock:
            stale = self.discovery - topics
            self.discovery = set(topics)
            self.save()
            return stale

    def set_identity(self, mapping: dict[str, str]) -> None:
        """Record which unique_id each anchor is published under.

        Written before the first discovery message goes out, not after. The
        order matters on a first run: a crash between publishing an entity and
        remembering what it was called would leave Home Assistant holding a
        unique_id this bridge has no record of, and the next start would mint a
        second one beside it.
        """
        with self._lock:
            if mapping == self.identity:
                return
            self.identity = dict(mapping)
            self.save()
