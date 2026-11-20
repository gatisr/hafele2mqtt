"""The incoming command queue: coalescing, the bound, and the rate limit.

The clock is faked throughout. These are assertions about behaviour, not about
how fast anything is, so nothing here waits on a real second.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app import main as main_mod
from app.main import CommandQueue

LIGHT_A = "hafele/light/a/set"
LIGHT_B = "hafele/light/b/set"


class Clock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(main_mod.time, "monotonic", c)
    return c


def run(body) -> object:
    """Run an async test body.

    Everything goes through here because `offer` belongs to the event loop
    thread - in the bridge it is only ever reached through
    `call_soon_threadsafe` - and it schedules its own report timer, so there has
    to be a running loop for it to attach to.
    """
    return asyncio.run(body())


async def drain(q: CommandQueue, count: int) -> list[tuple[str, str]]:
    """Take `count` commands, with sleeps satisfied instantly by the fake clock."""
    return [await q.get() for _ in range(count)]


# --- ordering and coalescing -------------------------------------------------

def test_commands_come_back_in_order(clock):
    async def go():
        q = CommandQueue(maxsize=8, rate=100.0)
        q.offer(LIGHT_A, "1")
        q.offer(LIGHT_B, "2")
        return await drain(q, 2)

    assert run(go) == [(LIGHT_A, "1"), (LIGHT_B, "2")]


def test_same_topic_is_replaced_not_appended(clock):
    """A slider drag is twenty payloads and one intention."""
    async def go():
        q = CommandQueue(maxsize=8, rate=100.0)
        for value in range(20):
            q.offer(LIGHT_A, str(value))
        assert len(q) == 1
        return await drain(q, 1)

    assert run(go) == [(LIGHT_A, "19")]


def test_a_replaced_command_keeps_its_place(clock):
    """Otherwise one busy light could keep jumping the queue."""
    async def go():
        q = CommandQueue(maxsize=8, rate=100.0)
        q.offer(LIGHT_A, "first")
        q.offer(LIGHT_B, "b")
        q.offer(LIGHT_A, "second")
        return await drain(q, 2)

    assert run(go) == [(LIGHT_A, "second"), (LIGHT_B, "b")]


# --- the bound ---------------------------------------------------------------

def test_a_full_queue_drops_the_oldest(clock):
    async def go():
        q = CommandQueue(maxsize=3, rate=100.0)
        for i in range(5):
            q.offer(f"hafele/light/{i}/set", str(i))
        assert len(q) == 3
        return await drain(q, 3)

    # 0 and 1 are gone; the three most recent survive.
    assert [t for t, _ in run(go)] == ["hafele/light/2/set",
                                       "hafele/light/3/set",
                                       "hafele/light/4/set"]


def test_coalescing_does_not_count_against_the_bound(clock):
    async def go():
        q = CommandQueue(maxsize=2, rate=100.0)
        q.offer(LIGHT_A, "1")
        q.offer(LIGHT_B, "1")
        for value in range(50):
            q.offer(LIGHT_A, str(value))
        assert len(q) == 2
        return await drain(q, 2)

    assert run(go) == [(LIGHT_A, "49"), (LIGHT_B, "1")]


# --- the rate limit ----------------------------------------------------------

def test_a_full_bucket_lets_a_whole_burst_through(clock):
    """A scene switching everything at once must not be throttled."""
    async def go():
        q = CommandQueue(maxsize=16, rate=1.0)
        for i in range(16):
            q.offer(f"hafele/light/{i}/set", "ON")
        # The clock never advances, so no token is refilled: all 16 come from
        # the bucket the queue started with.
        return await drain(q, 16)

    assert len(run(go)) == 16


def test_the_bucket_refills_at_the_configured_rate(clock):
    q = CommandQueue(maxsize=2, rate=4.0)

    async def go():
        q.offer(LIGHT_A, "1")
        q.offer(LIGHT_B, "1")
        await drain(q, 2)            # bucket empty

    run(go)
    assert q._take_token() == pytest.approx(0.25)   # 1 token at 4/s
    clock.advance(0.25)
    assert q._take_token() == 0.0
    clock.advance(10.0)
    # Long idle, but the bucket cannot hold more than a queue's worth.
    assert q._take_token() == 0.0
    assert q._take_token() == 0.0
    assert q._take_token() > 0.0


def test_throttling_keeps_coalescing(clock, monkeypatch):
    """The wait is where a flood collapses into one command per topic.

    Commands keep arriving while the queue is out of tokens; because it is
    the consumer that sleeps, everything for one topic folds into the last
    value instead of queueing up behind the limit.
    """
    q = CommandQueue(maxsize=1, rate=2.0)

    async def go():
        q.offer(LIGHT_A, "0")
        first = await q.get()        # spends the only token
        for value in range(1, 30):
            q.offer(LIGHT_A, str(value))
        # Out of tokens: get() sleeps, and the sleep is what the fake clock
        # turns into an instant - so the queue is read after the flood.
        clock.advance(1.0)
        second = await q.get()
        return first, second

    first, second = asyncio.run(go())
    assert first == (LIGHT_A, "0")
    assert second == (LIGHT_A, "29")
    assert len(q) == 0


# --- reporting and shutdown ---------------------------------------------------

def test_a_flood_produces_one_log_line_not_a_hundred(clock, caplog):
    q = CommandQueue(maxsize=2, rate=100.0)

    async def go():
        with caplog.at_level(logging.WARNING, logger="bridge"):
            for i in range(200):
                q.offer(f"hafele/light/{i}/set", "ON")

    asyncio.run(go())
    lines = [r for r in caplog.records if "command rate limit" in r.getMessage()]
    assert len(lines) == 1
    # 198 drops, folded into the one line that was logged first.
    assert "1 dropped" in lines[0].getMessage()


def test_close_releases_a_waiting_get(clock):
    q = CommandQueue(maxsize=4, rate=100.0)

    async def go():
        waiter = asyncio.ensure_future(q.get())
        await asyncio.sleep(0)       # let it reach the wait
        q.close()
        return await asyncio.wait_for(waiter, 1.0)

    assert asyncio.run(go()) == ("", "")


def test_a_closed_queue_takes_nothing_more(clock):
    async def go():
        q = CommandQueue(maxsize=4, rate=100.0)
        q.close()
        q.offer(LIGHT_A, "ON")
        assert len(q) == 0
        return await drain(q, 1)

    assert run(go) == [("", "")]
