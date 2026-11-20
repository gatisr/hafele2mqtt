"""Durability of the things whose loss breaks the mesh silently.

A sequence number that goes backwards, or an IV index that goes backwards, does
not raise anything anywhere: the lights just stop obeying. So these tests are
mostly about the counter never moving the wrong way.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from app.config import ConfigError
from app.state import SEQ_WARN_AT, FatalStateError, State, _clock_floor


@pytest.fixture
def state(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10)
    st.open()
    return st


def test_fresh_install_starts_above_zero(state):
    # The clock floor means even a brand new install does not start at 1, so a
    # wiped /data cannot replay yesterday's sequence numbers.
    assert state.seq > 1000


def test_sequence_numbers_only_go_up(state):
    seen = [state.next_seq() for _ in range(50)]
    assert seen == sorted(seen)
    assert len(set(seen)) == 50


def test_reservation_survives_a_crash_without_a_clean_save(tmp_path):
    first = State(tmp_path / "state.json", seq_block=10)
    first.open()
    used = [first.next_seq() for _ in range(3)]
    # No save() here: simulate the process being killed mid-flight.
    second = State(tmp_path / "state.json", seq_block=10)
    second.open()
    assert second.seq >= first.reserved > max(used)


def test_block_is_reserved_ahead_of_use(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10)
    st.open()
    on_disk = json.loads((tmp_path / "state.json").read_text())
    assert on_disk["reserved"] == st.seq + 10


class Killed(BaseException):
    """The process dying in the middle of save().

    Deliberately not an OSError: save() turns those into FatalStateError, which
    is the error path, not the crash path. A BaseException is also closer to
    what a SIGKILL looks like from the filesystem's side - nothing gets to
    clean up after it.
    """


@pytest.mark.parametrize("die_at", [1, 2, 3, 4, 5])
def test_dying_anywhere_in_save_still_leaves_a_readable_state_file(tmp_path, monkeypatch, die_at):
    """state.json has to be readable at every instant, not merely at rest.

    The order used to be `os.replace(state.json, .bak)` and only *then* writing
    the new file, which left a window in which state.json did not exist at all
    while .bak had already been overwritten with what state.json used to hold.
    Dying in that window threw away a whole generation of state - the counter,
    the IV index, the discovery list - with no power cut required, just bad
    timing on a container restart.

    The kill points are the mutating syscalls of one save, in order: fsync the
    new payload, link the backup, rename the backup into place, rename the new
    payload over state.json, fsync the directory.
    """
    st = State(tmp_path / "state.json", seq_block=10)
    st.open(0x7FF7)
    used = [st.next_seq() for _ in range(5)]
    st.base_topic = "hafele"
    st.iv_index = 4

    calls = {"n": 0}

    def trip(real):
        def wrapper(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == die_at:
                raise Killed(f"killed before mutation {die_at}")
            return real(*args, **kwargs)
        return wrapper

    for name in ("fsync", "link", "replace"):
        monkeypatch.setattr(os, name, trip(getattr(os, name)))
    with pytest.raises(Killed):
        st.save()
    monkeypatch.undo()
    assert calls["n"] == die_at, "save() performed fewer mutations than the test assumed"

    survivor = tmp_path / "state.json"
    assert survivor.exists(), "state.json vanished mid-save; a restart here loses the counter"
    assert json.loads(survivor.read_text())["version"] == 1   # parses, not merely exists

    restarted = State(tmp_path / "state.json", seq_block=10)
    restarted.open(0x7FF7)
    assert restarted.seq > max(used), "a crash during save replayed sequence numbers"


def test_a_save_interrupted_last_time_leaves_no_rubbish_behind(tmp_path, monkeypatch):
    """The staging files are reused, not accumulated, so a crash cannot fill /data."""
    st = State(tmp_path / "state.json", seq_block=10)
    st.open(0x7FF7)

    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(Killed("boom")))
    with pytest.raises(Killed):
        st.save()
    monkeypatch.setattr(os, "replace", real_replace)

    st.save()
    st.save()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.bak"]


def test_resuming_from_the_backup_skips_ahead_rather_than_replaying(tmp_path):
    """The backup is behind by construction, so resuming from it has to jump.

    .bak is a snapshot of an *earlier* save, so every number in its reserved
    block has already been handed out and the nodes have recorded them. Taking
    it at face value sends numbers the mesh silently ignores. The clock floor
    is no protection: it rises 1440 a day while the bridge spends tens of
    thousands, so the gap only widens the longer the bridge runs.
    """
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open(0x7FF7)
    for _ in range(25):            # forces a re-reservation, so a .bak exists
        st.next_seq()
    st.save()
    behind = json.loads((tmp_path / "state.json.bak").read_text())["reserved"]
    (tmp_path / "state.json").unlink()

    again = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    again.open(0x7FF7)
    assert again.seq >= behind + 100000


def test_a_healthy_state_file_does_not_get_the_recovery_jump(tmp_path):
    """The jump is for lost state. Spending 100k numbers on every clean restart
    would burn the 24-bit space in a few months."""
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open(0x7FF7)
    for _ in range(25):
        st.next_seq()
    st.save()
    mark = json.loads((tmp_path / "state.json").read_text())["reserved"]

    again = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    again.open(0x7FF7)
    assert mark <= again.seq < mark + 100000


def test_a_primary_that_lost_ground_is_corrected_by_the_backup(tmp_path):
    """Both files parse, so this is not a fallback - it is a disagreement.

    The older file can only ever name a number that was already used, so taking
    the higher of the two is always safe and sometimes saves the mesh.
    """
    (tmp_path / "state.json.bak").write_text(json.dumps({
        "version": 1, "reserved": 9_000_000, "iv_index": 5, "src_addr": 0x7FF7,
        "base_topic": "hafele", "discovery": [], "seq_by_src": {"32759": 9_000_000},
    }))
    (tmp_path / "state.json").write_text(json.dumps({
        "version": 1, "reserved": 1_000_000, "iv_index": 2, "src_addr": 0x7FF7,
        "base_topic": "hafele", "discovery": [], "seq_by_src": {"32759": 1_000_000},
    }))
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open(0x7FF7)
    assert st.seq >= 9_000_000
    assert st.seq < 9_000_000 + 100000       # the primary parsed: no jump on top
    assert st.iv_index == 5


@pytest.mark.parametrize("name, value", [
    ("iv_index", None), ("iv_index", "soon"), ("iv_index", True),
    ("src_addr", None), ("reserved", "lots"), ("reserved", [1]),
])
def test_a_corrupt_number_falls_back_to_the_backup_instead_of_crash_looping(tmp_path, name, value):
    """int() on whatever JSON held used to take the process down on every start.

    A crash-loop is the worst of the options: the backup is never tried, so a
    single bad byte costs the counter permanently rather than one generation.
    """
    good = {"version": 1, "reserved": 4_000_000, "iv_index": 3, "src_addr": 0x7FF7,
            "base_topic": "hafele", "discovery": [], "seq_by_src": {"32759": 4_000_000}}
    (tmp_path / "state.json.bak").write_text(json.dumps(good))
    (tmp_path / "state.json").write_text(json.dumps({**good, name: value}))

    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open(0x7FF7)
    assert st.seq >= 4_000_000 + 100000      # came from the backup, so it jumped
    assert st.base_topic == "hafele"


def test_a_corrupt_number_with_no_backup_is_still_not_a_crash(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "version": 1, "reserved": None, "iv_index": 0, "src_addr": 0x7FF7,
        "base_topic": "", "discovery": [],
    }))
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open(0x7FF7)
    assert st.seq >= _clock_floor() + 100000


def test_a_state_file_that_is_not_an_object_is_treated_as_unreadable(tmp_path):
    (tmp_path / "state.json").write_text("[1, 2, 3]")
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open(0x7FF7)
    assert st.seq >= _clock_floor() + 100000


def test_a_corrupt_state_file_falls_back_to_the_backup(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10)
    st.open()
    for _ in range(25):            # forces at least one re-reservation -> a .bak exists
        st.next_seq()
    st.save()
    assert (tmp_path / "state.json.bak").exists()
    (tmp_path / "state.json").write_text("{ this is not json")

    again = State(tmp_path / "state.json", seq_block=10)
    again.open()
    assert again.seq > 1000


def test_losing_the_state_file_in_a_used_directory_skips_ahead(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open()
    baseline = st.seq
    (tmp_path / "state.json").unlink()
    (tmp_path / "something-else").write_text("the volume is clearly in use")

    recovered = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    recovered.open()
    assert recovered.seq >= baseline + 100000


def test_the_recovery_jump_is_configurable(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=7)
    st.open()
    baseline = st.seq
    (tmp_path / "state.json").unlink()
    (tmp_path / "something-else").write_text("the volume is clearly in use")

    recovered = State(tmp_path / "state.json", seq_block=10, recovery_jump=7)
    recovered.open()
    assert baseline + 7 <= recovered.seq < baseline + 100


def test_an_empty_directory_is_treated_as_a_fresh_install(tmp_path):
    from app.state import _clock_floor
    st = State(tmp_path / "state.json", seq_block=10, recovery_jump=100000)
    st.open()
    # Starts at the clock floor, with no recovery jump added on top.
    assert _clock_floor() <= st.seq < _clock_floor() + 100000


def test_unusable_state_directory_is_a_startup_error(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("a regular file where the directory should be")
    st = State(blocker / "state.json")
    with pytest.raises(ConfigError, match="State directory"):
        st.open()


def test_iv_index_is_persisted(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10)
    st.open()
    assert st.set_iv(7) is True
    again = State(tmp_path / "state.json", seq_block=10)
    again.open()
    assert again.iv_index == 7


def test_iv_index_never_goes_backwards(state):
    state.set_iv(9)
    assert state.set_iv(4) is False
    assert state.iv_index == 9
    assert state.set_iv(9) is False          # no churn on a repeat beacon
    assert state.set_iv(10) is True


def test_discovery_topics_report_what_went_stale(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10)
    st.open()
    assert st.set_discovery({"a/config", "b/config"}) == set()
    assert st.set_discovery({"b/config", "c/config"}) == {"a/config"}

    again = State(tmp_path / "state.json", seq_block=10)
    again.open()
    assert again.discovery == {"b/config", "c/config"}


def test_base_topic_is_remembered_across_restarts(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10)
    st.open()
    st.base_topic = "hafele"
    st.save()
    again = State(tmp_path / "state.json", seq_block=10)
    again.open()
    assert again.base_topic == "hafele"


def test_exhausting_the_sequence_space_is_a_loud_error(tmp_path):
    st = State(tmp_path / "state.json", seq_block=10)
    st.open()
    st.seq = 0xFFFFFF
    st.reserved = 0xFFFFFF + 10
    with pytest.raises(RuntimeError, match="sequence number space"):
        st.next_seq()


def test_the_exhaustion_error_names_the_only_recovery_that_works(tmp_path):
    # The spec's answer is an IV update, which this bridge cannot initiate. The
    # message must therefore point at a fresh SRC_ADDR, and the counter has to
    # really restart for that advice to be true: see the next test.
    st = State(tmp_path / "state.json", seq_block=10)
    st.open(0x7FF7)
    st.seq = 0xFFFFFF
    st.reserved = 0xFFFFFF + 10
    with pytest.raises(RuntimeError, match="SRC_ADDR"):
        st.next_seq()


def test_a_new_source_address_gets_a_counter_of_its_own(tmp_path):
    """The documented recovery from an exhausted counter has to actually work.

    Nodes keep replay records per source address, so a fresh address is clean
    even at a low sequence number. If the bridge carried the old high-water mark
    across, moving SRC_ADDR would fix nothing.
    """
    old = State(tmp_path / "state.json", seq_block=10)
    old.open(0x7FF7)
    old.seq = 9_000_000
    old._reserve()

    moved = State(tmp_path / "state.json", seq_block=10)
    moved.open(0x7FF8)
    assert moved.seq < 1_000_000


def test_going_back_to_an_old_source_address_resumes_rather_than_replays(tmp_path):
    first = State(tmp_path / "state.json", seq_block=10)
    first.open(0x7FF7)
    first.seq = 9_000_000
    first._reserve()
    high_water = first.reserved

    State(tmp_path / "state.json", seq_block=10).open(0x7FF8)

    back = State(tmp_path / "state.json", seq_block=10)
    back.open(0x7FF7)
    assert back.seq >= high_water


def test_state_written_before_per_source_marks_is_still_honoured(tmp_path):
    """An upgrade must not hand the running address a lower number than it had."""
    (tmp_path / "state.json").write_text(json.dumps({
        "version": 1, "reserved": 5_000_000, "iv_index": 3,
        "src_addr": 0x7FF7, "base_topic": "hafele", "discovery": [],
    }))
    st = State(tmp_path / "state.json", seq_block=10)
    st.open(0x7FF7)
    assert st.seq >= 5_000_000
    assert st.iv_index == 3


def test_an_omitted_source_address_keeps_using_the_stored_one(tmp_path):
    first = State(tmp_path / "state.json", seq_block=10)
    first.open(0x7FF7)
    first.seq = 2_000_000
    first._reserve()

    again = State(tmp_path / "state.json", seq_block=10)
    again.open()
    assert again.src_addr == 0x7FF7
    assert again.seq >= 2_000_000


def test_concurrent_saves_never_leave_a_file_a_reader_can_regress_on(tmp_path):
    """Two threads really do save this object, and they shared one .tmp path.

    paho's network thread runs on_connect -> _discovery -> set_discovery ->
    save(), while the asyncio thread saves on every reservation. Both wrote
    state.json.tmp and then renamed it over state.json, so one writer could
    rename a file the other was still filling, and the half-written result
    either fails to parse or parses with a *lower* reserved. A sequence number
    that goes backwards does not raise anything: the lights simply stop obeying.
    """
    st = State(tmp_path / "state.json", seq_block=10)
    st.open(0x7FF7)
    stop = threading.Event()
    seen: list[int] = []
    errors: list[BaseException] = []

    def bump():
        try:
            while not stop.is_set():
                st.next_seq()
        except BaseException as ex:          # noqa: BLE001 - reported below
            errors.append(ex)

    def churn():
        try:
            while not stop.is_set():
                st.set_discovery({f"homeassistant/light/x{st.seq % 3}/config"})
        except BaseException as ex:          # noqa: BLE001
            errors.append(ex)

    def watch():
        while not stop.is_set():
            try:
                raw = (tmp_path / "state.json").read_text()
            except FileNotFoundError as ex:
                # Nothing in save() unlinks state.json or renames it away any
                # more, so there is no longer an instant at which it is absent
                # "by design". If a reader ever fails to find it, a restart at
                # that instant would have fallen back to the stale backup.
                errors.append(ex)
                continue
            try:
                seen.append(int(json.loads(raw)["reserved"]))
            except (ValueError, KeyError, TypeError) as ex:
                errors.append(ex)

    threads = [threading.Thread(target=f) for f in (bump, churn, watch)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(5)

    assert not errors, errors
    assert len(seen) > 10, "the reader never managed to read the file"
    assert seen == sorted(seen), "state.json went backwards under concurrent saves"


def test_a_state_file_that_cannot_be_written_is_fatal_not_a_warning(tmp_path):
    """Losing the volume is not an ordinary error that deserves a retry.

    It is distinguishable from a dropped command so the bridge can stop and
    exit non-zero rather than keep running without a usable counter.
    """
    st = State(tmp_path / "state.json", seq_block=10)
    st.open(0x7FF7)
    st.path = tmp_path / "gone" / "state.json"
    st.bak = st.path.with_suffix(".json.bak")
    with pytest.raises(FatalStateError, match="refusing to continue"):
        st.save()


def test_unreadable_per_source_marks_are_ignored_not_fatal(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "version": 1, "reserved": 0, "iv_index": 0, "src_addr": 0x7FF7,
        "base_topic": "", "discovery": [],
        "seq_by_src": {"not-a-number": 5, "32759": "not-a-number", "32760": 400},
    }))
    st = State(tmp_path / "state.json", seq_block=10)
    st.open(0x7FF8)
    assert st.seq > 1000                     # fell back to the clock floor


# --- automatic source address rotation ------------------------------------
#
# The counter is 24 bits and spent rather than wrapped, so every bridge has a
# finite life on one address. Moving to another one is the only reset available
# to a guest in someone else's mesh, and these tests are about it happening
# early, happening once, and never landing somewhere it has already been.


class FakePlanner:
    """Stands in for config.plan_src_address: the free addresses a mesh offers.

    Records what it was asked to avoid, because "do not reuse a spent address"
    is a property of the question, not only of the answer.
    """

    def __init__(self, *free: int) -> None:
        self.free = list(free)
        self.avoided: list[set[int]] = []

    def __call__(self, avoid: set[int]) -> int:
        self.avoided.append(set(avoid))
        for addr in self.free:
            if addr not in avoid:
                return addr
        raise ConfigError(
            "No free unicast address in the mesh - every address 0x0001-0x7FFF is taken. "
            "Set SRC_ADDR explicitly.")


def rotating(tmp_path, *free: int, **kw) -> State:
    return State(tmp_path / "state.json", seq_block=10, pick_src=FakePlanner(*free), **kw)


def test_a_counter_with_room_left_is_left_alone(tmp_path):
    st = rotating(tmp_path, 0x7FF8)
    st.open(0x7FF7)
    for _ in range(100):
        st.next_seq()
    assert not st.needs_rotation
    assert st.src_addr == 0x7FF7
    assert st.pick_src.avoided == []


def test_the_address_moves_while_there_are_still_numbers_left_to_send(tmp_path):
    """Early, not as a rescue.

    Rotating at the moment next_seq() has nothing left means commands have
    already been failing for a while. Rotating a million numbers earlier means
    the move happens during an ordinary send, with a working counter on both
    sides of it, and nobody notices.
    """
    st = rotating(tmp_path, 0x7FF8)
    st.open(0x7FF7)
    st.seq = SEQ_WARN_AT + 1
    assert st.needs_rotation

    assert st.rotate() == 0x7FF8
    assert st.src_addr == 0x7FF8
    assert st.seq < 1_000_000                # a counter of its own, not the old one
    assert not st.needs_rotation
    # The address it left still had room: it was not exhausted, just low.
    assert st.seq_by_src[0x7FF7] < 0xFFFFFF


def test_rotation_never_lands_on_an_address_whose_counter_is_spent(tmp_path):
    # 0x7FF8 is free as far as the mesh is concerned - nothing is provisioned
    # there - and that is exactly the trap: the nodes still hold a replay record
    # for it from the last time this bridge used it.
    st = rotating(tmp_path, 0x7FF8, 0x7FF9)
    st.open(0x7FF7)
    st.seq_by_src[0x7FF8] = 0xFFFFFF
    st.seq = SEQ_WARN_AT + 1

    assert st.rotate() == 0x7FF9
    assert 0x7FF8 in st.pick_src.avoided[0]


def test_an_address_it_leaves_is_off_the_table_for_good(tmp_path):
    st = rotating(tmp_path, 0x7FF8, 0x7FF9, 0x7FFA)
    st.open(0x7FF7)
    seen = [st.src_addr]
    for _ in range(3):
        st.seq = SEQ_WARN_AT + 1
        seen.append(st.rotate())
    # Not a ring: four addresses used, none of them twice.
    assert seen == [0x7FF7, 0x7FF8, 0x7FF9, 0x7FFA]


def test_a_restart_comes_back_on_the_address_it_rotated_to(tmp_path):
    st = rotating(tmp_path, 0x7FF8)
    st.open(0x7FF7)
    st.seq = SEQ_WARN_AT + 1
    st.rotate()

    # No save() here on purpose: rotate() has to have persisted the move by
    # itself, or a power cut a second afterwards would bring the bridge back up
    # on the address it had just given up on.
    again = State(tmp_path / "state.json", seq_block=10)
    again.open()
    assert again.src_addr == 0x7FF8
    assert again.seq < 1_000_000


def test_a_pinned_src_addr_does_not_drag_the_bridge_back_onto_a_spent_address(tmp_path):
    """SRC_ADDR in compose.yaml outlives the address it names.

    The live deployment pins one. If a restart honoured it after a rotation, the
    bridge would come up on a counter the nodes have already seen past and move
    off it again within a minute - a fresh address burnt on every restart, and
    replay lists hold one entry per source address.
    """
    st = rotating(tmp_path, 0x7FF8, 0x7FF9)
    st.open(0x7FF7)
    st.seq = SEQ_WARN_AT + 1
    st.rotate()

    again = rotating(tmp_path, 0x7FF8, 0x7FF9)
    again.open(0x7FF7)
    assert again.src_addr == 0x7FF8
    assert not again.needs_rotation
    assert again.pick_src.avoided == []      # it declined, it did not rotate


def test_a_src_addr_that_is_merely_different_is_still_honoured(tmp_path):
    """Declining a spent address must not turn into ignoring SRC_ADDR."""
    st = rotating(tmp_path, 0x7FF9)
    st.open(0x7FF7)
    st.next_seq()

    again = rotating(tmp_path, 0x7FF9)
    again.open(0x7FF8)
    assert again.src_addr == 0x7FF8


def test_a_mesh_with_nowhere_to_go_keeps_the_bridge_running_and_says_why(tmp_path, caplog):
    """A rotation that cannot find an address is not a reason to stop.

    There is still a million numbers left at that point. The bridge carries on
    and dies later with the error it has always had, which at least names a
    recovery a human can carry out.
    """
    st = rotating(tmp_path)                  # no free address anywhere
    st.open(0x7FF7)
    st.seq = SEQ_WARN_AT + 1
    with caplog.at_level("ERROR"):
        assert st.rotate() is None

    assert st.src_addr == 0x7FF7
    assert "No free unicast address in the mesh" in caplog.text
    assert st.next_seq() > SEQ_WARN_AT       # still sending
    # And it does not ask the same hopeless question on every send after that.
    assert not st.needs_rotation
    assert len(st.pick_src.avoided) == 1


def test_a_planner_that_offers_a_spent_address_is_refused(tmp_path, caplog):
    """Belt and braces: the ban is enforced here, not only asked of the planner."""
    st = State(tmp_path / "state.json", seq_block=10,
               pick_src=lambda avoid: 0x7FF8)       # deaf to `avoid`
    st.open(0x7FF7)
    st.seq_by_src[0x7FF8] = 0xFFFFFF
    st.seq = SEQ_WARN_AT + 1
    with caplog.at_level("ERROR"):
        assert st.rotate() is None
    assert st.src_addr == 0x7FF7
    assert "spent its sequence number space" in caplog.text


def test_turning_rotation_off_leaves_the_old_behaviour_intact(tmp_path):
    st = rotating(tmp_path, 0x7FF8, auto_rotate=False)
    st.open(0x7FF7)
    st.seq = SEQ_WARN_AT + 1
    assert not st.needs_rotation
    assert st.rotate() is None
    assert st.src_addr == 0x7FF7
    assert st.pick_src.avoided == []


def test_nothing_but_the_counter_can_set_a_rotation_off(tmp_path):
    """Not a restart, not a wiped volume, not a lost state file."""
    st = rotating(tmp_path, 0x7FF8)
    st.open(0x7FF7)
    st.next_seq()

    again = rotating(tmp_path, 0x7FF8)
    again.open(0x7FF7)
    assert again.src_addr == 0x7FF7
    assert not again.needs_rotation

    # A wiped volume: no state file, no backup, but the directory is not empty,
    # which is the case that applies the recovery jump.
    (tmp_path / "state.json").unlink()
    (tmp_path / "state.json.bak").unlink(missing_ok=True)
    (tmp_path / "something-else").write_text("x")
    wiped = State(tmp_path / "state.json", seq_block=10, recovery_jump=100_000,
                  pick_src=FakePlanner(0x7FF8))
    wiped.open(0x7FF7)
    assert wiped.src_addr == 0x7FF7
    assert not wiped.needs_rotation
    assert wiped.pick_src.avoided == []
