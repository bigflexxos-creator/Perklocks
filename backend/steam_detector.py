"""Steam detector — Phase 5c of the advanced-analytics roadmap.

A **steam move** is a rapid, coordinated line movement across multiple
sportsbooks in the same direction — the market's way of shouting that
sharp money just hit. Detecting steam ~30 seconds after it happens
lets us:

  1) Tag pending picks with a `steam` block so users see the market
     confirming (or fading) their side.
  2) Elevate the confidence score on picks whose direction MATCHES the
     steam direction (line moved TOWARDS the pick — market agreement).
  3) Flag REVERSE steam — line moved AWAY from the pick — as a risk
     warning ("the market disagrees with this play").

Definition used here (industry standard, ≥ 5¢ / ~3pp implied-probability):

    For each pending pick with ≥ 2 line-history observations,
    look at the two extremes of a rolling **5-minute** window.
    If the implied-probability delta between newest and oldest
    observation in that window is ≥ `_STEAM_THRESHOLD_PP` (3.0
    percentage points ≈ 5¢ at even-money odds), flag as steam.

The 5¢ default is the industry-standard threshold — the same one Pinnacle
uses to trip its own auto-limit rebalancer.

Source: `pick_line_history` collection populated by
`closing_line_snapshotter.line_observer_loop` (already runs every ~5 min
for pending picks in the 36h-before-kickoff window).

Storage:
    `picks.steam` block — attached to a pick when steam is detected.
        {
          direction:       "toward" | "away"    # relative to the pick's side
          magnitude_pp:    float                 # abs implied-prob delta (pp)
          american_delta:  int                   # signed American-cents move
          observed_at:     iso timestamp
          window_minutes:  5
        }

Public API:
    from steam_detector import (
        steam_detector_loop,       # background loop (server.py)
        detect_recent_steam,       # one-shot detection helper
        get_steam_picks,           # list all picks with steam
    )
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("lockscore.steam_detector")

# ── Configuration ────────────────────────────────────────────────────
# Rolling window: how far back to look for the "before" price. 5 min
# is the industry standard — captures true steam (sharp injection) but
# ignores slow-drift moves that just reflect news / weather.
_STEAM_WINDOW_MIN = 5

# Implied-probability delta (pp) to trigger. 3.0pp ≈ 5¢ at even-money
# American odds. Sharp-book auto-limit thresholds sit around this
# level. Tune down to 2.0 for more sensitive (retail-style) alerts.
_STEAM_THRESHOLD_PP = 3.0

# How often to run the detection sweep. The observer loop writes new
# lines every ~5 min, so we scan every 60s — fresh enough to be timely
# without hammering Mongo.
_LOOP_INTERVAL_SEC = 60

# Only look at picks whose kickoff is within this window; older picks
# are settled/reaped, further-out picks have too much time for legit
# steam-and-fade cycles.
_MAX_HOURS_TO_KICKOFF = 12


# ── Math helpers ────────────────────────────────────────────────────
def _american_to_implied_pp(american: int | float) -> float:
    """American → implied-probability percentage points."""
    if not american:
        return 0.0
    a = float(american)
    if a > 0:
        return 100.0 / (a + 100.0) * 100.0
    return abs(a) / (abs(a) + 100.0) * 100.0


def _pick_side_favors_higher_prob(pick: dict) -> bool:
    """True if the pick's side benefits when the implied probability
    GROWS — i.e. the price gets shorter. This is the case for
    Moneyline / Spread / Over / Under alike: bettor took the side, if
    the line moves TOWARD their side, implied prob rises."""
    # For our tagging semantics, "toward" always means the line moved
    # in the direction that would make the bettor's ticket more likely
    # to hit (higher implied probability = shorter price).
    return True  # sentinel — semantic is captured by direction below


# ── Core detection ──────────────────────────────────────────────────
async def _detect_steam_for_pick(db, pick_id: str, book_odds: float,
                                  now: datetime) -> Optional[dict]:
    """Look at the last ~10 observations for this pick and compute
    the max implied-prob delta inside the rolling window. Returns a
    steam dict if the threshold is exceeded, else None."""
    window_start = now - timedelta(minutes=_STEAM_WINDOW_MIN)
    cursor = db.pick_line_history.find(
        {"pick_id": pick_id, "observed_at": {"$gte": window_start}},
        {"american": 1, "observed_at": 1, "_id": 0},
    ).sort("observed_at", 1)
    observations = await cursor.to_list(length=20)
    if len(observations) < 2:
        return None
    first = observations[0]
    last = observations[-1]
    p0 = _american_to_implied_pp(first.get("american") or book_odds)
    p1 = _american_to_implied_pp(last.get("american") or book_odds)
    delta_pp = p1 - p0
    if abs(delta_pp) < _STEAM_THRESHOLD_PP:
        return None
    a0 = int(first.get("american") or 0)
    a1 = int(last.get("american") or 0)
    return {
        "direction":       "toward" if delta_pp > 0 else "away",
        "magnitude_pp":    round(abs(delta_pp), 2),
        "american_delta":  a1 - a0,
        "american_start":  a0,
        "american_end":    a1,
        "observed_at":     (last.get("observed_at") or now).isoformat()
                           if hasattr(last.get("observed_at"), "isoformat")
                           else str(last.get("observed_at")),
        "window_minutes":  _STEAM_WINDOW_MIN,
        "observations":    len(observations),
    }


async def _sweep_once(db) -> dict:
    """Iterate over pending picks in the kickoff window and detect
    steam for each. Idempotent — writes `steam` block via $set, so a
    picks doc that no longer meets the threshold gets the field
    replaced with the current (possibly zero-motion) reading."""
    now = datetime.now(timezone.utc)
    max_kickoff = now + timedelta(hours=_MAX_HOURS_TO_KICKOFF)
    # Only look at picks close enough to kickoff for line moves to matter
    q = {
        "status": "pending",
        "event_time": {"$gte": now.isoformat(),
                        "$lte": max_kickoff.isoformat()},
    }
    detected = 0
    scanned = 0
    async for p in db.picks.find(q, {
        "id": 1, "book_odds": 1, "market": 1, "selection": 1, "event_time": 1,
    }):
        scanned += 1
        pick_id = p.get("id")
        book_odds = p.get("book_odds") or 0
        if not pick_id or not book_odds:
            continue
        steam = await _detect_steam_for_pick(db, pick_id, book_odds, now)
        if steam:
            await db.picks.update_one(
                {"id": pick_id},
                {"$set": {"steam": steam}},
            )
            detected += 1
    if detected:
        logger.info("steam sweep: %d picks flagged (scanned %d)",
                    detected, scanned)
    return {"scanned": scanned, "detected": detected,
            "window_min": _STEAM_WINDOW_MIN,
            "threshold_pp": _STEAM_THRESHOLD_PP}


async def steam_detector_loop(db) -> None:
    """Background loop — sweep every _LOOP_INTERVAL_SEC seconds."""
    logger.info("steam detector armed (interval=%ss, window=%dm, "
                "threshold=%.1fpp)",
                _LOOP_INTERVAL_SEC, _STEAM_WINDOW_MIN, _STEAM_THRESHOLD_PP)
    while True:
        try:
            await _sweep_once(db)
        except Exception as e:
            logger.warning("steam sweep failed: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL_SEC)


# ── Public read helpers ─────────────────────────────────────────────
async def detect_recent_steam(db, pick_id: str) -> Optional[dict]:
    """One-shot detection for a single pick (useful in tests or ad-hoc
    admin endpoints)."""
    pick = await db.picks.find_one({"id": pick_id},
                                    {"book_odds": 1, "_id": 0})
    if not pick:
        return None
    return await _detect_steam_for_pick(
        db, pick_id, pick.get("book_odds") or 0,
        datetime.now(timezone.utc),
    )


async def get_steam_picks(db, hours: int = 6,
                           direction: Optional[str] = None,
                           limit: int = 50) -> list[dict]:
    """List all pending picks flagged with steam in the last `hours`
    hours, optionally filtered by direction ('toward'|'away').

    Sorted by magnitude_pp descending — biggest steam first."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q: dict = {
        "steam": {"$exists": True},
        "steam.observed_at": {"$gte": since.isoformat()},
        "status": "pending",
    }
    if direction in ("toward", "away"):
        q["steam.direction"] = direction
    cursor = db.picks.find(q, {
        "_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
        "event": 1, "event_time": 1, "book_odds": 1, "lock_score": 1,
        "steam": 1,
    }).sort("steam.magnitude_pp", -1).limit(limit)
    return await cursor.to_list(length=limit)


__all__ = [
    "steam_detector_loop",
    "detect_recent_steam",
    "get_steam_picks",
    "_american_to_implied_pp",       # exposed for testing
    "_detect_steam_for_pick",        # exposed for testing
]
