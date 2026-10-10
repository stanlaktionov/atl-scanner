"""Binance public market data source: /api/v3/exchangeInfo and /api/v3/klines
(1h, at most 1 year back, incremental from the last stored candle). No API key.

API notes (checked 2026-10): request weights exchangeInfo = 20, ticker/price for all
symbols = 4; IP limit 6000 weight/min reported in X-MBX-USED-WEIGHT-1M. exchangeInfo for
all symbols is ~18 MB of JSON, so it is cached for a day.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from atl_scanner.config import AppConfig, BinanceConfig
from atl_scanner.sources.cache import RawCache
from atl_scanner.sources.http import ApiClient
from atl_scanner.sources.ratelimit import SlidingWindowLimiter

USED_WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
EXCHANGE_INFO_ENDPOINT = "/api/v3/exchangeInfo"
EXCHANGE_INFO_WEIGHT = 20
TICKER_PRICE_ENDPOINT = "/api/v3/ticker/price"
TICKER_PRICE_ALL_WEIGHT = 4


def build_binance_client(config: AppConfig) -> ApiClient:
    """ApiClient wired with the Binance weight budget and the raw cache."""
    bn = config.sources.binance
    return ApiClient(
        source="binance",
        base_url=bn.base_url,
        http_config=config.http,
        limiter=SlidingWindowLimiter(bn.weight_budget_per_min),
        cache=RawCache(config.storage.raw_dir),
        used_weight_header=USED_WEIGHT_HEADER,
    )


class BinanceSource:
    """CandleSource implementation over the Binance public REST API."""

    name = "binance"

    def __init__(self, client: ApiClient, config: BinanceConfig) -> None:
        self._client = client
        self._config = config

    def list_symbols(self, quote_assets: Sequence[str]) -> list[dict[str, Any]]:
        """Raw exchangeInfo symbol entries quoted in `quote_assets` (any status)."""
        body = self._client.get_json(
            EXCHANGE_INFO_ENDPOINT,
            cost=EXCHANGE_INFO_WEIGHT,
            ttl_hours=self._config.cache_ttl_hours.exchange_info,
        )
        wanted = set(quote_assets)
        return [s for s in body["symbols"] if s.get("quoteAsset") in wanted]

    def fetch_prices(self) -> list[dict[str, Any]]:
        """Latest price of every symbol as raw [{symbol, price}] (never cached)."""
        body: list[dict[str, Any]] = self._client.get_json(
            TICKER_PRICE_ENDPOINT, cost=TICKER_PRICE_ALL_WEIGHT
        )
        return body

    def fetch_candles(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        raise NotImplementedError("PLAN.md step 6")
