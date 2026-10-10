"""Tests for the single-coin lookup: pipeline.find_coins / coin_details and `atl-scanner coin`."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from atl_scanner.cli import app
from atl_scanner.config import load_config
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.pipeline import NoMarketDataError, coin_details, find_coins
from atl_scanner.store.db import Store
from tests.conftest import EXAMPLE_CONFIG

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def fill(store: Store) -> None:
    old = (NOW - timedelta(days=900)).isoformat()
    rows = [
        {"id": "cosmos", "symbol": "atom", "name": "Cosmos Hub", "market_cap": 1e9,
         "current_price": 2.0, "atl": 1.0, "atl_date": old, "ath_date": old,
         "last_updated": NOW.isoformat()},
        {"id": "memetoon", "symbol": "meme", "name": "MEMETOON", "market_cap": 5e7,
         "current_price": 0.0005, "atl": 0.0004, "last_updated": NOW.isoformat()},
        {"id": "memecoin-2", "symbol": "meme", "name": "Memecoin", "market_cap": 4e7,
         "current_price": 0.00057, "atl": 0.0004, "last_updated": NOW.isoformat()},
    ]  # fmt: skip
    markets = normalize_markets(rows)
    store.upsert_coins(coins_frame(markets), NOW)
    store.upsert_market_snapshots(snapshots_frame(markets, "fetch-1", NOW))
    store.update_coin_columns(
        pd.DataFrame(
            {
                "cg_id": ["cosmos", "memecoin-2", "memetoon"],
                "binance_symbol": ["ATOMUSDT", "MEMEUSDT", None],
                "binance_price_multiplier": pd.array([1, 1, None], dtype="Int64"),
                "mapping_status": ["mapped", "ambiguous", "ambiguous"],
                "excluded": [False, False, False],
            }
        ),
        NOW,
    )
    start = NOW - timedelta(hours=20)
    lows = [1.5] * 10 + [1.2] + [1.6] * 9
    store.upsert_ohlcv(
        pd.DataFrame(
            {
                "exchange": "binance",
                "symbol": "ATOMUSDT",
                "open_time": [start + timedelta(hours=i) for i in range(20)],
                "open": [x + 0.1 for x in lows],
                "high": [x + 0.2 for x in lows],
                "low": lows,
                "close": [x + 0.1 for x in lows],
                "volume": 1.0,
                "quote_volume": 1.0,
                "trades": pd.array([1] * 20, dtype="Int64"),
            }
        )
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "data" / "scanner.duckdb")
    fill(store)
    return store


@pytest.mark.parametrize("query", ["cosmos", "ATOM", "atom", "atomusdt", "Cosmos Hub", " atom "])
def test_find_by_id_ticker_pair_or_name(store: Store, query: str) -> None:
    assert find_coins(store, query)["cg_id"].tolist() == ["cosmos"]


def test_ambiguous_ticker_and_id_wins(store: Store) -> None:
    assert sorted(find_coins(store, "meme")["cg_id"]) == ["memecoin-2", "memetoon"]
    assert find_coins(store, "memecoin-2")["cg_id"].tolist() == ["memecoin-2"]


def test_not_found_and_empty_store(store: Store, tmp_path: Path) -> None:
    assert find_coins(store, "nope").empty
    with Store(tmp_path / "empty.duckdb") as empty, pytest.raises(NoMarketDataError):
        find_coins(empty, "atom")


def test_coin_details(store: Store) -> None:
    details = coin_details(load_config(EXAMPLE_CONFIG), store, "cosmos", NOW)
    bn = details.metrics["binance_1y"]
    assert bn["atl_price"] == pytest.approx(1.3)  # min close = 1.2 + 0.1
    assert bn["price"] == pytest.approx(1.7)
    cg = details.metrics["coingecko_all_time"]
    assert cg["dist_log"] == pytest.approx(math.log(2.0))
    assert details.candles == 20
    assert details.first_candle == NOW - timedelta(hours=20)
    assert details.screen["age_status"] == "coingecko_atl_or_ath_date"
    assert details.screen["passes_all"]


def test_coin_details_unknown_id(store: Store) -> None:
    with pytest.raises(KeyError):
        coin_details(load_config(EXAMPLE_CONFIG), store, "nope", NOW)


# --- CLI ------------------------------------------------------------------------


@pytest.fixture
def cli_config(tmp_path: Path, store: Store) -> Path:
    store.close()
    data: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["storage"] = {
        "data_dir": str(tmp_path / "data"),
        "raw_dir": str(tmp_path / "data" / "raw"),
        "db_path": str(tmp_path / "data" / "scanner.duckdb"),
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_cli_coin_card(cli_config: Path) -> None:
    result = CliRunner().invoke(app, ["coin", "atom", "-c", str(cli_config)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "ATOM — Cosmos Hub (cosmos)" in out
    assert "ATOMUSDT (mapped" in out
    assert "свечи 1h:       20" in out
    assert "→ прошла все: да" in out
    assert "Data provided by CoinGecko" in out


def test_cli_coin_ambiguous(cli_config: Path) -> None:
    result = CliRunner().invoke(app, ["coin", "meme", "-c", str(cli_config)])
    assert result.exit_code == 2
    assert "memecoin-2" in result.output and "memetoon" in result.output


def test_cli_coin_not_found(cli_config: Path) -> None:
    result = CliRunner().invoke(app, ["coin", "nope", "-c", str(cli_config)])
    assert result.exit_code == 1
    assert "not found" in result.output
