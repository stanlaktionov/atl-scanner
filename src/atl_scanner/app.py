"""Streamlit UI: run scans, browse screen results, inspect one coin, check data quality.

Launch with `atl-scanner ui` (or `streamlit run src/atl_scanner/app.py -- --config config.yaml`).
Local only. It reads and writes the same DuckDB file as the CLI and never edits config.yaml:
thresholds changed on the page apply to the page and to reports saved from it.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from atl_scanner.config import (
    AppConfig,
    ConfigError,
    coingecko_api_key,
    load_config,
    load_env,
)
from atl_scanner.models import AtlSource, DistMetric
from atl_scanner.pipeline import (
    CANDLE_EXCHANGE,
    NoMarketDataError,
    coin_details,
    run_scan,
    run_screen,
    utc_now,
    write_screen_reports,
)
from atl_scanner.quality import run_checks
from atl_scanner.report import ATTRIBUTION, DISCLAIMER
from atl_scanner.screen import apply_screen, near_atl_column
from atl_scanner.sources.binance import build_binance_client
from atl_scanner.sources.coingecko import UnknownCategoryError, build_coingecko_client
from atl_scanner.sources.http import ApiError, CallBudgetExceeded
from atl_scanner.store.db import Store

FLAG_COLUMNS = ["passes_mcap", "passes_age", "passes_near_atl", "passes_all"]
TABLE_COLUMNS = [
    "symbol",
    "cg_id",
    "binance_symbol",
    "mapping_status",
    "market_cap_usd",
    "age_status",
    "cg_dist_pct",
    "cg_dist_log",
    "cg_dist_atr",
    "bn_dist_pct",
    "bn_dist_log",
    "bn_dist_atr",
    "bn_is_new_low_in_window",
    "atl_divergence_log",
    "excluded",
    "exclusion_reason",
    "passes_all",
]
COLUMN_CONFIG = {
    "symbol": st.column_config.TextColumn("Монета"),
    "cg_id": st.column_config.TextColumn("CoinGecko id"),
    "binance_symbol": st.column_config.TextColumn("Пара Binance"),
    "mapping_status": st.column_config.TextColumn("Сопоставление"),
    "market_cap_usd": st.column_config.NumberColumn("Market cap, $", format="compact"),
    "age_status": st.column_config.TextColumn("Возраст"),
    "cg_dist_pct": st.column_config.NumberColumn("CG dist", format="percent"),
    "cg_dist_log": st.column_config.NumberColumn("CG dist log", format="%.3f"),
    "cg_dist_atr": st.column_config.NumberColumn("CG dist ATR", format="%.1f"),
    "bn_dist_pct": st.column_config.NumberColumn("BN dist", format="percent"),
    "bn_dist_log": st.column_config.NumberColumn("BN dist log", format="%.3f"),
    "bn_dist_atr": st.column_config.NumberColumn("BN dist ATR", format="%.1f"),
    "bn_is_new_low_in_window": st.column_config.CheckboxColumn("BN новый минимум"),
    "atl_divergence_log": st.column_config.NumberColumn("Расхождение ATL", format="%.3f"),
    "excluded": st.column_config.CheckboxColumn("Исключена"),
    "exclusion_reason": st.column_config.TextColumn("Причина исключения"),
    "passes_all": st.column_config.CheckboxColumn("Прошла"),
}


# --- setup -------------------------------------------------------------------


def config_path_from_argv() -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=os.environ.get("ATL_SCANNER_CONFIG", "config.yaml"))
    args, _ = parser.parse_known_args(sys.argv[1:])
    return Path(args.config)


def load_app_config(path: Path) -> AppConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        st.error(str(exc))
        st.stop()
        raise  # unreachable: st.stop() ends the script run


def db_ready(config: AppConfig) -> bool:
    return config.storage.db_path.exists()


# --- data access (short-lived connections, so CLI and UI can take turns) --------


def latest_screen(config: AppConfig) -> tuple[str | None, pd.DataFrame]:
    with Store(config.storage.db_path, read_only=True) as store:
        frame = store.query_df(
            "SELECT * FROM screen_results WHERE run_id = (SELECT max(run_id) FROM screen_results)"
        )
    run_id = str(frame["run_id"].iloc[0]) if not frame.empty else None
    return run_id, frame


def snapshot_coins(config: AppConfig) -> pd.DataFrame:
    with Store(config.storage.db_path, read_only=True) as store:
        return store.latest_snapshot()


def recent_runs(config: AppConfig, limit: int = 30) -> pd.DataFrame:
    with Store(config.storage.db_path, read_only=True) as store:
        return store.query_df(
            "SELECT run_id, status, snapshot_run_id, cg_calls, binance_weight, started_at, "
            "finished_at - started_at AS duration FROM runs ORDER BY run_id DESC LIMIT ?",
            [limit],
        )


# --- sidebar actions -------------------------------------------------------------


def sidebar(config: AppConfig, path: Path) -> None:
    st.sidebar.title("ATL Scanner")
    st.sidebar.caption(DISCLAIMER)
    st.sidebar.caption(f"Конфиг: `{path}`")
    if st.sidebar.button("Полный скан (scan)", type="primary", width="stretch"):
        action_scan(config)
    st.sidebar.caption("CoinGecko + Binance, затем отбор и отчёт. Обычно ~1 мин.")
    if st.sidebar.button("Пересчитать отбор (screen)", width="stretch"):
        action_screen(config)
    st.sidebar.caption("Без сети, по загруженным данным и порогам из config.yaml.")
    st.sidebar.divider()
    st.sidebar.caption(ATTRIBUTION)


def action_scan(config: AppConfig) -> None:
    with st.sidebar.status("Скан…", expanded=True) as status:
        bar = st.progress(0.0, text="CoinGecko и сопоставление…")

        def progress(done: int, total: int, symbol: str) -> None:
            bar.progress(done / max(total, 1), text=f"свечи {done}/{total} ({symbol})")

        try:
            with ExitStack() as stack:
                cg = stack.enter_context(build_coingecko_client(config, coingecko_api_key(config)))
                bn = stack.enter_context(build_binance_client(config))
                store = stack.enter_context(Store(config.storage.db_path))
                summary = run_scan(config, store, cg, bn, progress=progress)
        except (ConfigError, ApiError, CallBudgetExceeded, UnknownCategoryError) as exc:
            status.update(label="Скан не удался", state="error")
            st.error(str(exc))
            return
        passed = int(summary.screen.results["passes_all"].sum())
        status.update(label=f"Готово: {passed} монет прошли отбор", state="complete")


def action_screen(config: AppConfig) -> None:
    with st.sidebar.status("Отбор…") as status:
        try:
            with Store(config.storage.db_path) as store:
                summary = run_screen(config, store)
        except NoMarketDataError as exc:
            status.update(label="Нет данных", state="error")
            st.error(str(exc))
            return
        write_screen_reports(config, summary, utc_now())
        status.update(label=f"Готово: прогон {summary.run_id}", state="complete")


# --- tab: screen -------------------------------------------------------------------


def tab_screen(config: AppConfig) -> None:
    run_id, results = latest_screen(config)
    if run_id is None:
        st.info("Отбора ещё не было: нажмите «Полный скан» или «Пересчитать отбор».")
        return
    page_config = screen_controls(config)
    flagged = apply_screen(results.drop(columns=FLAG_COLUMNS), page_config)
    funnel(flagged)
    shown = filter_table(flagged)
    column = near_atl_column(page_config)
    shown = shown.sort_values(["passes_all", column], ascending=[False, True], na_position="last")
    st.caption(f"Прогон отбора `{run_id}` · сортировка по `{column}` · {len(shown)} строк")
    st.dataframe(
        shown[TABLE_COLUMNS],
        column_config=COLUMN_CONFIG,
        hide_index=True,
        width="stretch",
        height=560,
    )
    if st.button("Сохранить эти настройки как прогон и отчёт"):
        with Store(config.storage.db_path) as store:
            summary = run_screen(page_config, store)
        paths = write_screen_reports(page_config, summary, utc_now())
        st.success("Отчёт: " + ", ".join(str(p) for p in paths))


def screen_controls(config: AppConfig) -> AppConfig:
    near = config.screen.near_atl
    left, mid, right = st.columns(3)
    metrics = [m.value for m in DistMetric]
    sources = [s.value for s in AtlSource]
    metric = left.selectbox("Метрика «около ATL»", metrics, index=metrics.index(near.metric.value))
    source = mid.selectbox("Источник ATL", sources, index=sources.index(near.atl_source.value))
    use_max = right.toggle("Задать порог", value=near.max is not None)
    limit = (
        right.number_input("Максимум", value=float(near.max or 0.2), step=0.05, format="%.3f")
        if use_max
        else None
    )
    a, b = st.columns(2)
    require_pair = a.toggle("Только с парой Binance", value=config.screen.require_binance_pair)
    require_age = b.toggle("Возраст должен быть доказан", value=config.screen.require_age)
    new_near = near.model_copy(
        update={"metric": DistMetric(metric), "atl_source": AtlSource(source), "max": limit}
    )
    screen = config.screen.model_copy(
        update={
            "near_atl": new_near,
            "require_binance_pair": require_pair,
            "require_age": require_age,
        }
    )
    return config.model_copy(update={"screen": screen})


def funnel(frame: pd.DataFrame) -> None:
    cols = st.columns(5)
    cols[0].metric("Всего", len(frame))
    cols[1].metric("Market cap ок", int(frame["passes_mcap"].sum()))
    cols[2].metric("Не исключены", int((~frame["excluded"].astype(bool)).sum()))
    cols[3].metric("С парой Binance", int(frame["binance_symbol"].notna().sum()))
    cols[4].metric("Прошли всё", int(frame["passes_all"].sum()))


def filter_table(frame: pd.DataFrame) -> pd.DataFrame:
    a, b = st.columns([1, 2])
    only_passing = a.toggle("Только прошедшие", value=True)
    search = b.text_input("Поиск (тикер, id, пара)", "").strip().lower()
    out = frame[frame["passes_all"]] if only_passing else frame
    if search:
        hay = (
            out["symbol"].fillna("")
            + " "
            + out["cg_id"].fillna("")
            + " "
            + out["binance_symbol"].fillna("")
        ).str.lower()
        out = out[hay.str.contains(search, regex=False)]
    return out


# --- tab: coin ----------------------------------------------------------------------


def tab_coin(config: AppConfig) -> None:
    coins = snapshot_coins(config)
    if coins.empty:
        st.info("Нет данных: сначала «Полный скан».")
        return
    labels = {
        f"{str(r.symbol).upper()} — {r.name} ({r.cg_id})": str(r.cg_id) for r in coins.itertuples()
    }
    choice = st.selectbox("Монета", list(labels), index=None, placeholder="Начните вводить тикер…")
    if choice is None:
        return
    with Store(config.storage.db_path, read_only=True) as store:
        details = coin_details(config, store, labels[choice], utc_now())
        symbol = details.snapshot["binance_symbol"]
        candles = (
            store.ohlcv(CANDLE_EXCHANGE, symbol) if isinstance(symbol, str) else pd.DataFrame()
        )
    coin_header(details.snapshot, details.screen)
    st.dataframe(metrics_table(details.metrics), width="stretch")
    if candles.empty:
        st.info("Свечей Binance нет (нет пары или ещё не загружены).")
    else:
        multiplier = details.snapshot["binance_price_multiplier"]
        mult = 1 if pd.isna(multiplier) else int(multiplier)
        cg_atl = details.metrics.get(AtlSource.COINGECKO_ALL_TIME.value, {}).get("atl_price")
        bn_atl = details.metrics.get(AtlSource.BINANCE_1Y.value, {}).get("atl_price")
        log_scale = st.toggle("Логарифмическая шкала", value=True)
        st.altair_chart(price_chart(candles, mult, cg_atl, bn_atl, log_scale), width="stretch")
    flags = {k: details.screen[k] for k in FLAG_COLUMNS}
    st.caption(
        "Фильтры текущего конфига: " + ", ".join(f"{k} = {_flag(v)}" for k, v in flags.items())
    )


def coin_header(snapshot: dict[str, object], screen: dict[str, object]) -> None:
    cols = st.columns(4)
    cap = snapshot["market_cap_usd"]
    cap_text = f"${cap / 1e6:,.1f}M" if isinstance(cap, int | float) and cap == cap else "—"
    cols[0].metric("Market cap", cap_text)
    cols[1].metric(
        "Пара Binance", _text(snapshot["binance_symbol"]), _text(snapshot["mapping_status"])
    )
    cols[2].metric("Возраст", _text(screen["age_status"]))
    excluded = "да" if bool(snapshot["excluded"]) else "нет"
    reason = snapshot["exclusion_reason"]
    cols[3].metric("Исключена", excluded, _text(reason) if isinstance(reason, str) else None)


def metrics_table(metrics: dict[str, dict[str, object]]) -> pd.DataFrame:
    rows = ["atl_price", "atl_ts", "price", "price_ts", "dist_pct", "dist_log", "dist_atr",
            "atr_value", "is_new_low_in_window", "days_since_atl"]  # fmt: skip
    names = {
        AtlSource.COINGECKO_ALL_TIME.value: "CoinGecko (вся история)",
        AtlSource.BINANCE_1Y.value: "Binance (1 год)",
    }
    table = {names[src]: [_fmt(m.get(r)) for r in rows] for src, m in metrics.items()}
    return pd.DataFrame(table, index=rows)


def price_chart(
    candles: pd.DataFrame,
    multiplier: int,
    cg_atl: object,
    bn_atl: object,
    log_scale: bool,
) -> alt.LayerChart | alt.FacetChart:
    data = pd.DataFrame(
        {"time": candles["open_time"].dt.tz_localize(None), "close": candles["close"] / multiplier}
    )
    scale = alt.Scale(type="log" if log_scale else "linear", zero=False)
    line = (
        alt.Chart(data)
        .mark_line(strokeWidth=1)
        .encode(
            x=alt.X("time:T", title="UTC"),
            y=alt.Y("close:Q", title="цена за токен, USDT", scale=scale),
            tooltip=["time:T", alt.Tooltip("close:Q", format=".6g")],
        )
    )
    levels = pd.DataFrame(
        [
            {"label": "ATL CoinGecko", "price": cg_atl},
            {"label": "ATL Binance (1 год)", "price": bn_atl},
        ]
    ).dropna()
    rules = (
        alt.Chart(levels)
        .mark_rule(strokeDash=[6, 4])
        .encode(
            y="price:Q",
            color=alt.Color("label:N", title=None),
            tooltip=["label:N", alt.Tooltip("price:Q", format=".6g")],
        )
    )
    return alt.layer(line, rules).properties(height=380)


# --- tab: data quality --------------------------------------------------------------


def tab_quality(config: AppConfig) -> None:
    if not st.button("Запустить проверку"):
        st.caption("Проверки только читают базу: пропуски свечей, нулевой объём, свежесть, "
                   "расхождение ATL, сопоставление, возраст, исключения.")  # fmt: skip
        return
    with Store(config.storage.db_path, read_only=True) as store:
        results = run_checks(config, store, utc_now())
    summary = pd.DataFrame(
        [
            {
                "Проверка": r.name,
                "Затронуто": r.affected,
                "Примеры": ", ".join(r.examples),
                "Что делать": r.action,
            }
            for r in results
        ]  # fmt: skip
    )
    st.dataframe(summary, hide_index=True, width="stretch")
    for r in results:
        if not r.details.empty:
            with st.expander(f"{r.name}: {r.affected}"):
                st.dataframe(r.details, hide_index=True, width="stretch")


# --- tab: runs and reports ------------------------------------------------------------


def tab_runs(config: AppConfig) -> None:
    st.dataframe(recent_runs(config), hide_index=True, width="stretch")
    reports = sorted(config.report.output_dir.glob("*.md"), reverse=True)
    if not reports:
        st.info("Отчётов пока нет.")
        return
    chosen = st.selectbox("Отчёт", reports, format_func=lambda p: p.name)
    if chosen is None:
        return
    csv = chosen.with_suffix(".csv")
    a, b = st.columns(2)
    a.download_button("Скачать .md", chosen.read_bytes(), file_name=chosen.name)
    if csv.exists():
        b.download_button("Скачать .csv", csv.read_bytes(), file_name=csv.name)
    st.markdown(chosen.read_text(encoding="utf-8"))


# --- helpers ----------------------------------------------------------------------------


def _fmt(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if isinstance(value, datetime):
        return f"{value:%Y-%m-%d %H:%M} UTC"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _text(value: object) -> str:
    return value if isinstance(value, str) and value else "—"


def _flag(value: object) -> str:
    if value is None or value is pd.NA:
        return "—"
    return "да" if bool(value) else "нет"


def main() -> None:
    st.set_page_config(page_title="ATL Scanner", page_icon="📉", layout="wide")
    load_env()
    path = config_path_from_argv()
    config = load_app_config(path)
    sidebar(config, path)
    if not db_ready(config):
        st.info("База ещё пустая. Нажмите «Полный скан» слева: первая загрузка свечей ~15 мин.")
        return
    screen_tab, coin_tab, quality_tab, runs_tab = st.tabs(
        ["Отбор", "Монета", "Качество данных", "Прогоны и отчёты"]
    )
    with screen_tab:
        tab_screen(config)
    with coin_tab:
        tab_coin(config)
    with quality_tab:
        tab_quality(config)
    with runs_tab:
        tab_runs(config)
    st.caption(ATTRIBUTION)


if __name__ == "__main__":  # streamlit runs the script as __main__
    main()
