"""Source interfaces. Every exchange or data provider implements one of these Protocols.

A source returns raw payloads (and persists them via the raw cache); turning them into
DataFrames is the job of normalize/. See .claude/skills/add-data-source/SKILL.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol


class MarketListSource(Protocol):
    """Provider of the coin universe with market caps and all-time ATL (e.g. CoinGecko)."""

    name: str

    def fetch_markets(
        self, min_market_cap_usd: float, max_coins: int | None = None
    ) -> list[dict[str, Any]]:
        """Return raw market rows sorted by market cap, stopping below the threshold."""
        ...

    def fetch_category_ids(self, category: str, min_market_cap_usd: float) -> set[str]:
        """Return coin ids belonging to a provider category (used for exclusions)."""
        ...


class CandleSource(Protocol):
    """Exchange providing OHLCV candles for spot pairs (e.g. Binance)."""

    name: str

    def list_symbols(self, quote_assets: Sequence[str]) -> list[dict[str, Any]]:
        """Return raw tradable pairs with base/quote assets and status."""
        ...

    def fetch_candles(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        """Return raw candles in [start, end), UTC, paginating as needed."""
        ...
