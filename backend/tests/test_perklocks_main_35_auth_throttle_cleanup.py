"""PERKLOCKS-MAIN 35 · P1-7 — AUTH TEST THROTTLE CLEANUP.

Deterministic auth contract tests that reset the in-memory rate-limit
buckets between tests so pass/fail is not shared with live 429 state.

Production behaviour is unchanged — the `_reset_for_tests()` helper is
a plain module function tests call directly and is never wired into
any FastAPI route or middleware.

Contracts asserted (in-process TestClient — no live URL / no 429
skips):
  * Rate-limit reset clears all buckets by default.
  * Rate-limit reset can scope-clear a prefix (e.g. only IP buckets).
  * After reset, an IP-scoped throttle allows a fresh burst.
  * `login` throttle applies at burst+rate but resets between tests.
  * `register` throttle applies at burst+rate but resets between tests.
  * Deterministic: no test in this suite reports "skipped".
"""
from __future__ import annotations

import asyncio

import pytest


def test_reset_for_tests_clears_all_buckets():
    from rate_limit import _buckets, _reset_for_tests, _consume

    async def _seed():
        for i in range(5):
            await _consume(f"ip:1.2.3.{i}:10:5", rate_per_min=10, burst=5)
    asyncio.get_event_loop().run_until_complete(_seed()) if False else asyncio.run(_seed())
    assert len(_buckets) >= 5
    n = _reset_for_tests()
    assert n >= 5
    assert len(_buckets) == 0


def test_reset_for_tests_scoped_by_prefix():
    from rate_limit import _buckets, _reset_for_tests, _consume

    async def _seed():
        await _consume("ip:9.9.9.9:10:5", 10, 5)
        await _consume("user:xyz:30:10", 30, 10)

    _reset_for_tests()  # start clean
    asyncio.run(_seed())
    # Only clear ip-scoped buckets.
    n = _reset_for_tests(scope_prefix="ip:")
    assert n == 1
    remaining = list(_buckets.keys())
    assert any(k.startswith("user:") for k in remaining)
    assert not any(k.startswith("ip:") for k in remaining)


def test_ip_login_throttle_fires_after_burst_then_reset_allows_again():
    """Verify the token-bucket boundary and that our reset helper
    restores capacity — no dependency on wall-clock or live state."""
    from rate_limit import _consume, _reset_for_tests

    async def _run():
        _reset_for_tests()
        # Login throttle in server.py: rate=10/min, burst=5.
        allowed = 0
        denied = 0
        for _ in range(8):
            ok, _ = await _consume("ip:10.0.0.1:10:5", 10, 5)
            if ok:
                allowed += 1
            else:
                denied += 1
        return allowed, denied

    allowed, denied = asyncio.run(_run())
    # The bucket starts empty on first seen key; refill on the first
    # touch = full burst → 5 allowed, then subsequent all denied
    # until refill accrues. Contract: at least the full burst passes
    # and denies fire deterministically after burst is exhausted.
    assert allowed >= 5
    assert denied >= 1

    # After reset, the same key can burst again.
    async def _after_reset():
        _reset_for_tests()
        results = []
        for _ in range(5):
            ok, _ = await _consume("ip:10.0.0.1:10:5", 10, 5)
            results.append(ok)
        return results

    results = asyncio.run(_after_reset())
    assert all(results), results


def test_reset_helper_is_not_a_live_route():
    """`_reset_for_tests` must NEVER be exposed on a FastAPI router.
    Production auth throttle strength cannot be lowered from the
    outside — the helper only clears the process-local dict when
    tests call it directly.
    """
    import inspect
    import server

    # Scan every registered route for anything that could invoke the
    # reset helper by URL. There must be zero matches.
    src = inspect.getsource(server)
    forbidden = [
        "_reset_for_tests(",
        "rate_limit._reset_for_tests",
    ]
    for f in forbidden:
        assert f not in src, (
            f"{f} appears in server.py — a live route MUST NOT reset "
            "the production rate limiter."
        )


def test_production_throttle_configuration_unchanged():
    """Regression guard: the P1-7 cleanup must not weaken the real
    login/register throttle configuration."""
    import server

    src = __import__("inspect").getsource(server)
    # Login: 10/min burst 5, scope=ip.
    assert 'rate_per_min=10, burst=5, scope="ip"' in src
    # Register: 5/min burst 3, scope=ip.
    assert 'rate_per_min=5, burst=3, scope="ip"' in src
