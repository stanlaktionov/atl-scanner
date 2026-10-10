"""Technical indicators on OHLCV frames, starting with ATR(N) (Wilder smoothing)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def true_range(frame: pd.DataFrame) -> pd.Series:
    """max(high - low, |high - prev_close|, |low - prev_close|); first row is high - low."""
    prev_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1, skipna=True)


def atr_wilder(frame: pd.DataFrame, period: int) -> pd.Series:
    """ATR with Wilder smoothing.

    The first value (at row period-1) is the mean of the first `period` true ranges, then
    ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period. Earlier rows are NaN.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    tr = true_range(frame).to_numpy(dtype="float64")
    out = np.full(len(tr), np.nan)
    if len(tr) >= period:
        out[period - 1] = tr[:period].mean()
        for i in range(period, len(tr)):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(out, index=frame.index)


def last_atr(frame: pd.DataFrame, period: int) -> float:
    """Latest ATR value, NaN if the history is shorter than `period`."""
    if len(frame) < period:
        return math.nan
    return float(atr_wilder(frame, period).iloc[-1])
