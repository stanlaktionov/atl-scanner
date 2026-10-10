"""Normalize Binance klines into ohlcv_1h frames and resample 1h -> 2h.

Also normalizes exchangeInfo symbols plus ticker prices into a frame of tradable pairs.
"""

from __future__ import annotations

from collections.abc import Sequence
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
