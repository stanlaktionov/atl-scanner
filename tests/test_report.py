"""Tests for report.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from atl_scanner.config import AppConfig, load_config
from atl_scanner.report import (
    ATTRIBUTION,
    ReportContext,
    render_markdown,
    sort_results,
    write_reports,
)
from atl_scanner.screen import SCREEN_COLUMNS
from tests.conftest import EXAMPLE_CONFIG

NOW = datetime(2026, 10, 10, 13, 0, tzinfo=UTC)
NAN = float("nan")


def config(tmp_path: Path, top_n: int = 50, formats: list[str] | None = None) -> AppConfig:
    base = load_config(EXAMPLE_CONFIG)
    report = base.report.model_copy(
        update={
            "output_dir": tmp_path / "reports",
            "md_top_n": top_n,
            "formats": formats or ["csv", "md"],
        }
    )
    return base.model_copy(update={"report": report})


def row(cg_id: str, dist: float, passes: bool = True, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = dict.fromkeys(SCREEN_COLUMNS)
    data.update(
        {
            "run_id": "screen-1",
            "cg_id": cg_id,
            "symbol": cg_id,
            "binance_symbol": f"{cg_id.upper()}USDT",
            "mapping_status": "mapped",
            "market_cap_usd": 5e7,
            "age_status": "binance_first_candle",
            "excluded": False,
            "cg_dist_pct": 0.1,
            "cg_dist_log": dist,
            "bn_is_new_low_in_window": False,
            "passes_mcap": True,
            "passes_age": True,
            "passes_near_atl": pd.NA,
            "passes_all": passes,
        }
    )
    data.update(extra)
    return data


def results(*rows: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=SCREEN_COLUMNS)


def ctx(cfg: AppConfig) -> ReportContext:
    return ReportContext("screen-1", "fetch-1", NOW, cfg)


def test_sort_passing_first_then_ascending_nan_last() -> None:
    frame = results(
        row("fail", 0.0, passes=False),
        row("nan", NAN),
        row("far", 2.0),
        row("near", 0.1),
    )
    assert sort_results(frame, "cg_dist_log")["cg_id"].tolist() == ["near", "far", "nan", "fail"]


def test_sort_by_unknown_column() -> None:
    with pytest.raises(ValueError, match="sort_by"):
        sort_results(results(row("a", 1.0)), "magic")


def test_csv_has_attribution_and_reads_back(tmp_path: Path) -> None:
    cfg = config(tmp_path, formats=["csv"])
    (path,) = write_reports(results(row("b", 0.5), row("a", 0.1)), ctx(cfg))
    assert path == tmp_path / "reports" / "screen-1.csv"
    assert path.read_text(encoding="utf-8").splitlines()[0] == f"# {ATTRIBUTION}"
    back = pd.read_csv(path, comment="#")
    assert list(back.columns) == SCREEN_COLUMNS
    assert back["cg_id"].tolist() == ["a", "b"]


def test_markdown_content(tmp_path: Path) -> None:
    cfg = config(tmp_path, top_n=2)
    frame = results(
        row("a", 0.1),
        row("b", 0.2, binance_symbol=None, mapping_status="no_pair", bn_dist_log=NAN),
        row("c", 0.3),
        row("usdt", 0.0, passes=False, excluded=True),
    )
    text = render_markdown(sort_results(frame, "cg_dist_log"), ctx(cfg))
    assert ATTRIBUTION in text
    assert "не торговые сигналы" in text
    assert "| Прошли все фильтры | 3 |" in text
    assert "| Не исключены (стейблкоины, wrapped, RWA…) | 3 |" in text
    assert "| mapping: no_pair | 1 |" in text
    assert "## Топ 2 из 3 прошедших" in text
    assert "| a | a | AUSDT | 50.0 |" in text
    assert "| b | b | — |" in text  # missing pair shown as a dash
    assert "| c |" not in text  # beyond md_top_n
    assert "fetch-1" in text and "порог не задан" in text


def test_markdown_escapes_pipes(tmp_path: Path) -> None:
    text = render_markdown(results(row("x|y", 0.1)), ctx(config(tmp_path)))
    assert "x\\|y" in text


def test_markdown_when_nothing_passes(tmp_path: Path) -> None:
    text = render_markdown(results(row("a", 0.1, passes=False)), ctx(config(tmp_path)))
    assert "Ни одна монета не прошла фильтры." in text


def test_write_both_formats(tmp_path: Path) -> None:
    paths = write_reports(results(row("a", 0.1)), ctx(config(tmp_path)))
    assert [p.name for p in paths] == ["screen-1.csv", "screen-1.md"]
    assert all(p.exists() for p in paths)
