"""Tests for store/db.py and the recorded CoinGecko run in pipeline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from atl_scanner.config import load_config
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.pipeline import new_run_id, run_fetch
from atl_scanner.sources.cache import RawCache
from atl_scanner.sources.http import ApiClient
from atl_scanner.sources.ratelimit import SlidingWindowLimiter
from atl_scanner.store.db import TABLES, Store
from tests.conftest import EXAMPLE_CONFIG, FakeClock

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "coingecko" / "markets_sample.json"


def sample_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return rows


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "db" / "test.duckdb")


def test_schema_created(store: Store) -> None:
    tables = set(store.query_df("SELECT table_name FROM information_schema.tables")["table_name"])
    assert set(TABLES) <= tables


def test_schema_creation_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "test.duckdb"
    Store(path).close()
    with Store(path) as store:
        assert store.count("coins") == 0


def test_upsert_coins_is_idempotent_and_keeps_first_seen(store: Store) -> None:
    markets = normalize_markets(sample_rows())
    store.upsert_coins(coins_frame(markets), T0)
    store.upsert_coins(coins_frame(markets), T0 + timedelta(hours=1))
    assert store.count("coins") == 5
    renamed = coins_frame(markets).assign(name=lambda f: f["name"] + " (renamed)")
    store.upsert_coins(renamed, T0 + timedelta(hours=2))
    row = store.query_df("SELECT * FROM coins WHERE cg_id = 'bitcoin'").iloc[0]
    assert row["name"] == "Bitcoin (renamed)"
    assert row["first_seen_at"] == datetime(2026, 10, 10, 12, 0)
    assert row["updated_at"] == datetime(2026, 10, 10, 14, 0)


def test_upsert_coins_keeps_exclusion_and_mapping(store: Store) -> None:
    markets = normalize_markets(sample_rows())
    store.upsert_coins(coins_frame(markets), T0)
    store.con.execute(
        "UPDATE coins SET excluded = true, exclusion_reason = 'category:stablecoins' "
        "WHERE cg_id = 'tether'"
    )
    store.upsert_coins(coins_frame(markets), T0)
    row = store.query_df("SELECT * FROM coins WHERE cg_id = 'tether'").iloc[0]
    assert row["excluded"] and row["exclusion_reason"] == "category:stablecoins"


def test_snapshots_upsert_per_run(store: Store) -> None:
    markets = normalize_markets(sample_rows())
    store.upsert_market_snapshots(snapshots_frame(markets, "r1", T0))
    store.upsert_market_snapshots(snapshots_frame(markets, "r1", T0))
    assert store.count("market_snapshots") == 5
    store.upsert_market_snapshots(snapshots_frame(markets, "r2", T0 + timedelta(hours=1)))
    assert store.count("market_snapshots") == 10


def test_timestamps_stored_as_utc(store: Store) -> None:
    markets = normalize_markets(sample_rows())
    store.upsert_market_snapshots(snapshots_frame(markets, "r1", T0))
    row = store.query_df(
        "SELECT atl_date, snapshot_ts, market_cap_rank FROM market_snapshots "
        "WHERE cg_id = 'bitcoin'"
    ).iloc[0]
    assert row["atl_date"] == datetime(2013, 7, 5, 16, 0)
    assert row["snapshot_ts"] == datetime(2026, 10, 10, 12, 0)
    assert row["market_cap_rank"] == 1


def test_naive_datetime_is_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        store.start_run("r", datetime(2026, 1, 1), "h", "yaml")


def test_count_rejects_unknown_table(store: Store) -> None:
    with pytest.raises(ValueError):
        store.count("coins; DROP TABLE coins")


def test_run_id_is_sortable_utc() -> None:
    assert new_run_id(T0, "abcdef123456") == "20261010T120000Z-abcdef"


def make_client(handler: httpx.MockTransport, tmp_path: Path) -> ApiClient:
    config = load_config(EXAMPLE_CONFIG)
    clock = FakeClock()
    return ApiClient(
        source="coingecko",
        base_url=config.sources.coingecko.base_url,
        http_config=config.http,
        limiter=SlidingWindowLimiter(100, clock=clock.now, sleep=clock.sleep),
        cache=RawCache(tmp_path / "raw"),
        max_calls=30,
        transport=handler,
        sleep=clock.sleep,
    )


def test_fetch_and_store_records_run(store: Store, tmp_path: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    categories = [{"category_id": c, "name": c} for c in config.exclusions.coingecko_categories]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/categories/list"):
            return httpx.Response(200, json=categories)
        if "category" in request.url.params:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=sample_rows())  # short page -> last page

    summary = run_fetch(
        config, store, make_client(httpx.MockTransport(handler), tmp_path), now=lambda: T0
    )
    assert summary.coins_stored == 5
    run = store.query_df("SELECT * FROM runs").iloc[0]
    assert run["run_id"] == summary.run_id and run["status"] == "ok"
    assert run["cg_calls"] == 1 + 1 + len(categories)
    assert "min_market_cap_usd" in run["config_yaml"]
    assert store.count("market_snapshots") == 5


def test_failed_run_is_recorded(store: Store, tmp_path: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="bad key"))
    with pytest.raises(Exception, match="401"):
        run_fetch(config, store, make_client(transport, tmp_path), now=lambda: T0)
    run = store.query_df("SELECT status, cg_calls FROM runs").iloc[0]
    assert run["status"] == "failed" and run["cg_calls"] == 1
    assert store.count("coins") == 0


def test_update_coin_columns(store: Store) -> None:
    markets = normalize_markets(sample_rows())
    store.upsert_coins(coins_frame(markets), T0)
    flags = pd.DataFrame(
        {
            "cg_id": ["tether", "bitcoin"],
            "categories": [["stablecoins"], []],
            "excluded": [True, False],
            "exclusion_reason": ["category:stablecoins", None],
        }
    )
    store.update_coin_columns(flags, T0 + timedelta(hours=1))
    row = store.query_df("SELECT * FROM coins WHERE cg_id = 'tether'").iloc[0]
    assert list(row["categories"]) == ["stablecoins"]
    assert row["excluded"] and row["exclusion_reason"] == "category:stablecoins"
    assert row["updated_at"] == datetime(2026, 10, 10, 13, 0)
    untouched = store.query_df("SELECT excluded FROM coins WHERE cg_id = 'ethereum'").iloc[0]
    assert pd.isna(untouched["excluded"])


def test_run_fetch_with_binance(store: Store, tmp_path: Path) -> None:
    config = load_config(EXAMPLE_CONFIG)
    categories = [{"category_id": c, "name": c} for c in config.exclusions.coingecko_categories]

    def coingecko(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/categories/list"):
            return httpx.Response(200, json=categories)
        if request.url.params.get("category") == "stablecoins":
            return httpx.Response(200, json=[{"id": "tether", "market_cap": 1e11}])
        if "category" in request.url.params:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=sample_rows())

    btc_price = next(r["current_price"] for r in sample_rows() if r["id"] == "bitcoin")

    def binance(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/klines"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/exchangeInfo"):
            symbols = [
                {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"}
            ]
            return httpx.Response(200, json={"symbols": symbols})
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "price": str(btc_price)}])

    summary = run_fetch(
        config,
        store,
        make_client(httpx.MockTransport(coingecko), tmp_path),
        make_client(httpx.MockTransport(binance), tmp_path / "bn"),
        now=lambda: T0,
    )
    coins = store.query_df("SELECT * FROM coins").set_index("cg_id")
    assert coins.loc["tether", "excluded"]
    assert coins.loc["bitcoin", "binance_symbol"] == "BTCUSDT"
    assert coins.loc["bitcoin", "mapping_status"] == "mapped"
    assert coins.loc["ethereum", "mapping_status"] == "no_pair"
    assert summary.candles is not None and summary.candles.symbols_total == 1
    assert summary.binance_weight == 20 + 4 + 2  # exchangeInfo + ticker + one klines page
    assert store.query_df("SELECT binance_weight FROM runs").iloc[0, 0] == 26
