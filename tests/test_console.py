"""Tests for console.py: status lines of the terminal progress, driven by a fake clock."""

from __future__ import annotations

import threading
import time

import pytest

from atl_scanner.console import ConsoleProgress, format_duration


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00"), (59.9, "0:59"), (61, "1:01"), (3600, "1:00:00"), (3725, "1:02:05"), (-5, "0:00")],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


def make_progress(interval_s: float = 30) -> tuple[ConsoleProgress, FakeClock, list[str]]:
    clock, lines = FakeClock(), []
    return ConsoleProgress(interval_s, lines.append, clock), clock, lines


def test_stage_is_written_at_once_with_elapsed_time() -> None:
    progress, clock, lines = make_progress()
    clock.now += 75
    progress.stage("coingecko: markets")
    assert lines == ["[1:15] coingecko: markets"]


def test_status_shows_time_in_step() -> None:
    progress, clock, _ = make_progress()
    progress.stage("metrics and screen")
    clock.now += 40
    assert progress.status() == "metrics and screen, 0:40 in this step"


def test_candle_start_and_end_are_written() -> None:
    progress, clock, lines = make_progress()
    progress.candles(0, 2, "")
    clock.now += 10
    progress.candles(1, 2, "ABCUSDT")
    progress.candles(2, 2, "NEWUSDT")
    assert lines == ["[0:00] candles: 2 symbols to update", "[0:10] candles: 2/2 done"]


def test_candle_status_estimates_time_left() -> None:
    progress, clock, _ = make_progress()
    progress.candles(0, 300, "")
    clock.now += 60
    progress.candles(100, 300, "LINKUSDT")  # 100 symbols per minute -> 2 minutes left
    assert progress.status() == "candles 100/300 (LINKUSDT), ~2:00 left"


def test_stalled_candles_are_reported() -> None:
    progress, clock, _ = make_progress(interval_s=30)
    progress.candles(0, 300, "")
    clock.now += 60
    progress.candles(100, 300, "LINKUSDT")
    clock.now += 29
    assert "no progress" not in progress.status()
    clock.now += 61
    assert progress.status().endswith(", no progress for 1:30")


def test_new_stage_clears_candles() -> None:
    progress, clock, _ = make_progress()
    progress.candles(0, 2, "")
    progress.candles(2, 2, "ABCUSDT")
    progress.stage("reports")
    assert progress.status().startswith("reports")


def test_heartbeat_thread_writes_and_stops() -> None:
    lines: list[str] = []
    beats = threading.Event()

    def write(line: str) -> None:
        lines.append(line)
        if len(lines) >= 3:
            beats.set()

    with ConsoleProgress(0.01, write) as progress:
        progress.stage("coingecko: markets")
        assert beats.wait(timeout=5)
    count = len(lines)
    time.sleep(0.05)
    assert all("coingecko: markets" in line for line in lines)
    assert len(lines) == count  # nothing written after exit
