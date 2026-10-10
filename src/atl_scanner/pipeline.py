"""Orchestrates one run: fetch -> normalize -> store -> screen -> report.

Creates the run record (run_id, config snapshot, API call counters) in the runs table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from atl_scanner.config import AppConfig, Timeframe, config_hash, config_snapshot
from atl_scanner.exclusions import classify_exclusions
from atl_scanner.mapping import map_to_binance
from atl_scanner.normalize.binance import normalize_klines, normalize_pairs
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.sources.binance import BinanceSource
from atl_scanner.sources.coingecko import CoinGeckoSource
from atl_scanner.sources.http import ApiClient, ApiError, RateLimitBanError
from atl_scanner.store.db import Store

logger = logging.getLogger(__name__)

Now = Callable[[], datetime]
Progress = Callable[[int, int, str], None]
CANDLE_EXCHANGE = "binance"
CANDLE_TIMEFRAME: Timeframe = "1h"
HOUR = timedelta(hours=1)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CoinGeckoFetch:
    """Raw CoinGecko data for one run plus API usage."""

    markets: list[dict[str, Any]]
    category_ids: dict[str, set[str]]
    network_calls: int
    cache_hits: int

    def count_at_least(self, min_market_cap_usd: float) -> int:
        return sum(
            1
            for row in self.markets
            if isinstance(row.get("market_cap"), int | float)
            and row["market_cap"] >= min_market_cap_usd
        )


@dataclass(frozen=True)
class CandleFetch:
    """Result of updating 1h candles for all target symbols."""

    symbols_total: int
    symbols_updated: int
    candles_stored: int
    candles_pruned: int
    failures: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchSummary:
    run_id: str
    coingecko: CoinGeckoFetch
    coins_stored: int
    exclusions: pd.DataFrame
    mapping: pd.DataFrame | None  # None when Binance was not queried
    candles: CandleFetch | None  # None when candles were skipped
    binance_weight: int | None


def new_run_id(started_at: datetime, cfg_hash: str) -> str:
    """Sortable run id: UTC start time plus config hash prefix."""
    return f"{started_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-{cfg_hash[:6]}"


def fetch_coingecko(config: AppConfig, client: ApiClient) -> CoinGeckoFetch:
    """Fetch the market universe and exclusion category members from CoinGecko."""
    source = CoinGeckoSource(client, config.sources.coingecko, config.universe.vs_currency)
    threshold = config.universe.min_market_cap_usd
    categories = config.exclusions.coingecko_categories
    if categories:
        source.check_categories(categories)
    markets = source.fetch_markets(threshold, config.universe.max_coins)
    category_ids = {
        category: source.fetch_category_ids(category, threshold) for category in categories
    }
    return CoinGeckoFetch(markets, category_ids, client.network_calls, client.cache_hits)


def ingest_coingecko(
    store: Store, fetched: CoinGeckoFetch, run_id: str, snapshot_ts: datetime
) -> pd.DataFrame:
    """Normalize markets, upsert coins and this run's market snapshot; return the markets."""
    markets = normalize_markets(fetched.markets)
    store.upsert_coins(coins_frame(markets), snapshot_ts)
    store.upsert_market_snapshots(snapshots_frame(markets, run_id, snapshot_ts))
    return markets


def fetch_binance_pairs(config: AppConfig, client: ApiClient) -> pd.DataFrame:
    """Tradable Binance pairs in the configured quote assets, with latest prices."""
    source = BinanceSource(client, config.sources.binance)
    return normalize_pairs(source.list_symbols(config.mapping.quote_assets), source.fetch_prices())


def floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def fetch_candles(
    config: AppConfig,
    client: ApiClient,
    store: Store,
    now: datetime,
    progress: Progress | None = None,
) -> CandleFetch:
    """Bring 1h candles of all target symbols up to the last closed hour.

    The window is the last `history_days`, floored to the hour, so a coin listed exactly a
    year ago still has its first candle >= 365 days back. Only the missing tail is
    downloaded; candles older than the window are pruned. Failures of single symbols are
    collected; a rate-limit ban aborts the whole step.
    """
    bn = config.sources.binance
    end = floor_hour(now)
    window_start = floor_hour(now - timedelta(days=bn.history_days))
    last = store.last_open_times(CANDLE_EXCHANGE)
    symbols = store.candle_targets(config.universe.min_market_cap_usd)
    starts = {
        symbol: max(window_start, last[symbol] + HOUR) if symbol in last else window_start
        for symbol in symbols
    }
    jobs = {symbol: start for symbol, start in starts.items() if start < end}
    source = BinanceSource(client, bn)
    stored, done = 0, 0
    failures: dict[str, str] = {}
    pool = ThreadPoolExecutor(max_workers=bn.max_workers)
    try:
        futures: dict[Future[list[list[Any]]], str] = {
            pool.submit(source.fetch_candles, symbol, CANDLE_TIMEFRAME, start, end): symbol
            for symbol, start in jobs.items()
        }
        for future in as_completed(futures):
            symbol = futures[future]
            done += 1
            try:
                raw = future.result()
            except RateLimitBanError:
                raise
            except ApiError as exc:
                logger.warning("candles for %s failed: %s", symbol, exc)
                failures[symbol] = str(exc)
                continue
            frame = normalize_klines(raw, CANDLE_EXCHANGE, symbol, now)
            store.upsert_ohlcv(frame)
            stored += len(frame)
            if progress is not None:
                progress(done, len(jobs), symbol)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    pruned = store.prune_ohlcv(CANDLE_EXCHANGE, window_start)
    return CandleFetch(len(symbols), len(jobs) - len(failures), stored, pruned, failures)


def run_fetch(
    config: AppConfig,
    store: Store,
    cg_client: ApiClient,
    bn_client: ApiClient | None = None,
    now: Now = utc_now,
    *,
    with_candles: bool = True,
    progress: Progress | None = None,
) -> FetchSummary:
    """One recorded run: CoinGecko -> exclusions -> Binance pairs -> mapping -> candles.

    Binance steps run only with `bn_client`; candles also need `with_candles`.

    The run is marked ok or failed together with the API usage counters.
    """
    started = now()
    cfg_hash = config_hash(config)
    run_id = new_run_id(started, cfg_hash)
    store.start_run(run_id, started, cfg_hash, config_snapshot(config))

    def weight() -> int | None:
        return bn_client.cost_used if bn_client else None

    try:
        fetched = fetch_coingecko(config, cg_client)
        markets = ingest_coingecko(store, fetched, run_id, started)
        exclusions = classify_exclusions(markets, fetched.category_ids, config.exclusions)
        store.update_coin_columns(exclusions, started)
        mapping, candles = None, None
        if bn_client is not None:
            pairs = fetch_binance_pairs(config, bn_client)
            mapping = map_to_binance(markets, pairs, config.mapping)
            store.update_coin_columns(mapping, started)
            if with_candles:
                candles = fetch_candles(config, bn_client, store, started, progress)
    except Exception:
        store.finish_run(
            run_id, now(), "failed", cg_calls=cg_client.network_calls, binance_weight=weight()
        )
        raise
    store.finish_run(run_id, now(), "ok", cg_calls=cg_client.network_calls, binance_weight=weight())
    return FetchSummary(run_id, fetched, len(markets), exclusions, mapping, candles, weight())
