"""PERKLOCKS MAIN 39 · P0.1-P0.3 — bounded regression tests.

Tests T1 (lite H2H = 0), T2 (concurrent healer = 1), T4/T5 (cache
gate), and T10 (in-flight dedupe semantics).  Full-load matrix is
run separately by the live perf script; these guarantee the SHAPE
of the fix survives future edits.
"""
from __future__ import annotations

import asyncio
import time

import pytest  # noqa: F401


def test_t1_lite_path_skips_build_h2h_bundle():
    """Grep the current picks_routes source and prove the lite branch
    sets ``build_h2h_bundle_gate = None`` — the request-time H2H
    fan-out must be dead-coded on the lite path.
    """
    import inspect
    from routes import picks_routes as _pr
    src = inspect.getsource(_pr)
    assert "if lite and canonical:" in src, \
        "PERKLOCKS MAIN 39 P0.3 lite guard missing"
    assert "build_h2h_bundle_gate = None" in src
    assert "build_h2h_bundle_gate is not None" in src
    # The full path still exists for /api/picks/{id}/h2h — sanity.
    assert "build_h2h_bundle" in src


def test_t2_concurrent_calls_schedule_at_most_one_healer():
    """50 concurrent ``_ensure_today_picks`` calls must schedule
    exactly ONE healer sweep — the in-flight + cooldown guard.
    """
    async def _run():
        import server as _s
        # Reset guard state
        _s._HEALER_IN_FLIGHT = False
        _s._HEALER_LAST_RUN  = 0.0
        _s._ENSURE_HEALTH_STATE = "HEALTHY"
        _s._ENSURE_HEALTH_TS    = time.time()   # cache fresh → skip body

        scheduled = {"n": 0}
        real_heal = None
        try:
            from services import publication_reconciliation as _pr
            real_heal = _pr.heal_rejected_publications
            async def _fake_heal(*a, **kw):
                scheduled["n"] += 1
                await asyncio.sleep(0.5)          # simulate work
                return {"ok": True, "scanned": 0, "healed": 0}
            _pr.heal_rejected_publications = _fake_heal
            # 50 concurrent calls
            await asyncio.gather(*[
                _s._ensure_today_picks() for _ in range(50)
            ])
            # Allow scheduled tasks to run
            await asyncio.sleep(1.0)
        finally:
            if real_heal is not None:
                from services import publication_reconciliation as _pr
                _pr.heal_rejected_publications = real_heal
        assert scheduled["n"] == 1, (
            f"expected exactly 1 healer sweep, got {scheduled['n']}"
        )
    asyncio.run(_run())


def test_t4_fresh_cache_skips_full_health_sweep():
    """When ``_ENSURE_HEALTH_STATE == 'HEALTHY'`` and
    ``_ENSURE_HEALTH_TS`` is within TTL, the full body must NOT run
    (no count_documents / no MongoDB traffic beyond the healer path).
    """
    async def _run():
        import server as _s
        _s._ENSURE_HEALTH_STATE = "HEALTHY"
        _s._ENSURE_HEALTH_TS    = time.time()
        _s._HEALER_IN_FLIGHT    = True     # suppress healer path too
        # Wrap db to count operations.
        orig_db = _s.db
        class _CountingDB:
            def __init__(self, real): self._r = real; self.calls = 0
            def __getattr__(self, k):
                self.calls += 1
                return getattr(self._r, k)
        cdb = _CountingDB(orig_db)
        _s.db = cdb  # type: ignore
        try:
            await _s._ensure_today_picks()
        finally:
            _s.db = orig_db
            _s._HEALER_IN_FLIGHT = False
        assert cdb.calls == 0, (
            f"fresh cache must skip the health sweep entirely, "
            f"but db was accessed {cdb.calls} times"
        )
    asyncio.run(_run())


def test_t5_stale_cache_triggers_health_sweep():
    """When the cache is stale (TS older than TTL), the function
    must fall through to the original body — this proves we didn't
    accidentally short-circuit the ONLY path that can detect a
    starved slate.
    """
    async def _run():
        import server as _s
        _s._ENSURE_HEALTH_STATE = "HEALTHY"
        _s._ENSURE_HEALTH_TS    = time.time() - 999.0     # stale
        _s._HEALER_IN_FLIGHT    = True
        # Function should run to completion (may take 1-3s locally).
        await asyncio.wait_for(_s._ensure_today_picks(), timeout=15.0)
        # After the body, cache should be re-stamped HEALTHY.
        assert _s._ENSURE_HEALTH_STATE == "HEALTHY"
        assert (time.time() - _s._ENSURE_HEALTH_TS) < 15.0
    asyncio.run(_run())
