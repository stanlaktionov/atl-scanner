"""Orchestrates one run: fetch -> normalize -> store -> screen -> report.

Creates the run record (run_id, config snapshot, API call counters) in the runs table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atl_scanner.config import AppConfig
from atl_scanner.sources.coingecko import CoinGeckoSource
from atl_scanner.sources.http import ApiClient


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
