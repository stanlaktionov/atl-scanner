"""Write CSV/Markdown reports for a run to report.output_dir.

Every report includes the attribution "Data provided by CoinGecko". The CSV carries it as a
leading '#' comment line (read it back with pandas.read_csv(path, comment="#")).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from atl_scanner.config import AppConfig, config_hash
from atl_scanner.screen import SCREEN_COLUMNS

ATTRIBUTION = "Data provided by CoinGecko (https://www.coingecko.com)"
DISCLAIMER = (
    "Выборка для исследования гипотезы STABLE → PUMP → DUMP → STABLE → PUMP → FINAL DUMP. "
    "Гипотеза не проверена; это не торговые сигналы."
)

MD_COLUMNS: list[tuple[str, str]] = [
    ("symbol", "Монета"),
    ("cg_id", "CoinGecko id"),
    ("binance_symbol", "Пара Binance"),
    ("market_cap_usd", "Market cap, $M"),
    ("age_status", "Возраст"),
    ("cg_dist_pct", "CG dist %"),
    ("cg_dist_log", "CG dist log"),
    ("bn_dist_log", "BN dist log"),
    ("bn_dist_atr", "BN dist ATR"),
    ("bn_is_new_low_in_window", "BN новый минимум"),
    ("atl_divergence_log", "Расхождение ATL"),
]
LEGEND = [
    "**CG** — ATL CoinGecko за всю историю по всем биржам; **BN** — минимум 1h-свечей Binance "
    "за последний год.",
    "**dist %** = (цена − ATL) / ATL; **dist log** = ln(цена) − ln(ATL); "
    "**dist ATR** = (цена − ATL) / ATR(N) на 1h.",
    "**Новый минимум** — ATL Binance установлен за последние N дней (N из конфига).",
    "**Расхождение ATL** = ln(ATL Binance) − ln(ATL CoinGecko): ≥ 0 ожидаемо, "
    "< 0 — проверить данные.",
    "**Возраст**: `binance_first_candle` / `coingecko_atl_or_ath_date` — доказан, "
    "`unknown` — доказательств нет.",
]


@dataclass(frozen=True)
class ReportContext:
    run_id: str
    snapshot_run_id: str
    generated_at: datetime
    config: AppConfig


def sort_results(results: pd.DataFrame, column: str) -> pd.DataFrame:
    """Passing coins first, then ascending by `column`, missing values last."""
    if column not in SCREEN_COLUMNS:
        raise ValueError(f"report.sort_by {column!r} is not a screen_results column")
    return results.sort_values(
        ["passes_all", column], ascending=[False, True], na_position="last", kind="stable"
    ).reset_index(drop=True)


def write_reports(results: pd.DataFrame, ctx: ReportContext) -> list[Path]:
    """Write the configured formats; return the written paths."""
    cfg = ctx.config.report
    ordered = sort_results(results, cfg.sort_by)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    if "csv" in cfg.formats:
        paths.append(write_csv(ordered, cfg.output_dir / f"{ctx.run_id}.csv"))
    if "md" in cfg.formats:
        path = cfg.output_dir / f"{ctx.run_id}.md"
        path.write_text(render_markdown(ordered, ctx), encoding="utf-8")
        paths.append(path)
    return paths


def write_csv(results: pd.DataFrame, path: Path) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# {ATTRIBUTION}\n")
        results[SCREEN_COLUMNS].to_csv(fh, index=False)
    return path


def render_markdown(results: pd.DataFrame, ctx: ReportContext) -> str:
    cfg = ctx.config
    near = cfg.screen.near_atl
    passing = results[results["passes_all"]]
    top = passing.head(cfg.report.md_top_n)
    limit = "не задан (только сортировка)" if near.max is None else f"≤ {near.max}"
    lines = [
        f"# ATL scan {ctx.run_id}",
        "",
        f"> {DISCLAIMER}",
        "",
        "## Параметры",
        "",
        f"- Сформирован: {ctx.generated_at:%Y-%m-%d %H:%M} UTC",
        f"- Данные: прогон загрузки `{ctx.snapshot_run_id}`",
        f"- Конфиг: hash `{config_hash(cfg)}`",
        f"- Market cap ≥ ${cfg.universe.min_market_cap_usd:,.0f}",
        f"- Возраст ≥ {cfg.universe.min_age_days} дн. (обязателен: {_yes(cfg.screen.require_age)})",
        f"- Около ATL: `{near.metric}` по `{near.atl_source}`, порог {limit}",
        f"- Пара Binance обязательна: {_yes(cfg.screen.require_binance_pair)}",
        f"- ATR({cfg.metrics.atr_period}) на {cfg.metrics.atr_timeframe}; "
        f"ATL Binance по `{cfg.metrics.binance_atl_price}`; "
        f"окно нового минимума {cfg.metrics.new_low_window_days} дн.",
        "",
        "## Воронка",
        "",
        *_table(["Этап", "Монет"], _funnel(results)),
        "",
        f"## Топ {len(top)} из {len(passing)} прошедших, по `{cfg.report.sort_by}`",
        "",
    ]
    if top.empty:
        lines.append("Ни одна монета не прошла фильтры.")
    else:
        lines.extend(_table([title for _, title in MD_COLUMNS], _top_rows(top)))
    lines += ["", "## Обозначения", "", *[f"- {item}" for item in LEGEND]]
    lines += ["", "---", "", ATTRIBUTION, ""]
    return "\n".join(lines)


def _funnel(results: pd.DataFrame) -> list[list[str]]:
    mapping = results["mapping_status"].value_counts()
    rows = [
        ("Всего монет в снимке", len(results)),
        ("Market cap выше порога", int(results["passes_mcap"].sum())),
        ("Не исключены (стейблкоины, wrapped, RWA…)", int((~results["excluded"]).sum())),
        ("Возраст доказан", int(results["passes_age"].sum())),
        ("Есть пара на Binance", int(results["binance_symbol"].notna().sum())),
        ("Прошли все фильтры", int(results["passes_all"].sum())),
    ]
    out = [[name, str(count)] for name, count in rows]
    out += [[f"mapping: {status}", str(count)] for status, count in mapping.items()]
    return out


def _top_rows(top: pd.DataFrame) -> list[list[str]]:
    rows = []
    for record in top.to_dict("records"):
        rows.append([_cell(column, record.get(column)) for column, _ in MD_COLUMNS])
    return rows


def _cell(column: str, value: object) -> str:
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if column == "market_cap_usd":
        return f"{float(value) / 1e6:,.1f}"  # type: ignore[arg-type]
    if column == "cg_dist_pct":
        return f"{float(value) * 100:.1f}%"  # type: ignore[arg-type]
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value).replace("|", "\\|")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _yes(flag: bool) -> str:
    return "да" if flag else "нет"
