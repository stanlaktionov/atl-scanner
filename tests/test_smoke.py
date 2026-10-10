"""Smoke test: every module of the skeleton imports."""

import importlib
import importlib.util
import pkgutil

import atl_scanner


def test_all_modules_import() -> None:
    has_ui = importlib.util.find_spec("streamlit") is not None
    for module in pkgutil.walk_packages(atl_scanner.__path__, prefix="atl_scanner."):
        if module.name == "atl_scanner.app" and not has_ui:
            continue  # optional `ui` extra
        importlib.import_module(module.name)
