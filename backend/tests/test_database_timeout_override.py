"""test_database_timeout_override — Phase-5-R3 regression.

The Emergent-managed Production ``MONGO_URL`` embeds
``timeoutMS=10000`` in its connection-string querystring, which
otherwise overrides any application-side CSOT.  PyMongo / Motor
honour an explicit top-level ``timeoutMS`` kwarg over the URL value,
so :mod:`services.database` now ALWAYS passes a top-level ``timeoutMS``
derived from the ``MONGO_SOCKET_TIMEOUT_MS`` env var.

This test proves:
  1. :func:`_resolve_pool_kwargs` reads the env var and populates
     ``timeoutMS`` with the right integer.
  2. :func:`initialize_database` passes ``timeoutMS=300000`` as a
     TOP-LEVEL kwarg to :class:`AsyncIOMotorClient`, which is what
     actually overrides a URL-embedded ``timeoutMS=10000``.
  3. If the operator removes the env var, the default is still
     explicit (never silently inherits the URL's 10 000).

No real Mongo connection is made; the client constructor is spied
via monkeypatch.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _fresh_database_module(monkeypatch):
    """Reload services.database with a clean module-level state so
    this test cannot be polluted by (or pollute) any previously-
    imported client.  Also prevents us from touching the real
    production singleton."""
    # Ensure the env is predictable per-test; individual tests set
    # MONGO_SOCKET_TIMEOUT_MS as they need.
    monkeypatch.delenv("MONGO_SOCKET_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("MONGO_URL", "mongodb://localhost:27017/?timeoutMS=10000")
    monkeypatch.setenv("DB_NAME", "test_override_db")

    # Reload the module so _state is a fresh instance.
    if "services.database" in sys.modules:
        del sys.modules["services.database"]
    mod = importlib.import_module("services.database")
    yield mod
    # Clean-up: drop the state so test-order independence is preserved.
    if "services.database" in sys.modules:
        del sys.modules["services.database"]


def test_resolve_pool_kwargs_pulls_timeout_from_env(_fresh_database_module, monkeypatch):
    """Env override must propagate to the top-level ``timeoutMS`` key."""
    mod = _fresh_database_module
    monkeypatch.setenv("MONGO_SOCKET_TIMEOUT_MS", "300000")
    kw = mod._resolve_pool_kwargs()
    assert kw["timeoutMS"] == 300_000, (
        f"MONGO_SOCKET_TIMEOUT_MS=300000 must map to top-level "
        f"timeoutMS=300000; got {kw.get('timeoutMS')!r}"
    )
    # Same env var ALSO drives socketTimeoutMS (contract unchanged).
    assert kw["socketTimeoutMS"] == 300_000


def test_resolve_pool_kwargs_default_is_explicit(_fresh_database_module):
    """Even with the env var unset, we MUST pass an explicit top-level
    ``timeoutMS`` so a URL-embedded ``timeoutMS=10000`` cannot leak in."""
    mod = _fresh_database_module
    kw = mod._resolve_pool_kwargs()
    assert "timeoutMS" in kw, "timeoutMS must always be present in pool kwargs"
    assert isinstance(kw["timeoutMS"], int)
    assert kw["timeoutMS"] >= 60_000, (
        "default must be at least 60s so the fix is useful even if the "
        "operator forgets to set MONGO_SOCKET_TIMEOUT_MS"
    )
    # Hard-forbid the URL value from leaking back in as a default.
    assert kw["timeoutMS"] != 10_000


def test_initialize_database_passes_timeoutMS_top_level(_fresh_database_module, monkeypatch):
    """End-to-end: initialize_database() constructs AsyncIOMotorClient
    with timeoutMS=300000 at the TOP LEVEL (not inside the URL)."""
    mod = _fresh_database_module
    monkeypatch.setenv("MONGO_SOCKET_TIMEOUT_MS", "300000")

    captured: dict = {}

    class _SpyClient:
        def __init__(self, url, **kwargs):
            captured["url"]    = url
            captured["kwargs"] = kwargs

        def __getitem__(self, name):
            class _DB:
                name = "test_override_db"
            return _DB()

        def close(self):
            pass

    # Replace the AsyncIOMotorClient symbol used inside the module.
    monkeypatch.setattr(mod, "AsyncIOMotorClient", _SpyClient)
    mod.initialize_database()

    kw = captured["kwargs"]
    assert "timeoutMS" in kw, (
        "AsyncIOMotorClient must receive timeoutMS as a top-level kwarg "
        "— that's the ONLY form that overrides a URL-querystring value."
    )
    assert kw["timeoutMS"] == 300_000, (
        f"timeoutMS must be 300000; got {kw['timeoutMS']!r}.  This is "
        "the Phase-5-R3 production fix for the shared-cluster "
        "NetworkTimeout bug."
    )
    # The URL is passed through unchanged — no rewriting / stripping.
    assert captured["url"] == "mongodb://localhost:27017/?timeoutMS=10000", (
        "the fix must NOT touch MONGO_URL — only add an explicit kwarg"
    )
    # Co-contract: socketTimeoutMS also drives from the same env var.
    assert kw.get("socketTimeoutMS") == 300_000
