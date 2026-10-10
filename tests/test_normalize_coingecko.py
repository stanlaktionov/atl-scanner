"""Tests for normalize/coingecko.py on recorded /coins/markets rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from atl_scanner.normalize.coingecko import (
    MARKET_COLUMNS,
    SNAPSHOT_COLUMNS,
    coins_frame,
    normalize_markets,
    snapshots_frame,
)

FIXTURE = Path(__file__).parent / "fixtures" / "coingecko" / "markets_sample.json"


def sample_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return rows


def test_columns_and_dtypes() -> None:
    frame = normalize_markets(sample_rows())
    assert list(frame.columns) == MARKET_COLUMNS
    assert len(frame) == 5
    for column in ["price_usd", "market_cap_usd", "atl", "ath", "atl_change_percentage"]:
        assert frame[column].dtype == "float64", column
    assert str(frame["market_cap_rank"].dtype) == "Int64"
    for column in ["atl_date", "ath_date", "cg_last_updated"]:
        dtype = frame[column].dtype
        assert isinstance(dtype, pd.DatetimeTZDtype) and str(dtype.tz) == "UTC", column


def test_values_from_real_row() -> None:
    btc = normalize_markets(sample_rows()).set_index("cg_id").loc["bitcoin"]
    assert btc["symbol"] == "btc"
    assert btc["atl"] == 67.81
    assert btc["atl_date"] == pd.Timestamp("2013-07-05T16:00:00Z")


def test_null_rank_is_kept() -> None:
    frame = normalize_markets(sample_rows()).set_index("cg_id")
    assert pd.isna(frame.loc["usdkg", "market_cap_rank"])
    assert frame.loc["usdkg", "market_cap_usd"] > 0


def test_missing_values_become_nan_and_nat() -> None:
    frame = normalize_markets([{"id": "x", "symbol": "x", "atl": None, "atl_date": None}])
    assert pd.isna(frame.loc[0, "atl"]) and pd.isna(frame.loc[0, "atl_date"])
    assert pd.isna(frame.loc[0, "market_cap_usd"])


def test_duplicates_and_missing_ids_dropped() -> None:
    rows = [
        {"id": "a", "symbol": "a", "market_cap": 2},
        {"id": None, "symbol": "?", "market_cap": 1},
        {"id": "a", "symbol": "a", "market_cap": 1},
    ]
    frame = normalize_markets(rows)
    assert frame["cg_id"].tolist() == ["a"]
    assert frame.loc[0, "market_cap_usd"] == 2


def test_empty_input() -> None:
    frame = normalize_markets([])
    assert frame.empty and list(frame.columns) == MARKET_COLUMNS


def test_coin_and_snapshot_frames() -> None:
    markets = normalize_markets(sample_rows())
    assert list(coins_frame(markets).columns) == ["cg_id", "symbol", "name"]
    ts = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    snaps = snapshots_frame(markets, "run-1", ts)
    assert list(snaps.columns) == SNAPSHOT_COLUMNS
    assert (snaps["run_id"] == "run-1").all()
    assert (snaps["snapshot_ts"] == pd.Timestamp(ts)).all()
