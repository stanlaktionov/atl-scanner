"""Tests for exclusions.py."""

from __future__ import annotations

import pandas as pd

from atl_scanner.config import ExclusionsConfig
from atl_scanner.exclusions import EXCLUSION_COLUMNS, classify_exclusions

CONFIG = ExclusionsConfig(
    coingecko_categories=["stablecoins", "wrapped-tokens"],
    ids=["manual-coin"],
    symbols=["BAD"],
    name_patterns=[r"(?i)^wrapped\b", r"(?i)\bbridged\b"],
    keep_ids=["kept-coin"],
)
CATEGORY_IDS = {
    "stablecoins": {"tether", "usdc"},
    "wrapped-tokens": {"wrapped-bitcoin", "kept-coin"},
}


def coins(*rows: tuple[str, str, str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["cg_id", "symbol", "name"])


def classify(*rows: tuple[str, str, str]) -> pd.DataFrame:
    return classify_exclusions(coins(*rows), CATEGORY_IDS, CONFIG).set_index("cg_id")


def test_columns() -> None:
    frame = classify_exclusions(coins(("bitcoin", "btc", "Bitcoin")), CATEGORY_IDS, CONFIG)
    assert list(frame.columns) == EXCLUSION_COLUMNS


def test_regular_coin_is_kept() -> None:
    row = classify(("bitcoin", "btc", "Bitcoin")).loc["bitcoin"]
    assert not row["excluded"]
    assert row["exclusion_reason"] is None
    assert row["categories"] == []


def test_category() -> None:
    row = classify(("tether", "usdt", "Tether")).loc["tether"]
    assert row["excluded"] and row["exclusion_reason"] == "category:stablecoins"
    assert row["categories"] == ["stablecoins"]


def test_manual_id_and_symbol_case_insensitive() -> None:
    frame = classify(("manual-coin", "mc", "Manual"), ("other", "bad", "Other"))
    assert frame.loc["manual-coin", "exclusion_reason"] == "manual_id"
    assert frame.loc["other", "exclusion_reason"] == "manual_symbol"


def test_name_pattern() -> None:
    frame = classify(("w-eth", "weth", "Wrapped Ether"), ("x", "x", "Some Bridged USDC"))
    assert frame.loc["w-eth", "exclusion_reason"] == r"name_pattern:(?i)^wrapped\b"
    assert frame.loc["x", "exclusion_reason"] == r"name_pattern:(?i)\bbridged\b"


def test_name_pattern_does_not_match_inside_words() -> None:
    row = classify(("u", "u", "Unwrapped Protocol")).loc["u"]
    assert not row["excluded"]


def test_all_reasons_are_recorded_in_order() -> None:
    row = classify(("wrapped-bitcoin", "bad", "Wrapped Bitcoin")).loc["wrapped-bitcoin"]
    assert row["exclusion_reason"] == (
        r"manual_symbol;category:wrapped-tokens;name_pattern:(?i)^wrapped\b"
    )


def test_missing_name_and_symbol() -> None:
    row = classify(("n", None, None)).loc["n"]  # type: ignore[arg-type]
    assert not row["excluded"]


def test_keep_ids_stay_in_universe_with_reason() -> None:
    row = classify(("kept-coin", "kc", "Kept")).loc["kept-coin"]
    assert not row["excluded"]
    assert row["exclusion_reason"] == "category:wrapped-tokens"
