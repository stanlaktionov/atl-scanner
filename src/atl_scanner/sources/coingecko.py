"""CoinGecko Demo API source: /coins/markets (paginated, stops below the market cap
threshold), category listings for exclusions, optional market_chart age fallback.

Counts calls per run and enforces sources.coingecko.max_calls_per_run.

API notes (Demo plan, checked 2026-10): ~10k call credits/month, 100 calls/min, history
limited to 365 days, key sent as the x-cg-demo-api-key header, root URL api.coingecko.com.
Requests that fail with 4xx/5xx still count towards the per-minute limit.
"""

from __future__ import annotations

from typing import Any

from atl_scanner.config import AppConfig, CoinGeckoConfig
from atl_scanner.sources.cache import RawCache
from atl_scanner.sources.http import ApiClient
from atl_scanner.sources.ratelimit import SlidingWindowLimiter

API_KEY_HEADER = "x-cg-demo-api-key"
MARKETS_ENDPOINT = "/coins/markets"
CATEGORIES_LIST_ENDPOINT = "/coins/categories/list"


class UnknownCategoryError(ValueError):
    """Configured exclusion category does not exist on CoinGecko."""


def build_coingecko_client(config: AppConfig, api_key: str) -> ApiClient:
    """ApiClient wired with CoinGecko limits, call budget and the raw cache."""
    cg = config.sources.coingecko
    return ApiClient(
        source="coingecko",
        base_url=cg.base_url,
        http_config=config.http,
        limiter=SlidingWindowLimiter(cg.rate_limit_per_min),
        cache=RawCache(config.storage.raw_dir),
        headers={API_KEY_HEADER: api_key, "accept": "application/json"},
        max_calls=cg.max_calls_per_run,
    )


class CoinGeckoSource:
    """MarketListSource implementation over the CoinGecko Demo API."""

    name = "coingecko"

    def __init__(self, client: ApiClient, config: CoinGeckoConfig, vs_currency: str) -> None:
        self._client = client
        self._config = config
        self._vs_currency = vs_currency

    def fetch_markets(
        self, min_market_cap_usd: float, max_coins: int | None = None
    ) -> list[dict[str, Any]]:
        """Raw /coins/markets rows by market cap desc, stopping below the threshold.

        The last page may contain rows below the threshold; filtering is the screen's job.
        """
        return self._paginate_markets({}, min_market_cap_usd, max_coins)

    def fetch_category_ids(self, category: str, min_market_cap_usd: float) -> set[str]:
        """Ids of coins in a CoinGecko category, down to the market cap threshold."""
        rows = self._paginate_markets({"category": category}, min_market_cap_usd, None)
        return {row["id"] for row in rows}

    def fetch_category_list(self) -> list[dict[str, Any]]:
        """All CoinGecko categories as [{category_id, name}]."""
        body: list[dict[str, Any]] = self._client.get_json(
            CATEGORIES_LIST_ENDPOINT, ttl_hours=self._config.cache_ttl_hours.categories
        )
        return body

    def check_categories(self, categories: list[str]) -> None:
        """Fail early if a configured category id is unknown (one cached call)."""
        known = {row["category_id"] for row in self.fetch_category_list()}
        unknown = sorted(set(categories) - known)
        if unknown:
            raise UnknownCategoryError(
                f"unknown CoinGecko categories in exclusions.coingecko_categories: {unknown}"
            )

    def _paginate_markets(
        self, extra: dict[str, str], min_market_cap_usd: float, max_coins: int | None
    ) -> list[dict[str, Any]]:
        ttl = (
            self._config.cache_ttl_hours.categories
            if "category" in extra
            else self._config.cache_ttl_hours.markets
        )
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._markets_page(page, extra, ttl)
            rows.extend(batch)
            if _is_last_page(batch, self._config.per_page, min_market_cap_usd):
                break
            if max_coins is not None and len(rows) >= max_coins:
                return rows[:max_coins]
            page += 1
        return rows if max_coins is None else rows[:max_coins]

    def _markets_page(self, page: int, extra: dict[str, str], ttl: float) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "vs_currency": self._vs_currency,
            "order": "market_cap_desc",
            "per_page": self._config.per_page,
            "page": page,
            "sparkline": "false",
            **extra,
        }
        body: list[dict[str, Any]] = self._client.get_json(MARKETS_ENDPOINT, params, ttl_hours=ttl)
        return body


def _is_last_page(batch: list[dict[str, Any]], per_page: int, min_market_cap: float) -> bool:
    if len(batch) < per_page:
        return True
    last_cap = batch[-1].get("market_cap")
    return not isinstance(last_cap, int | float) or last_cap < min_market_cap
