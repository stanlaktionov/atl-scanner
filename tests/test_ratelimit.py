"""Tests for sources/ratelimit.py with a fake clock."""

from __future__ import annotations

import pytest

from atl_scanner.sources.ratelimit import SlidingWindowLimiter
from tests.conftest import FakeClock


def make(clock: FakeClock, capacity: int) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(capacity, 60.0, clock=clock.now, sleep=clock.sleep)


def test_within_capacity_does_not_wait(clock: FakeClock) -> None:
    limiter = make(clock, 3)
    for _ in range(3):
        limiter.acquire()
    assert clock.sleeps == []


def test_waits_until_oldest_call_leaves_window(clock: FakeClock) -> None:
    limiter = make(clock, 3)
    for _ in range(3):
        limiter.acquire()
        clock.t += 10  # calls at t=0, 10, 20
    limiter.acquire()  # at t=30: the t=0 call expires at t=60
    assert clock.sleeps == [30]


def test_weighted_cost(clock: FakeClock) -> None:
    limiter = make(clock, 10)
    limiter.acquire(6)
    limiter.acquire(4)
    assert clock.sleeps == []
    limiter.acquire(6)
    assert clock.sleeps == [60]


def test_cost_above_capacity_is_rejected(clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="cost"):
        make(clock, 10).acquire(11)


def test_server_usage_blocks_until_fixed_window_resets(clock: FakeClock) -> None:
    limiter = make(clock, 10)
    clock.t = 125.0
    limiter.sync_server_usage(10)  # other clients used the whole minute [120, 180)
    limiter.acquire(1)
    assert clock.sleeps == [55]


def test_server_usage_expires_after_reset(clock: FakeClock) -> None:
    limiter = make(clock, 10)
    limiter.sync_server_usage(10)
    clock.t = 61.0
    limiter.acquire(1)
    assert clock.sleeps == []
