"""Terminal progress for long runs: stage changes at once, a status line on a fixed interval."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Self

Clock = Callable[[], float]
Write = Callable[[str], None]


def format_duration(seconds: float) -> str:
    """Seconds as m:ss, or h:mm:ss from one hour on."""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class ConsoleProgress:
    """Shows that a run is alive: what step it is on, how far candles got, how long it idles.

    Use as a context manager; `stage` and `candles` are the pipeline callbacks. A status
    line is written every `interval_s`: time spent in the current step, or for candles the
    count, a rough time left and, if the count stalls, for how long — so a stuck run shows.
    """

    def __init__(self, interval_s: float, write: Write, clock: Clock = time.monotonic) -> None:
        self._interval = interval_s
        self._write = write
        self._clock = clock
        self._lock = threading.Lock()
        self._started = clock()
        self._advanced = self._started
        self._stage = "starting"
        self._candles: tuple[int, int, str] | None = None
        self._candles_started = self._started
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="console-progress", daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()

    def stage(self, name: str) -> None:
        with self._lock:
            self._stage, self._candles = name, None
            self._advanced = self._clock()
        self._emit(name)

    def candles(self, done: int, total: int, symbol: str) -> None:
        with self._lock:
            now = self._clock()
            if done == 0:
                self._candles_started = now
            self._candles, self._advanced = (done, total, symbol), now
        if done == 0:
            self._emit(f"candles: {total} symbols to update")
        elif done == total:
            self._emit(f"candles: {done}/{total} done")

    def status(self) -> str:
        """Current status line without the elapsed-time prefix."""
        with self._lock:
            now = self._clock()
            idle = now - self._advanced
            if self._candles is None:
                return f"{self._stage}, {format_duration(idle)} in this step"
            done, total, symbol = self._candles
            started = self._candles_started
        line = f"candles {done}/{total}" + (f" ({symbol})" if symbol else "")
        if 0 < done < total:
            line += f", ~{format_duration((now - started) / done * (total - done))} left"
        if idle >= self._interval:
            line += f", no progress for {format_duration(idle)}"
        return line

    def _beat(self) -> None:
        while not self._stop.wait(self._interval):
            self._emit(self.status())

    def _emit(self, message: str) -> None:
        self._write(f"[{format_duration(self._clock() - self._started)}] {message}")
