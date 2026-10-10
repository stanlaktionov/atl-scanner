"""Tests for quality.py on a synthetic store with known defects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from atl_scanner.config import load_config
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.quality import (
    atl_divergence,
    bad_candles,
    candle_gaps,
    render_markdown,
    run_checks,
    stale_candles,
    write_markdown,
)
from atl_scanner.screen import SCREEN_COLUMNS
from atl_scanner.store.db import Store
from tests.conftest import EXAMPLE_CONFIG

NOW = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)
START = datetime(2026, 10, 10, 0, tzinfo=UTC)


def candle(symbol: str, hour: int, low: float = 1.0, volume: float = 1.0) -> dict[str, object]:
    return {
        "exchange": "binance",
        "symbol": symbol,
        "open_time": START + timedelta(hours=hour),
        "open": 1.5,
        "high": 2.0,
        "low": low,
        "close": 1.5,
        "volume": volume,
        "quote_volume": volume,
        "trades": 1,
    }


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "db.duckdb")
    rows = [
        {"id": c, "symbol": c, "name": c, "market_cap": 5e7, "current_price": 1.0, "atl": 1.0,
         "last_updated": NOW.isoformat()}
        for c in ("good", "gappy", "bad", "old")
    ]  # fmt: skip
    markets = normalize_markets(rows)
    store.upsert_coins(coins_frame(markets), NOW)
    store.upsert_market_snapshots(snapshots_frame(markets, "fetch-1", NOW))
    store.update_coin_columns(
        pd.DataFrame(
            {
                "cg_id": ["good", "gappy", "bad", "old"],
                "binance_symbol": ["GOODUSDT", "GAPPYUSDT", "BADUSDT", "OLDUSDT"],
                "excluded": [False, False, False, False],
            }
        ),
        NOW,
    )
    candles = [candle("GOODUSDT", h) for h in range(12)]
    candles += [candle("GAPPYUSDT", h) for h in (0, 1, 5, 6, 7, 8, 9, 10, 11)]  # 3h hole
    candles += [candle("BADUSDT", h, volume=0.0 if h < 2 else 1.0) for h in range(11)]
    candles += [candle("BADUSDT", 11, low=1.8)]  # low above open/close: invalid
    candles += [candle("OLDUSDT", h) for h in range(6)]  # stops at 05:00
    store.upsert_ohlcv(pd.DataFrame(candles))
    return store


def test_candle_gaps(store: Store) -> None:
    result = candle_gaps(store, 5)
    assert result.affected == 1
    row = result.details.iloc[0]
    assert row["symbol"] == "GAPPYUSDT"
    assert row["missing_hours"] == 3 and row["longest_gap_hours"] == 3
    assert result.examples == ["GAPPYUSDT (3ч)"]


def test_bad_candles(store: Store) -> None:
    result = bad_candles(store, 5)
    row = result.details.set_index("symbol").loc["BADUSDT"]
    assert row["zero_volume"] == 2 and row["invalid"] == 1
    assert result.affected == 1


def test_stale_candles(store: Store) -> None:
    config = load_config(EXAMPLE_CONFIG)  # stale after 2h: last candle must close >= 10:30
    result = stale_candles(config, store, NOW, 5)
    assert result.details["symbol"].tolist() == ["OLDUSDT"]


def screen_rows(*rows: dict[str, object]) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    for column in SCREEN_COLUMNS:
        if column not in frame:
            frame[column] = None
    return frame[SCREEN_COLUMNS]


def test_divergence_ignores_excluded_coins(store: Store) -> None:
    store.start_run("screen-1", NOW, "h", "yaml")
    store.upsert_screen_results(
        screen_rows(
            {"run_id": "screen-1", "cg_id": "good", "excluded": False, "atl_divergence_log": 0.2,
             "binance_symbol": "GOODUSDT", "age_status": "binance_first_candle"},
            {"run_id": "screen-1", "cg_id": "bad", "excluded": False, "atl_divergence_log": -0.5,
             "binance_symbol": "BADUSDT", "age_status": "unknown"},
            {"run_id": "screen-1", "cg_id": "old", "excluded": True, "atl_divergence_log": -2.0,
             "binance_symbol": "OLDUSDT", "age_status": "binance_first_candle"},
        )
    )  # fmt: skip
    result = atl_divergence(load_config(EXAMPLE_CONFIG), store, 5)
    assert result.details["cg_id"].tolist() == ["bad"]
    assert result.examples == ["BADUSDT (-0.500)"]


def test_all_checks_run_and_render(store: Store, tmp_path: Path) -> None:
    results = run_checks(load_config(EXAMPLE_CONFIG), store, NOW)
    names = [r.name for r in results]
    assert len(names) == 8 and len(set(names)) == 8
    text = render_markdown(results, NOW)
    assert "| Пропуски 1h-свечей | 1 | GAPPYUSDT (3ч) |" in text
    assert "ничего не удалено" in text
    path = write_markdown(results, NOW, tmp_path / "reports")
    assert path.name == "dq-20261010T123000Z.md" and path.exists()


def test_checks_without_screen_run(store: Store) -> None:
    by_name = {r.name: r for r in run_checks(load_config(EXAMPLE_CONFIG), store, NOW)}
    assert by_name["Возраст"].action.startswith("нет данных")
