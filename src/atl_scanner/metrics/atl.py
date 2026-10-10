"""ATL metrics per coin and ATL source (coingecko_all_time, binance_1y):
dist_pct, dist_log, dist_atr, new-low-in-window, days_since_atl, ATL divergence, age status.

Invalid inputs (ATL <= 0, NaN, too-short history) yield NaN rather than raising.
All prices passed here are per one token: Binance prices are divided by the pair's
multiplier (1000SATS -> SATS) before they are compared with CoinGecko.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from atl_scanner.models import AgeStatus


@dataclass(frozen=True)
class PricePoint:
    """A price and when it was observed (None if unknown)."""

    price: float
    ts: datetime | None


NO_POINT = PricePoint(math.nan, None)


def _positive(value: float) -> bool:
    return math.isfinite(value) and value > 0


def dist_pct(price: float, atl: float) -> float:
    """(price - ATL) / ATL."""
    if not (_positive(atl) and math.isfinite(price)):
        return math.nan
    return (price - atl) / atl


def dist_log(price: float, atl: float) -> float:
    """ln(price) - ln(ATL)."""
    if not (_positive(price) and _positive(atl)):
        return math.nan
    return math.log(price) - math.log(atl)


def dist_atr(price: float, atl: float, atr: float) -> float:
    """(price - ATL) / ATR, i.e. distance to the ATL in typical hourly ranges."""
    if not (_positive(atr) and _positive(atl) and math.isfinite(price)):
        return math.nan
    return (price - atl) / atr


def atl_divergence_log(binance_atl: float, coingecko_atl: float) -> float:
    """ln(ATL_binance_1y) - ln(ATL_coingecko); >= 0 expected, < 0 hints at bad data."""
    return dist_log(binance_atl, coingecko_atl)


def candle_atl(candles: pd.DataFrame, multiplier: int = 1) -> PricePoint:
    """Lowest low in the candles (first occurrence), per token."""
    lows = candles["low"].to_numpy(dtype="float64")
    if lows.size == 0 or np.isnan(lows).all():
        return NO_POINT
    i = int(np.nanargmin(lows))
    return PricePoint(float(lows[i]) / multiplier, _to_datetime(candles["open_time"].iloc[i]))


def last_close(candles: pd.DataFrame, multiplier: int = 1) -> PricePoint:
    """Close of the latest candle, timestamped at its close (open + 1h), per token."""
    if candles.empty:
        return NO_POINT
    last = candles.iloc[-1]
    return PricePoint(
        float(last["close"]) / multiplier, _to_datetime(last["open_time"]) + timedelta(hours=1)
    )


def is_new_low_in_window(atl_ts: datetime | None, now: datetime, window_days: int) -> bool | None:
    """True if the ATL was set within the last `window_days` (None if its time is unknown)."""
    if atl_ts is None:
        return None
    return atl_ts >= now - timedelta(days=window_days)


def days_since(ts: datetime | None, now: datetime) -> float:
    if ts is None:
        return math.nan
    return (now - ts).total_seconds() / 86_400


def age_status(
    first_candle: datetime | None,
    atl_date: datetime | None,
    ath_date: datetime | None,
    now: datetime,
    min_age_days: int,
) -> AgeStatus:
    """Evidence that the coin had a price at least `min_age_days` ago.

    Binance's first stored candle is checked first; otherwise a CoinGecko ATL/ATH date that
    old proves the coin was trading then. Without evidence the age is unknown, not "young".
    """
    cutoff = now - timedelta(days=min_age_days)
    if first_candle is not None and first_candle <= cutoff:
        return AgeStatus.BINANCE_FIRST_CANDLE
    if any(d is not None and d <= cutoff for d in (atl_date, ath_date)):
        return AgeStatus.COINGECKO_ATL_OR_ATH_DATE
    return AgeStatus.UNKNOWN


def metric_row(
    atl: PricePoint,
    price: PricePoint,
    atr: float,
    now: datetime,
    window_days: int,
) -> dict[str, object]:
    """Distance metrics for one coin and one ATL source."""
    return {
        "atl_price": atl.price,
        "atl_ts": atl.ts,
        "price": price.price,
        "price_ts": price.ts,
        "dist_pct": dist_pct(price.price, atl.price),
        "dist_log": dist_log(price.price, atl.price),
        "atr_value": atr,
        "dist_atr": dist_atr(price.price, atl.price, atr),
        "is_new_low_in_window": is_new_low_in_window(atl.ts, now, window_days),
        "days_since_atl": days_since(atl.ts, now),
    }


def _to_datetime(value: object) -> datetime:
    ts = pd.Timestamp(value)  # type: ignore[arg-type]
    if ts.tzinfo is None:
        raise ValueError("candle timestamps must be timezone-aware")
    return ts.to_pydatetime()
