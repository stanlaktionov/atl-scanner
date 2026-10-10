"""Command line interface (typer). Thin wrapper over pipeline; no business logic here."""

from __future__ import annotations

import logging
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
from atl_scanner.pipeline import fetch_coingecko
from atl_scanner.sources.coingecko import UnknownCategoryError, build_coingecko_client
from atl_scanner.sources.http import ApiError, CallBudgetExceeded

app = typer.Typer(help="Crypto ATL Scanner — research screener, not trading signals.")

ConfigOpt = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config YAML.")
OnlyOpt = typer.Option(None, "--only", help="Fetch a single source.")
ATTRIBUTION = "Data provided by CoinGecko (https://www.coingecko.com)"


class SourceName(StrEnum):
    COINGECKO = "coingecko"
    BINANCE = "binance"


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
def scan(config: Path = ConfigOpt) -> None:
    """Full run: fetch -> normalize -> store -> screen -> report."""
    raise NotImplementedError("PLAN.md step 9")


@app.command()
def fetch(
    config: Path = ConfigOpt,
    only: SourceName | None = OnlyOpt,
) -> None:
    """Fetch CoinGecko markets and Binance candles into raw cache and DuckDB."""
    cfg = _load(config)
    if only in (None, SourceName.COINGECKO):
        _fetch_coingecko(cfg)
    if only in (None, SourceName.BINANCE):
        typer.echo("binance: not implemented yet (PLAN.md step 6)", err=True)


def _fetch_coingecko(cfg: AppConfig) -> None:
    threshold = cfg.universe.min_market_cap_usd
    try:
        with build_coingecko_client(cfg, coingecko_api_key(cfg)) as client:
            result = fetch_coingecko(cfg, client)
    except (ConfigError, ApiError, CallBudgetExceeded, UnknownCategoryError) as exc:
        typer.echo(f"coingecko: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"coingecko: {len(result.markets)} coins fetched, "
        f"{result.count_at_least(threshold)} with market cap >= {threshold:,.0f}"
    )
    for category, ids in result.category_ids.items():
        typer.echo(f"  exclusion category {category}: {len(ids)} coins")
    budget = cfg.sources.coingecko.max_calls_per_run
    typer.echo(
        f"  calls: {result.network_calls} network (budget {budget}), {result.cache_hits} from cache"
    )
    typer.echo(ATTRIBUTION)


@app.command()
def screen(config: Path = ConfigOpt) -> None:
    """Recompute metrics and filters from data already in DuckDB (no network)."""
    raise NotImplementedError("PLAN.md steps 7-8")


@app.command()
def dq(config: Path = ConfigOpt) -> None:
    """Data quality report: candle gaps, zero volume, ATL divergence, mapping/age status."""
    raise NotImplementedError("PLAN.md step 10")
