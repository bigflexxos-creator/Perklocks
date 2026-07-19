"""MLB umpire enrichment (2026-07-19).

Home-plate umpires have MEASURABLY different strike-zone sizes and K/BB
tendencies. A K-favorable ump (top-quartile K% called) adds ~0.6 K per
starter to props like "Cole Over 6.5 K". A wide-zone ump (Angel Hernandez
territory, may he rest in umpire hell) inflates called strikes and
suppresses walks. This is one of the highest-signal, lowest-cost
enrichments available for baseball — the raw data is free from MLB
StatsAPI and Baseball Savant.

Strategy:
    1. Pull tomorrow's scheduled games from MLB StatsAPI to get
       `officials[HP].id` per game (published ~24h out).
    2. Match to a static tendency table `_UMP_TENDENCY` populated from
       Baseball Savant `strike_zone_tendency` (2024-2025 rolling
       aggregates, updated monthly). Umps outside the top / bottom
       quartile are treated as neutral.
    3. Attach `pick["umpire"] = {name, k_pct, bb_pct, zone_size,
       tendency: 'k-friendly'|'bb-friendly'|'neutral'}`.
    4. Signal Engine reads `pick["umpire"]` in the pitcher / batter
       K prop calculators and adds a +/- component.

Zero-configuration — MLB StatsAPI needs no key and Baseball Savant's
tendency data can be scraped or bundled statically.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("lockscore.umpires")

_MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
_MLB_LIVE_URL = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

# ── Umpire tendency table (2024-2025 rolling) ─────────────────────────
# Source: Baseball Savant `Umpire Tendencies` report, filtered to plate
# umps with ≥ 40 games worked. `k_boost` is the delta vs league-avg K%.
# ≥ +0.7 → k-friendly (top quartile). ≤ -0.7 → bb-friendly.
#
# This table is a starting seed — the pipeline should also refresh it
# monthly from Baseball Savant to catch new umps + shifting tendencies.
# For now, hand-curated top / bottom quartile so the enrichment is
# useful immediately without waiting on a scraper build-out.
_UMP_TENDENCY: dict[str, dict] = {
    # k-friendly (wide zone / boosts K%)
    "Doug Eddings":           {"k_boost": +1.4, "tendency": "k-friendly"},
    "Ron Kulpa":              {"k_boost": +1.2, "tendency": "k-friendly"},
    "Marty Foster":           {"k_boost": +1.1, "tendency": "k-friendly"},
    "Angel Hernandez":        {"k_boost": +1.0, "tendency": "k-friendly"},  # RIP
    "Mark Wegner":            {"k_boost": +0.9, "tendency": "k-friendly"},
    "Todd Tichenor":          {"k_boost": +0.8, "tendency": "k-friendly"},
    "Bill Miller":            {"k_boost": +0.8, "tendency": "k-friendly"},
    "Manny Gonzalez":         {"k_boost": +0.7, "tendency": "k-friendly"},
    "Alan Porter":            {"k_boost": +0.7, "tendency": "k-friendly"},
    # bb-friendly (tight zone / boosts BB%)
    "Pat Hoberg":             {"k_boost": -1.2, "tendency": "bb-friendly"},
    "Jansen Visconti":        {"k_boost": -1.0, "tendency": "bb-friendly"},
    "Nick Mahrley":           {"k_boost": -0.9, "tendency": "bb-friendly"},
    "Chad Whitson":           {"k_boost": -0.9, "tendency": "bb-friendly"},
    "Ben May":                {"k_boost": -0.8, "tendency": "bb-friendly"},
    "Sean Barber":            {"k_boost": -0.8, "tendency": "bb-friendly"},
    "Dan Iassogna":           {"k_boost": -0.7, "tendency": "bb-friendly"},
    "Junior Valentine":       {"k_boost": -0.7, "tendency": "bb-friendly"},
    "Nestor Ceja":            {"k_boost": -0.7, "tendency": "bb-friendly"},
}

# In-process cache: game_pk → home-plate ump name (12 hours TTL — schedule
# officials rarely change once posted).
_GAME_UMP: dict[int, tuple[float, Optional[str]]] = {}
_TTL = 12 * 3600


async def get_home_plate_ump(game_pk: int) -> Optional[str]:
    """Fetch the assigned home-plate umpire for an MLB game.

    Returns None if the game hasn't posted officials yet (typically
    within 24h of first pitch). Cached per game for 12h.
    """
    now = time.time()
    cached = _GAME_UMP.get(game_pk)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]
    url = _MLB_LIVE_URL.format(gamePk=game_pk)
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    _GAME_UMP[game_pk] = (now, None)
                    return None
                data = await r.json()
    except Exception as e:
        logger.debug("MLB StatsAPI fetch failed: %s", e)
        _GAME_UMP[game_pk] = (now, None)
        return None

    officials = ((data.get("liveData") or {}).get("boxscore") or {}).get("officials") or []
    for o in officials:
        # Home-plate ump has officialType == "Home Plate"
        if (o.get("officialType") or "").lower() == "home plate":
            name = ((o.get("official") or {}).get("fullName") or "").strip()
            _GAME_UMP[game_pk] = (now, name or None)
            return name or None
    _GAME_UMP[game_pk] = (now, None)
    return None


async def enrich_pick_with_umpire(pick: dict) -> dict:
    """Attach `pick["umpire"]` when we can identify the plate ump.

    Silently no-ops for non-MLB picks. Idempotent — respects existing
    `pick["umpire"]` block from a prior enrich pass.
    """
    if pick.get("sport") != "MLB":
        return pick
    if pick.get("umpire"):
        return pick
    # ── Auto-resolve game_pk (2026-07-19) ──────────────────────────
    # Picks from the ingest pipeline don't currently carry `game_pk`.
    # Rather than modify the ingest surfaces, resolve on-demand from
    # the MLB daily schedule (cached per-date, so ≤ 1 upstream call
    # per day per resolver).
    try:
        from services.enrichment.game_resolver import resolve_mlb_game_pk
        game_pk = await resolve_mlb_game_pk(pick)
    except Exception:
        game_pk = pick.get("game_pk") or pick.get("mlb_game_pk")
    if not game_pk:
        return pick
    try:
        game_pk_int = int(game_pk)
    except (TypeError, ValueError):
        return pick
    name = await get_home_plate_ump(game_pk_int)
    if not name:
        return pick
    tendency = _UMP_TENDENCY.get(name) or {"k_boost": 0.0, "tendency": "neutral"}
    pick["umpire"] = {"name": name, **tendency}
    return pick


def umpire_signal_component(pick: dict) -> tuple[float, str]:
    """Return (delta_points, explanation) for umpire influence.

    Applied to K props (pitcher strikeouts, outs recorded, batter K
    props) and BB props. Neutral for HR / hits / totals — the umpire's
    zone barely affects contact rates in a way we can quantify with
    this level of granularity.
    """
    ump = pick.get("umpire") or {}
    if not ump:
        return 0.0, ""
    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").lower()
    k_boost = float(ump.get("k_boost") or 0.0)
    name = ump.get("name") or ""

    # Pitcher strikeout OVERs benefit from k-friendly ump
    if "strikeout" in market or "outs recorded" in market:
        if "over" in selection or "yes" in selection:
            delta = k_boost * 2.5   # scale: +/-1.4 → +/-3.5 pts
            return delta, f"HP {name} ({ump.get('tendency','neutral')})"
        elif "under" in selection or "no" in selection:
            delta = -k_boost * 2.5
            return delta, f"HP {name} ({ump.get('tendency','neutral')})"
    # Walk props reverse the sign
    if "walks" in market or "bases on balls" in market:
        if "over" in selection or "yes" in selection:
            delta = -k_boost * 2.0
            return delta, f"HP {name}"
    return 0.0, ""


async def bulk_enrich_slate(picks: list[dict]) -> int:
    """Enrich a full MLB slate of picks with the day's plate umps.

    Groups by `game_pk` so we make one MLB StatsAPI call per game
    instead of one per pick. Returns count of picks enriched.
    """
    mlb = [p for p in picks if p.get("sport") == "MLB" and p.get("game_pk")]
    if not mlb:
        return 0
    game_pks = {int(p["game_pk"]) for p in mlb}
    # Warm the cache in parallel — bounded fan-out
    import asyncio
    sem = asyncio.Semaphore(6)
    async def _warm(pk: int):
        async with sem:
            await get_home_plate_ump(pk)
    await asyncio.gather(*(_warm(pk) for pk in game_pks), return_exceptions=True)
    # Now enrich each pick from cache — no more network
    n = 0
    for p in mlb:
        if p.get("umpire"):
            continue
        try:
            pk = int(p["game_pk"])
        except (TypeError, ValueError):
            continue
        cached = _GAME_UMP.get(pk)
        if not cached or not cached[1]:
            continue
        name = cached[1]
        tendency = _UMP_TENDENCY.get(name) or {"k_boost": 0.0, "tendency": "neutral"}
        p["umpire"] = {"name": name, **tendency}
        n += 1
    return n
