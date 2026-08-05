"""Phase A/B — Odds API burn-reduction regression tests (iter114, 2026-08).

Covers:
  A. Bad-market registry — mark + filter round-trip, 24h TTL
  B. Scheduled snapshot helper — next-slot math is correct
  C. Off-peak TTL scaling — applies 2× multiplier during 03:00-14:00 UTC
  D. alt_lines_feed picks-scope — skips events with no picks today
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import pytest


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    from services.odds_cache import _reset_db_cache
    _reset_db_cache()
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


# ─────────────────────────────────────────────────────────
# A — Bad-market registry
# ─────────────────────────────────────────────────────────
def test_bad_market_registry_round_trip():
    async def run():
        db = _fresh_db()
        from services.bad_market_registry import (
            ensure_indices, mark_bad, filter_markets, stats, COLLECTION,
        )
        await ensure_indices(db)
        # clean slate
        await db[COLLECTION].delete_many(
            {"sport_key": "unit_test_sport"})

        # Mark two markets bad
        await mark_bad(
            db, sport_key="unit_test_sport",
            markets=["player_totally_fake_market", "another_bad_market"],
            reason="unit_test_422",
        )

        # Filter should remove them
        out = await filter_markets(
            db, sport_key="unit_test_sport",
            markets=["player_totally_fake_market", "player_goal_scorer_anytime",
                     "another_bad_market"],
        )
        assert "player_totally_fake_market" not in out
        assert "another_bad_market" not in out
        assert "player_goal_scorer_anytime" in out

        # Stats endpoint returns them
        st = await stats(db)
        assert st["active_entries"] >= 2
        assert st["by_sport"].get("unit_test_sport", 0) >= 2

        # Cleanup
        await db[COLLECTION].delete_many(
            {"sport_key": "unit_test_sport"})
    _run(run())


# ─────────────────────────────────────────────────────────
# B — Scheduled snapshot next-slot math
# ─────────────────────────────────────────────────────────
def test_scheduled_snapshot_next_slot():
    from services.scheduled_snapshot import _seconds_until_next_slot
    # It's 10:00 UTC — next slot should be 12:00 → 7200s
    now = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
    s = _seconds_until_next_slot([12, 18, 23], now=now)
    assert 7100 <= s <= 7300

    # It's 23:30 UTC — next slot is 12:00 tomorrow → ~12.5h = 45000s
    now = datetime(2026, 8, 1, 23, 30, 0, tzinfo=timezone.utc)
    s = _seconds_until_next_slot([12, 18, 23], now=now)
    assert 44900 <= s <= 45100

    # It's exactly 12:00 UTC — should skip to next slot 18:00
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    s = _seconds_until_next_slot([12, 18, 23], now=now)
    assert 21500 <= s <= 21700


# ─────────────────────────────────────────────────────────
# C — Off-peak TTL scaling (Phase B)
# ─────────────────────────────────────────────────────────
def test_offpeak_multiplier_applies_in_offpeak_hours():
    from services.odds_cache import _offpeak_multiplier, _OFFPEAK_MULT
    import services.odds_cache as oc
    # Force UTC hour into off-peak window
    real_now = datetime.now
    try:
        oc.datetime = type("D", (), {
            "now": staticmethod(lambda tz=None: datetime(
                2026, 8, 1, 5, 0, 0, tzinfo=timezone.utc)),
            "fromisoformat": datetime.fromisoformat,
        })
        assert _offpeak_multiplier() == _OFFPEAK_MULT
        # Peak hour → 1.0
        oc.datetime = type("D", (), {
            "now": staticmethod(lambda tz=None: datetime(
                2026, 8, 1, 20, 0, 0, tzinfo=timezone.utc)),
            "fromisoformat": datetime.fromisoformat,
        })
        assert _offpeak_multiplier() == 1.0
    finally:
        oc.datetime = datetime  # restore


# ─────────────────────────────────────────────────────────
# D — alt_lines_feed._todays_pick_scope reads db.picks
# ─────────────────────────────────────────────────────────
def test_alt_lines_picks_scope_shape():
    async def run():
        db = _fresh_db()
        from alt_lines_feed import _todays_pick_scope, _norm
        # Insert a synthetic pick for TODAY
        today = datetime.now(timezone.utc).date().isoformat()
        pick_id = "unit_test_scope_pick_xyz"
        await db.picks.delete_many({"id": pick_id})
        await db.picks.insert_one({
            "id": pick_id,
            "pick_date": today,
            "sport": "MLB",
            "odds_api_sport_key": "baseball_mlb",
            "home_team": "New York Yankees",
            "away_team": "Boston Red Sox",
        })
        scope = await _todays_pick_scope(db)
        assert "baseball_mlb" in scope["sport_keys"]
        home_n = _norm("New York Yankees")
        away_n = _norm("Boston Red Sox")
        assert (home_n, away_n) in scope["team_pairs"]
        assert (away_n, home_n) in scope["team_pairs"]  # both orderings stored
        assert (home_n, away_n) in scope["by_sport_key"].get(
            "baseball_mlb", set())
        await db.picks.delete_many({"id": pick_id})
    _run(run())
