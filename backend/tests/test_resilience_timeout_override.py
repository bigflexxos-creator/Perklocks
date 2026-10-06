"""Regression tests for the route-specific timeout override introduced
by the 2026-10-06 Emergent-Support canonical-import fix.

Asserts:
  * The exact route POST /api/admin/canonical-import resolves to 300 s.
  * Every other common route resolves to the global 85 s default.
  * Method matching is case-insensitive.
  * Path matching is EXACT (prefix matches do NOT inherit the override).
  * Install log emits the override summary.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

from middleware import resilience


def test_canonical_import_override_is_300s():
    assert resilience._timeout_for("POST", "/api/admin/canonical-import") == 300.0


def test_default_timeout_unchanged_for_other_api_routes():
    # Representative sample of unrelated API routes.
    for method, path in [
        ("GET",  "/api/health"),
        ("POST", "/api/auth/login"),
        ("GET",  "/api/picks/today"),
        ("GET",  "/api/admin/data-authority/status"),
        # Even sibling endpoints under /canonical-import must NOT inherit
        # the write-side override:
        ("GET",  "/api/admin/canonical-import/status"),
        ("GET",  "/api/admin/canonical-import/forensic-audit"),
        # The read status endpoint via POST should also not inherit:
        ("POST", "/api/admin/canonical-import/status"),
    ]:
        assert resilience._timeout_for(method, path) == resilience.REQUEST_TIMEOUT_SECONDS, (
            f"{method} {path} should use default 85 s, "
            f"got {resilience._timeout_for(method, path)}"
        )


def test_method_match_is_case_insensitive():
    assert resilience._timeout_for("post", "/api/admin/canonical-import") == 300.0
    assert resilience._timeout_for("Post", "/api/admin/canonical-import") == 300.0


def test_path_match_is_exact_not_prefix():
    # Trailing slash variants and nested paths must NOT inherit.
    assert resilience._timeout_for("POST", "/api/admin/canonical-import/") \
            == resilience.REQUEST_TIMEOUT_SECONDS
    assert resilience._timeout_for("POST", "/api/admin/canonical-import/anything") \
            == resilience.REQUEST_TIMEOUT_SECONDS


def test_install_logs_override_summary(caplog):
    app = MagicMock()
    caplog.set_level(logging.INFO, logger="lockscore.resilience")
    resilience.install(app)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "default_timeout=85s" in msgs or "default_timeout=85" in msgs
    assert "POST /api/admin/canonical-import=300s" in msgs
    # Belt-and-braces: confirm the middleware was wired exactly once.
    app.add_middleware.assert_called_once()
