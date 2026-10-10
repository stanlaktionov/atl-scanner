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

from atl_scanner.config import (
    AppConfig,
    Timeframe,
    config_hash,
    config_snapshot,
    timeframe_hours,
)
from atl_scanner.exclusions import classify_exclusions
from atl_scanner.mapping import map_to_binance
from atl_scanner.metrics.atl import (
    NO_POINT,
    PricePoint,
    age_status,
    atl_divergence_log,
    candle_atl,
    last_close,
    metric_row,
)
from atl_scanner.metrics.indicators import last_atr
from atl_scanner.models import AtlSource
from atl_scanner.normalize.binance import normalize_klines, normalize_pairs, resample_ohlcv
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.screen import SCREEN_COLUMNS, apply_screen
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
    """Sortable run id: UTC start time to the millisecond plus config hash prefix."""
    moment = started_at.astimezone(UTC)
    return f"{moment:%Y%m%dT%H%M%S}{moment.microsecond // 1000:03d}Z-{cfg_hash[:6]}"


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


ATL_METRIC_COLUMNS = [
    "run_id",
    "cg_id",
    "atl_source",
    "atl_price",
    "atl_ts",
    "price",
    "price_ts",
    "dist_pct",
    "dist_log",
    "atr_period",
    "atr_value",
    "dist_atr",
    "new_low_window_days",
    "is_new_low_in_window",
    "days_since_atl",
]
METRIC_FIELDS = ["dist_pct", "dist_log", "dist_atr"]


class NoMarketDataError(RuntimeError):
    """Screening was requested before any fetch stored market data."""


@dataclass(frozen=True)
class MetricsResult:
    """atl_metrics rows (two per coin) plus one wide row per coin for screening."""

    snapshot_run_id: str
    atl_metrics: pd.DataFrame
    coins: pd.DataFrame


def compute_metrics(config: AppConfig, store: Store, run_id: str, now: datetime) -> MetricsResult:
    """ATL metrics for every coin of the latest snapshot, from both ATL sources.

    Binance prices are converted per token with the pair multiplier, so the ATR is in the
    same units as both ATLs (USDT ~ USD assumed for the CoinGecko row).
    """
    snapshot = store.latest_snapshot()
    if snapshot.empty:
        raise NoMarketDataError("no market snapshot stored yet; run `atl-scanner fetch`")
    long_rows: list[dict[str, object]] = []
    wide_rows: list[dict[str, object]] = []
    for record in snapshot.to_dict("records"):
        coin = {str(key): value for key, value in record.items()}
        rows, wide = _coin_metrics(config, store, coin, now)
        long_rows.extend({"run_id": run_id, "cg_id": coin["cg_id"], **row} for row in rows)
        wide_rows.append(wide)
    atl_metrics = pd.DataFrame(long_rows, columns=ATL_METRIC_COLUMNS)
    snapshot_run_id = str(snapshot["snapshot_run_id"].iloc[0])
    return MetricsResult(snapshot_run_id, atl_metrics, pd.DataFrame(wide_rows))


def _coin_metrics(
    config: AppConfig, store: Store, coin: dict[str, Any], now: datetime
) -> tuple[list[dict[str, object]], dict[str, object]]:
    metrics_cfg = config.metrics
    symbol = coin["binance_symbol"] if isinstance(coin["binance_symbol"], str) else None
    multiplier = _int_or(coin["binance_price_multiplier"], 1)
    candles = store.ohlcv(CANDLE_EXCHANGE, symbol) if symbol else pd.DataFrame()
    atr = _atr_per_token(candles, config, multiplier)
    window = metrics_cfg.new_low_window_days
    common = {"atr_period": metrics_cfg.atr_period, "new_low_window_days": window}

    cg_atl = PricePoint(_float(coin["atl"]), _dt(coin["atl_date"]))
    cg_price = PricePoint(_float(coin["price_usd"]), _dt(coin["cg_last_updated"]))
    cg_row = metric_row(cg_atl, cg_price, atr, now, window)
    rows = [{"atl_source": AtlSource.COINGECKO_ALL_TIME.value, **common, **cg_row}]

    bn_atl, bn_row = NO_POINT, None
    if not candles.empty:
        bn_atl = candle_atl(candles, multiplier, metrics_cfg.binance_atl_price)
        bn_row = metric_row(bn_atl, last_close(candles, multiplier), atr, now, window)
        rows.append({"atl_source": AtlSource.BINANCE_1Y.value, **common, **bn_row})

    first_candle = _dt(candles["open_time"].iloc[0]) if not candles.empty else None
    age = age_status(
        first_candle,
        _dt(coin["atl_date"]),
        _dt(coin["ath_date"]),
        now,
        config.universe.min_age_days,
    )
    wide: dict[str, object] = {
        "cg_id": coin["cg_id"],
        "symbol": coin["symbol"],
        "binance_symbol": symbol,
        "mapping_status": coin["mapping_status"],
        "market_cap_usd": coin["market_cap_usd"],
        "age_status": age.value,
        "first_candle_ts": first_candle,
        "excluded": bool(coin["excluded"]) if coin["excluded"] is not None else False,
        "exclusion_reason": coin["exclusion_reason"],
        **{f"cg_{f}": cg_row[f] for f in METRIC_FIELDS},
        **{f"bn_{f}": (bn_row[f] if bn_row else float("nan")) for f in METRIC_FIELDS},
        "bn_is_new_low_in_window": bn_row["is_new_low_in_window"] if bn_row else None,
        "atl_divergence_log": atl_divergence_log(bn_atl.price, cg_atl.price),
    }
    return rows, wide


def _atr_per_token(candles: pd.DataFrame, config: AppConfig, multiplier: int) -> float:
    if candles.empty:
        return float("nan")
    hours = timeframe_hours(config.metrics.atr_timeframe)
    frame = candles if hours == 1 else resample_ohlcv(candles, hours)
    return last_atr(frame, config.metrics.atr_period) / multiplier


def _dt(value: object) -> datetime | None:
    if value is None or value is pd.NaT or (isinstance(value, float) and value != value):
        return None
    return pd.Timestamp(value).to_pydatetime()  # type: ignore[arg-type]


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


def _int_or(value: object, default: int) -> int:
    if value is None or value is pd.NA or (isinstance(value, float) and value != value):
        return default
    return int(float(value))  # type: ignore[arg-type]


@dataclass(frozen=True)
class ScreenSummary:
    run_id: str
    snapshot_run_id: str
    results: pd.DataFrame


def run_screen(config: AppConfig, store: Store, now: Now = utc_now) -> ScreenSummary:
    """One recorded run without network: metrics + thresholds over the latest snapshot."""
    started = now()
    cfg_hash = config_hash(config)
    run_id = new_run_id(started, cfg_hash)
    store.start_run(run_id, started, cfg_hash, config_snapshot(config))
    try:
        metrics = compute_metrics(config, store, run_id, started)
        results = apply_screen(metrics.coins, config).assign(run_id=run_id)
        results["first_candle_ts"] = pd.to_datetime(results["first_candle_ts"], utc=True)
        results = results[SCREEN_COLUMNS]
        store.upsert_atl_metrics(metrics.atl_metrics)
        store.upsert_screen_results(results)
    except Exception:
        store.finish_run(run_id, now(), "failed", cg_calls=0, binance_weight=0)
        raise
    store.finish_run(
        run_id, now(), "ok", cg_calls=0, binance_weight=0, snapshot_run_id=metrics.snapshot_run_id
    )
    return ScreenSummary(run_id, metrics.snapshot_run_id, results)
