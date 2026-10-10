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
