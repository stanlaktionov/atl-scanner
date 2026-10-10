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
from atl_scanner.models import AgeStatus, AtlSource
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


# --- single coin ----------------------------------------------------------------

COIN_METRIC_ROWS: list[tuple[str, str]] = [
    ("atl_price", "ATL"),
    ("atl_ts", "дата ATL"),
    ("price", "цена"),
    ("price_ts", "цена на"),
    ("dist_pct", "dist %"),
    ("dist_log", "dist log"),
    ("dist_atr", "dist ATR"),
    ("is_new_low_in_window", "новый минимум за N дн."),
    ("days_since_atl", "дней с ATL"),
]


def render_coin(
    snapshot: dict[str, object],
    metrics: dict[str, dict[str, object]],
    screen: dict[str, object],
    candle_info: tuple[int, datetime | None, datetime | None],
    config: AppConfig,
) -> list[str]:
    """Plain-text card for one coin (CLI output)."""
    count, first, last = candle_info
    near = config.screen.near_atl
    bn_title = f"Binance (1 год, {config.metrics.binance_atl_price})"
    lines = [
        f"{str(snapshot['symbol']).upper()} — {snapshot['name']} ({snapshot['cg_id']})",
        f"  снимок данных:  {snapshot['snapshot_run_id']}, цена CoinGecko от "
        f"{_when(snapshot['cg_last_updated'])}",
        f"  market cap:     ${_num(snapshot['market_cap_usd'], 'cap')}"
        f" (rank {_text(snapshot['market_cap_rank'])})",
        f"  исключена:      {_excluded(snapshot)}",
        f"  Binance:        {_pair(snapshot)}",
        f"  свечи 1h:       {count}" + (f", {_when(first)} → {_when(last)}" if count else ""),
        f"  возраст:        {_age(screen['age_status'])}",
        "",
        f"  {'':24}{'CoinGecko (вся история)':>26}{bn_title:>28}",
    ]
    cg = metrics.get(AtlSource.COINGECKO_ALL_TIME.value, {})
    bn = metrics.get(AtlSource.BINANCE_1Y.value, {})
    window = config.metrics.new_low_window_days
    for key, title in COIN_METRIC_ROWS:
        label = title.replace("N", str(window))
        lines.append(f"  {label:<24}{_metric(key, cg.get(key)):>26}{_metric(key, bn.get(key)):>28}")
    atr = cg.get("atr_value")
    lines += [
        "",
        f"  ATR({config.metrics.atr_period}) {config.metrics.atr_timeframe}: "
        f"{_metric('atl_price', atr)} (за токен)",
        f"  расхождение ATL: {_metric('dist_log', screen.get('atl_divergence_log'))}"
        " (ln ATL Binance − ln ATL CoinGecko)",
        "",
        "  фильтры текущего конфига:",
        f"    market cap ≥ ${config.universe.min_market_cap_usd:,.0f}: "
        f"{_flag(screen['passes_mcap'])}",
        f"    возраст ≥ {config.universe.min_age_days} дн.: {_flag(screen['passes_age'])}"
        + ("" if config.screen.require_age else " (не обязателен)"),
        f"    около ATL ({near.metric} по {near.atl_source}, порог "
        f"{'не задан' if near.max is None else near.max}): {_flag(screen['passes_near_atl'])}",
        f"    пара Binance: {_flag(isinstance(snapshot['binance_symbol'], str))}"
        + ("" if config.screen.require_binance_pair else " (не обязательна)"),
        f"    → прошла все: {_flag(screen['passes_all'])}",
        "",
        ATTRIBUTION,
    ]
    return lines


def _missing(value: object) -> bool:
    return (
        value is None
        or value is pd.NA
        or value is pd.NaT
        or (isinstance(value, float) and math.isnan(value))
    )


def _text(value: object) -> str:
    return "—" if _missing(value) else str(value)


def _num(value: object, kind: str) -> str:
    if _missing(value):
        return "—"
    number = float(value)  # type: ignore[arg-type]
    if kind == "cap":
        return f"{number / 1e6:,.1f}M"
    return f"{number:.6g}"


def _when(value: object) -> str:
    if _missing(value):
        return "—"
    return f"{pd.Timestamp(value):%Y-%m-%d %H:%M} UTC"  # type: ignore[arg-type]


def _metric(key: str, value: object) -> str:
    if _missing(value):
        return "—"
    if key in ("atl_ts", "price_ts"):
        return _when(value)
    if key == "is_new_low_in_window":
        return _flag(value)
    if key == "dist_pct":
        return f"{float(value) * 100:.2f}%"  # type: ignore[arg-type]
    if key == "days_since_atl":
        return f"{float(value):.1f}"  # type: ignore[arg-type]
    if key in ("dist_log", "dist_atr"):
        return f"{float(value):.3f}"  # type: ignore[arg-type]
    return _num(value, "price")


def _flag(value: object) -> str:
    if _missing(value):
        return "— (неизвестно / не задано)"
    return "да" if bool(value) else "нет"


def _excluded(snapshot: dict[str, object]) -> str:
    reason = snapshot.get("exclusion_reason")
    if bool(snapshot.get("excluded")):
        return f"да ({reason})"
    return "нет" if _missing(reason) else f"нет (оставлена через keep_ids; {reason})"


def _pair(snapshot: dict[str, object]) -> str:
    status = _text(snapshot.get("mapping_status"))
    symbol = snapshot.get("binance_symbol")
    if not isinstance(symbol, str):
        return f"нет пары ({status})"
    multiplier = snapshot.get("binance_price_multiplier")
    mult = "" if _missing(multiplier) or int(multiplier) == 1 else f", ×{int(multiplier)}"  # type: ignore[call-overload]
    dev = _metric("dist_log", snapshot.get("binance_price_dev_log"))
    return f"{symbol} ({status}{mult}, отклонение цены от 24h-диапазона CoinGecko {dev})"


def _age(status: object) -> str:
    if status == AgeStatus.BINANCE_FIRST_CANDLE.value:
        return "доказан первой свечой Binance"
    if status == AgeStatus.COINGECKO_ATL_OR_ATH_DATE.value:
        return "доказан датой ATL/ATH CoinGecko"
    return "unknown — доказательств нет"
