"""Headless test of the Streamlit page (streamlit.testing) on a small synthetic store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from atl_scanner.config import load_config  # noqa: E402
from atl_scanner.pipeline import run_screen  # noqa: E402
from atl_scanner.store.db import Store  # noqa: E402
from tests.conftest import EXAMPLE_CONFIG  # noqa: E402
from tests.test_coin import NOW, fill  # noqa: E402

APP = Path(__file__).parents[1] / "src" / "atl_scanner" / "app.py"


@pytest.fixture
def app_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["storage"] = {
        "data_dir": str(tmp_path / "data"),
        "raw_dir": str(tmp_path / "data" / "raw"),
        "db_path": str(tmp_path / "data" / "scanner.duckdb"),
    }
    data["report"]["output_dir"] = str(tmp_path / "reports")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with Store(Path(data["storage"]["db_path"])) as store:
        fill(store)
        run_screen(load_config(path), store, now=lambda: NOW)
    monkeypatch.setenv("ATL_SCANNER_CONFIG", str(path))
    return path


def by_label(elements: Any, label: str) -> Any:
    return next(e for e in elements if e.label == label)


def test_page_renders_tabs_and_funnel(app_config: Path) -> None:
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Отбор", "Монета", "Качество данных", "Прогоны и отчёты"]
    funnel = {m.label: m.value for m in at.metric}
    assert funnel["Всего"] == "3"


def test_threshold_controls_filter_in_memory(app_config: Path) -> None:
    at = AppTest.from_file(str(APP), default_timeout=60).run()

    def passed() -> str:
        return str({m.label: m.value for m in at.metric}["Прошли всё"])

    assert passed() == "1"  # only cosmos has age evidence
    by_label(at.toggle, "Возраст должен быть доказан").set_value(False).run()
    assert passed() == "3"
    by_label(at.toggle, "Только с парой Binance").set_value(True).run()
    assert passed() == "2"  # memetoon has no pair
    assert not at.exception


def test_coin_tab_shows_card_and_chart(app_config: Path) -> None:
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    select = by_label(at.selectbox, "Монета")
    select.set_value(next(o for o in select.options if o.startswith("ATOM "))).run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Пара Binance"] == "ATOMUSDT"
    assert at.get("arrow_vega_lite_chart") or at.get("vega_lite_chart")


def test_quality_tab_runs_checks(app_config: Path) -> None:
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    by_label(at.button, "Запустить проверку").click().run()
    assert not at.exception
    assert len(at.dataframe) >= 2
