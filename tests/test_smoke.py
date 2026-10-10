"""Smoke test: every module of the skeleton imports."""

import importlib
import pkgutil

import atl_scanner


def test_all_modules_import() -> None:
    for module in pkgutil.walk_packages(atl_scanner.__path__, prefix="atl_scanner."):
        importlib.import_module(module.name)
