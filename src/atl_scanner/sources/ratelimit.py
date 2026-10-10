"""Rate limiters: calls-per-minute (CoinGecko) and weight-per-minute (Binance,
synced with the X-MBX-USED-WEIGHT-1M response header).

One class covers both: a CoinGecko call costs 1, a Binance call costs its request weight.
Thread-safe: concurrent workers share one limiter.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class SlidingWindowLimiter:
    """Allow at most `capacity` cost units per rolling `window_s` seconds.

    Optionally also respects usage reported by the server for its fixed window
    (e.g. Binance counts weight per calendar minute, including other clients on the IP).
    """

    def __init__(
        self,
        capacity: int,
        window_s: float = 60.0,
        *,
        clock: Clock = time.time,
        sleep: Sleep = time.sleep,
    ) -> None:
        if capacity <= 0 or window_s <= 0:
            raise ValueError("capacity and window_s must be positive")
        self.capacity = capacity
        self.window_s = window_s
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, int]] = deque()
        self._server_used = 0
        self._server_reset_at = 0.0
        self._lock = threading.Lock()

    def acquire(self, cost: int = 1) -> None:
        """Block until `cost` units fit into the window, then record them."""
        if not 0 < cost <= self.capacity:
            raise ValueError(f"cost must be in 1..{self.capacity}, got {cost}")
        while True:
            with self._lock:
                wait = self._wait_time(cost)
                if wait <= 0:
                    self._events.append((self._clock(), cost))
                    return
            self._sleep(wait)

    def sync_server_usage(self, used: int) -> None:
        """Record usage the server reports for its current fixed window."""
        with self._lock:
            now = self._clock()
            self._server_used = used
            self._server_reset_at = (now // self.window_s + 1) * self.window_s

    def _wait_time(self, cost: int) -> float:
        now = self._clock()
        self._evict(now)
        return max(self._local_wait(now, cost), self._server_wait(now, cost))

    def _evict(self, now: float) -> None:
        while self._events and self._events[0][0] <= now - self.window_s:
            self._events.popleft()

    def _local_wait(self, now: float, cost: int) -> float:
        used = sum(c for _, c in self._events)
        if used + cost <= self.capacity:
            return 0.0
        freed = 0
        for ts, c in self._events:
            freed += c
            if used - freed + cost <= self.capacity:
                return ts + self.window_s - now
        return self.window_s  # unreachable: cost <= capacity

    def _server_wait(self, now: float, cost: int) -> float:
        if now < self._server_reset_at and self._server_used + cost > self.capacity:
            return self._server_reset_at - now
        return 0.0
