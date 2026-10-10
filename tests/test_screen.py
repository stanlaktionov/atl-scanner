"""Tests for screen.py and pipeline.run_screen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from atl_scanner.config import AppConfig, load_config
from atl_scanner.models import AtlSource, DistMetric
from atl_scanner.normalize.coingecko import coins_frame, normalize_markets, snapshots_frame
from atl_scanner.pipeline import NoMarketDataError, run_screen
from atl_scanner.screen import SCREEN_COLUMNS, apply_screen, near_atl_column
from atl_scanner.store.db import Store
from tests.conftest import EXAMPLE_CONFIG

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NAN = float("nan")


def config(
    near_max: float | None = None,
    metric: str = "dist_log",
    source: str = "coingecko_all_time",
    require_age: bool = True,
    require_pair: bool = False,
) -> AppConfig:
    base = load_config(EXAMPLE_CONFIG)
    near = base.screen.near_atl.model_copy(
        update={"max": near_max, "metric": DistMetric(metric), "atl_source": AtlSource(source)}
    )
    screen = base.screen.model_copy(
        update={"near_atl": near, "require_age": require_age, "require_binance_pair": require_pair}
    )
    return base.model_copy(update={"screen": screen})


def coin(cg_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cg_id": cg_id,
        "symbol": cg_id,
        "binance_symbol": f"{cg_id.upper()}USDT",
        "mapping_status": "mapped",
        "market_cap_usd": 5e7,
        "age_status": "binance_first_candle",
        "first_candle_ts": NOW - timedelta(days=365),
        "excluded": False,
        "exclusion_reason": None,
        "cg_dist_pct": 0.1,
        "cg_dist_log": 0.1,
        "cg_dist_atr": 3.0,
        "bn_dist_pct": 0.05,
        "bn_dist_log": 0.05,
        "bn_dist_atr": 1.0,
        "bn_is_new_low_in_window": False,
        "atl_divergence_log": 0.05,
    }
    row.update(overrides)
    return row


def screen(*rows: dict[str, Any], cfg: AppConfig | None = None) -> pd.DataFrame:
    return apply_screen(pd.DataFrame(list(rows)), cfg or config()).set_index("cg_id")


def test_default_config_ranks_without_near_atl_filter() -> None:
    out = screen(coin("a", cg_dist_log=5.0), coin("b", cg_dist_log=0.01))
    assert out["passes_near_atl"].isna().all()
    assert out["passes_all"].all()


def test_market_cap() -> None:
    out = screen(coin("small", market_cap_usd=1e6), coin("none", market_cap_usd=NAN))
    assert not out.loc["small", "passes_mcap"] and not out.loc["small", "passes_all"]
    assert not out.loc["none", "passes_mcap"]


def test_excluded_never_passes() -> None:
    out = screen(coin("usdt", excluded=True, exclusion_reason="category:stablecoins"))
    assert out.loc["usdt", "passes_mcap"] and not out.loc["usdt", "passes_all"]


def test_unknown_age_is_null_and_required_by_default() -> None:
    out = screen(coin("young", age_status="unknown"))
    assert pd.isna(out.loc["young", "passes_age"])
    assert not out.loc["young", "passes_all"]
    relaxed = screen(coin("young", age_status="unknown"), cfg=config(require_age=False))
    assert relaxed.loc["young", "passes_all"]


def test_coingecko_age_evidence_passes() -> None:
    out = screen(coin("old", age_status="coingecko_atl_or_ath_date"))
    assert out.loc["old", "passes_age"] and out.loc["old", "passes_all"]


def test_near_atl_threshold_on_chosen_metric_and_source() -> None:
    cfg = config(near_max=1.5, metric="dist_atr", source="binance_1y")
    assert near_atl_column(cfg) == "bn_dist_atr"
    out = screen(
        coin("near", bn_dist_atr=1.5),
        coin("far", bn_dist_atr=1.6),
        coin("nopair", bn_dist_atr=NAN, binance_symbol=None, mapping_status="no_pair"),
        cfg=cfg,
    )
    assert out.loc["near", "passes_near_atl"] and out.loc["near", "passes_all"]
    assert not out.loc["far", "passes_near_atl"] and not out.loc["far", "passes_all"]
    assert pd.isna(out.loc["nopair", "passes_near_atl"])
    assert not out.loc["nopair", "passes_all"]


def test_require_binance_pair() -> None:
    rows = [coin("paired"), coin("nopair", binance_symbol=None, mapping_status="no_pair")]
    assert screen(*rows)["passes_all"].all()
    strict = screen(*rows, cfg=config(require_pair=True))
    assert strict.loc["paired", "passes_all"] and not strict.loc["nopair", "passes_all"]


def test_flag_dtypes() -> None:
    out = screen(coin("a"), coin("b", age_status="unknown"))
    for column in ["passes_mcap", "passes_age", "passes_near_atl"]:
        assert str(out[column].dtype) == "boolean", column
    assert out["passes_all"].dtype == bool


# --- run_screen against a store ---------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "db.duckdb")
    old = (NOW - timedelta(days=900)).isoformat()
    rows = [
        {"id": "aaa", "symbol": "aaa", "name": "A", "market_cap": 5e7, "current_price": 1.2,
         "atl": 1.0, "atl_date": old, "ath_date": old, "last_updated": NOW.isoformat()},
        {"id": "bbb", "symbol": "bbb", "name": "B", "market_cap": 5e7, "current_price": 3.0,
         "atl": 1.0, "atl_date": old, "ath_date": old, "last_updated": NOW.isoformat()},
        {"id": "usdt", "symbol": "usdt", "name": "Tether", "market_cap": 1e11,
         "current_price": 1.0, "atl": 0.9, "atl_date": old, "ath_date": old,
         "last_updated": NOW.isoformat()},
    ]  # fmt: skip
    markets = normalize_markets(rows)
    store.upsert_coins(coins_frame(markets), NOW)
    store.upsert_market_snapshots(snapshots_frame(markets, "fetch-1", NOW))
    store.update_coin_columns(pd.DataFrame({"cg_id": ["usdt"], "excluded": [True]}), NOW)
    return store


def test_run_screen_records_run_and_results(store: Store) -> None:
    summary = run_screen(config(near_max=0.5), store, now=lambda: NOW)
    assert summary.snapshot_run_id == "fetch-1"
    run = store.query_df("SELECT * FROM runs").iloc[0]
    assert run["status"] == "ok" and run["snapshot_run_id"] == "fetch-1"
    assert run["cg_calls"] == 0 and run["binance_weight"] == 0
    results = store.query_df("SELECT * FROM screen_results").set_index("cg_id")
    assert list(store.query_df("SELECT * FROM screen_results").columns) == SCREEN_COLUMNS
    assert results.loc["aaa", "passes_all"]  # ln(1.2) = 0.18 <= 0.5
    assert not results.loc["bbb", "passes_all"]  # ln(3) = 1.10 > 0.5
    assert not results.loc["usdt", "passes_all"]
    assert store.count("atl_metrics") == 3  # CoinGecko rows only: no candles stored


def test_run_screen_without_data_fails_cleanly(tmp_path: Path) -> None:
    with Store(tmp_path / "empty.duckdb") as store:
        with pytest.raises(NoMarketDataError, match="fetch"):
            run_screen(config(), store, now=lambda: NOW)
        assert store.query_df("SELECT status FROM runs").iloc[0, 0] == "failed"
