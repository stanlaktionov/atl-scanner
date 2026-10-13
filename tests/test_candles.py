"""Tests for step 6: Binance klines fetch, normalization, 2h resample, incremental store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from atl_scanner.config import AppConfig, load_config
from atl_scanner.normalize.binance import OHLCV_COLUMNS, normalize_klines, resample_ohlcv
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.pipeline import fetch_candles, floor_hour
from atl_scanner.sources.binance import BinanceSource
from atl_scanner.sources.cache import RawCache
from atl_scanner.sources.http import ApiClient, RateLimitBanError
from atl_scanner.sources.ratelimit import SlidingWindowLimiter
from tests.conftest import EXAMPLE_CONFIG, FakeClock

H = 3_600_000
NOW = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def kline(open_ms: int, price: float = 1.0) -> list[Any]:
    """Binance kline array; close_time = open + 1h - 1 ms."""
    return [
        open_ms,
        f"{price:.8f}",
        f"{price * 1.1:.8f}",
        f"{price * 0.9:.8f}",
        f"{price * 1.05:.8f}",
        "10.0",
        open_ms + H - 1,
        "100.0",
        7,
        "5.0",
        "50.0",
        "0",
    ]


class FakeKlines:
    """Serves hourly klines for symbols listed since `listed`, honoring start/end/limit."""

    def __init__(self, listed: dict[str, datetime], fail: dict[str, int] | None = None) -> None:
        self.listed = listed
        self.fail = fail or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        symbol = params["symbol"]
        if symbol in self.fail:
            return httpx.Response(self.fail[symbol], json={"code": -1121, "msg": "Invalid symbol."})
        first = max(int(params["startTime"]), ms(self.listed[symbol]))
        first += (-first) % H
        last = int(params["endTime"])
        opens = range(first, last + 1, H)
        rows = [kline(t) for t in list(opens)[: int(params["limit"])]]
        return httpx.Response(200, json=rows)

    def starts(self, symbol: str) -> list[datetime]:
        return [
            datetime.fromtimestamp(int(r.url.params["startTime"]) / 1000, UTC)
            for r in self.requests
            if r.url.params["symbol"] == symbol
        ]


def make_config(limit: int = 1000, history_days: int = 365, workers: int = 2) -> AppConfig:
    base = load_config(EXAMPLE_CONFIG)
    bn = base.sources.binance.model_copy(
        update={"klines_limit": limit, "history_days": history_days, "max_workers": workers}
    )
    return base.model_copy(update={"sources": base.sources.model_copy(update={"binance": bn})})


def make_client(server: FakeKlines, tmp_path: Path, config: AppConfig) -> ApiClient:
    clock = FakeClock()
    return ApiClient(
        source="binance",
        base_url=config.sources.binance.base_url,
        http_config=config.http,
        limiter=SlidingWindowLimiter(100_000, clock=clock.now, sleep=clock.sleep),
        cache=RawCache(tmp_path / "raw"),
        transport=httpx.MockTransport(server),
        sleep=clock.sleep,
    )


# --- source -----------------------------------------------------------------


def test_fetch_candles_paginates(tmp_path: Path) -> None:
    config = make_config(limit=10)
    server = FakeKlines({"ABCUSDT": datetime(2020, 1, 1, tzinfo=UTC)})
    source = BinanceSource(make_client(server, tmp_path, config), config.sources.binance)
    start = datetime(2026, 10, 9, 0, tzinfo=UTC)
    rows = source.fetch_candles("ABCUSDT", "1h", start, start + timedelta(hours=25))
    assert len(rows) == 25
    assert [r[0] for r in rows] == [ms(start) + i * H for i in range(25)]
    assert server.starts("ABCUSDT") == [start + timedelta(hours=h) for h in (0, 10, 20)]
    params = server.requests[0].url.params
    assert params["interval"] == "1h" and params["limit"] == "10"
    assert int(params["endTime"]) == ms(start + timedelta(hours=25)) - 1


def test_fetch_candles_rejects_naive_datetimes(tmp_path: Path) -> None:
    config = make_config()
    server = FakeKlines({})
    source = BinanceSource(make_client(server, tmp_path, config), config.sources.binance)
    with pytest.raises(ValueError, match="timezone-aware"):
        source.fetch_candles("X", "1h", datetime(2026, 1, 1), datetime(2026, 1, 2))


# --- normalize ----------------------------------------------------------------


def test_normalize_klines_types_and_open_candle_dropped() -> None:
    t0 = ms(datetime(2026, 10, 10, 10, tzinfo=UTC))
    raw = [kline(t0, 2.0), kline(t0 + H, 3.0), kline(t0 + 2 * H, 4.0)]  # last one is open
    frame = normalize_klines(raw, "binance", "ABCUSDT", NOW)
    assert list(frame.columns) == OHLCV_COLUMNS
    assert len(frame) == 2
    assert frame.loc[0, "open_time"] == pd.Timestamp("2026-10-10T10:00Z")
    assert frame.loc[0, "open"] == 2.0 and frame.loc[0, "low"] == pytest.approx(1.8)
    assert frame.loc[0, "trades"] == 7
    assert frame["close"].dtype == "float64"


def test_normalize_klines_dedupes_and_sorts() -> None:
    t0 = ms(datetime(2026, 10, 10, 8, tzinfo=UTC))
    frame = normalize_klines([kline(t0 + H), kline(t0), kline(t0)], "binance", "X", NOW)
    assert frame["open_time"].is_monotonic_increasing and len(frame) == 2


def test_normalize_klines_empty() -> None:
    frame = normalize_klines([], "binance", "X", NOW)
    assert frame.empty and list(frame.columns) == OHLCV_COLUMNS


def candles(opens: list[datetime], prices: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "exchange": "binance",
            "symbol": "X",
            "open_time": pd.to_datetime(opens, utc=True),
            "open": [p[0] for p in prices],
            "high": [p[1] for p in prices],
            "low": [p[2] for p in prices],
            "close": [p[3] for p in prices],
            "volume": [1.0, 2.0, 3.0, 4.0, 5.0][: len(opens)],
            "quote_volume": [10.0, 20.0, 30.0, 40.0, 50.0][: len(opens)],
            "trades": pd.array([1, 2, 3, 4, 5][: len(opens)], dtype="Int64"),
        }
    )


def test_resample_2h_matches_hand_calculation() -> None:
    day = datetime(2026, 10, 10, tzinfo=UTC)
    opens = [day + timedelta(hours=h) for h in (1, 2, 3, 4, 5)]
    prices = [
        (10, 12, 9, 11),  # 01:00 -> bucket 00:00 (incomplete, dropped)
        (11, 15, 10, 14),  # 02:00 -> bucket 02:00
        (14, 16, 8, 9),  # 03:00 -> bucket 02:00
        (9, 10, 7, 8),  # 04:00 -> bucket 04:00
        (8, 20, 8, 19),  # 05:00 -> bucket 04:00
    ]
    out = resample_ohlcv(candles(opens, prices), 2)
    assert list(out.columns) == OHLCV_COLUMNS
    assert out["open_time"].tolist() == [
        pd.Timestamp("2026-10-10T02:00Z"),
        pd.Timestamp("2026-10-10T04:00Z"),
    ]
    first, second = out.iloc[0], out.iloc[1]
    assert (first["open"], first["high"], first["low"], first["close"]) == (11, 16, 8, 9)
    assert (second["open"], second["high"], second["low"], second["close"]) == (9, 20, 7, 19)
    assert first["volume"] == 5.0 and first["quote_volume"] == 50.0 and first["trades"] == 5


def test_resample_drops_buckets_with_gaps() -> None:
    day = datetime(2026, 10, 10, tzinfo=UTC)
    opens = [day, day + timedelta(hours=3)]  # 00:00 and 03:00: both buckets miss an hour
    out = resample_ohlcv(candles(opens, [(1, 1, 1, 1), (2, 2, 2, 2)]), 2)
    assert out.empty


# --- pipeline: incremental store ---------------------------------------------------


@pytest.fixture
def store_with_targets(tmp_path: Path) -> Any:
    from atl_scanner.store.db import Store

    store = Store(tmp_path / "db.duckdb")
    rows = [
        {"id": "abc", "symbol": "abc", "name": "Abc", "market_cap": 5e7, "current_price": 1.0},
        {"id": "new", "symbol": "new", "name": "New", "market_cap": 5e7, "current_price": 1.0},
        {"id": "tiny", "symbol": "tiny", "name": "Tiny", "market_cap": 1e6, "current_price": 1.0},
        {"id": "usd", "symbol": "usd", "name": "Usd", "market_cap": 9e9, "current_price": 1.0},
    ]
    markets = normalize_markets(rows)
    store.upsert_coins(coins_frame(markets), NOW)
    store.upsert_market_snapshots(snapshots_frame(markets, "r1", NOW))
    store.update_coin_columns(
        pd.DataFrame(
            {
                "cg_id": ["abc", "new", "tiny", "usd"],
                "binance_symbol": ["ABCUSDT", "NEWUSDT", "TINYUSDT", "USDUSDT"],
                "excluded": [False, False, False, True],
            }
        ),
        NOW,
    )
    return store


def test_candle_targets_skip_excluded_and_small(store_with_targets: Any) -> None:
    assert store_with_targets.candle_targets(2e7) == ["ABCUSDT", "NEWUSDT"]


def test_fetch_candles_full_then_incremental(store_with_targets: Any, tmp_path: Path) -> None:
    store = store_with_targets
    config = make_config(history_days=3)
    listed = {
        "ABCUSDT": datetime(2020, 1, 1, tzinfo=UTC),
        "NEWUSDT": datetime(2026, 10, 9, 5, tzinfo=UTC),  # listed inside the window
    }
    server = FakeKlines(listed)
    result = fetch_candles(config, make_client(server, tmp_path, config), store, NOW)
    window_start = floor_hour(NOW - timedelta(days=3))
    assert result.symbols_total == 2 and result.symbols_updated == 2 and not result.failures
    counts = store.query_df(
        "SELECT symbol, count(*) n, min(open_time) first_ts FROM ohlcv_1h GROUP BY 1"
    )
    counts = counts.set_index("symbol")
    assert counts.loc["ABCUSDT", "n"] == 72  # 3 days of closed candles up to 12:00
    assert counts.loc["ABCUSDT", "first_ts"] == window_start.replace(tzinfo=None)
    assert counts.loc["NEWUSDT", "first_ts"] == datetime(2026, 10, 9, 5)

    later = NOW + timedelta(hours=2)
    server.requests.clear()
    result = fetch_candles(config, make_client(server, tmp_path, config), store, later)
    assert server.starts("ABCUSDT") == [datetime(2026, 10, 10, 12, tzinfo=UTC)]
    assert result.candles_stored == 4  # 12:00 and 13:00 for both symbols
    assert result.candles_pruned == 2  # window moved 2h; NEWUSDT was listed inside it
    first = store.query_df("SELECT min(open_time) FROM ohlcv_1h WHERE symbol = 'ABCUSDT'")
    assert first.iloc[0, 0] == (window_start + timedelta(hours=2)).replace(tzinfo=None)


def test_fetch_candles_up_to_date_makes_no_requests(
    store_with_targets: Any, tmp_path: Path
) -> None:
    config = make_config(history_days=1)
    listed = datetime(2020, 1, 1, tzinfo=UTC)
    server = FakeKlines({"ABCUSDT": listed, "NEWUSDT": listed})
    fetch_candles(config, make_client(server, tmp_path, config), store_with_targets, NOW)
    server.requests.clear()
    fetch_candles(config, make_client(server, tmp_path, config), store_with_targets, NOW)
    assert server.requests == []


def test_failed_symbol_is_collected(store_with_targets: Any, tmp_path: Path) -> None:
    config = make_config(history_days=1)
    server = FakeKlines({"ABCUSDT": datetime(2020, 1, 1, tzinfo=UTC)}, fail={"NEWUSDT": 400})
    result = fetch_candles(config, make_client(server, tmp_path, config), store_with_targets, NOW)
    assert set(result.failures) == {"NEWUSDT"}
    assert result.symbols_updated == 1 and result.candles_stored == 24


def test_progress_starts_at_zero_and_counts_failures(
    store_with_targets: Any, tmp_path: Path
) -> None:
    config = make_config(history_days=1)
    server = FakeKlines({"ABCUSDT": datetime(2020, 1, 1, tzinfo=UTC)}, fail={"NEWUSDT": 400})
    calls: list[tuple[int, int, str]] = []
    client = make_client(server, tmp_path, config)
    fetch_candles(config, client, store_with_targets, NOW, lambda *call: calls.append(call))
    assert calls[0] == (0, 2, "")
    assert [done for done, _, _ in calls] == [0, 1, 2]
    assert {symbol for _, _, symbol in calls[1:]} == {"ABCUSDT", "NEWUSDT"}


def test_ban_aborts(store_with_targets: Any, tmp_path: Path) -> None:
    config = make_config(history_days=1)
    server = FakeKlines({}, fail={"ABCUSDT": 418, "NEWUSDT": 418})
    with pytest.raises(RateLimitBanError):
        fetch_candles(config, make_client(server, tmp_path, config), store_with_targets, NOW)
