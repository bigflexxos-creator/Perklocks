"""PERKLOCKS ROOT FIX (2026-09-03) — Pick-Breakdown Same-Game Filter.

Regression: users kept opening Pick Breakdown for TONIGHT's MLB game
and seeing hitter picks from THREE DAYS AGO in the Market
Competition panel — "Jake Bauers Over 0.5 Hits · 94 Lock" was
listed as a competitor for tonight's MIL @ CHC game even though the
pick was from the 08-31 series game.  Because the panel didn't
bound candidates by event_time, every historical pick for the same
(away, home) matchup bled through and looked like it belonged to
the current game — creating the false impression that "hitters
exist in DB but are missing from the board".

The fix bounds ``_rank_markets_for_event`` candidates to within
±12 h of the current pick's ``event_time`` — same-day slate window
with a cushion for cross-midnight ET/UTC drift.  Legacy behaviour
preserved when the current pick lacks event_time.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest  # noqa: F401
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv("/app/backend/.env")

MONGO_URL = os.getenv("MONGO_URL")
DB_NAME   = os.getenv("DB_NAME", "lockscore_db")


def _mk_pick(pid: str, event_time: str, market: str) -> dict:
    return {
        "id":         pid,
        "sport":      "MLB",
        "event":      "Milwaukee Brewers @ Chicago Cubs",
        "event_time": event_time,
        "market":     market,
        "selection":  market.split("(")[0].strip(),
        "line":       0.5,
        "book_odds":  -140,
        "lock_score": 94.0,
        "published_lock_score": 94.0,
        "grade":      "Strong Lock",
        "published_grade": "Strong Lock",
        "win_probability": 88.0,
    }


def test_market_rank_excludes_prior_series_games():
    """The panel MUST NOT show yesterday's or last-week's pick for
    the same team matchup — only same-game candidates within ±12 h
    of the current pick's event_time are eligible.
    """
    async def _run():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        cur_id  = f"mrq-cur-{uuid.uuid4()}"
        same_id = f"mrq-same-{uuid.uuid4()}"
        prior_id = f"mrq-prior-{uuid.uuid4()}"
        prior2_id = f"mrq-prior2-{uuid.uuid4()}"
        try:
            await db.picks.delete_many({"id": {"$in": [
                cur_id, same_id, prior_id, prior2_id,
            ]}})
            # Current pick — tonight (moneyline Cubs)
            _cur = _mk_pick(
                cur_id, "2099-12-31T23:15:00Z",
                "Chicago Cubs Moneyline",
            )
            _cur["selection"] = "Chicago Cubs"
            await db.picks.insert_one(_cur)
            # Same-game candidate — same event_time  ✔ SHOULD appear
            _same = _mk_pick(
                same_id, "2099-12-31T23:15:00Z",
                "Chicago Cubs -1.5 Spread",
            )
            _same["selection"] = "Chicago Cubs"
            await db.picks.insert_one(_same)
            # Prior series game 3 days earlier  ✘ MUST NOT appear
            _prior = _mk_pick(
                prior_id, "2099-12-28T23:15:00Z",
                "Chicago Cubs Moneyline",
            )
            _prior["selection"] = "Chicago Cubs"
            await db.picks.insert_one(_prior)
            # Prior series game 1 day earlier  ✘ MUST NOT appear
            _prior2 = _mk_pick(
                prior2_id, "2099-12-30T23:15:00Z",
                "Chicago Cubs -1.5 Spread",
            )
            _prior2["selection"] = "Chicago Cubs"
            await db.picks.insert_one(_prior2)

            from market_competition.routes import _rank_markets_for_event
            ranked = await _rank_markets_for_event(
                db, event="Milwaukee Brewers @ Chicago Cubs",
                sport="MLB", exclude_id=cur_id,
            )
            ids = {r.get("id") for r in ranked}
            assert same_id in ids, ranked
            assert prior_id  not in ids, "prior-series pick leaked in"
            assert prior2_id not in ids, "prior-series pick leaked in"
        finally:
            await db.picks.delete_many({"id": {"$in": [
                cur_id, same_id, prior_id, prior2_id,
            ]}})
            client.close()
    asyncio.run(_run())


def test_market_rank_cross_midnight_pick_still_matches():
    """A soccer/late-baseball pick crossing UTC midnight must still
    resolve its own siblings — the ±12 h window covers the drift.
    """
    async def _run():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        cur_id  = f"mrq-cur-{uuid.uuid4()}"
        near_id = f"mrq-near-{uuid.uuid4()}"
        far_id  = f"mrq-far-{uuid.uuid4()}"
        try:
            await db.picks.delete_many({"id": {"$in": [
                cur_id, near_id, far_id,
            ]}})
            # Current pick — 23:45Z (crosses midnight when read as ET)
            _cur = _mk_pick(
                cur_id, "2099-12-31T23:45:00Z",
                "Chicago Cubs Moneyline",
            )
            _cur["selection"] = "Chicago Cubs"
            await db.picks.insert_one(_cur)
            # Sibling on same game — 30 min earlier ✔ SHOULD appear
            _near = _mk_pick(
                near_id, "2099-12-31T23:15:00Z",
                "Chicago Cubs -1.5 Spread",
            )
            _near["selection"] = "Chicago Cubs"
            await db.picks.insert_one(_near)
            # Same matchup 7 days later ✘ MUST NOT appear
            _far = _mk_pick(
                far_id, "2100-01-07T23:15:00Z",
                "Chicago Cubs Moneyline",
            )
            _far["selection"] = "Chicago Cubs"
            await db.picks.insert_one(_far)

            from market_competition.routes import _rank_markets_for_event
            ranked = await _rank_markets_for_event(
                db, event="Milwaukee Brewers @ Chicago Cubs",
                sport="MLB", exclude_id=cur_id,
            )
            ids = {r.get("id") for r in ranked}
            assert near_id in ids
            assert far_id not in ids
        finally:
            await db.picks.delete_many({"id": {"$in": [
                cur_id, near_id, far_id,
            ]}})
            client.close()
    asyncio.run(_run())
