"""Tests for normalize/binance.py."""

from __future__ import annotations

import pandas as pd

from atl_scanner.normalize.binance import PAIR_COLUMNS, normalize_pairs


def symbol(sym: str, base: str, status: str = "TRADING", spot: bool = True) -> dict[str, object]:
    return {
        "symbol": sym,
        "baseAsset": base,
        "quoteAsset": "USDT",
        "status": status,
        "isSpotTradingAllowed": spot,
    }


def test_only_trading_spot_pairs_with_prices() -> None:
    symbols = [
        symbol("BTCUSDT", "BTC"),
        symbol("OLDUSDT", "OLD", status="BREAK"),
        symbol("NOSPOTUSDT", "NOSPOT", spot=False),
        symbol("NEWUSDT", "NEW"),
    ]
    prices = [{"symbol": "BTCUSDT", "price": "60000.5"}, {"symbol": "ETHBTC", "price": "0.03"}]
    frame = normalize_pairs(symbols, prices)
    assert list(frame.columns) == PAIR_COLUMNS
    assert frame["symbol"].tolist() == ["BTCUSDT", "NEWUSDT"]
    assert frame.loc[0, "price"] == 60000.5
    assert pd.isna(frame.loc[1, "price"])
    assert frame["price"].dtype == "float64"


def test_empty() -> None:
    frame = normalize_pairs([], [])
    assert frame.empty and list(frame.columns) == PAIR_COLUMNS
