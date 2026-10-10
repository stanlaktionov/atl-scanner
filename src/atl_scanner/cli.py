"""Command line interface (typer). Thin wrapper over pipeline; no business logic here."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from enum import StrEnum
from pathlib import Path

import typer

from atl_scanner.config import (
    AppConfig,
    ConfigError,
    coingecko_api_key,
    config_hash,
    load_config,
    load_env,
)
from atl_scanner.pipeline import (
    FetchSummary,
    NoMarketDataError,
    ScreenSummary,
    run_fetch,
    run_scan,
    run_screen,
    utc_now,
    write_screen_reports,
)
from atl_scanner.report import ATTRIBUTION
from atl_scanner.screen import near_atl_column
from atl_scanner.sources.binance import build_binance_client
from atl_scanner.sources.coingecko import UnknownCategoryError, build_coingecko_client
from atl_scanner.sources.http import ApiError, CallBudgetExceeded
from atl_scanner.store.db import Store

app = typer.Typer(help="Crypto ATL Scanner — research screener, not trading signals.")

ConfigOpt = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config YAML.")
OnlyOpt = typer.Option(None, "--only", help="Skip Binance and fetch CoinGecko only.")
NoCandlesOpt = typer.Option(False, "--no-candles", help="Refresh mapping, skip klines.")
TopOpt = typer.Option(15, "--top", help="How many passing coins to print.")


class SourceName(StrEnum):
    COINGECKO = "coingecko"


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Load .env and set up logging before any command."""
    load_env()
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _load(config: Path) -> AppConfig:
    try:
        return load_config(config)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command("check-config")
def check_config(config: Path = ConfigOpt) -> None:
    """Validate the config file and print its key thresholds."""
    cfg = _load(config)
    near = cfg.screen.near_atl
    near_max = "not set (rank only)" if near.max is None else near.max
    typer.echo(f"OK {config} (hash {config_hash(cfg)})")
    typer.echo(f"  min_market_cap_usd: {cfg.universe.min_market_cap_usd:,.0f}")
    typer.echo(f"  min_age_days:       {cfg.universe.min_age_days}")
    typer.echo(f"  near_atl:           {near.metric} vs {near.atl_source}, max = {near_max}")


@app.command()
def scan(config: Path = ConfigOpt, top: int = TopOpt) -> None:
    """Full run: fetch -> normalize -> store -> screen -> report."""
    cfg = _load(config)
    try:
        with ExitStack() as stack:
            cg_client = stack.enter_context(build_coingecko_client(cfg, coingecko_api_key(cfg)))
            bn_client = stack.enter_context(build_binance_client(cfg))
            store = stack.enter_context(Store(cfg.storage.db_path))
            summary = run_scan(cfg, store, cg_client, bn_client, progress=_progress)
    except (
        ConfigError,
        ApiError,
        CallBudgetExceeded,
        UnknownCategoryError,
        NoMarketDataError,
    ) as exc:
        typer.echo(f"scan failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print_fetch_summary(cfg, summary.fetch, attribution=False)
    _print_screen_summary(cfg, summary.screen, top, summary.reports)


@app.command()
def fetch(
    config: Path = ConfigOpt,
    only: SourceName | None = OnlyOpt,
    no_candles: bool = NoCandlesOpt,
) -> None:
    """Fetch CoinGecko markets, exclusions, Binance mapping and 1h candles."""
    cfg = _load(config)
    try:
        with ExitStack() as stack:
            cg_client = stack.enter_context(build_coingecko_client(cfg, coingecko_api_key(cfg)))
            bn_client = None if only else stack.enter_context(build_binance_client(cfg))
            store = stack.enter_context(Store(cfg.storage.db_path))
            summary = run_fetch(
                cfg, store, cg_client, bn_client, with_candles=not no_candles, progress=_progress
            )
    except (ConfigError, ApiError, CallBudgetExceeded, UnknownCategoryError) as exc:
        typer.echo(f"fetch failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print_fetch_summary(cfg, summary)


def _progress(done: int, total: int, symbol: str) -> None:
    if done == total or done % 25 == 0:
        typer.echo(f"  candles {done}/{total} ({symbol})", err=True)


def _print_fetch_summary(
    cfg: AppConfig, summary: FetchSummary, *, attribution: bool = True
) -> None:
    threshold = cfg.universe.min_market_cap_usd
    result = summary.coingecko
    typer.echo(f"run {summary.run_id}: {summary.coins_stored} coins stored")
    typer.echo(
        f"coingecko: {len(result.markets)} coins fetched, "
        f"{result.count_at_least(threshold)} with market cap >= {threshold:,.0f}; "
        f"{result.network_calls} calls (budget {cfg.sources.coingecko.max_calls_per_run}), "
        f"{result.cache_hits} from cache"
    )
    excluded = summary.exclusions[summary.exclusions["excluded"]]
    typer.echo(f"exclusions: {len(excluded)} coins excluded")
    first_reasons = excluded["exclusion_reason"].str.split(";").str[0]
    for reason, count in first_reasons.value_counts().items():
        typer.echo(f"  {reason}: {count}")
    if summary.mapping is not None:
        typer.echo(f"binance: weight used {summary.binance_weight}")
        for status, count in summary.mapping["mapping_status"].value_counts().items():
            typer.echo(f"  {status}: {count}")
    candles = summary.candles
    if candles is not None:
        typer.echo(
            f"candles: {candles.symbols_updated}/{candles.symbols_total} symbols updated, "
            f"{candles.candles_stored} stored, {candles.candles_pruned} pruned"
        )
        for symbol, error in list(candles.failures.items())[:10]:
            typer.echo(f"  failed {symbol}: {error}", err=True)
    if attribution:
        typer.echo(ATTRIBUTION)


@app.command()
def screen(config: Path = ConfigOpt, top: int = TopOpt) -> None:
    """Recompute metrics and filters from data already in DuckDB (no network)."""
    cfg = _load(config)
    try:
        with Store(cfg.storage.db_path) as store:
            summary = run_screen(cfg, store)
    except NoMarketDataError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    reports = write_screen_reports(cfg, summary, utc_now())
    _print_screen_summary(cfg, summary, top, reports)


def _print_screen_summary(
    cfg: AppConfig, summary: ScreenSummary, top: int, reports: list[Path]
) -> None:
    results = summary.results
    near = cfg.screen.near_atl
    typer.echo(f"run {summary.run_id} (data from {summary.snapshot_run_id})")
    typer.echo(f"  coins:            {len(results)}")
    typer.echo(f"  passes market cap: {int(results['passes_mcap'].sum())}")
    typer.echo(f"  not excluded:      {int((~results['excluded']).sum())}")
    typer.echo(f"  age proven:        {int(results['passes_age'].sum())}")
    typer.echo(f"  with Binance pair: {int(results['binance_symbol'].notna().sum())}")
    limit = "not set (rank only)" if near.max is None else f"<= {near.max}"
    typer.echo(f"  near ATL:          {near.metric} vs {near.atl_source}, {limit}")
    typer.echo(f"  passes all:        {int(results['passes_all'].sum())}")
    column = near_atl_column(cfg)
    shown = results[results["passes_all"]].sort_values(column).head(top)
    columns = ["symbol", "binance_symbol", "market_cap_usd", column, "atl_divergence_log"]
    if not shown.empty:
        typer.echo(f"top {len(shown)} by {column}:")
        typer.echo(shown[columns].to_string(index=False, float_format=lambda v: f"{v:.4g}"))
    for path in reports:
        typer.echo(f"report: {path}")
    typer.echo(ATTRIBUTION)


@app.command()
def dq(config: Path = ConfigOpt) -> None:
    """Data quality report: candle gaps, zero volume, ATL divergence, mapping/age status."""
    raise NotImplementedError("PLAN.md step 10")
