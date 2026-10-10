"""Normalize Binance klines into ohlcv_1h frames and resample 1h -> 2h.

Also normalizes exchangeInfo symbols plus ticker prices into a frame of tradable pairs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

PAIR_COLUMNS = ["symbol", "base_asset", "quote_asset", "price"]


def normalize_pairs(
    symbols: Sequence[dict[str, Any]], prices: Sequence[dict[str, Any]]
) -> pd.DataFrame:
    """Tradable spot pairs with their latest price (NaN if the ticker lacks it)."""
    rows = [
        {"symbol": s["symbol"], "base_asset": s["baseAsset"], "quote_asset": s["quoteAsset"]}
        for s in symbols
        if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", True)
    ]
    pairs = pd.DataFrame(rows, columns=PAIR_COLUMNS[:3])
    price_map = {p["symbol"]: p.get("price") for p in prices}
    pairs["price"] = pd.to_numeric(pairs["symbol"].map(price_map), errors="coerce")
    pairs = pairs.astype(
        {"symbol": "string", "base_asset": "string", "quote_asset": "string", "price": "float64"}
    )
    return pairs.drop_duplicates("symbol").reset_index(drop=True)


OHLCV_COLUMNS = [
    "exchange",
    "symbol",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
]
# Binance kline array layout (first 9 fields used)
KLINE_FIELDS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
]
PRICE_COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]


def normalize_klines(
    raw: Sequence[Sequence[Any]], exchange: str, symbol: str, now: datetime
) -> pd.DataFrame:
    """Raw klines -> ohlcv_1h rows; candles still open at `now` are dropped."""
    if not raw:
        return _empty_ohlcv()
    frame = pd.DataFrame([list(row[: len(KLINE_FIELDS)]) for row in raw], columns=KLINE_FIELDS)
    now_ms = int(now.timestamp() * 1000)
    frame = frame[pd.to_numeric(frame["close_time"]) < now_ms]
    frame = frame.assign(
        exchange=exchange,
        symbol=symbol,
        open_time=pd.to_datetime(pd.to_numeric(frame["open_time"]), unit="ms", utc=True),
    )
    for column in PRICE_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    frame["trades"] = pd.to_numeric(frame["trades"], errors="coerce").astype("Int64")
    frame = frame.drop_duplicates("open_time").sort_values("open_time")
    return _ohlcv_dtypes(frame[OHLCV_COLUMNS].reset_index(drop=True))


def resample_ohlcv(frame: pd.DataFrame, hours: int) -> pd.DataFrame:
    """Aggregate 1h candles of one symbol into `hours`-long candles aligned to 00:00 UTC.

    Buckets missing any 1h candle are dropped, so every output candle is complete.
    """
    if frame.empty:
        return _empty_ohlcv()
    indexed = frame.set_index("open_time").sort_index()
    grouped = indexed.resample(f"{hours}h", origin="epoch", label="left", closed="left")
    out = grouped.agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "quote_volume": "sum",
            "trades": "sum",
        }
    )
    complete = grouped["close"].count() == hours
    out = out[complete].reset_index()
    out["exchange"] = frame["exchange"].iloc[0]
    out["symbol"] = frame["symbol"].iloc[0]
    return _ohlcv_dtypes(out[OHLCV_COLUMNS])


def _ohlcv_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.astype(
        {
            "exchange": "string",
            "symbol": "string",
            **dict.fromkeys(PRICE_COLUMNS, "float64"),
            "trades": "Int64",
        }
    )


def _empty_ohlcv() -> pd.DataFrame:
    frame = pd.DataFrame({c: [] for c in OHLCV_COLUMNS})
    frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True)
    return _ohlcv_dtypes(frame)
