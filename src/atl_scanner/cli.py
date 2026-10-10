"""Command line interface (typer). Thin wrapper over pipeline; no business logic here."""

from __future__ import annotations

from pathlib import Path

import typer

from atl_scanner.config import ConfigError, config_hash, load_config

app = typer.Typer(help="Crypto ATL Scanner — research screener, not trading signals.")

ConfigOpt = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config YAML.")


@app.command("check-config")
def check_config(config: Path = ConfigOpt) -> None:
    """Validate the config file and print its key thresholds."""
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
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
def fetch(config: Path = ConfigOpt) -> None:
    """Fetch CoinGecko markets and Binance candles into raw cache and DuckDB."""
    raise NotImplementedError("PLAN.md steps 3-6")


@app.command()
def screen(config: Path = ConfigOpt) -> None:
    """Recompute metrics and filters from data already in DuckDB (no network)."""
    raise NotImplementedError("PLAN.md steps 7-8")


@app.command()
def dq(config: Path = ConfigOpt) -> None:
    """Data quality report: candle gaps, zero volume, ATL divergence, mapping/age status."""
    raise NotImplementedError("PLAN.md step 10")
