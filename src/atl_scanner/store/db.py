"""DuckDB connection, schema creation (see ARCHITECTURE.md section 3), idempotent
upserts and read helpers.

Timestamps are stored as naive TIMESTAMP in UTC; the connection time zone is pinned to UTC
so tz-aware pandas columns are converted consistently on any machine.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import duckdb
import pandas as pd

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id         VARCHAR PRIMARY KEY,
        started_at     TIMESTAMP NOT NULL,
        finished_at    TIMESTAMP,
        config_hash    VARCHAR NOT NULL,
        config_yaml    VARCHAR NOT NULL,
        cg_calls       INTEGER,
        binance_weight INTEGER,
        status         VARCHAR NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS coins (
        cg_id            VARCHAR PRIMARY KEY,
        symbol           VARCHAR,
        name             VARCHAR,
        categories       VARCHAR[],
        excluded         BOOLEAN,
        exclusion_reason VARCHAR,
        binance_symbol   VARCHAR,
        binance_price_multiplier INTEGER,
        mapping_status   VARCHAR,
        binance_price_dev_log    DOUBLE,
        first_seen_at    TIMESTAMP NOT NULL,
        updated_at       TIMESTAMP NOT NULL
    )""",
    """
    CREATE TABLE IF NOT EXISTS market_snapshots (
        run_id                VARCHAR NOT NULL,
        cg_id                 VARCHAR NOT NULL,
        snapshot_ts           TIMESTAMP NOT NULL,
        price_usd             DOUBLE,
        market_cap_usd        DOUBLE,
        market_cap_rank       INTEGER,
        total_volume_usd      DOUBLE,
        ath                   DOUBLE,
        ath_date              TIMESTAMP,
        atl                   DOUBLE,
        atl_date              TIMESTAMP,
        atl_change_percentage DOUBLE,
        cg_last_updated       TIMESTAMP,
        PRIMARY KEY (run_id, cg_id)
    )""",
    """
    CREATE TABLE IF NOT EXISTS ohlcv_1h (
        exchange     VARCHAR NOT NULL,
        symbol       VARCHAR NOT NULL,
        open_time    TIMESTAMP NOT NULL,
        open         DOUBLE,
        high         DOUBLE,
        low          DOUBLE,
        close        DOUBLE,
        volume       DOUBLE,
        quote_volume DOUBLE,
        trades       INTEGER,
        PRIMARY KEY (exchange, symbol, open_time)
    )""",
    """
    CREATE TABLE IF NOT EXISTS atl_metrics (
        run_id               VARCHAR NOT NULL,
        cg_id                VARCHAR NOT NULL,
        atl_source           VARCHAR NOT NULL,
        atl_price            DOUBLE,
        atl_ts               TIMESTAMP,
        price                DOUBLE,
        price_ts             TIMESTAMP,
        dist_pct             DOUBLE,
        dist_log             DOUBLE,
        atr_period           INTEGER,
        atr_value            DOUBLE,
        dist_atr             DOUBLE,
        new_low_window_days  INTEGER,
        is_new_low_in_window BOOLEAN,
        days_since_atl       DOUBLE,
        PRIMARY KEY (run_id, cg_id, atl_source)
    )""",
    """
    CREATE TABLE IF NOT EXISTS screen_results (
        run_id                  VARCHAR NOT NULL,
        cg_id                   VARCHAR NOT NULL,
        symbol                  VARCHAR,
        binance_symbol          VARCHAR,
        mapping_status          VARCHAR,
        market_cap_usd          DOUBLE,
        age_status              VARCHAR,
        first_candle_ts         TIMESTAMP,
        excluded                BOOLEAN,
        exclusion_reason        VARCHAR,
        cg_dist_pct             DOUBLE,
        cg_dist_log             DOUBLE,
        cg_dist_atr             DOUBLE,
        bn_dist_pct             DOUBLE,
        bn_dist_log             DOUBLE,
        bn_dist_atr             DOUBLE,
        bn_is_new_low_in_window BOOLEAN,
        atl_divergence_log      DOUBLE,
        passes_mcap             BOOLEAN,
        passes_age              BOOLEAN,
        passes_near_atl         BOOLEAN,
        passes_all              BOOLEAN,
        PRIMARY KEY (run_id, cg_id)
    )""",
]

# Columns added after a table was first created; applied to existing databases on open.
MIGRATIONS = [
    "ALTER TABLE coins ADD COLUMN IF NOT EXISTS binance_price_multiplier INTEGER",
    "ALTER TABLE coins ADD COLUMN IF NOT EXISTS binance_price_dev_log DOUBLE",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS snapshot_run_id VARCHAR",
]

TABLES = (
    "runs",
    "coins",
    "market_snapshots",
    "ohlcv_1h",
    "atl_metrics",
    "screen_results",
)


class Store:
    """Single-file DuckDB store; creates the schema on open."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path), read_only=read_only)
        self.con.execute("SET TimeZone = 'UTC'")
        if not read_only:
            for ddl in [*SCHEMA, *MIGRATIONS]:
                self.con.execute(ddl)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # runs ---------------------------------------------------------------

    def start_run(
        self, run_id: str, started_at: datetime, config_hash: str, config_yaml: str
    ) -> None:
        self.con.execute(
            "INSERT INTO runs (run_id, started_at, config_hash, config_yaml, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            [run_id, _naive_utc(started_at), config_hash, config_yaml],
        )

    def finish_run(
        self,
        run_id: str,
        finished_at: datetime,
        status: str,
        *,
        cg_calls: int | None = None,
        binance_weight: int | None = None,
        snapshot_run_id: str | None = None,
    ) -> None:
        """Close a run; snapshot_run_id names the fetch run whose data was screened."""
        self.con.execute(
            "UPDATE runs SET finished_at = ?, status = ?, cg_calls = ?, binance_weight = ?, "
            "snapshot_run_id = coalesce(?, snapshot_run_id) WHERE run_id = ?",
            [
                _naive_utc(finished_at),
                status,
                cg_calls,
                binance_weight,
                snapshot_run_id,
                run_id,
            ],
        )

    # coins & snapshots --------------------------------------------------

    def upsert_coins(self, coins: pd.DataFrame, now: datetime) -> None:
        """Insert new coins, refresh symbol/name of known ones.

        Exclusion and mapping columns are owned by their own steps and left untouched here.
        """
        frame = coins.assign(first_seen_at=_naive_utc(now), updated_at=_naive_utc(now))
        self._upsert(
            "coins",
            frame,
            key=["cg_id"],
            update=["symbol", "name", "updated_at"],
        )

    def update_coin_columns(self, frame: pd.DataFrame, now: datetime) -> None:
        """Set the given columns (exclusions, mapping) for existing coins by cg_id."""
        columns = [c for c in frame.columns if c != "cg_id"]
        if frame.empty or not columns:
            return
        casts = {"categories": "CAST(i.categories AS VARCHAR[])"}
        assignments = ", ".join(f"{c} = {casts.get(c, f'i.{c}')}" for c in columns)
        self.con.register("incoming", frame)
        try:
            self.con.execute(
                f"UPDATE coins SET {assignments}, updated_at = ? "
                "FROM incoming i WHERE coins.cg_id = i.cg_id",
                [_naive_utc(now)],
            )
        finally:
            self.con.unregister("incoming")

    def upsert_market_snapshots(self, snapshots: pd.DataFrame) -> None:
        columns = [c for c in snapshots.columns if c not in ("run_id", "cg_id")]
        self._upsert("market_snapshots", snapshots, key=["run_id", "cg_id"], update=columns)

    # candles ------------------------------------------------------------

    def upsert_ohlcv(self, frame: pd.DataFrame) -> None:
        values = ["open", "high", "low", "close", "volume", "quote_volume", "trades"]
        self._upsert("ohlcv_1h", frame, key=["exchange", "symbol", "open_time"], update=values)

    def last_open_times(self, exchange: str) -> dict[str, datetime]:
        """Latest stored candle per symbol, as tz-aware UTC."""
        rows = self.con.execute(
            "SELECT symbol, max(open_time) FROM ohlcv_1h WHERE exchange = ? GROUP BY symbol",
            [exchange],
        ).fetchall()
        return {symbol: _aware_utc(ts) for symbol, ts in rows}

    def prune_ohlcv(self, exchange: str, before: datetime) -> int:
        """Delete candles older than `before` (history is capped at one year)."""
        cutoff = _naive_utc(before)
        row = self.con.execute(
            "SELECT count(*) FROM ohlcv_1h WHERE exchange = ? AND open_time < ?",
            [exchange, cutoff],
        ).fetchone()
        self.con.execute(
            "DELETE FROM ohlcv_1h WHERE exchange = ? AND open_time < ?", [exchange, cutoff]
        )
        return int(row[0]) if row else 0

    def candle_targets(self, min_market_cap_usd: float) -> list[str]:
        """Binance symbols of non-excluded coins above the threshold in the latest snapshot."""
        rows = self.con.execute(
            """
            SELECT DISTINCT c.binance_symbol
            FROM coins c
            JOIN market_snapshots m USING (cg_id)
            WHERE m.run_id = (SELECT max(run_id) FROM market_snapshots)
              AND m.market_cap_usd >= ?
              AND c.binance_symbol IS NOT NULL
              AND NOT coalesce(c.excluded, false)
            ORDER BY 1
            """,
            [min_market_cap_usd],
        ).fetchall()
        return [row[0] for row in rows]

    def ohlcv(self, exchange: str, symbol: str) -> pd.DataFrame:
        """All stored candles of one symbol, oldest first, open_time tz-aware UTC."""
        frame = self.query_df(
            "SELECT * FROM ohlcv_1h WHERE exchange = ? AND symbol = ? ORDER BY open_time",
            [exchange, symbol],
        )
        return _localize(frame, ["open_time"])

    # metrics --------------------------------------------------------------

    def latest_snapshot(self) -> pd.DataFrame:
        """Latest market snapshot joined with coin flags; timestamps tz-aware UTC."""
        frame = self.query_df(
            """
            SELECT m.run_id AS snapshot_run_id, m.cg_id, c.symbol, c.name,
                   m.price_usd, m.market_cap_usd, m.atl, m.atl_date, m.ath_date,
                   m.cg_last_updated, c.binance_symbol, c.binance_price_multiplier,
                   c.mapping_status, c.excluded, c.exclusion_reason
            FROM market_snapshots m
            JOIN coins c USING (cg_id)
            WHERE m.run_id = (SELECT max(run_id) FROM market_snapshots)
            ORDER BY m.market_cap_usd DESC
            """
        )
        return _localize(frame, ["atl_date", "ath_date", "cg_last_updated"])

    def upsert_atl_metrics(self, frame: pd.DataFrame) -> None:
        key = ["run_id", "cg_id", "atl_source"]
        update = [c for c in frame.columns if c not in key]
        self._upsert("atl_metrics", frame, key=key, update=update)

    def upsert_screen_results(self, frame: pd.DataFrame) -> None:
        key = ["run_id", "cg_id"]
        update = [c for c in frame.columns if c not in key]
        self._upsert("screen_results", frame, key=key, update=update)

    # reads --------------------------------------------------------------

    def query_df(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def count(self, table: str) -> int:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        row = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    # internals ----------------------------------------------------------

    def _upsert(
        self, table: str, frame: pd.DataFrame, *, key: list[str], update: list[str]
    ) -> None:
        if frame.empty:
            return
        columns = list(frame.columns)
        column_list = ", ".join(columns)
        assignments = ", ".join(f"{c} = excluded.{c}" for c in update)
        self.con.register("incoming", frame)
        try:
            self.con.execute(
                f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM incoming "
                f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {assignments}"
            )
        finally:
            self.con.unregister("incoming")


def _naive_utc(moment: datetime) -> datetime:
    """Naive UTC datetime for TIMESTAMP columns; rejects naive input to avoid local-time bugs."""
    if moment.tzinfo is None:
        raise ValueError("expected a timezone-aware datetime")
    return pd.Timestamp(moment).tz_convert("UTC").tz_localize(None).to_pydatetime()


def _aware_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC)


def _localize(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Mark naive UTC TIMESTAMP columns as tz-aware UTC."""
    for column in columns:
        frame[column] = pd.to_datetime(frame[column]).dt.tz_localize("UTC")
    return frame
