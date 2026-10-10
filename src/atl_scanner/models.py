"""Domain types shared across modules: enums (AtlSource, MappingStatus, AgeStatus,
ExclusionReason) and small frozen dataclasses. No I/O.
"""

from __future__ import annotations

from enum import StrEnum


class AtlSource(StrEnum):
    """Where an ATL value comes from; the two sources cover different history."""

    COINGECKO_ALL_TIME = "coingecko_all_time"
    BINANCE_1Y = "binance_1y"


class DistMetric(StrEnum):
    """Distance-to-ATL metrics stored for every coin and ATL source."""

    DIST_PCT = "dist_pct"
    DIST_LOG = "dist_log"
    DIST_ATR = "dist_atr"


class MappingStatus(StrEnum):
    """Result of matching a CoinGecko coin to a Binance pair."""

    MAPPED = "mapped"
    NO_PAIR = "no_pair"
    AMBIGUOUS = "ambiguous"  # several coins match the pair; the largest by market cap holds it
    MANUAL = "manual"  # from mapping.overrides
    PRICE_MISMATCH = "price_mismatch"  # same ticker, price too far from CoinGecko's


class AgeStatus(StrEnum):
    """Evidence that a coin had a price at least universe.min_age_days ago."""

    BINANCE_FIRST_CANDLE = "binance_first_candle"
    COINGECKO_ATL_OR_ATH_DATE = "coingecko_atl_or_ath_date"
    UNKNOWN = "unknown"  # no evidence either way; not the same as "young"
