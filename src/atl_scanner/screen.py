"""Apply config thresholds (market cap, age, optional near-ATL limit) to metrics
and write screen_results. Without a near-ATL threshold coins are ranked, not filtered.

Flags are three-valued where the answer can be unknown: passes_age is NULL for coins
without age evidence, passes_near_atl is NULL when no threshold is set or the metric is
missing. passes_all treats unknown as "does not pass" only for the checks that are required.
"""

from __future__ import annotations

import pandas as pd

from atl_scanner.config import AppConfig
from atl_scanner.models import AgeStatus, AtlSource

SCREEN_COLUMNS = [
    "run_id",
    "cg_id",
    "symbol",
    "binance_symbol",
    "mapping_status",
    "market_cap_usd",
    "age_status",
    "first_candle_ts",
    "excluded",
    "exclusion_reason",
    "cg_dist_pct",
    "cg_dist_log",
    "cg_dist_atr",
    "bn_dist_pct",
    "bn_dist_log",
    "bn_dist_atr",
    "bn_is_new_low_in_window",
    "atl_divergence_log",
    "passes_mcap",
    "passes_age",
    "passes_near_atl",
    "passes_all",
]
SOURCE_PREFIX = {AtlSource.COINGECKO_ALL_TIME: "cg", AtlSource.BINANCE_1Y: "bn"}


def near_atl_column(config: AppConfig) -> str:
    """Column holding the metric the near-ATL threshold applies to, e.g. cg_dist_log."""
    near = config.screen.near_atl
    return f"{SOURCE_PREFIX[near.atl_source]}_{near.metric.value}"


def apply_screen(coins: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    """Add passes_* flags to per-coin metric rows (output of pipeline.compute_metrics)."""
    frame = coins.copy()
    cap = pd.to_numeric(frame["market_cap_usd"], errors="coerce")
    frame["passes_mcap"] = (cap >= config.universe.min_market_cap_usd).astype("boolean")
    frame["passes_age"] = _passes_age(frame["age_status"])
    frame["passes_near_atl"] = _passes_near_atl(frame, config)
    frame["passes_all"] = _passes_all(frame, config)
    return frame


def _passes_age(status: pd.Series) -> pd.Series:
    known = status != AgeStatus.UNKNOWN.value
    return pd.Series(pd.NA, index=status.index, dtype="boolean").mask(known, True)


def _passes_near_atl(frame: pd.DataFrame, config: AppConfig) -> pd.Series:
    limit = config.screen.near_atl.max
    unknown = pd.Series(pd.NA, index=frame.index, dtype="boolean")
    if limit is None:
        return unknown
    values = pd.to_numeric(frame[near_atl_column(config)], errors="coerce")
    return unknown.mask(values.notna(), values <= limit)


def _passes_all(frame: pd.DataFrame, config: AppConfig) -> pd.Series:
    screen = config.screen
    passes = frame["passes_mcap"].fillna(False) & ~frame["excluded"].fillna(False).astype(bool)
    if screen.require_age:
        passes &= frame["passes_age"].fillna(False)
    if screen.near_atl.max is not None:
        passes &= frame["passes_near_atl"].fillna(False)
    if screen.require_binance_pair:
        passes &= frame["binance_symbol"].notna()
    return passes.astype(bool)
