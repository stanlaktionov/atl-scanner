"""Tests for config.py: the example config is valid, bad configs fail with readable errors."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from atl_scanner.config import (
    ConfigError,
    coingecko_api_key,
    config_hash,
    load_config,
)
from atl_scanner.models import AtlSource, DistMetric
from tests.conftest import EXAMPLE_CONFIG

WriteConfig = Callable[[dict[str, Any]], Path]


def test_example_config_is_valid() -> None:
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.universe.min_market_cap_usd == 20_000_000
    assert cfg.universe.min_age_days == 365
    assert cfg.screen.near_atl.metric is DistMetric.DIST_LOG
    assert cfg.screen.near_atl.atl_source is AtlSource.COINGECKO_ALL_TIME
    assert cfg.screen.near_atl.max is None
    assert cfg.sources.binance.history_days <= 365


def test_missing_file() -> None:
    with pytest.raises(ConfigError, match="config not found"):
        load_config(Path("no-such-config.yaml"))


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path)


def test_unknown_key_is_rejected(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config_dict["universe"]["min_market_cap"] = 1
    with pytest.raises(ConfigError, match=r"universe\.min_market_cap"):
        load_config(write_config(example_config_dict))


def test_near_atl_max_key_is_required(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    del example_config_dict["screen"]["near_atl"]["max"]
    with pytest.raises(ConfigError, match=r"screen\.near_atl\.max"):
        load_config(write_config(example_config_dict))


def test_near_atl_max_can_be_set(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config_dict["screen"]["near_atl"]["max"] = 0.25
    assert load_config(write_config(example_config_dict)).screen.near_atl.max == 0.25


@pytest.mark.parametrize(
    ("section", "key", "value", "error"),
    [
        (("universe",), "min_market_cap_usd", -1, r"universe\.min_market_cap_usd"),
        (("universe",), "min_age_days", 0, r"universe\.min_age_days"),
        (("metrics",), "atr_period", 0, r"metrics\.atr_period"),
        (("screen", "near_atl"), "metric", "dist_magic", r"screen\.near_atl\.metric"),
        (("screen", "near_atl"), "atl_source", "kraken", r"screen\.near_atl\.atl_source"),
        (("sources", "binance"), "history_days", 400, r"history_days"),
        (("sources", "binance"), "timeframes", ["1m"], r"timeframes"),
        (("sources", "binance"), "klines_limit", 5000, r"klines_limit"),
        (("sources", "coingecko"), "per_page", 1000, r"per_page"),
        (("sources", "coingecko"), "base_url", "api.coingecko.com", r"base_url"),
        (("exclusions",), "name_patterns", ["(unclosed"], r"invalid regex"),
        (("http",), "backoff_max_s", 0.5, r"backoff_max_s"),
    ],
)
def test_invalid_values_are_rejected(
    example_config_dict: dict[str, Any],
    write_config: WriteConfig,
    section: tuple[str, ...],
    key: str,
    value: Any,
    error: str,
) -> None:
    node = example_config_dict
    for part in section:
        node = node[part]
    node[key] = value
    with pytest.raises(ConfigError, match=error):
        load_config(write_config(example_config_dict))


def test_derived_timeframe_must_fit_fetched(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config_dict["sources"]["binance"]["timeframes"] = ["1h", "2h"]
    example_config_dict["sources"]["binance"]["derived_timeframes"] = ["2h"]
    with pytest.raises(ConfigError, match="derived timeframe"):
        load_config(write_config(example_config_dict))


def test_1h_timeframe_is_required(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config_dict["sources"]["binance"]["timeframes"] = ["2h"]
    example_config_dict["sources"]["binance"]["derived_timeframes"] = []
    example_config_dict["metrics"]["atr_timeframe"] = "2h"
    with pytest.raises(ConfigError, match="must include 1h"):
        load_config(write_config(example_config_dict))


def test_atr_timeframe_must_be_available(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config_dict["metrics"]["atr_timeframe"] = "4h"
    with pytest.raises(ConfigError, match="atr_timeframe"):
        load_config(write_config(example_config_dict))


def test_new_low_window_within_history(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config_dict["metrics"]["new_low_window_days"] = 500
    with pytest.raises(ConfigError, match="new_low_window_days"):
        load_config(write_config(example_config_dict))


def test_coingecko_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(EXAMPLE_CONFIG)
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="COINGECKO_API_KEY"):
        coingecko_api_key(cfg)
    monkeypatch.setenv("COINGECKO_API_KEY", " demo-key ")
    assert coingecko_api_key(cfg) == "demo-key"


def test_config_hash_tracks_changes(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    base = config_hash(load_config(EXAMPLE_CONFIG))
    assert base == config_hash(load_config(EXAMPLE_CONFIG))
    example_config_dict["universe"]["min_market_cap_usd"] = 50_000_000
    assert config_hash(load_config(write_config(example_config_dict))) != base


def test_atr_timeframe_may_be_derived(
    example_config_dict: dict[str, Any], write_config: WriteConfig
) -> None:
    example_config_dict["metrics"]["atr_timeframe"] = "2h"
    assert load_config(write_config(example_config_dict)).metrics.atr_timeframe == "2h"
