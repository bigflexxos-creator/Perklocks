"""Closing-line snapshotter + full pick-line history.

WHY THIS EXISTS
---------------
CLV (Closing Line Value) is the gold-standard sharp-betting metric — it
measures whether you beat the closing market, which is the single best
predictor of long-term profit. Until today the codebase stored
`closing_odds = book_odds` (same value as `odds_at_pick`), so CLV math
always returned 0. This module finally captures the real closing line.

WHAT IT DOES
------------
1. **Periodic line observations** — every ~15 minutes, every still-pending
   pick gets its current Odds API price logged to a new
   `pick_line_history` collection. That gives a full trajectory: how the
   line moved between when we picked it and when it closed.
2. **Closing snapshot** — every minute, picks with `commence_time` in
   the next 5–20 minutes that haven't been "closed" yet get their final
   pre-kickoff price written to `picks.closing_odds`. CLV math now
   compares the real two endpoints (pick price vs. close price) instead
   of book_odds vs. book_odds.

API COST
--------
We batch by sport/event — one Odds API call returns every market for a
given event, so the per-pick marginal cost is ~0. With the 5M-req quota
the user upgraded to today, this is a rounding error.

DESIGN
------
- All state lives in MongoDB. No in-memory caches that drift on restart.
- The snapshotter and the observer are two independent async loops so a
  failure in one can't stop the other.
- The closing snapshot is idempotent — re-running on an already-closed
  pick is a no-op (we gate on `closing_odds_snapshotted`).
- The pick-line history collection is append-only — never overwritten.
  Indexed on (pick_id, observed_at) so admin queries are fast.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.clv_snapshotter")

_ODDS_API_KEY = os.environ.get("THE_ODDS_API_KEY") or ""
_ODDS_BASE   = "https://api.the-odds-api.com/v4"

OBSERVER_INTERVAL_SECONDS = 15 * 60   # log a line obs every 15 min per pending pick
CLOSER_INTERVAL_SECONDS   = 60        # check every minute for picks about to close
CLOSE_WINDOW_MIN          = 5         # pick is "closing now" if 5–20 min from start
CLOSE_WINDOW_MAX          = 20


# Sport key → Odds API sport_key mapping (subset — only sports we ship
# picks for). Each call returns all events for the sport, so we can
# batch many picks per request.
_ODDS_SPORT_KEY = {
    "MLB":    "baseball_mlb",
    "NBA":    "basketball_nba",
    "NFL":    "americanfootball_nfl",
    "CFB":    "americanfootball_ncaaf",
    "Tennis": "tennis_atp_singles",
    "Soccer": None,  # multi-league; handled separately
    "UFC":    "mma_mixed_martial_arts",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _extract_event_id(pick: dict) -> Optional[str]:
    """Extract the Odds API event_id from a pick.

    Pick records don't store the bare event_id — it's embedded as the
    second token of `external_id` (format: `{sport}-{event_id}-...`).
    """
    eid = pick.get("event_id") or pick.get("game_id")
    if eid:
        return str(eid)
    ext = str(pick.get("external_id") or "")
    if not ext:
        return None
    parts = ext.split("-")
    # `Soccer-<32-char-hash>-...` → take parts[1] when it looks like a hash.
    if len(parts) >= 2 and len(parts[1]) >= 16:
        return parts[1]
    return None


def _pick_event_time(pick: dict):
    """Pick records use `event_time` in most places; some legacy paths
    use `commence_time`. Return whichever is present."""
    return pick.get("event_time") or pick.get("commence_time")


def _parse_iso(s) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        ss = str(s).strip()
        if ss.endswith("Z"):
            ss = ss[:-1] + "+00:00"
        return datetime.fromisoformat(ss)
    except Exception:
        return None


async def _http_get(url: str, params: dict) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=12.0) as cx:
            r = await cx.get(url, params=params)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 422:
            return None
        logger.debug("Odds API %s → %d", url, r.status_code)
    except Exception as e:
        logger.debug("Odds API request failed: %s", e)
    return None


async def _fetch_event_odds(sport_key: str, event_id: str) -> Optional[list]:
    """Fetch current odds for a specific event. Returns list of bookmaker rows."""
    if not _ODDS_API_KEY:
        return None
    url = f"{_ODDS_BASE}/sports/{sport_key}/events/{event_id}/odds"
    data = await _http_get(url, {
        "apiKey":     _ODDS_API_KEY,
        "regions":    "us",
        "markets":    "h2h,spreads,totals",
        "oddsFormat": "american",
    })
    if not data:
        return None
    return data.get("bookmakers") or []


def _match_pick_to_odds(pick: dict, bookmakers: list) -> Optional[float]:
    """Find the current American odds for this pick's market/selection."""
    if not bookmakers:
        return None
    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").lower()
    # Heuristic match:
    #   "Yankees Moneyline"  → market_key h2h, outcome name "New York Yankees"
    #   "Total Runs Over 8.5" → market_key totals, outcome name "Over", point 8.5
    target_market_key = None
    if "moneyline" in market: target_market_key = "h2h"
    elif "spread"  in market: target_market_key = "spreads"
    elif "total"   in market: target_market_key = "totals"
    if not target_market_key:
        return None  # prop markets not snapshotted via h2h endpoint
    # Pick the median price across bookmakers for robustness.
    prices = []
    for bk in bookmakers:
        for mk in (bk.get("markets") or []):
            if (mk.get("key") or "").lower() != target_market_key:
                continue
            for out in (mk.get("outcomes") or []):
                nm = (out.get("name") or "").lower()
                if selection and (nm == selection or selection in nm or nm in selection):
                    price = out.get("price")
                    if isinstance(price, (int, float)):
                        prices.append(float(price))
    if not prices:
        return None
    prices.sort()
    return prices[len(prices) // 2]   # median


# ──────────────────────────────────────────────────────────────────────
#  Loop 1 — line observer
# ──────────────────────────────────────────────────────────────────────
async def line_observer_loop(db) -> None:
    """Every OBSERVER_INTERVAL_SECONDS, log a line observation for every
    still-pending pick into `pick_line_history`."""
    await db.pick_line_history.create_index([("pick_id", 1), ("observed_at", -1)])
    while True:
        try:
            await _observe_once(db)
        except Exception as e:
            logger.warning("line observer cycle failed: %s", e)
        await asyncio.sleep(OBSERVER_INTERVAL_SECONDS)


async def _observe_once(db) -> dict:
    now = _now_utc()
    # Group pending picks by (sport, event_external_id) — one Odds API call per group.
    cursor = db.picks.find(
        {"status": "pending",
         "event_time": {"$exists": True, "$ne": None}},
        {"id": 1, "sport": 1, "market": 1, "selection": 1, "event_time": 1,
         "event_id": 1, "external_id": 1, "book_odds": 1},
    )
    by_event: dict = {}
    async for p in cursor:
        ct = _parse_iso(_pick_event_time(p))
        if not ct:
            continue
        # Skip if already started or > 36h away (saves API quota).
        if ct < now or ct > now + timedelta(hours=36):
            continue
        sport = p.get("sport")
        eid = _extract_event_id(p)
        if not (sport and eid):
            continue
        by_event.setdefault((sport, eid), []).append(p)
    observed = 0
    for (sport, eid), picks_for_event in by_event.items():
        sport_key = _ODDS_SPORT_KEY.get(sport)
        if not sport_key:
            continue
        bookmakers = await _fetch_event_odds(sport_key, eid)
        if not bookmakers:
            continue
        for pick in picks_for_event:
            price = _match_pick_to_odds(pick, bookmakers)
            if price is None:
                continue
            await db.pick_line_history.insert_one({
                "pick_id":     pick.get("id"),
                "sport":       sport,
                "event_id":    eid,
                "market":      pick.get("market"),
                "selection":   pick.get("selection"),
                "observed_at": now,
                "american":    price,
                "minutes_to_kickoff":
                    int((_parse_iso(_pick_event_time(pick)) - now).total_seconds() // 60),
            })
            observed += 1
    logger.info("line observer: %d observations across %d events",
                observed, len(by_event))
    return {"observations": observed, "events": len(by_event)}


# ──────────────────────────────────────────────────────────────────────
#  Loop 2 — closing snapshotter
# ──────────────────────────────────────────────────────────────────────
async def closing_snapshotter_loop(db) -> None:
    while True:
        try:
            await _snapshot_closes_once(db)
        except Exception as e:
            logger.warning("closing snapshotter cycle failed: %s", e)
        await asyncio.sleep(CLOSER_INTERVAL_SECONDS)


async def _snapshot_closes_once(db) -> dict:
    now = _now_utc()
    win_from = now + timedelta(minutes=CLOSE_WINDOW_MIN)
    win_to   = now + timedelta(minutes=CLOSE_WINDOW_MAX)
    # Find picks in the close window that haven't been snapshotted yet.
    cursor = db.picks.find({
        "closing_odds_snapshotted": {"$ne": True},
        "event_time": {"$exists": True, "$ne": None},
    }, {"id": 1, "sport": 1, "market": 1, "selection": 1, "event_time": 1,
        "event_id": 1, "external_id": 1, "book_odds": 1, "odds_at_pick": 1})

    by_event: dict = {}
    async for p in cursor:
        ct = _parse_iso(_pick_event_time(p))
        if not ct:
            continue
        if not (win_from <= ct <= win_to):
            continue
        sport = p.get("sport")
        eid = _extract_event_id(p)
        if not (sport and eid):
            continue
        by_event.setdefault((sport, eid), []).append(p)

    snapped = 0
    for (sport, eid), picks_for_event in by_event.items():
        sport_key = _ODDS_SPORT_KEY.get(sport)
        if not sport_key:
            # For sports we can't snapshot live, fall back to book_odds so
            # the analytics page never returns NaN.
            for pick in picks_for_event:
                await db.picks.update_one(
                    {"id": pick["id"]},
                    {"$set": {
                        "closing_odds":              pick.get("book_odds"),
                        "closing_odds_snapshotted":  True,
                        "closing_odds_source":       "fallback_book_odds",
                        "closing_odds_at":           _now_utc(),
                    }},
                )
            continue
        bookmakers = await _fetch_event_odds(sport_key, eid)
        for pick in picks_for_event:
            price = _match_pick_to_odds(pick, bookmakers or [])
            if price is None:
                # If we can't read the close, fall back to book_odds.
                price = pick.get("book_odds")
                source = "fallback_book_odds"
            else:
                source = "odds_api_live"
            from analytics import clv_units as _clv
            clv = _clv(pick.get("odds_at_pick"), price)
            await db.picks.update_one(
                {"id": pick["id"]},
                {"$set": {
                    "closing_odds":              price,
                    "closing_odds_snapshotted":  True,
                    "closing_odds_source":       source,
                    "closing_odds_at":           _now_utc(),
                    "clv_value":                 clv,
                }},
            )
            snapped += 1
    if snapped:
        logger.info("closing snapshotter: closed %d picks across %d events",
                    snapped, len(by_event))
    return {"closed": snapped, "events": len(by_event)}


# ──────────────────────────────────────────────────────────────────────
#  Admin status helper
# ──────────────────────────────────────────────────────────────────────
async def snapshot_status(db) -> dict:
    """Quick health-check used by /api/admin/clv/snapshot-status."""
    total_picks = await db.picks.count_documents(
        {"status": {"$in": ["won", "lost", "push"]}},
    )
    snapped = await db.picks.count_documents(
        {"closing_odds_snapshotted": True,
         "status": {"$in": ["won", "lost", "push"]}},
    )
    live_snapped = await db.picks.count_documents(
        {"closing_odds_source": "odds_api_live"},
    )
    fallback = await db.picks.count_documents(
        {"closing_odds_source": "fallback_book_odds"},
    )
    obs_n = await db.pick_line_history.count_documents({})
    return {
        "settled_picks":         total_picks,
        "closing_snapshotted":   snapped,
        "live_snapshots":        live_snapped,
        "fallback_snapshots":    fallback,
        "line_observations":     obs_n,
        "coverage_pct":          round(snapped / max(1, total_picks) * 100, 1),
    }
