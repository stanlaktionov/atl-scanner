"""Exclude stablecoins and wrapped/bridged tokens (CoinGecko categories, manual
lists, name regexes from config).

Every exclusion records its reason; nothing is dropped silently. Coins listed in
exclusions.keep_ids stay in the universe but keep their reasons for visibility.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import pandas as pd

from atl_scanner.config import ExclusionsConfig

EXCLUSION_COLUMNS = ["cg_id", "categories", "excluded", "exclusion_reason"]


def classify_exclusions(
    coins: pd.DataFrame,
    category_ids: Mapping[str, set[str]],
    config: ExclusionsConfig,
) -> pd.DataFrame:
    """Per coin: checked categories it belongs to, excluded flag and all reasons.

    `coins` needs cg_id, symbol, name. Reasons are joined with ';' in a fixed order:
    manual_id, manual_symbol, category:<id>..., name_pattern:<regex>...
    """
    manual_ids = set(config.ids)
    keep_ids = set(config.keep_ids)
    manual_symbols = {s.lower() for s in config.symbols}
    patterns = [(p, re.compile(p)) for p in config.name_patterns]
    rows = []
    for cg_id, symbol, name in coins[["cg_id", "symbol", "name"]].itertuples(index=False):
        categories = [c for c, ids in category_ids.items() if cg_id in ids]
        reasons = _reasons(cg_id, symbol, name, categories, manual_ids, manual_symbols, patterns)
        rows.append(
            {
                "cg_id": cg_id,
                "categories": categories,
                "excluded": bool(reasons) and cg_id not in keep_ids,
                "exclusion_reason": ";".join(reasons) if reasons else None,
            }
        )
    return pd.DataFrame(rows, columns=EXCLUSION_COLUMNS)


def _reasons(
    cg_id: str,
    symbol: object,
    name: object,
    categories: list[str],
    manual_ids: set[str],
    manual_symbols: set[str],
    patterns: list[tuple[str, re.Pattern[str]]],
) -> list[str]:
    reasons = []
    if cg_id in manual_ids:
        reasons.append("manual_id")
    if isinstance(symbol, str) and symbol.lower() in manual_symbols:
        reasons.append("manual_symbol")
    reasons.extend(f"category:{c}" for c in categories)
    if isinstance(name, str):
        reasons.extend(f"name_pattern:{p}" for p, rx in patterns if rx.search(name))
    return reasons
