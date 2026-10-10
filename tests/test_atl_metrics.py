"""Tests for metrics/atl.py and metrics/indicators.py with hand-computed expectations
(step 7 of PLAN.md). Required for every change in metrics/.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from atl_scanner.metrics.atl import (
    NO_POINT,
    PricePoint,
    age_status,
    atl_divergence_log,
    candle_atl,
    days_since,
    dist_atr,
    dist_log,
    dist_pct,
    is_new_low_in_window,
    last_close,
    metric_row,
)
from atl_scanner.metrics.indicators import atr_wilder, last_atr, true_range
from atl_scanner.models import AgeStatus

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NAN = float("nan")


def hlc(*rows: tuple[float, float, float]) -> pd.DataFrame:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    return pd.DataFrame(
        {
            "open_time": [start + timedelta(hours=i) for i in range(len(rows))],
            "high": [r[0] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[2] for r in rows],
        }
    )


# Hand calculation, period 3:
#   row  high low close  TR = max(h-l, |h-prev_c|, |l-prev_c|)
#   0    10   8   9      2                (first row: h-l)
#   1    11   9   10     max(2, 2, 0)  = 2
#   2    12   10  11     max(2, 2, 0)  = 2   ATR = (2+2+2)/3        = 2
#   3    15   11  14     max(4, 4, 0)  = 4   ATR = (2*2 + 4)/3      = 8/3
#   4    13   7   8      max(6, 1, 7)  = 7   ATR = (8/3*2 + 7)/3    = 37/9
CANDLES = hlc((10, 8, 9), (11, 9, 10), (12, 10, 11), (15, 11, 14), (13, 7, 8))


# --- indicators ----------------------------------------------------------------


def test_true_range_hand_calculated() -> None:
    assert true_range(CANDLES).tolist() == [2, 2, 2, 4, 7]


def test_true_range_uses_previous_close_across_gap() -> None:
    gap_up = hlc((10, 9, 10), (20, 18, 19))
    assert true_range(gap_up).tolist() == [1, 10]


def test_atr_wilder_hand_calculated() -> None:
    atr = atr_wilder(CANDLES, 3).tolist()
    assert math.isnan(atr[0]) and math.isnan(atr[1])
    assert atr[2:] == pytest.approx([2, 8 / 3, 37 / 9])


def test_last_atr() -> None:
    assert last_atr(CANDLES, 3) == pytest.approx(37 / 9)


def test_atr_period_one_is_true_range() -> None:
    assert atr_wilder(CANDLES, 1).tolist() == pytest.approx([2, 2, 2, 4, 7])


def test_atr_history_shorter_than_period_is_nan() -> None:
    assert math.isnan(last_atr(CANDLES.head(2), 3))
    assert atr_wilder(CANDLES.head(2), 3).isna().all()


def test_atr_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        atr_wilder(CANDLES, 0)


# --- distances ----------------------------------------------------------------


def test_distances_hand_calculated() -> None:
    assert dist_pct(2.0, 1.0) == pytest.approx(1.0)
    assert dist_log(2.0, 1.0) == pytest.approx(math.log(2))
    assert dist_atr(2.0, 1.0, 0.25) == pytest.approx(4.0)
    assert dist_pct(1.5, 1.2) == pytest.approx(0.25)
    assert dist_log(1.5, 1.2) == pytest.approx(math.log(1.25))


def test_price_equal_to_atl_is_zero() -> None:
    assert dist_pct(3.0, 3.0) == 0
    assert dist_log(3.0, 3.0) == 0
    assert dist_atr(3.0, 3.0, 0.1) == 0


def test_price_below_atl_is_negative() -> None:
    # possible when the ATL source lags behind the price source
    assert dist_pct(0.9, 1.0) == pytest.approx(-0.1)
    assert dist_log(0.9, 1.0) < 0
    assert dist_atr(0.9, 1.0, 0.05) == pytest.approx(-2.0)


@pytest.mark.parametrize("atl", [0.0, -1.0, NAN, math.inf])
def test_invalid_atl_gives_nan(atl: float) -> None:
    assert math.isnan(dist_pct(1.0, atl))
    assert math.isnan(dist_log(1.0, atl))
    assert math.isnan(dist_atr(1.0, atl, 0.1))


@pytest.mark.parametrize("price", [NAN, math.inf])
def test_invalid_price_gives_nan(price: float) -> None:
    assert math.isnan(dist_pct(price, 1.0))
    assert math.isnan(dist_log(price, 1.0))
    assert math.isnan(dist_atr(price, 1.0, 0.1))


def test_zero_price_only_breaks_log() -> None:
    assert dist_pct(0.0, 1.0) == -1.0
    assert math.isnan(dist_log(0.0, 1.0))


@pytest.mark.parametrize("atr", [0.0, -0.1, NAN])
def test_invalid_atr_gives_nan(atr: float) -> None:
    assert math.isnan(dist_atr(2.0, 1.0, atr))


def test_atl_divergence() -> None:
    assert atl_divergence_log(2.0, 1.0) == pytest.approx(math.log(2))
    assert atl_divergence_log(1.0, 1.0) == 0
    assert atl_divergence_log(0.5, 1.0) < 0  # Binance below the all-time low: suspicious
    assert math.isnan(atl_divergence_log(1.0, 0.0))


# --- candles -> ATL / price -----------------------------------------------------------


def test_candle_atl_first_occurrence_and_time() -> None:
    frame = hlc((5, 3, 4), (5, 2, 4), (5, 2, 4), (5, 4, 4))
    point = candle_atl(frame)
    assert point.price == 2
    assert point.ts == datetime(2026, 10, 1, 1, tzinfo=UTC)


def test_candle_atl_skips_nan_and_applies_multiplier() -> None:
    frame = hlc((5, NAN, 4), (5, 3000, 4))
    assert candle_atl(frame, multiplier=1000).price == pytest.approx(3.0)


def test_candle_atl_empty_or_all_nan() -> None:
    assert candle_atl(hlc()) == NO_POINT
    assert candle_atl(hlc((1, NAN, 1))) == NO_POINT


def test_candle_atl_rejects_naive_time() -> None:
    frame = hlc((5, 3, 4))
    frame["open_time"] = frame["open_time"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        candle_atl(frame)


def test_last_close_is_stamped_at_candle_close() -> None:
    point = last_close(CANDLES, multiplier=2)
    assert point.price == 4.0
    assert point.ts == datetime(2026, 10, 1, 5, tzinfo=UTC)


def test_last_close_empty() -> None:
    assert last_close(hlc()) == NO_POINT


# --- windows, age ---------------------------------------------------------------


def test_new_low_window_boundaries() -> None:
    assert is_new_low_in_window(NOW - timedelta(days=30), NOW, 30) is True
    assert is_new_low_in_window(NOW - timedelta(days=30, seconds=1), NOW, 30) is False
    assert is_new_low_in_window(None, NOW, 30) is None


def test_days_since() -> None:
    assert days_since(NOW - timedelta(hours=36), NOW) == pytest.approx(1.5)
    assert math.isnan(days_since(None, NOW))


def test_age_from_binance_first_candle() -> None:
    first = NOW - timedelta(days=365)
    assert age_status(first, None, None, NOW, 365) is AgeStatus.BINANCE_FIRST_CANDLE


def test_age_from_coingecko_dates_when_binance_is_young() -> None:
    young = NOW - timedelta(days=100)
    old = NOW - timedelta(days=800)
    assert age_status(young, old, None, NOW, 365) is AgeStatus.COINGECKO_ATL_OR_ATH_DATE
    assert age_status(None, None, old, NOW, 365) is AgeStatus.COINGECKO_ATL_OR_ATH_DATE


def test_age_unknown_without_evidence() -> None:
    recent = NOW - timedelta(days=10)
    assert age_status(recent, recent, recent, NOW, 365) is AgeStatus.UNKNOWN
    assert age_status(None, None, None, NOW, 365) is AgeStatus.UNKNOWN


# --- row ------------------------------------------------------------------------


def test_metric_row() -> None:
    atl = PricePoint(1.0, NOW - timedelta(days=2))
    price = PricePoint(1.5, NOW)
    row = metric_row(atl, price, atr=0.25, now=NOW, window_days=30)
    assert row["dist_pct"] == pytest.approx(0.5)
    assert row["dist_log"] == pytest.approx(math.log(1.5))
    assert row["dist_atr"] == pytest.approx(2.0)
    assert row["is_new_low_in_window"] is True
    assert row["days_since_atl"] == pytest.approx(2.0)


def test_metric_row_without_candles() -> None:
    row = metric_row(PricePoint(1.0, None), PricePoint(1.5, NOW), atr=NAN, now=NOW, window_days=30)
    assert np.isnan(row["dist_atr"])  # type: ignore[arg-type]
    assert row["is_new_low_in_window"] is None
    assert row["dist_log"] == pytest.approx(math.log(1.5))
