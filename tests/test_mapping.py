"""Tests for mapping.py: CoinGecko -> Binance pair matching."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import pytest

from atl_scanner.config import MappingConfig
from atl_scanner.mapping import MAPPING_COLUMNS, map_to_binance

CONFIG = MappingConfig(
    quote_assets=["USDT", "USDC"],
    base_prefix_multipliers={"1000": 1000},
    max_price_deviation_log=0.1,
    overrides={},
)


def coins(*rows: tuple[str, str, float, float]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["cg_id", "symbol", "price_usd", "market_cap_usd"])


def pairs(*rows: tuple[str, str, str, float]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "base_asset", "quote_asset", "price"])


PAIRS = pairs(
    ("BTCUSDT", "BTC", "USDT", 60000.0),
    ("ETHUSDC", "ETH", "USDC", 3000.0),
    ("ETHUSDT", "ETH", "USDT", 3001.0),
    ("ONLYCUSDC", "ONLYC", "USDC", 1.0),
    ("1000SATSUSDT", "1000SATS", "USDT", 0.03),
    ("ABCUSDT", "ABC", "USDT", 2.0),
)


def run(
    coin_rows: list[tuple[str, str, float, float]], config: MappingConfig = CONFIG, **kw: Any
) -> pd.DataFrame:
    return map_to_binance(coins(*coin_rows), kw.get("pairs", PAIRS), config).set_index("cg_id")


def test_columns_and_order() -> None:
    frame = map_to_binance(coins(("b", "x", 1, 1), ("a", "y", 1, 1)), PAIRS, CONFIG)
    assert list(frame.columns) == MAPPING_COLUMNS
    assert frame["cg_id"].tolist() == ["b", "a"]


def test_mapped_case_insensitive() -> None:
    row = run([("bitcoin", "btc", 60300.0, 1e12)]).loc["bitcoin"]
    assert row["binance_symbol"] == "BTCUSDT"
    assert row["mapping_status"] == "mapped"
    assert row["binance_price_multiplier"] == 1
    assert row["binance_price_dev_log"] == pytest.approx(math.log(60300 / 60000))


def test_no_pair_is_kept() -> None:
    row = run([("nothing", "zzz", 1.0, 1e8)]).loc["nothing"]
    assert row["mapping_status"] == "no_pair"
    assert pd.isna(row["binance_symbol"])


def test_quote_priority() -> None:
    frame = run([("eth", "eth", 3000.0, 1e11), ("onlyc", "onlyc", 1.0, 1e8)])
    assert frame.loc["eth", "binance_symbol"] == "ETHUSDT"
    assert frame.loc["onlyc", "binance_symbol"] == "ONLYCUSDC"


def test_prefixed_pair_uses_multiplier_for_price_check() -> None:
    row = run([("sats", "sats", 0.00003, 3e8)]).loc["sats"]
    assert row["binance_symbol"] == "1000SATSUSDT"
    assert row["binance_price_multiplier"] == 1000
    assert row["mapping_status"] == "mapped"
    assert row["binance_price_dev_log"] == pytest.approx(0.0)


def test_direct_pair_preferred_over_prefixed() -> None:
    both = pairs(("SATSUSDT", "SATS", "USDT", 0.00003), ("1000SATSUSDT", "1000SATS", "USDT", 0.03))
    row = run([("sats", "sats", 0.00003, 3e8)], pairs=both).loc["sats"]
    assert row["binance_symbol"] == "SATSUSDT"


def test_price_mismatch() -> None:
    row = run([("fake-btc", "btc", 0.5, 1e8)]).loc["fake-btc"]
    assert row["mapping_status"] == "price_mismatch"
    assert pd.isna(row["binance_symbol"])
    assert row["binance_price_dev_log"] > 10


def test_collision_resolved_by_price() -> None:
    frame = run([("abc-big", "abc", 9.0, 5e9), ("abc-real", "abc", 2.02, 1e8)])
    assert frame.loc["abc-real", "mapping_status"] == "mapped"
    assert frame.loc["abc-real", "binance_symbol"] == "ABCUSDT"
    assert frame.loc["abc-big", "mapping_status"] == "price_mismatch"


def test_collision_with_matching_prices_is_ambiguous() -> None:
    frame = run([("abc-small", "abc", 2.0, 1e8), ("abc-large", "abc", 2.0, 5e8)])
    assert frame.loc["abc-large", "binance_symbol"] == "ABCUSDT"
    assert pd.isna(frame.loc["abc-small", "binance_symbol"])
    assert set(frame["mapping_status"]) == {"ambiguous"}


def test_unknown_price_does_not_block_mapping() -> None:
    row = run([("abc", "abc", float("nan"), 1e8)]).loc["abc"]
    assert row["mapping_status"] == "mapped"
    assert pd.isna(row["binance_price_dev_log"])


def test_overrides() -> None:
    config = CONFIG.model_copy(
        update={"overrides": {"real-abc": "ABCUSDT", "skip-btc": None, "gone": "NOPEUSDT"}}
    )
    frame = run(
        [
            ("real-abc", "xyz", 2.0, 1e8),
            ("other-abc", "abc", 2.0, 9e9),
            ("skip-btc", "btc", 60000.0, 1e12),
            ("gone", "gone", 1.0, 1e8),
        ],
        config,
    )
    assert frame.loc["real-abc", "binance_symbol"] == "ABCUSDT"
    assert frame.loc["real-abc", "mapping_status"] == "manual"
    assert frame.loc["other-abc", "mapping_status"] == "ambiguous"
    assert pd.isna(frame.loc["other-abc", "binance_symbol"])
    assert frame.loc["skip-btc", "mapping_status"] == "manual"
    assert pd.isna(frame.loc["skip-btc", "binance_symbol"])
    assert frame.loc["gone", "mapping_status"] == "no_pair"


def test_override_to_prefixed_pair_gets_multiplier() -> None:
    config = CONFIG.model_copy(update={"overrides": {"sats-x": "1000SATSUSDT"}})
    row = run([("sats-x", "sats", 0.00003, 1e8)], config).loc["sats-x"]
    assert row["binance_price_multiplier"] == 1000


def test_collision_closest_price_holds_pair_over_larger_cap() -> None:
    # real case 2026-10-10: MEMEUSDT 0.000569, memecoin-2 0.00056963, memetoon 0.00051674
    meme = pairs(("MEMEUSDT", "MEME", "USDT", 0.000569))
    frame = run(
        [("memetoon", "meme", 0.00051674, 5.2e7), ("memecoin-2", "meme", 0.00056963, 3.7e7)],
        pairs=meme,
    )
    assert frame.loc["memecoin-2", "binance_symbol"] == "MEMEUSDT"
    assert pd.isna(frame.loc["memetoon", "binance_symbol"])
    assert set(frame["mapping_status"]) == {"ambiguous"}
    assert frame.loc["memetoon", "binance_price_dev_log"] > 0.09
