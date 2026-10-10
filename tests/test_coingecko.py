"""Tests for sources/coingecko.py and pipeline.fetch_coingecko against a fake server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from atl_scanner.config import AppConfig, load_config
from atl_scanner.pipeline import fetch_coingecko
from atl_scanner.sources.cache import RawCache
from atl_scanner.sources.coingecko import (
    API_KEY_HEADER,
    CoinGeckoSource,
    UnknownCategoryError,
)
from atl_scanner.sources.http import ApiClient, CallBudgetExceeded
from atl_scanner.sources.ratelimit import SlidingWindowLimiter
from tests.conftest import EXAMPLE_CONFIG, FakeClock

PER_PAGE = 3


def coin(coin_id: str, market_cap: float | None) -> dict[str, Any]:
    return {"id": coin_id, "symbol": coin_id, "market_cap": market_cap}


class FakeCoinGecko:
    """Serves /coins/markets pages from per-category coin lists, records requests."""

    def __init__(
        self, universe: list[dict[str, Any]], categories: dict[str, list[dict[str, Any]]]
    ) -> None:
        self.universe = universe
        self.categories = categories
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/coins/categories/list"):
            return httpx.Response(
                200, json=[{"category_id": c, "name": c} for c in self.categories]
            )
        params = request.url.params
        rows = self.categories[params["category"]] if "category" in params else self.universe
        per_page, page = int(params["per_page"]), int(params["page"])
        return httpx.Response(200, json=rows[(page - 1) * per_page : page * per_page])

    def market_pages(self, category: str | None = None) -> list[int]:
        return [
            int(r.url.params["page"])
            for r in self.requests
            if r.url.path.endswith("/coins/markets") and r.url.params.get("category") == category
        ]


def make_config(**coingecko: Any) -> AppConfig:
    base = load_config(EXAMPLE_CONFIG)
    cg = base.sources.coingecko.model_copy(update={"per_page": PER_PAGE, **coingecko})
    return base.model_copy(update={"sources": base.sources.model_copy(update={"coingecko": cg})})


def make_client(server: FakeCoinGecko, config: AppConfig, tmp_path: Path) -> ApiClient:
    clock = FakeClock()
    cg = config.sources.coingecko
    return ApiClient(
        source="coingecko",
        base_url=cg.base_url,
        http_config=config.http,
        limiter=SlidingWindowLimiter(cg.rate_limit_per_min, clock=clock.now, sleep=clock.sleep),
        cache=RawCache(tmp_path),
        headers={API_KEY_HEADER: "test-key"},
        max_calls=cg.max_calls_per_run,
        transport=httpx.MockTransport(server),
        sleep=clock.sleep,
    )


def make_source(server: FakeCoinGecko, tmp_path: Path, **coingecko: Any) -> CoinGeckoSource:
    config = make_config(**coingecko)
    return CoinGeckoSource(make_client(server, config, tmp_path), config.sources.coingecko, "usd")


UNIVERSE = [coin(f"c{i}", cap) for i, cap in enumerate([100, 90, 80, 70, 60, 50, 40, 30, 20])]


def test_pagination_stops_below_threshold(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE, {})
    rows = make_source(server, tmp_path).fetch_markets(min_market_cap_usd=55)
    assert server.market_pages() == [1, 2]  # page 2 ends with 50 < 55
    assert [r["id"] for r in rows] == ["c0", "c1", "c2", "c3", "c4", "c5"]


def test_pagination_continues_while_last_row_above_threshold(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE, {})
    make_source(server, tmp_path).fetch_markets(min_market_cap_usd=45)
    assert server.market_pages() == [1, 2, 3]


def test_pagination_stops_on_short_or_empty_page(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE[:4], {})
    rows = make_source(server, tmp_path).fetch_markets(min_market_cap_usd=0)
    assert server.market_pages() == [1, 2] and len(rows) == 4
    server = FakeCoinGecko(UNIVERSE[:3], {})
    make_source(server, tmp_path / "b").fetch_markets(min_market_cap_usd=0)
    assert server.market_pages() == [1, 2]


def test_missing_market_cap_stops(tmp_path: Path) -> None:
    server = FakeCoinGecko([coin("a", 100), coin("b", 90), coin("c", None), coin("d", 1)], {})
    make_source(server, tmp_path).fetch_markets(min_market_cap_usd=0)
    assert server.market_pages() == [1]


def test_max_coins(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE, {})
    rows = make_source(server, tmp_path).fetch_markets(min_market_cap_usd=0, max_coins=4)
    assert [r["id"] for r in rows] == ["c0", "c1", "c2", "c3"]
    assert server.market_pages() == [1, 2]


def test_request_params_and_key_header(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE, {})
    make_source(server, tmp_path).fetch_markets(min_market_cap_usd=95)
    request = server.requests[0]
    assert dict(request.url.params) == {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": str(PER_PAGE),
        "page": "1",
        "sparkline": "false",
    }
    assert request.headers[API_KEY_HEADER] == "test-key"
    assert "test-key" not in str(request.url)


def test_category_ids(tmp_path: Path) -> None:
    stables = [coin("usdt", 100), coin("usdc", 90), coin("dai", 50)]
    server = FakeCoinGecko(UNIVERSE, {"stablecoins": stables})
    ids = make_source(server, tmp_path).fetch_category_ids("stablecoins", min_market_cap_usd=20)
    assert ids == {"usdt", "usdc", "dai"}
    assert server.market_pages("stablecoins") == [1, 2]  # full page, last cap 50 >= 20


def test_unknown_category(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE, {"stablecoins": []})
    with pytest.raises(UnknownCategoryError, match="wrapped-tokenz"):
        make_source(server, tmp_path).check_categories(["stablecoins", "wrapped-tokenz"])


def test_markets_are_cached_within_ttl(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE, {})
    source = make_source(server, tmp_path)
    source.fetch_markets(min_market_cap_usd=95)
    source.fetch_markets(min_market_cap_usd=95)
    assert server.market_pages() == [1]


def test_call_budget_is_enforced(tmp_path: Path) -> None:
    server = FakeCoinGecko(UNIVERSE, {})
    with pytest.raises(CallBudgetExceeded):
        make_source(server, tmp_path, max_calls_per_run=2).fetch_markets(min_market_cap_usd=0)


def test_fetch_coingecko_pipeline(tmp_path: Path) -> None:
    categories = {
        "stablecoins": [coin("usdt", 100)],
        "wrapped-tokens": [coin("wbtc", 90)],
        "bridged-tokens": [],
    }
    server = FakeCoinGecko(UNIVERSE, categories)
    config = make_config()
    universe = config.universe.model_copy(update={"min_market_cap_usd": 55})
    config = config.model_copy(update={"universe": universe})
    result = fetch_coingecko(config, make_client(server, config, tmp_path))
    assert len(result.markets) == 6
    assert result.count_at_least(55) == 5
    assert result.category_ids == {
        "stablecoins": {"usdt"},
        "wrapped-tokens": {"wbtc"},
        "bridged-tokens": set(),
    }
    assert result.network_calls == 1 + 2 + 3  # category list + 2 market pages + 3 categories
    assert result.cache_hits == 0
