"""Tests for pipeline.compute_metrics on a small synthetic store."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from atl_scanner.config import AppConfig, load_config
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.pipeline import compute_metrics
from atl_scanner.store.db import Store
from tests.conftest import EXAMPLE_CONFIG

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def config(period: int = 3, window: int = 1) -> AppConfig:
    base = load_config(EXAMPLE_CONFIG)
    metrics = base.metrics.model_copy(
        # "low" keeps the hand-calculated ATL below; "close" is covered in test_atl_metrics
        update={"atr_period": period, "new_low_window_days": window, "binance_atl_price": "low"}
    )
    return base.model_copy(update={"metrics": metrics})


def candles(symbol: str, start: datetime, lows: list[float]) -> pd.DataFrame:
    """Candles with high = low + 2 and close = low + 1, so every TR is 2 (in quoted units)."""
    return pd.DataFrame(
        {
            "exchange": "binance",
            "symbol": symbol,
            "open_time": [start + timedelta(hours=i) for i in range(len(lows))],
            "open": [x + 1 for x in lows],
            "high": [x + 2 for x in lows],
            "low": lows,
            "close": [x + 1 for x in lows],
            "volume": 1.0,
            "quote_volume": 1.0,
            "trades": pd.array([1] * len(lows), dtype="Int64"),
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "db.duckdb")
    old = (NOW - timedelta(days=900)).isoformat()
    rows = [
        {
            "id": "sats",
            "symbol": "sats",
            "name": "Sats",
            "market_cap": 3e8,
            "current_price": 0.012,
            "atl": 0.004,
            "atl_date": old,
            "ath_date": old,
            "last_updated": NOW.isoformat(),
        },
        {
            "id": "lonely",
            "symbol": "lnl",
            "name": "Lonely",
            "market_cap": 5e7,
            "current_price": 2.0,
            "atl": 1.0,
            "atl_date": (NOW - timedelta(days=10)).isoformat(),
            "ath_date": (NOW - timedelta(days=20)).isoformat(),
            "last_updated": NOW.isoformat(),
        },
    ]
    markets = normalize_markets(rows)
    store.upsert_coins(coins_frame(markets), NOW)
    store.upsert_market_snapshots(snapshots_frame(markets, "snap-1", NOW))
    store.update_coin_columns(
        pd.DataFrame(
            {
                "cg_id": ["sats", "lonely"],
                "binance_symbol": ["1000SATSUSDT", None],
                "binance_price_multiplier": pd.array([1000, None], dtype="Int64"),
                "mapping_status": ["mapped", "no_pair"],
                "excluded": [False, False],
            }
        ),
        NOW,
    )
    # 1000SATS quotes: lows 10, 6, 8, 9 -> per token 0.010, 0.006, 0.008, 0.009
    start = NOW - timedelta(hours=4)
    store.upsert_ohlcv(candles("1000SATSUSDT", start, [10.0, 6.0, 8.0, 9.0]))
    return store


def test_two_sources_per_paired_coin(store: Store) -> None:
    result = compute_metrics(config(), store, "run-1", NOW)
    rows = result.atl_metrics.set_index(["cg_id", "atl_source"])
    assert len(rows) == 3  # sats: both sources, lonely: CoinGecko only
    assert (result.atl_metrics["run_id"] == "run-1").all()

    bn = rows.loc[("sats", "binance_1y")]
    assert bn["atl_price"] == pytest.approx(0.006)  # min low 6 / 1000
    assert bn["atl_ts"] == NOW - timedelta(hours=3)
    assert bn["price"] == pytest.approx(0.010)  # last close 10 / 1000
    assert bn["price_ts"] == NOW  # close of the 11:00 candle
    # True ranges in quoted units, rows (h,l,c): (12,10,11) (8,6,7) (10,8,9) (11,9,10)
    # TR: 2, max(2,|8-11|,|6-11|)=5, max(2,|10-7|,|8-7|)=3, max(2,|11-9|,|9-9|)=2
    # ATR(3): (2+5+3)/3 = 10/3, then (10/3*2 + 2)/3 = 26/9; per token / 1000
    atr = 26 / 9 / 1000
    assert bn["atr_value"] == pytest.approx(atr)
    assert bn["dist_pct"] == pytest.approx((0.010 - 0.006) / 0.006)
    assert bn["dist_log"] == pytest.approx(math.log(0.010 / 0.006))
    assert bn["dist_atr"] == pytest.approx((0.010 - 0.006) / atr)
    assert bn["is_new_low_in_window"]

    cg = rows.loc[("sats", "coingecko_all_time")]
    assert cg["atl_price"] == 0.004 and cg["price"] == 0.012
    assert cg["dist_atr"] == pytest.approx((0.012 - 0.004) / atr)
    assert not cg["is_new_low_in_window"]


def test_wide_rows(store: Store) -> None:
    wide = compute_metrics(config(), store, "run-1", NOW).coins.set_index("cg_id")
    sats = wide.loc["sats"]
    assert sats["age_status"] == "coingecko_atl_or_ath_date"  # candles are only 4h old
    assert sats["atl_divergence_log"] == pytest.approx(math.log(0.006 / 0.004))
    assert sats["first_candle_ts"] == NOW - timedelta(hours=4)

    lonely = wide.loc["lonely"]
    assert lonely["age_status"] == "unknown"
    assert math.isnan(lonely["bn_dist_log"]) and math.isnan(lonely["cg_dist_atr"])
    assert lonely["cg_dist_log"] == pytest.approx(math.log(2))
    assert lonely["mapping_status"] == "no_pair"


def test_metrics_round_trip_through_store(store: Store) -> None:
    result = compute_metrics(config(), store, "run-1", NOW)
    store.upsert_atl_metrics(result.atl_metrics)
    store.upsert_atl_metrics(result.atl_metrics)
    assert store.count("atl_metrics") == 3
