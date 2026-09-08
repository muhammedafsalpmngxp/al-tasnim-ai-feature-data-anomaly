"""Shared fixtures. Nothing here touches the live database — those tests are marked
`live` and skipped unless AlTasnimBI is reachable, so `pytest` alone stays fast and safe
to run anywhere, including CI with no network access to the source.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: requires a reachable AlTasnimBI connection")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("DQ_RUN_LIVE_TESTS"):
        return
    skip_live = pytest.mark.skip(reason="set DQ_RUN_LIVE_TESTS=1 to run tests against AlTasnimBI")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture()
def tmp_store_path(tmp_path: Path) -> Path:
    return tmp_path / "sentinel_test.db"
