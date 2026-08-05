"""Phase 3 — Time-aware TTL & no-games pre-flight tests (iter112, 2026-08).

Verifies the new adaptive-refresh logic in `services/odds_cache.py`:

  A. `_select_ttl_multiplier` returns the right band for each horizon.
  B. `_time_aware_ttls` scales `bulk_odds` and `event_odds` (but NOT
     `sports_list` / `generic`).
  C. Stale TTL is capped at 24h even for far-future games.
  D. "no games in 48h" pre-flight returns [] without upstream call
     when the cached events for a sport show zero games in horizon.
  E. `_compute_hours_to_nearest_game` correctly reads the latest
     bulk-odds/events cache doc and picks the earliest future
     commence_time.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import pytest


def _run(c): return asyncio.run(c)


def _fresh_db():
    from services.odds_cache import _reset_db_cache
    _reset_db_cache()
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]


async def _seed_cached_events(sport_key: str, commence_times: list[str],
                                endpoint_type: str = "bulk_odds"):
    """Insert a fake cache row so the time-aware helper has something
    to compute against. Uses a unique cache_key each time."""
    import hashlib
    db = _fresh_db()
    body = [{"id": f"evt-{i}", "commence_time": ct,
              "home_team": "A", "away_team": "B", "bookmakers": []}
             for i, ct in enumerate(commence_times)]
    key = f"test-iter112-{sport_key}-{endpoint_type}-{time.time()}"
    ck = hashlib.sha256(key.encode()).hexdigest()
    await db.odds_api_cache.update_one(
        {"cache_key": ck},
        {"$set": {
            "cache_key":     ck,
            "url":           f"https://test-iter112/{sport_key}",
            "endpoint_type": endpoint_type,
            "sport_key":     sport_key,
            "body":          body,
            "body_hash":     "test",
            "refreshed_at":  time.time(),
        }},
        upsert=True,
    )


async def _clear_sport(sport_key: str):
    db = _fresh_db()
    await db.odds_api_cache.delete_many({"sport_key": sport_key})
    await db.odds_api_request_log.delete_many({"sport_key": sport_key})
    # Invalidate the in-memory nearest-game micro-cache.
    from services import odds_cache
    odds_cache._NEAREST_GAME_CACHE.pop(sport_key, None)


# ═════════════════════════════════════════════════════════════════════
# A. Multiplier selection
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("hours,expected_mult", [
    (0.1,  1.0),
    (1.0,  1.0),
    (2.0,  1.0),
    (3.0,  3.0),
    (6.0,  3.0),
    (9.0,  6.0),
    (12.0, 6.0),
    (18.0, 12.0),
    (24.0, 12.0),
    (36.0, 24.0),
    (48.0, 24.0),
    (72.0, 48.0),
    (168.0, 48.0),
    (None, 1.0),
])
def test_A1_ttl_multiplier_bands(hours, expected_mult):
    from services.odds_cache import _select_ttl_multiplier
    assert _select_ttl_multiplier(hours) == expected_mult


# ═════════════════════════════════════════════════════════════════════
# B. TTL scaling — endpoint-type gating
# ═════════════════════════════════════════════════════════════════════
def test_B1_time_aware_ttls_scales_bulk_odds():
    async def go():
        await _clear_sport("test_TB1_nba")
        ts = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
        await _seed_cached_events("test_TB1_nba", [ts])
        from services.odds_cache import _time_aware_ttls
        db = _fresh_db()
        fresh, stale, meta = await _time_aware_ttls(
            db, "bulk_odds", "test_TB1_nba")
        # 8h → 6× multiplier
        assert meta["ttl_multiplier"] == 6.0
        assert fresh == 5 * 60 * 6
        assert stale == min(30 * 60 * 6, 24 * 3600)
    _run(go())


def test_B2_time_aware_ttls_no_effect_on_sports_list():
    from services.odds_cache import _time_aware_ttls
    async def go():
        db = _fresh_db()
        fresh, stale, meta = await _time_aware_ttls(
            db, "sports_list", "anything")
        assert meta == {}
        assert fresh == 24 * 3600
        assert stale == 7 * 24 * 3600
    _run(go())


def test_B3_time_aware_ttls_no_signal_returns_base():
    """When no cached events exist → no signal → base TTLs."""
    async def go():
        await _clear_sport("test_TB3_empty")
        from services.odds_cache import _time_aware_ttls
        db = _fresh_db()
        fresh, stale, meta = await _time_aware_ttls(
            db, "bulk_odds", "test_TB3_empty")
        # No signal → mult 1.0
        assert meta["ttl_multiplier"] == 1.0
        assert fresh == 5 * 60
        assert stale == 30 * 60
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# C. Stale TTL cap at 24h even for far-future games
# ═════════════════════════════════════════════════════════════════════
def test_C1_stale_ttl_capped_at_24h():
    async def go():
        await _clear_sport("test_TC1_far")
        # 72h out → mult 48 → naïve stale = 30 min × 48 = 24 h
        # BUT we cap at 24h regardless.
        ts = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
        await _seed_cached_events("test_TC1_far", [ts])
        from services.odds_cache import _time_aware_ttls
        db = _fresh_db()
        fresh, stale, meta = await _time_aware_ttls(
            db, "bulk_odds", "test_TC1_far")
        assert meta["ttl_multiplier"] == 48.0
        assert stale == 24 * 3600
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# D. "no games in 48h" pre-flight short-circuit
# ═════════════════════════════════════════════════════════════════════
def test_D1_no_games_in_48h_returns_empty_no_upstream():
    async def go():
        await _clear_sport("test_TD1_dead")
        # Only far-future games — 72h+
        ts = (datetime.now(timezone.utc) + timedelta(hours=100)).isoformat()
        await _seed_cached_events("test_TD1_dead", [ts])
        counter = {"n": 0}
        async def upstream():
            counter["n"] += 1
            return [{"real": "data"}]
        from services.odds_cache import cached_odds_get
        r = await cached_odds_get(
            url="https://test-iter112/no-games-check",
            params={"sport": "test_TD1_dead"},
            endpoint_type="bulk_odds",
            caller="test_D1",
            sport_key="test_TD1_dead",
            upstream_fetch=upstream,
        )
        # Must short-circuit: empty list, no upstream call.
        assert r == []
        assert counter["n"] == 0
    _run(go())


def test_D2_games_within_48h_proceeds_normally():
    """Sport with a game in 6h should NOT short-circuit; the actual
    fetch (or a hit from a warmed cache) still happens."""
    async def go():
        await _clear_sport("test_TD2_active")
        ts = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        await _seed_cached_events("test_TD2_active", [ts])
        counter = {"n": 0}
        async def upstream():
            counter["n"] += 1
            return [{"real": "data"}]
        from services.odds_cache import cached_odds_get
        r = await cached_odds_get(
            url="https://test-iter112/active-check",
            params={"sport": "test_TD2_active"},
            endpoint_type="bulk_odds",
            caller="test_D2",
            sport_key="test_TD2_active",
            upstream_fetch=upstream,
        )
        assert r == [{"real": "data"}]
        assert counter["n"] == 1
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# E. `_compute_hours_to_nearest_game` picks earliest future
# ═════════════════════════════════════════════════════════════════════
def test_E1_picks_earliest_future_game():
    async def go():
        await _clear_sport("test_TE1_mix")
        # Mix of past + future.
        past = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        soon = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        far  = (datetime.now(timezone.utc) + timedelta(hours=30)).isoformat()
        await _seed_cached_events("test_TE1_mix", [past, far, soon])
        from services.odds_cache import _compute_hours_to_nearest_game
        db = _fresh_db()
        h = await _compute_hours_to_nearest_game(db, "test_TE1_mix")
        assert h is not None
        # ~3h ± tolerance for compute latency.
        assert 2.5 < h < 3.5, f"expected ~3h, got {h}"
    _run(go())


def test_E2_returns_none_when_no_cache():
    async def go():
        await _clear_sport("test_TE2_none")
        from services.odds_cache import _compute_hours_to_nearest_game
        db = _fresh_db()
        h = await _compute_hours_to_nearest_game(db, "test_TE2_none")
        assert h is None
    _run(go())


__all__: list[str] = []
