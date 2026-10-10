"""Data quality checks: missing 1h candles, zero-volume candles, CoinGecko vs
Binance ATL divergence, mapping and age status summary.

Checks only read the store; nothing is deleted or changed. Each check returns how many
coins/symbols are affected, a few examples and what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from atl_scanner.config import AppConfig
from atl_scanner.models import AgeStatus
from atl_scanner.store.db import Store

EXCHANGE = "binance"


@dataclass(frozen=True)
class CheckResult:
    name: str
    affected: int
    examples: list[str]
    action: str
    details: pd.DataFrame = field(default_factory=pd.DataFrame)


def run_checks(config: AppConfig, store: Store, now: datetime) -> list[CheckResult]:
    """All checks in a fixed order."""
    n = config.dq.examples
    return [
        candle_gaps(store, n),
        bad_candles(store, n),
        stale_candles(config, store, now, n),
        stale_prices(config, store, n),
        atl_divergence(config, store, n),
        mapping_status(store, n),
        age_status(config, store, n),
        exclusions(store, n),
    ]


def candle_gaps(store: Store, n: int) -> CheckResult:
    details = store.query_df(
        """
        WITH s AS (
            SELECT symbol, open_time,
                   lag(open_time) OVER (PARTITION BY symbol ORDER BY open_time) AS prev
            FROM ohlcv_1h WHERE exchange = ?
        )
        SELECT symbol,
               count(*) FILTER (WHERE open_time - prev > INTERVAL 1 HOUR) AS gaps,
               CAST(sum(CASE WHEN open_time - prev > INTERVAL 1 HOUR
                             THEN date_diff('hour', prev, open_time) - 1 ELSE 0 END)
                    AS BIGINT) AS missing_hours,
               max(date_diff('hour', prev, open_time)) - 1 AS longest_gap_hours
        FROM s GROUP BY symbol
        HAVING gaps > 0
        ORDER BY missing_hours DESC
        """,
        [EXCHANGE],
    )
    return CheckResult(
        "Пропуски 1h-свечей",
        len(details),
        _examples(details, n, "symbol", "missing_hours", "ч"),
        "дыры искажают ATR и минимумы; длинные — проверить остановку торгов/делистинг",
        details,
    )


def bad_candles(store: Store, n: int) -> CheckResult:
    details = store.query_df(
        """
        SELECT symbol,
               count(*) FILTER (WHERE volume = 0) AS zero_volume,
               count(*) FILTER (WHERE low <= 0 OR low > least(open, close)
                                OR high < greatest(open, close)) AS invalid,
               count(*) AS candles,
               round(100.0 * count(*) FILTER (WHERE volume = 0) / count(*), 2) AS zero_volume_pct
        FROM ohlcv_1h WHERE exchange = ?
        GROUP BY symbol
        HAVING zero_volume > 0 OR invalid > 0
        ORDER BY invalid DESC, zero_volume DESC
        """,
        [EXCHANGE],
    )
    return CheckResult(
        "Нулевой объём / некорректные свечи",
        len(details),
        _examples(details, n, "symbol", "zero_volume", " св."),
        "нулевой объём — неликвидные часы; invalid (low > open/close и т.п.) — ошибка данных",
        details,
    )


def stale_candles(config: AppConfig, store: Store, now: datetime, n: int) -> CheckResult:
    cutoff = now - timedelta(hours=config.dq.stale_candle_hours)
    targets = store.candle_targets(config.universe.min_market_cap_usd)
    last = store.last_open_times(EXCHANGE)
    rows = [
        {"symbol": s, "last_open_time": last.get(s)}
        for s in targets
        if s not in last or last[s] + timedelta(hours=1) < cutoff
    ]
    details = pd.DataFrame(rows, columns=["symbol", "last_open_time"])
    return CheckResult(
        f"Свечи старше {config.dq.stale_candle_hours} ч",
        len(details),
        details["symbol"].head(n).tolist(),
        "запустить fetch; если не помогает — пара остановлена (status BREAK) или делистинг",
        details,
    )


def stale_prices(config: AppConfig, store: Store, n: int) -> CheckResult:
    snapshot = store.latest_snapshot()
    if snapshot.empty:
        return _no_data("Цены CoinGecko")
    cutoff = pd.Timestamp(_snapshot_time(store, snapshot)) - pd.Timedelta(
        hours=config.dq.stale_price_hours
    )
    stale = snapshot[snapshot["cg_last_updated"] < cutoff.tz_localize("UTC")]
    details = stale[["cg_id", "symbol", "cg_last_updated", "market_cap_usd"]]
    return CheckResult(
        f"Цены CoinGecko старше {config.dq.stale_price_hours} ч",
        len(details),
        details["cg_id"].head(n).tolist(),
        "CoinGecko давно не обновлял цену: монета малоактивна или торговля остановлена",
        details,
    )


def atl_divergence(config: AppConfig, store: Store, n: int) -> CheckResult:
    results = _latest_screen(store)
    if results.empty:
        return _no_data("Расхождение ATL")
    limit = config.dq.divergence_alert_log
    kept = results[~results["excluded"].fillna(False).astype(bool)]
    details = kept[kept["atl_divergence_log"] < limit][
        ["cg_id", "symbol", "binance_symbol", "atl_divergence_log"]
    ].sort_values("atl_divergence_log")
    return CheckResult(
        f"ATL Binance ниже ATL CoinGecko (log < {limit}), без исключённых",
        len(details),
        _examples(details, n, "binance_symbol", "atl_divergence_log", ""),
        "проверить пару вручную: другой токен с тем же тикером или ошибочные свечи",
        details,
    )


def mapping_status(store: Store, n: int) -> CheckResult:
    snapshot = store.latest_snapshot()
    if snapshot.empty:
        return _no_data("Сопоставление с Binance")
    counts = snapshot["mapping_status"].fillna("not_mapped").value_counts()
    review = snapshot[snapshot["mapping_status"].isin(["ambiguous", "price_mismatch"])]
    details = store.query_df(
        """
        SELECT cg_id, symbol, binance_symbol, mapping_status, binance_price_dev_log
        FROM coins WHERE mapping_status IN ('ambiguous', 'price_mismatch')
        ORDER BY mapping_status, symbol
        """
    )
    summary = ", ".join(f"{status}: {count}" for status, count in counts.items())
    return CheckResult(
        "Сопоставление: ambiguous / price_mismatch",
        len(review),
        [f"{r.cg_id} ({r.mapping_status})" for r in details.head(n).itertuples()],
        f"при необходимости задать mapping.overrides; всего — {summary}",
        details,
    )


def age_status(config: AppConfig, store: Store, n: int) -> CheckResult:
    results = _latest_screen(store)
    if results.empty:
        return _no_data("Возраст")
    unknown = results[results["age_status"] == AgeStatus.UNKNOWN.value]
    started = _scalar_time(
        store, "SELECT started_at FROM runs WHERE run_id = ?", str(results["run_id"].iloc[0])
    )
    cutoff = started - pd.Timedelta(days=config.universe.min_age_days)
    proven_by_candle = results[results["age_status"] == AgeStatus.BINANCE_FIRST_CANDLE.value]
    wrong = proven_by_candle[
        pd.to_datetime(proven_by_candle["first_candle_ts"]) > cutoff
    ]  # must be empty: first candle has to be old enough
    details = unknown[["cg_id", "symbol", "binance_symbol", "market_cap_usd"]]
    action = "unknown — нет доказательств возраста (не обязательно молодая монета)"
    if not wrong.empty:
        action += f"; ОШИБКА: {len(wrong)} монет с binance_first_candle моложе порога"
    return CheckResult(
        "Возраст не доказан (unknown)",
        len(details),
        details["cg_id"].head(n).tolist(),
        action,
        details,
    )


def exclusions(store: Store, n: int) -> CheckResult:
    snapshot = store.latest_snapshot()
    if snapshot.empty:
        return _no_data("Исключения")
    excluded = snapshot[snapshot["excluded"].fillna(False).astype(bool)]
    reasons = excluded["exclusion_reason"].str.split(";").str[0].value_counts()
    kept = snapshot[~snapshot["excluded"].fillna(False).astype(bool)].head(20)
    return CheckResult(
        "Исключено монет",
        len(excluded),
        [f"{reason}: {count}" for reason, count in reasons.head(n).items()],
        "проверить глазами топ-20 оставшихся (ниже) на пропущенные стейблкоины/wrapped/RWA: "
        + ", ".join(kept["symbol"].astype(str)),
        excluded[["cg_id", "symbol", "exclusion_reason"]],
    )


def render_markdown(results: list[CheckResult], generated_at: datetime) -> str:
    lines = [
        f"# Data quality {generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        "| Проверка | Затронуто | Примеры | Что делать |",
        "|---|---|---|---|",
    ]
    for r in results:
        examples = ", ".join(r.examples).replace("|", "\\|") or "—"
        lines.append(f"| {r.name} | {r.affected} | {examples} | {r.action} |")
    lines += ["", "Проверки только читают данные; ничего не удалено.", ""]
    return "\n".join(lines)


def write_markdown(results: list[CheckResult], generated_at: datetime, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"dq-{generated_at:%Y%m%dT%H%M%SZ}.md"
    path.write_text(render_markdown(results, generated_at), encoding="utf-8")
    return path


def _latest_screen(store: Store) -> pd.DataFrame:
    return store.query_df(
        "SELECT * FROM screen_results WHERE run_id = (SELECT max(run_id) FROM screen_results)"
    )


def _snapshot_time(store: Store, snapshot: pd.DataFrame) -> datetime:
    return _scalar_time(
        store,
        "SELECT max(snapshot_ts) FROM market_snapshots WHERE run_id = ?",
        str(snapshot["snapshot_run_id"].iloc[0]),
    ).to_pydatetime()


def _scalar_time(store: Store, sql: str, param: str) -> pd.Timestamp:
    """Single naive-UTC TIMESTAMP value from a query."""
    row = store.con.execute(sql, [param]).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"no value for {param!r}")
    return pd.Timestamp(row[0])


def _examples(frame: pd.DataFrame, n: int, key: str, value: str, unit: str) -> list[str]:
    out = []
    for record in frame.head(n).to_dict("records"):
        number = record[value]
        shown = f"{number:.3f}" if isinstance(number, float) else str(number)
        out.append(f"{record[key]} ({shown}{unit})")
    return out


def _no_data(name: str) -> CheckResult:
    return CheckResult(name, 0, [], "нет данных: сначала fetch и screen")
