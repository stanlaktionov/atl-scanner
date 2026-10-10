"""Binance public market data source: /api/v3/exchangeInfo and /api/v3/klines
(1h, at most 1 year back, incremental from the last stored candle). No API key.

API notes (checked 2026-10): request weights exchangeInfo = 20, ticker/price for all
symbols = 4, klines = 2; IP limit 6000 weight/min reported in X-MBX-USED-WEIGHT-1M.
exchangeInfo for all symbols is ~18 MB of JSON, so it is cached for a day. A klines request
takes ~1 s from here, so a year of 1h candles (9 pages) is fetched by parallel workers.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from atl_scanner.config import AppConfig, BinanceConfig, Timeframe, timeframe_hours
from atl_scanner.sources.cache import RawCache
from atl_scanner.sources.http import ApiClient
from atl_scanner.sources.ratelimit import SlidingWindowLimiter

USED_WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
EXCHANGE_INFO_ENDPOINT = "/api/v3/exchangeInfo"
EXCHANGE_INFO_WEIGHT = 20
TICKER_PRICE_ENDPOINT = "/api/v3/ticker/price"
TICKER_PRICE_ALL_WEIGHT = 4
KLINES_ENDPOINT = "/api/v3/klines"
KLINES_WEIGHT = 2


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
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[list[Any]]:
        """Raw klines with open time in [start, end), paginated by klines_limit.

        Every page is stored raw; pages are not served from cache because the request
        windows move with every run.
        """
        step_ms = timeframe_hours(timeframe) * 3_600_000
        start_ms, end_ms = _epoch_ms(start), _epoch_ms(end)
        limit = self._config.klines_limit
        rows: list[list[Any]] = []
        while start_ms < end_ms:
            params: dict[str, str | int] = {
                "symbol": symbol,
                "interval": timeframe,
                "startTime": start_ms,
                "endTime": end_ms - 1,
                "limit": limit,
            }
            batch: list[list[Any]] = self._client.get_json(
                KLINES_ENDPOINT, params, cost=KLINES_WEIGHT
            )
            rows.extend(batch)
            if len(batch) < limit:
                break
            start_ms = int(batch[-1][0]) + step_ms
        return rows


def _epoch_ms(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("expected a timezone-aware datetime")
    return int(moment.timestamp() * 1000)
