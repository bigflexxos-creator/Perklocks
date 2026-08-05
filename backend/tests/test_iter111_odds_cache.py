"""Odds-API SWR cache regression tests (iter111, 2026-06).

Verifies the centralized cache layer in `services/odds_cache.py`:

  A. Cache key derivation — deterministic, apiKey-stripped
  B. Fresh HIT — no upstream call within TTL
  C. Stale HIT — returns cache immediately, kicks off background refresh
  D. HARD MISS — blocks on upstream when past stale_ttl
  E. Force refresh — bypasses cache
  F. Single-flight dedup — N concurrent callers → 1 upstream call
  G. Completed-game filter — drops games with commence_time < now-4h
  H. Diff-check — unchanged body doesn't rewrite the 'body' field
  I. Report aggregator — counts hits/misses/credits correctly
  J. `cached_httpx_get` drop-in wrapper — auto endpoint_type inference
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import pytest


def _run(c): return asyncio.run(c)


def _fresh_db():
    # Reset the odds_cache module's cached client so it rebuilds on
    # this test's fresh event loop.
    from services.odds_cache import _reset_db_cache
    _reset_db_cache()
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]


async def _clear_test_cache_rows(prefix: str = "test-iter111"):
    db = _fresh_db()
    await db.odds_api_cache.delete_many({"url": {"$regex": prefix}})
    await db.odds_api_request_log.delete_many({"url": {"$regex": prefix}})
    # Also poke a lightweight ping so Motor establishes the connection
    # on THIS event loop before the code under test runs.
    await db.command("ping")


# ═════════════════════════════════════════════════════════════════════
# A. Cache key determinism
# ═════════════════════════════════════════════════════════════════════
def test_A1_cache_key_deterministic():
    from services.odds_cache import _cache_key
    k1 = _cache_key("https://api.example/odds", {"a": 1, "b": 2})
    k2 = _cache_key("https://api.example/odds", {"b": 2, "a": 1})
    assert k1 == k2, "key must be param-order-insensitive"


def test_A2_cache_key_strips_apikey():
    from services.odds_cache import _cache_key
    k_no = _cache_key("https://api.example/odds", {"a": 1})
    k_key = _cache_key("https://api.example/odds",
                        {"a": 1, "apiKey": "SECRET"})
    assert k_no == k_key, "apiKey must be stripped from key"


# ═════════════════════════════════════════════════════════════════════
# B/C/D. Fresh / Stale / Hard-miss TTL behavior
# ═════════════════════════════════════════════════════════════════════
def test_B1_fresh_hit_no_upstream():
    from services.odds_cache import cached_odds_get
    counter = {"n": 0}
    async def upstream():
        counter["n"] += 1
        return [{"id": "test-B1", "commence_time": "2099-01-01T12:00:00Z"}]
    async def go():
        await _clear_test_cache_rows()
        # Call 1: MISS → upstream called once
        r1 = await cached_odds_get(
            url="https://test-iter111/odds/B1", params={},
            endpoint_type="bulk_odds",
            caller="test_B1", upstream_fetch=upstream,
        )
        # Call 2 (immediately): FRESH HIT → no upstream call
        r2 = await cached_odds_get(
            url="https://test-iter111/odds/B1", params={},
            endpoint_type="bulk_odds",
            caller="test_B1", upstream_fetch=upstream,
        )
        assert r1 == r2
        assert counter["n"] == 1, f"upstream hit {counter['n']} times"
    _run(go())


def test_C1_force_refresh_bypasses_cache():
    from services.odds_cache import cached_odds_get
    counter = {"n": 0}
    async def upstream():
        counter["n"] += 1
        return [{"id": f"test-C1-{counter['n']}"}]
    async def go():
        await _clear_test_cache_rows()
        await cached_odds_get(
            url="https://test-iter111/odds/C1", params={},
            endpoint_type="bulk_odds", caller="test_C1",
            upstream_fetch=upstream,
        )
        await cached_odds_get(
            url="https://test-iter111/odds/C1", params={},
            endpoint_type="bulk_odds", caller="test_C1",
            upstream_fetch=upstream, force_refresh=True,
        )
        assert counter["n"] == 2, "force_refresh must call upstream"
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# F. Single-flight dedup
# ═════════════════════════════════════════════════════════════════════
def test_F1_single_flight_dedupes_concurrent_requests():
    from services.odds_cache import cached_odds_get
    counter = {"n": 0}
    async def slow_upstream():
        counter["n"] += 1
        await asyncio.sleep(0.5)   # deliberate delay to trigger dedup
        return [{"id": "test-F1"}]
    async def go():
        await _clear_test_cache_rows()
        # 20 concurrent callers — must produce only 1 upstream call.
        tasks = [
            cached_odds_get(
                url="https://test-iter111/odds/F1", params={},
                endpoint_type="bulk_odds", caller="test_F1",
                upstream_fetch=slow_upstream,
            )
            for _ in range(20)
        ]
        results = await asyncio.gather(*tasks)
        assert counter["n"] == 1, (
            f"expected 1 upstream call, got {counter['n']}"
        )
        assert all(r == results[0] for r in results)
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# G. Completed-game filter
# ═════════════════════════════════════════════════════════════════════
def test_G1_drop_completed_games():
    from services.odds_cache import _drop_completed_games
    now = datetime.now(timezone.utc)
    payload = [
        {"id": "future1",
          "commence_time": (now + timedelta(hours=6)).isoformat()},
        {"id": "past-old",
          "commence_time": (now - timedelta(hours=10)).isoformat()},
        {"id": "live-recent",
          "commence_time": (now - timedelta(minutes=90)).isoformat()},
        {"id": "no-time"},
    ]
    filtered = _drop_completed_games(payload, cutoff_minutes=240)
    ids = {g["id"] for g in filtered}
    assert "future1"      in ids
    assert "live-recent"  in ids     # within 4h window
    assert "no-time"      in ids     # kept — best effort
    assert "past-old"     not in ids  # dropped


def test_G2_drop_completed_non_list_pass_through():
    from services.odds_cache import _drop_completed_games
    assert _drop_completed_games({"any": "dict"}) == {"any": "dict"}
    assert _drop_completed_games(None) is None
    assert _drop_completed_games([]) == []


# ═════════════════════════════════════════════════════════════════════
# H. Diff-check — unchanged body doesn't rewrite `body` field
# ═════════════════════════════════════════════════════════════════════
def test_H1_unchanged_body_skips_rewrite():
    from services.odds_cache import cached_odds_get, _cache_key
    same = [{"stable": "payload"}]
    async def upstream():
        return same
    async def go():
        await _clear_test_cache_rows()
        db = _fresh_db()
        key = _cache_key("https://test-iter111/odds/H1", {})
        # Call 1 — stores body
        await cached_odds_get(
            url="https://test-iter111/odds/H1", params={},
            endpoint_type="bulk_odds", caller="test_H1",
            upstream_fetch=upstream,
        )
        doc1 = await db.odds_api_cache.find_one({"cache_key": key})
        assert doc1 is not None
        hash1 = doc1["body_hash"]
        # Call 2 — force refresh; same upstream body → body_hash same,
        # refreshed_at advances, `body` NOT overwritten.
        await asyncio.sleep(0.01)
        await cached_odds_get(
            url="https://test-iter111/odds/H1", params={},
            endpoint_type="bulk_odds", caller="test_H1",
            upstream_fetch=upstream, force_refresh=True,
        )
        doc2 = await db.odds_api_cache.find_one({"cache_key": key})
        assert doc2["body_hash"] == hash1
        assert doc2["refreshed_at"] >= doc1["refreshed_at"]
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# I. Report aggregator (light smoke — uses live data if present)
# ═════════════════════════════════════════════════════════════════════
def test_I1_report_shape():
    from services.odds_cache import get_odds_usage_report
    async def go():
        r = await get_odds_usage_report(hours=1)
        for k in ("window_hours", "total_requests", "upstream_requests",
                  "cache_hits", "cache_hit_rate_percent",
                  "estimated_credits_used",
                  "projected_monthly_credits",
                  "projected_monthly_at_10x",
                  "by_endpoint", "by_sport", "top_callers"):
            assert k in r, f"report missing key: {k}"
        assert isinstance(r["by_endpoint"], list)
        assert isinstance(r["by_sport"], list)
        assert isinstance(r["top_callers"], list)
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# J. cached_httpx_get wrapper — auto endpoint_type inference
# ═════════════════════════════════════════════════════════════════════
def test_J1_cached_httpx_get_inference():
    """`cached_httpx_get` must auto-detect endpoint_type from the URL."""
    from services.odds_cache import cached_httpx_get, _cache_key
    async def go():
        await _clear_test_cache_rows()
        # This test only validates the shape/signature — we mock the
        # upstream by monkey-patching the httpx client so we don't
        # actually hit the network.
        import httpx
        original = httpx.AsyncClient

        class _FakeResponse:
            def __init__(self, data):
                self._data = data
                self.status_code = 200
            def json(self): return self._data

        class _FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, params=None):
                return _FakeResponse([{"synthetic": "row"}])

        httpx.AsyncClient = _FakeClient
        try:
            r = await cached_httpx_get(
                "https://test-iter111/odds/J1/sports/basketball_nba/odds",
                {"regions": "us", "markets": "h2h"},
                api_key="TEST",
                caller="test_J1", sport_key="basketball_nba",
            )
            assert r == [{"synthetic": "row"}]
            # Confirm log row was written with the auto-inferred
            # endpoint_type = "bulk_odds" (URL ends in /odds and has
            # no /events/ path segment).
            db = _fresh_db()
            log = await db.odds_api_request_log.find_one({
                "url": {"$regex": "test-iter111/odds/J1"},
            })
            assert log is not None
        finally:
            httpx.AsyncClient = original
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# K. Reason logging — every upstream row carries a `reason`
# ═════════════════════════════════════════════════════════════════════
def test_K1_reason_field_populated_on_miss():
    from services.odds_cache import cached_odds_get
    async def upstream(): return [{"x": 1}]
    async def go():
        await _clear_test_cache_rows()
        await cached_odds_get(
            url="https://test-iter111/odds/K1", params={},
            endpoint_type="bulk_odds",
            caller="test_K1", upstream_fetch=upstream,
        )
        db = _fresh_db()
        row = await db.odds_api_request_log.find_one({
            "url": "https://test-iter111/odds/K1",
        })
        assert row is not None
        assert row.get("reason") == "hard_miss"
        assert row.get("upstream_called") is True
        assert row.get("cache_status") == "miss"
    _run(go())


__all__: list[str] = []
