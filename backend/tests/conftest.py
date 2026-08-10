"""Pytest configuration — Phase 3 (2026-08-11).

Deterministic test collection: skip live-smoke tests by default so
regression runs do not depend on live production / preview URLs,
live provider availability, or environment-only variables.

Run `pytest -m live_smoke` explicitly to include them.
"""
from __future__ import annotations

import os
import sys

# Ensure backend is importable regardless of pytest cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_collection_modifyitems(config, items):
    """Skip `live_smoke` tests unless explicitly requested via `-m`."""
    if config.getoption("-m") and "live_smoke" in config.getoption("-m"):
        return
    import pytest as _pt
    skip_live = _pt.mark.skip(reason="live_smoke — run explicitly with `-m live_smoke`")
    for it in items:
        if "live_smoke" in it.keywords:
            it.add_marker(skip_live)
