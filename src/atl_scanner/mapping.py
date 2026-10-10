"""Map CoinGecko ids to Binance symbols (e.g. ABCUSDT).

Statuses: mapped | no_pair | ambiguous | manual | price_mismatch. Unmapped coins are kept
and flagged.

Rules:
- the Binance base asset must equal the CoinGecko symbol (case-insensitive), optionally
  behind a configured prefix such as 1000 (1000SATSUSDT quotes 1000 SATS);
- the pair price, per one token, must be within mapping.max_price_deviation_log of the
  CoinGecko 24h range [low_24h, high_24h] (widened to include current_price), which
  filters out different tokens sharing a ticker. A range instead of a single price
  tolerates CoinGecko data being up to the markets cache TTL older than Binance's;
- if several coins still claim one pair, the one closest to its range holds it (then
  closest to the current price, then larger market cap); all are marked ambiguous;
- mapping.overrides win over everything.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import pandas as pd

from atl_scanner.config import MappingConfig
from atl_scanner.models import MappingStatus

logger = logging.getLogger(__name__)

MAPPING_COLUMNS = [
    "cg_id",
    "binance_symbol",
    "binance_price_multiplier",
    "mapping_status",
    "binance_price_dev_log",
]


@dataclass(frozen=True)
class Candidate:
    symbol: str
    multiplier: int
    price: float


@dataclass(frozen=True)
class Coin:
    cg_id: str
    symbol: str
    price_usd: float
    market_cap_usd: float
    low_24h: float = math.nan
    high_24h: float = math.nan


def map_to_binance(coins: pd.DataFrame, pairs: pd.DataFrame, config: MappingConfig) -> pd.DataFrame:
    """One row per coin with its Binance pair (or None) and mapping status.

    `coins` needs cg_id, symbol, price_usd, market_cap_usd and optionally low_24h,
    high_24h; `pairs` is normalize_pairs output.
    """
    nan_column = [math.nan] * len(coins)
    coin_list = [
        Coin(str(cg_id), str(symbol), float(price), float(cap), float(low), float(high))
        for cg_id, symbol, price, cap, low, high in zip(
            coins["cg_id"].tolist(),
            coins["symbol"].tolist(),
            coins["price_usd"].tolist(),
            coins["market_cap_usd"].tolist(),
            coins["low_24h"].tolist() if "low_24h" in coins else nan_column,
            coins["high_24h"].tolist() if "high_24h" in coins else nan_column,
            strict=True,
        )
    ]
    results: dict[str, dict[str, object]] = {}
    taken = _apply_overrides(coin_list, pairs, config, results)
    _map_automatically(coin_list, _pair_index(pairs, config), taken, config, results)
    frame = pd.DataFrame([results[c.cg_id] for c in coin_list], columns=MAPPING_COLUMNS)
    return frame.astype({"binance_price_multiplier": "Int64", "binance_price_dev_log": "float64"})


def _pair_index(pairs: pd.DataFrame, config: MappingConfig) -> dict[str, Candidate]:
    """Token symbol (upper) -> best pair: direct base before prefixed, then quote priority."""
    index: dict[str, Candidate] = {}
    prefixes = [("", 1), *config.base_prefix_multipliers.items()]
    for prefix, multiplier in prefixes:
        for quote in config.quote_assets:
            subset = pairs[(pairs["quote_asset"] == quote)]
            for symbol, base, price in subset[["symbol", "base_asset", "price"]].itertuples(
                index=False
            ):
                if prefix and not (base.startswith(prefix) and len(base) > len(prefix)):
                    continue
                key = base[len(prefix) :]
                index.setdefault(key, Candidate(symbol, multiplier, float(price)))
    return index


def _apply_overrides(
    coins: list[Coin],
    pairs: pd.DataFrame,
    config: MappingConfig,
    results: dict[str, dict[str, object]],
) -> set[str]:
    bases = dict(zip(pairs["symbol"].tolist(), pairs["base_asset"].tolist(), strict=True))
    prices = dict(zip(pairs["symbol"].tolist(), pairs["price"].tolist(), strict=True))
    taken: set[str] = set()
    for coin in coins:
        if coin.cg_id not in config.overrides:
            continue
        target = config.overrides[coin.cg_id]
        if target is None:
            results[coin.cg_id] = _row(coin, None, MappingStatus.MANUAL)
        elif target not in bases:
            logger.warning("override %s -> %s: pair is not trading", coin.cg_id, target)
            results[coin.cg_id] = _row(coin, None, MappingStatus.NO_PAIR)
        else:
            candidate = Candidate(
                target,
                _override_multiplier(str(bases[target]), coin.symbol, config),
                float(prices[target]),
            )
            results[coin.cg_id] = _row(coin, candidate, MappingStatus.MANUAL)
            taken.add(target)
    return taken


def _override_multiplier(base: str, coin_symbol: str, config: MappingConfig) -> int:
    for prefix, multiplier in config.base_prefix_multipliers.items():
        if base == f"{prefix}{coin_symbol.upper()}":
            return multiplier
    return 1


def _map_automatically(
    coins: list[Coin],
    index: dict[str, Candidate],
    taken: set[str],
    config: MappingConfig,
    results: dict[str, dict[str, object]],
) -> None:
    claims: dict[str, list[tuple[Coin, Candidate]]] = {}
    for coin in coins:
        if coin.cg_id in results:
            continue
        candidate = index.get(coin.symbol.upper())
        if candidate is None:
            results[coin.cg_id] = _row(coin, None, MappingStatus.NO_PAIR)
        elif candidate.symbol in taken:
            results[coin.cg_id] = _row(coin, None, MappingStatus.AMBIGUOUS)
        else:
            claims.setdefault(candidate.symbol, []).append((coin, candidate))
    for claimants in claims.values():
        _resolve_claims(claimants, config, results)


def _resolve_claims(
    claimants: list[tuple[Coin, Candidate]],
    config: MappingConfig,
    results: dict[str, dict[str, object]],
) -> None:
    plausible = []
    for coin, candidate in claimants:
        deviation = _price_deviation(coin, candidate)
        if deviation > config.max_price_deviation_log:  # NaN compares False: unknown passes
            row = _row(coin, None, MappingStatus.PRICE_MISMATCH)
            row["binance_price_dev_log"] = deviation
            results[coin.cg_id] = row
        else:
            plausible.append((coin, candidate))
    if len(plausible) == 1:
        coin, candidate = plausible[0]
        results[coin.cg_id] = _row(coin, candidate, MappingStatus.MAPPED)
        return
    plausible.sort(key=lambda pair: _claim_sort_key(*pair))
    for rank, (coin, candidate) in enumerate(plausible):
        row = _row(coin, candidate, MappingStatus.AMBIGUOUS)
        if rank > 0:
            row["binance_symbol"] = None
            row["binance_price_multiplier"] = None
        results[coin.cg_id] = row


def _claim_sort_key(coin: Coin, candidate: Candidate) -> tuple[float, float, float]:
    """Closest to the 24h range, then to the current price (unknown last), then cap."""
    cap = coin.market_cap_usd if math.isfinite(coin.market_cap_usd) else -math.inf
    return (
        _finite_or_inf(_price_deviation(coin, candidate)),
        _finite_or_inf(_point_deviation(coin, candidate)),
        -cap,
    )


def _finite_or_inf(value: float) -> float:
    return value if math.isfinite(value) else math.inf


def _price_deviation(coin: Coin, candidate: Candidate) -> float:
    """Log distance from the Binance price per token to the CoinGecko 24h range.

    0 inside the range; falls back to the current price when the range is missing;
    NaN if prices are unusable.
    """
    bn_price = candidate.price / candidate.multiplier
    if not (bn_price > 0 and coin.price_usd > 0):
        return math.nan
    bounds = [p for p in (coin.low_24h, coin.high_24h, coin.price_usd) if p > 0]
    low, high = min(bounds), max(bounds)
    if low <= bn_price <= high:
        return 0.0
    return min(abs(math.log(bn_price / low)), abs(math.log(bn_price / high)))


def _point_deviation(coin: Coin, candidate: Candidate) -> float:
    bn_price = candidate.price / candidate.multiplier
    if not (bn_price > 0 and coin.price_usd > 0):
        return math.nan
    return abs(math.log(bn_price / coin.price_usd))


def _row(coin: Coin, candidate: Candidate | None, status: MappingStatus) -> dict[str, object]:
    return {
        "cg_id": coin.cg_id,
        "binance_symbol": candidate.symbol if candidate else None,
        "binance_price_multiplier": candidate.multiplier if candidate else None,
        "mapping_status": status.value,
        "binance_price_dev_log": _price_deviation(coin, candidate) if candidate else math.nan,
    }
