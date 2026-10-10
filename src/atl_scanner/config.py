"""Load and validate config.yaml (pydantic models) and secrets from .env.

Every threshold, window, URL and limit used anywhere in the package comes from here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from atl_scanner.models import AtlSource, DistMetric

# Minute candles are out of scope, so the smallest timeframe is 1h.
Timeframe = Literal["1h", "2h", "4h", "6h", "8h", "12h", "1d"]
HttpBaseUrl = Field(pattern=r"^https?://\S+$")

MAX_HISTORY_DAYS = 365


class ConfigError(Exception):
    """Config file or secret is missing or invalid."""


def timeframe_hours(timeframe: Timeframe) -> int:
    """Length of a timeframe in hours."""
    return 24 if timeframe == "1d" else int(timeframe.removesuffix("h"))


class StrictModel(BaseModel):
    """Base for config sections: unknown keys are errors, values are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseConfig(StrictModel):
    vs_currency: str
    min_market_cap_usd: NonNegativeFloat
    min_age_days: PositiveInt
    max_coins: PositiveInt | None


class ExclusionsConfig(StrictModel):
    coingecko_categories: list[str]
    ids: list[str]
    symbols: list[str]
    name_patterns: list[str]
    keep_ids: list[str]  # never excluded, e.g. originals CoinGecko files under bridged-tokens

    @field_validator("name_patterns")
    @classmethod
    def _patterns_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return patterns


class MappingConfig(StrictModel):
    quote_assets: list[str] = Field(min_length=1)
    # Binance base asset prefix -> units per quoted price, e.g. 1000SATS quotes 1000 SATS.
    base_prefix_multipliers: dict[str, PositiveInt]
    # Data check, not a screening threshold: |ln(binance_price / coingecko_price)| above
    # this means the pair is probably a different token with the same ticker.
    max_price_deviation_log: PositiveFloat
    overrides: dict[str, str | None]


class MetricsConfig(StrictModel):
    atr_period: PositiveInt
    atr_timeframe: Timeframe
    new_low_window_days: PositiveInt


class NearAtlConfig(StrictModel):
    metric: DistMetric
    atl_source: AtlSource
    # Required key without a default: null means "rank only, do not filter".
    max: FiniteFloat | None


class ScreenConfig(StrictModel):
    near_atl: NearAtlConfig
    require_age: bool
    require_binance_pair: bool


class CoinGeckoCacheTtl(StrictModel):
    markets: PositiveFloat
    categories: PositiveFloat


class CoinGeckoConfig(StrictModel):
    base_url: str = HttpBaseUrl
    api_key_env: str = Field(min_length=1)
    per_page: int = Field(ge=1, le=250)
    rate_limit_per_min: PositiveInt
    max_calls_per_run: PositiveInt
    cache_ttl_hours: CoinGeckoCacheTtl
    age_fallback_max_calls: NonNegativeInt


class BinanceCacheTtl(StrictModel):
    exchange_info: PositiveFloat


class BinanceConfig(StrictModel):
    base_url: str = HttpBaseUrl
    klines_limit: int = Field(ge=1, le=1000)
    history_days: int = Field(ge=1, le=MAX_HISTORY_DAYS)
    timeframes: list[Timeframe] = Field(min_length=1)
    derived_timeframes: list[Timeframe]
    weight_budget_per_min: PositiveInt
    cache_ttl_hours: BinanceCacheTtl

    @model_validator(mode="after")
    def _derived_from_fetched(self) -> Self:
        base_hours = min(timeframe_hours(tf) for tf in self.timeframes)
        for tf in self.derived_timeframes:
            if tf in self.timeframes or timeframe_hours(tf) % base_hours != 0:
                raise ValueError(
                    f"derived timeframe {tf!r} must be a multiple of the smallest fetched "
                    f"timeframe and must not be fetched itself"
                )
        return self


class SourcesConfig(StrictModel):
    coingecko: CoinGeckoConfig
    binance: BinanceConfig


class HttpConfig(StrictModel):
    timeout_s: PositiveFloat
    max_retries: NonNegativeInt
    backoff_base_s: PositiveFloat
    backoff_max_s: PositiveFloat

    @model_validator(mode="after")
    def _backoff_order(self) -> Self:
        if self.backoff_max_s < self.backoff_base_s:
            raise ValueError("backoff_max_s must be >= backoff_base_s")
        return self


class StorageConfig(StrictModel):
    data_dir: Path
    raw_dir: Path
    db_path: Path


class ReportConfig(StrictModel):
    output_dir: Path
    formats: list[Literal["csv", "md"]] = Field(min_length=1)
    sort_by: str


class AppConfig(StrictModel):
    universe: UniverseConfig
    exclusions: ExclusionsConfig
    mapping: MappingConfig
    metrics: MetricsConfig
    screen: ScreenConfig
    sources: SourcesConfig
    http: HttpConfig
    storage: StorageConfig
    report: ReportConfig

    @model_validator(mode="after")
    def _cross_section(self) -> Self:
        binance = self.sources.binance
        if self.metrics.atr_timeframe not in binance.timeframes:
            raise ValueError(
                f"metrics.atr_timeframe {self.metrics.atr_timeframe!r} "
                f"must be in sources.binance.timeframes"
            )
        if self.metrics.new_low_window_days > binance.history_days:
            raise ValueError("metrics.new_low_window_days must be <= sources.binance.history_days")
        return self


def load_config(path: Path) -> AppConfig:
    """Read and validate a YAML config; raise ConfigError with a readable message."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path} (copy config.example.yaml)") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config {path}:\n{_format_errors(exc)}") from exc


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  {location}: {err['msg']}")
    return "\n".join(lines)


def load_env(env_file: Path = Path(".env")) -> None:
    """Load secrets from .env into the process environment (existing vars win)."""
    load_dotenv(env_file, override=False)


def coingecko_api_key(config: AppConfig) -> str:
    """CoinGecko Demo API key from the env var named in config."""
    name = config.sources.coingecko.api_key_env
    key = os.environ.get(name, "").strip()
    if not key:
        raise ConfigError(f"{name} is not set (put it in .env)")
    return key


def config_hash(config: AppConfig) -> str:
    """Stable short hash of the effective config, stored with every run."""
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def config_snapshot(config: AppConfig) -> str:
    """Effective config as YAML, stored with every run for reproducibility."""
    return yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
