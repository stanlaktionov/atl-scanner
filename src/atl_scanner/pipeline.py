"""Orchestrates one run: fetch -> normalize -> store -> screen -> report.

Creates the run record (run_id, config snapshot, API call counters) in the runs table.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from atl_scanner.config import AppConfig, config_hash, config_snapshot
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.sources.coingecko import CoinGeckoSource
from atl_scanner.sources.http import ApiClient
from atl_scanner.store.db import Store

Now = Callable[[], datetime]


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
class FetchSummary:
    run_id: str
    coingecko: CoinGeckoFetch
    coins_stored: int


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
) -> int:
    """Normalize markets and upsert coins and this run's market snapshot."""
    markets = normalize_markets(fetched.markets)
    store.upsert_coins(coins_frame(markets), snapshot_ts)
    store.upsert_market_snapshots(snapshots_frame(markets, run_id, snapshot_ts))
    return len(markets)


def fetch_and_store_coingecko(
    config: AppConfig, client: ApiClient, store: Store, now: Now = utc_now
) -> FetchSummary:
    """One recorded run: fetch CoinGecko, store it, mark the run ok or failed."""
    started = now()
    cfg_hash = config_hash(config)
    run_id = new_run_id(started, cfg_hash)
    store.start_run(run_id, started, cfg_hash, config_snapshot(config))
    try:
        fetched = fetch_coingecko(config, client)
        stored = ingest_coingecko(store, fetched, run_id, started)
    except Exception:
        store.finish_run(run_id, now(), "failed", cg_calls=client.network_calls)
        raise
    store.finish_run(run_id, now(), "ok", cg_calls=client.network_calls)
    return FetchSummary(run_id, fetched, stored)
