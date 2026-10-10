"""Normalize /coins/markets payloads into coins and market_snapshots frames."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# raw field -> normalized column
FLOAT_FIELDS = {
    "current_price": "price_usd",
    "low_24h": "low_24h",
    "high_24h": "high_24h",
    "market_cap": "market_cap_usd",
    "total_volume": "total_volume_usd",
    "ath": "ath",
    "atl": "atl",
    "atl_change_percentage": "atl_change_percentage",
}
TIME_FIELDS = {"ath_date": "ath_date", "atl_date": "atl_date", "last_updated": "cg_last_updated"}

MARKET_COLUMNS = [
    "cg_id",
    "symbol",
    "name",
    "price_usd",
    "low_24h",
    "high_24h",
    "market_cap_usd",
    "market_cap_rank",
    "total_volume_usd",
    "ath",
    "ath_date",
    "atl",
    "atl_date",
    "atl_change_percentage",
    "cg_last_updated",
]
COIN_COLUMNS = ["cg_id", "symbol", "name"]
SNAPSHOT_COLUMNS = ["run_id", "cg_id", "snapshot_ts", *MARKET_COLUMNS[3:]]


def normalize_markets(rows: Sequence[dict[str, Any]]) -> pd.DataFrame:
    """Raw /coins/markets rows -> one row per cg_id with strict dtypes (UTC, float64).

    Rows without an id are dropped; duplicates (rank shifts between pages) keep the first.
    """
    frame = pd.DataFrame(list(rows))
    if frame.empty:
        return _empty_markets()
    frame = frame.rename(columns={"id": "cg_id", **FLOAT_FIELDS, **TIME_FIELDS})
    frame = frame.reindex(columns=MARKET_COLUMNS)
    frame = frame[frame["cg_id"].notna()]
    duplicates = int(frame["cg_id"].duplicated().sum())
    if duplicates:
        logger.info("dropping %d duplicate coins from paginated markets", duplicates)
    frame = frame.drop_duplicates("cg_id", keep="first")
    return _apply_dtypes(frame).reset_index(drop=True)


def coins_frame(markets: pd.DataFrame) -> pd.DataFrame:
    """Columns of the coins table known from market data."""
    return markets[COIN_COLUMNS].copy()


def snapshots_frame(markets: pd.DataFrame, run_id: str, snapshot_ts: datetime) -> pd.DataFrame:
    """market_snapshots rows for one run."""
    frame = markets.copy()
    frame["run_id"] = run_id
    frame["snapshot_ts"] = pd.Timestamp(snapshot_ts).tz_convert("UTC")
    return frame[SNAPSHOT_COLUMNS]


def _apply_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column in ["cg_id", "symbol", "name"]:
        frame[column] = frame[column].astype("string")
    for column in FLOAT_FIELDS.values():
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    frame["market_cap_rank"] = pd.to_numeric(frame["market_cap_rank"], errors="coerce").astype(
        "Int64"
    )
    for column in TIME_FIELDS.values():
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce", format="ISO8601")
    return frame


def _empty_markets() -> pd.DataFrame:
    return _apply_dtypes(pd.DataFrame(columns=MARKET_COLUMNS))
